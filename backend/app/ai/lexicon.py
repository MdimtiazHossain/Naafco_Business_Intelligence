"""The approved vocabulary, in the shape the resolvers actually consult.

``agent_term_alias`` is a review artefact: it carries status, provenance, who
approved what and when. None of that matters at question time, where the only
question is "does this phrase mean anything?". This module answers that from a
process-local cache, so a question does not pay for a table scan.

**Only ACTIVE rows are ever loaded**, which is what makes deactivation
immediate and reversible: retiring an alias removes it from the next load, and
nothing else has to know.

**The cache is keyed on a generation counter, not on time.** A reviewer who
approves a term expects the assistant to know it, and a time-based cache would
leave a window in which it did not — with no way to tell whether the approval
had failed or simply not landed yet. Every write path in the review API bumps
the generation *after its commit*, so there is no stale window and no
possibility of caching a half-written transaction. This is the same mechanism
``map/resolver.py`` uses, and for the same reason.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database.models_learning import (
    AgentExample,
    AgentTermAlias,
    AliasKind,
    LearningStatus,
)

logger = logging.getLogger("app.ai.lexicon")


@dataclass(frozen=True)
class LearnedExample:
    """An approved question, and what answered it.

    Deliberately **not** the stored arguments. Those came from one past call by
    one person, carrying that day's dates and that caller's filters; replaying
    them would answer a new question with an old scope. The arguments live on
    the row for a reviewer to read and for a model to see the shape of, and the
    orchestrator builds its own from the validated query as it always does.
    """

    example_id: int
    tool_name: str
    intent: str | None = None
    question: str = ""


@dataclass(frozen=True)
class Lexicon:
    """Approved phrases, grouped by what they are consulted for.

    Each mapping is ``phrase -> target``, and a phrase appears at most once in
    each because the database allows only one ACTIVE row per (kind, phrase).
    """

    #: ``phrase -> (entity_type, entity_code)``
    entities: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: ``phrase -> key of intent.METRIC_KEYWORDS``
    metrics: dict[str, str] = field(default_factory=dict)
    #: ``phrase -> key of intent.MODIFIERS``
    modifiers: dict[str, str] = field(default_factory=dict)
    #: ``phrase -> value of schemas.GroupBy``
    group_by: dict[str, str] = field(default_factory=dict)
    #: ``normalised question -> the approved example that answers it``
    examples: dict[str, LearnedExample] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.entities or self.metrics or self.modifiers
                    or self.group_by or self.examples)

    @property
    def size(self) -> int:
        return (len(self.entities) + len(self.metrics) + len(self.modifiers)
                + len(self.group_by) + len(self.examples))


#: The empty lexicon, shared. Used wherever no session is available — a unit
#: test of the classifier, a script — so that "no approved vocabulary" is the
#: default rather than something a caller has to remember to pass.
EMPTY = Lexicon()


class _LexiconCache:
    """One cached lexicon, invalidated by generation like the marker cache."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 1
        self._cached: tuple[int, Lexicon] | None = None

    @property
    def generation(self) -> int:
        return self._generation

    def get(self) -> Lexicon | None:
        with self._lock:
            if self._cached is None:
                return None
            generation, value = self._cached
            return value if generation == self._generation else None

    def put(self, value: Lexicon) -> None:
        with self._lock:
            self._cached = (self._generation, value)

    def invalidate(self) -> int:
        with self._lock:
            self._generation += 1
            self._cached = None
            return self._generation


CACHE = _LexiconCache()


def invalidate_cache() -> int:
    """Called by every review write path, **after** its commit.

    After, not before: bumping first would let a concurrent reader load the
    uncommitted state and cache it under the new generation, leaving the
    committed version unreachable until something else happened to bump again.
    """
    generation = CACHE.invalidate()
    logger.debug("lexicon cache invalidated, generation=%s", generation)
    return generation


def load(session: Session) -> Lexicon:
    """The approved vocabulary, from cache where possible.

    A failure here returns the empty lexicon rather than raising: a question the
    agent could have answered from the master data alone must not fail because
    an optional vocabulary table was unreadable.
    """
    cached = CACHE.get()
    if cached is not None:
        return cached

    try:
        aliases = session.execute(
            select(AgentTermAlias).where(
                AgentTermAlias.status == LearningStatus.ACTIVE
            )
        ).scalars().all()
        examples = session.execute(
            select(AgentExample).where(
                AgentExample.status == LearningStatus.ACTIVE
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001 - vocabulary is an enhancement, not a need
        logger.exception("could not load the approved vocabulary")
        return EMPTY

    lexicon = build(aliases, examples)
    CACHE.put(lexicon)
    return lexicon


def build(rows: Any, examples: Any = ()) -> Lexicon:
    """Turn alias rows into the four lookups. Pure, so it is testable alone."""
    entities: dict[str, tuple[str, str]] = {}
    metrics: dict[str, str] = {}
    modifiers: dict[str, str] = {}
    group_by: dict[str, str] = {}

    for row in rows:
        phrase = (row.phrase or "").strip()
        if not phrase:
            continue
        if row.alias_kind == AliasKind.ENTITY:
            # Both halves or neither: an entity alias missing its code points
            # nowhere, and silently keeping it would make the phrase resolve to
            # nothing in a way no reviewer could see.
            if row.entity_type and row.entity_code:
                entities[phrase] = (row.entity_type, row.entity_code)
        elif row.alias_kind == AliasKind.METRIC and row.target_keyword:
            metrics[phrase] = row.target_keyword
        elif row.alias_kind == AliasKind.MODIFIER and row.target_keyword:
            modifiers[phrase] = row.target_keyword
        elif row.alias_kind == AliasKind.GROUP_BY and row.target_keyword:
            group_by[phrase] = row.target_keyword

    worked: dict[str, LearnedExample] = {}
    for row in examples or ():
        key = (row.normalized_question or "").strip()
        if key and row.tool_name:
            worked[key] = LearnedExample(
                example_id=row.example_id, tool_name=row.tool_name,
                intent=row.intent, question=row.question or key,
            )

    return Lexicon(entities=entities, metrics=metrics, modifiers=modifiers,
                   group_by=group_by, examples=worked)


def note_use(session: Session, example_id: int) -> None:
    """Count one match against an approved example.

    A bank nobody hits is a bank to prune, and this count is the only way to
    know which rows those are. Guarded, because a counter must never be the
    reason a question fails: the increment runs in its own SAVEPOINT so a
    failure here leaves the answer, and the surrounding transaction, untouched.
    """
    try:
        with session.begin_nested():
            session.execute(
                AgentExample.__table__.update()
                .where(AgentExample.example_id == example_id)
                .values(use_count=AgentExample.use_count + 1)
            )
    except Exception:  # noqa: BLE001 - a usage counter never costs an answer
        logger.warning("could not count a use of example %s", example_id)


__all__ = ["CACHE", "EMPTY", "LearnedExample", "Lexicon", "build",
           "invalidate_cache", "load", "note_use"]
