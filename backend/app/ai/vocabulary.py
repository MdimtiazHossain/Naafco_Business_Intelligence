"""The approval path: from a mined failure to vocabulary the agent may read.

Step three writes down what went wrong. This is where a person decides what, if
anything, to do about it — and it is the only way a row in ``agent_term_alias``
or ``agent_example`` ever becomes ACTIVE.

Three rules shape every function here.

**Nothing activates without a person.** ``propose_*`` writes a PROPOSED row and
``approve_*`` is a separate call by a named user. There is deliberately no
"propose and approve" convenience: the two halves exist to be done by different
people at different times, and collapsing them would quietly remove the review
this package exists to provide.

**An alias may not point at something that does not exist.** An ENTITY alias is
checked against the master data it names, and a METRIC, GROUP_BY or MODIFIER
alias against the keyword tables in ``intent``. Approving one that points
nowhere would be worse than useless: it would look approved, do nothing, and
give a reviewer no reason to suspect it. This is the same refusal the ETL makes
when a fact names a master record that is not there.

**Replacing an active meaning is explicit.** Approving an alias whose phrase is
already spoken for is refused and names the incumbent, unless the caller asks
for a replacement — in which case the incumbent is *retired*, not deleted, and
both rows carry who did it.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database.models_learning import (
    AgentExample,
    AgentLearningSignal,
    AgentTermAlias,
    AliasKind,
    LearningSource,
    LearningStatus,
    SignalStatus,
    SignalType,
)
from ..utils.text import normalize_code, normalize_text
from .entity_resolver import BINDING_BY_TYPE
from .exceptions import AgentError
from .intent import METRIC_KEYWORDS, MODIFIERS
from .mining import normalize_phrase
from .permission_filter import UserContext
from .schemas import GroupBy
from .tools import REGISTRY

logger = logging.getLogger("app.ai.vocabulary")


class ReviewRefused(AgentError):
    """The requested review action cannot be carried out."""

    code = "REVIEW_REFUSED"
    user_message = "That change can't be made."


def _refuse(detail: str) -> ReviewRefused:
    """A refusal whose reason is safe to show — these are all about *inputs*.

    Unlike most errors in this package, the reason here is the whole value: a
    reviewer told "that keyword doesn't exist" can fix it, while one told "that
    change can't be made" cannot.
    """
    return ReviewRefused(detail, user_message=detail)


# ---------------------------------------------------------------------------
# What an alias is allowed to point at
# ---------------------------------------------------------------------------


#: The keyword each alias kind may name, resolved from the shipped tables.
#:
#: Read from ``intent`` rather than restated, so a keyword added to the
#: classifier becomes a legal alias target with no edit here — and a keyword
#: removed stops being one, instead of leaving approved rows pointing at a key
#: nothing reads any more.
def _valid_keywords(alias_kind: str) -> set[str]:
    if alias_kind == AliasKind.METRIC:
        return set(METRIC_KEYWORDS)
    if alias_kind == AliasKind.MODIFIER:
        return set(MODIFIERS)
    if alias_kind == AliasKind.GROUP_BY:
        return {group.value for group in GroupBy}
    return set()


def _check_entity_exists(session: Session, entity_type: str,
                         entity_code: str) -> None:
    """Refuse an entity alias naming a master record that is not there.

    The master data is the authority on its own records; an alias adds a *name*
    for one, and cannot bring one into being.
    """
    binding = BINDING_BY_TYPE.get(entity_type)
    if binding is None:
        known = ", ".join(sorted(k.value for k in BINDING_BY_TYPE))
        raise _refuse(f"'{entity_type}' is not an entity type. Known types: {known}.")

    column = getattr(binding.model, binding.code_field)
    found = session.execute(
        select(func.count()).select_from(binding.model).where(column == entity_code)
    ).scalar_one()
    if not found:
        raise _refuse(
            f"No {entity_type} exists with code '{entity_code}'. An alias can "
            "name a master record, not create one."
        )


def validate_alias(session: Session, *, alias_kind: str, phrase: str | None,
                   entity_type: str | None, entity_code: str | None,
                   target_keyword: str | None) -> dict[str, Any]:
    """Check one proposed alias and return its cleaned fields.

    Runs at propose time *and* again at approve time. Twice, deliberately: a
    master record can be retired between the two, and approving an alias onto a
    record that has since gone would be exactly the dangling pointer this
    function exists to prevent.
    """
    if alias_kind not in AliasKind.ALL:
        raise _refuse(f"'{alias_kind}' is not an alias kind.")

    normalized = normalize_phrase(phrase)
    if not normalized:
        raise _refuse("An alias needs a phrase.")

    cleaned: dict[str, Any] = {
        "phrase": normalized,
        "alias_kind": alias_kind,
        "entity_type": None,
        "entity_code": None,
        "target_keyword": None,
    }

    if alias_kind == AliasKind.ENTITY:
        entity_type = normalize_text(entity_type)
        code = normalize_code(entity_code)
        if not entity_type or not code:
            raise _refuse(
                "An entity alias needs both an entity type and an entity code."
            )
        _check_entity_exists(session, entity_type, code)
        cleaned["entity_type"] = entity_type
        cleaned["entity_code"] = code
        return cleaned

    keyword = normalize_text(target_keyword)
    if not keyword:
        raise _refuse(f"A {alias_kind} alias needs a target keyword.")
    allowed = _valid_keywords(alias_kind)
    if keyword not in allowed:
        raise _refuse(
            f"'{keyword}' is not a {alias_kind} the assistant knows. "
            f"Choose one of: {', '.join(sorted(allowed))}."
        )
    cleaned["target_keyword"] = keyword
    return cleaned


# ---------------------------------------------------------------------------
# The review queue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Page:
    """One page of a review listing."""

    rows: list[dict[str, Any]]
    total: int


def list_signals(session: Session, *, status: str | None = None,
                 signal_type: str | None = None, limit: int = 50,
                 offset: int = 0) -> Page:
    """The mined queue, worst first.

    Ordered by ``occurrences`` rather than recency: the question that failed
    forty times matters more than the one that failed once this morning, and a
    reviewer's time is the scarce thing here.
    """
    if status and status not in SignalStatus.ALL:
        raise _refuse(f"'{status}' is not a signal status.")
    if signal_type and signal_type not in SignalType.ALL:
        raise _refuse(f"'{signal_type}' is not a signal type.")

    conditions = []
    if status:
        conditions.append(AgentLearningSignal.status == status)
    if signal_type:
        conditions.append(AgentLearningSignal.signal_type == signal_type)

    total = session.execute(
        select(func.count()).select_from(AgentLearningSignal).where(*conditions)
    ).scalar_one()

    rows = session.execute(
        select(AgentLearningSignal)
        .where(*conditions)
        .order_by(AgentLearningSignal.occurrences.desc(),
                  AgentLearningSignal.last_seen_at.desc())
        .limit(max(1, min(limit, 200)))
        .offset(max(0, offset))
    ).scalars().all()

    return Page([_signal_row(row) for row in rows], total)


def set_signal_status(session: Session, user: UserContext, signal_id: int,
                      status: str) -> dict[str, Any]:
    """Move one signal through the reviewer's queue."""
    if status not in SignalStatus.ALL:
        raise _refuse(f"'{status}' is not a signal status.")
    signal = session.get(AgentLearningSignal, signal_id)
    if signal is None:
        raise _refuse("That signal no longer exists.")
    signal.status = status
    session.flush()
    logger.info("signal %s -> %s by user=%s", signal_id, status, user.user_id)
    return _signal_row(signal)


def _signal_row(row: AgentLearningSignal) -> dict[str, Any]:
    return {
        "signal_id": row.signal_id,
        "signal_type": row.signal_type,
        "phrase": row.normalized_phrase,
        "raw_sample": row.raw_sample,
        "language": row.language,
        "occurrences": row.occurrences,
        "status": row.status,
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
    }


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------


def alias_active_key(alias_kind: str, phrase: str) -> str:
    """The value that makes at most one row active for a phrase.

    See the column's own note in ``models_learning``: language is not part of
    it, because the resolver looks a phrase up by the phrase.
    """
    return f"{alias_kind}|{phrase}"


def propose_alias(session: Session, user: UserContext, *, phrase: str,
                  alias_kind: str, entity_type: str | None = None,
                  entity_code: str | None = None,
                  target_keyword: str | None = None,
                  language: str | None = None, signal_id: int | None = None,
                  notes: str | None = None,
                  source: str = LearningSource.MINED) -> dict[str, Any]:
    """Write a PROPOSED alias. Inert until somebody approves it."""
    cleaned = validate_alias(
        session, alias_kind=alias_kind, phrase=phrase, entity_type=entity_type,
        entity_code=entity_code, target_keyword=target_keyword,
    )

    alias = AgentTermAlias(
        **cleaned,
        language=normalize_text(language),
        status=LearningStatus.PROPOSED,
        source=source if source in LearningSource.ALL else LearningSource.MINED,
        signal_id=signal_id,
        notes=normalize_text(notes),
        created_by=user.username,
        # NULL while not active: that is what lets a phrase be proposed again
        # after an earlier mapping for it was retired.
        active_key=None,
    )
    session.add(alias)

    if signal_id is not None:
        signal = session.get(AgentLearningSignal, signal_id)
        if signal is not None:
            signal.status = SignalStatus.PROPOSED

    session.flush()
    logger.info("alias %s proposed by user=%s", alias.alias_id, user.user_id)
    return _alias_row(alias)


def approve_alias(session: Session, user: UserContext, alias_id: int, *,
                  replace: bool = False) -> dict[str, Any]:
    """Make one alias ACTIVE, so the resolver may read it."""
    alias = _load_alias(session, alias_id)
    if alias.status == LearningStatus.ACTIVE:
        raise _refuse("That alias is already active.")

    # Re-validated: a master record can be retired between proposal and
    # approval, and approving onto one that has gone would leave a mapping
    # pointing at nothing.
    validate_alias(
        session, alias_kind=alias.alias_kind, phrase=alias.phrase,
        entity_type=alias.entity_type, entity_code=alias.entity_code,
        target_keyword=alias.target_keyword,
    )

    key = alias_active_key(alias.alias_kind, alias.phrase)
    incumbent = session.execute(
        select(AgentTermAlias).where(
            AgentTermAlias.active_key == key,
            AgentTermAlias.alias_id != alias_id,
        )
    ).scalar_one_or_none()

    if incumbent is not None:
        if not replace:
            raise _refuse(
                f"'{alias.phrase}' already means "
                f"{_alias_target(incumbent)}. Approve with replace to change it."
            )
        _retire(incumbent, user,
                reason=f"Replaced by alias {alias_id} on approval.")
        # Flushed before the new row claims the key: the unique constraint is
        # on one column, so both rows would hold it for an instant otherwise.
        session.flush()

    now = dt.datetime.now(dt.timezone.utc)
    alias.status = LearningStatus.ACTIVE
    alias.active_key = key
    alias.approved_by = user.username
    alias.approved_at = now
    alias.retired_at = None
    session.flush()

    logger.info("alias %s approved by user=%s (replaced=%s)", alias_id,
                user.user_id, incumbent.alias_id if incumbent else None)
    return _alias_row(alias)


def reject_alias(session: Session, user: UserContext, alias_id: int, *,
                 reason: str | None = None) -> dict[str, Any]:
    """Turn a proposal down, keeping it so it is not proposed again next week."""
    alias = _load_alias(session, alias_id)
    if alias.status == LearningStatus.ACTIVE:
        raise _refuse("That alias is active. Retire it instead of rejecting it.")
    alias.status = LearningStatus.REJECTED
    alias.active_key = None
    alias.notes = normalize_text(reason) or alias.notes
    alias.approved_by = user.username
    alias.approved_at = dt.datetime.now(dt.timezone.utc)
    session.flush()
    return _alias_row(alias)


def retire_alias(session: Session, user: UserContext, alias_id: int, *,
                 reason: str | None = None) -> dict[str, Any]:
    """Stop the resolver reading an alias, without destroying the record of it."""
    alias = _load_alias(session, alias_id)
    if alias.status != LearningStatus.ACTIVE:
        raise _refuse("Only an active alias can be retired.")
    _retire(alias, user, reason=reason)
    session.flush()
    return _alias_row(alias)


def _retire(alias: AgentTermAlias, user: UserContext, *,
            reason: str | None) -> None:
    alias.status = LearningStatus.RETIRED
    # Cleared, which is what frees the phrase for a better mapping later.
    alias.active_key = None
    alias.retired_at = dt.datetime.now(dt.timezone.utc)
    alias.approved_by = user.username
    if reason:
        alias.notes = normalize_text(reason)


def _load_alias(session: Session, alias_id: int) -> AgentTermAlias:
    alias = session.get(AgentTermAlias, alias_id)
    if alias is None:
        raise _refuse("That alias no longer exists.")
    return alias


def _alias_target(alias: AgentTermAlias) -> str:
    if alias.alias_kind == AliasKind.ENTITY:
        return f"{alias.entity_type} {alias.entity_code}"
    return f"{alias.alias_kind.lower()} '{alias.target_keyword}'"


def list_aliases(session: Session, *, status: str | None = None,
                 alias_kind: str | None = None, limit: int = 100,
                 offset: int = 0) -> Page:
    if status and status not in LearningStatus.ALL:
        raise _refuse(f"'{status}' is not a status.")
    if alias_kind and alias_kind not in AliasKind.ALL:
        raise _refuse(f"'{alias_kind}' is not an alias kind.")

    conditions = []
    if status:
        conditions.append(AgentTermAlias.status == status)
    if alias_kind:
        conditions.append(AgentTermAlias.alias_kind == alias_kind)

    total = session.execute(
        select(func.count()).select_from(AgentTermAlias).where(*conditions)
    ).scalar_one()
    rows = session.execute(
        select(AgentTermAlias)
        .where(*conditions)
        .order_by(AgentTermAlias.updated_at.desc(), AgentTermAlias.alias_id.desc())
        .limit(max(1, min(limit, 500)))
        .offset(max(0, offset))
    ).scalars().all()
    return Page([_alias_row(row) for row in rows], total)


def _alias_row(row: AgentTermAlias) -> dict[str, Any]:
    return {
        "alias_id": row.alias_id,
        "phrase": row.phrase,
        "language": row.language,
        "alias_kind": row.alias_kind,
        "entity_type": row.entity_type,
        "entity_code": row.entity_code,
        "target_keyword": row.target_keyword,
        "target": _alias_target(row),
        "status": row.status,
        "source": row.source,
        "signal_id": row.signal_id,
        "notes": row.notes,
        "created_by": row.created_by,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at,
        "retired_at": row.retired_at,
    }


# ---------------------------------------------------------------------------
# Examples
# ---------------------------------------------------------------------------


def propose_example(session: Session, user: UserContext, *, question: str,
                    tool_name: str, intent: str | None = None,
                    arguments: dict[str, Any] | None = None,
                    language: str | None = None,
                    source_message_id: int | None = None,
                    notes: str | None = None,
                    source: str = LearningSource.MINED) -> dict[str, Any]:
    """Write a PROPOSED worked example. Inert until approved."""
    text = normalize_text(question)
    normalized = normalize_phrase(question)
    if not text or not normalized:
        raise _refuse("An example needs a question.")
    # Checked against the live registry rather than a list here: a tool that has
    # been removed must not stay reachable through an approved example.
    if tool_name not in REGISTRY:
        raise _refuse(f"'{tool_name}' is not a tool the assistant has.")

    example = AgentExample(
        question=text,
        normalized_question=normalized,
        language=normalize_text(language),
        intent=normalize_text(intent),
        tool_name=tool_name,
        arguments=arguments or None,
        status=LearningStatus.PROPOSED,
        source=source if source in LearningSource.ALL else LearningSource.MINED,
        source_message_id=source_message_id,
        use_count=0,
        notes=normalize_text(notes),
        created_by=user.username,
        active_key=None,
    )
    session.add(example)
    session.flush()
    return _example_row(example)


def approve_example(session: Session, user: UserContext, example_id: int, *,
                    replace: bool = False) -> dict[str, Any]:
    example = _load_example(session, example_id)
    if example.status == LearningStatus.ACTIVE:
        raise _refuse("That example is already active.")
    if example.tool_name not in REGISTRY:
        raise _refuse(f"'{example.tool_name}' is not a tool the assistant has.")

    incumbent = session.execute(
        select(AgentExample).where(
            AgentExample.active_key == example.normalized_question,
            AgentExample.example_id != example_id,
        )
    ).scalar_one_or_none()

    if incumbent is not None:
        if not replace:
            raise _refuse(
                f"That question is already answered by "
                f"'{incumbent.tool_name}'. Approve with replace to change it."
            )
        incumbent.status = LearningStatus.RETIRED
        incumbent.active_key = None
        incumbent.retired_at = dt.datetime.now(dt.timezone.utc)
        incumbent.approved_by = user.username
        session.flush()

    now = dt.datetime.now(dt.timezone.utc)
    example.status = LearningStatus.ACTIVE
    example.active_key = example.normalized_question
    example.approved_by = user.username
    example.approved_at = now
    example.retired_at = None
    session.flush()
    return _example_row(example)


def reject_example(session: Session, user: UserContext, example_id: int, *,
                   reason: str | None = None) -> dict[str, Any]:
    example = _load_example(session, example_id)
    if example.status == LearningStatus.ACTIVE:
        raise _refuse("That example is active. Retire it instead.")
    example.status = LearningStatus.REJECTED
    example.active_key = None
    example.notes = normalize_text(reason) or example.notes
    example.approved_by = user.username
    example.approved_at = dt.datetime.now(dt.timezone.utc)
    session.flush()
    return _example_row(example)


def retire_example(session: Session, user: UserContext, example_id: int, *,
                   reason: str | None = None) -> dict[str, Any]:
    example = _load_example(session, example_id)
    if example.status != LearningStatus.ACTIVE:
        raise _refuse("Only an active example can be retired.")
    example.status = LearningStatus.RETIRED
    example.active_key = None
    example.retired_at = dt.datetime.now(dt.timezone.utc)
    example.approved_by = user.username
    if reason:
        example.notes = normalize_text(reason)
    session.flush()
    return _example_row(example)


def _load_example(session: Session, example_id: int) -> AgentExample:
    example = session.get(AgentExample, example_id)
    if example is None:
        raise _refuse("That example no longer exists.")
    return example


def list_examples(session: Session, *, status: str | None = None,
                  limit: int = 100, offset: int = 0) -> Page:
    if status and status not in LearningStatus.ALL:
        raise _refuse(f"'{status}' is not a status.")
    conditions = [AgentExample.status == status] if status else []

    total = session.execute(
        select(func.count()).select_from(AgentExample).where(*conditions)
    ).scalar_one()
    rows = session.execute(
        select(AgentExample)
        .where(*conditions)
        .order_by(AgentExample.updated_at.desc(), AgentExample.example_id.desc())
        .limit(max(1, min(limit, 500)))
        .offset(max(0, offset))
    ).scalars().all()
    return Page([_example_row(row) for row in rows], total)


def _example_row(row: AgentExample) -> dict[str, Any]:
    return {
        "example_id": row.example_id,
        "question": row.question,
        "normalized_question": row.normalized_question,
        "language": row.language,
        "intent": row.intent,
        "tool_name": row.tool_name,
        "arguments": row.arguments,
        "status": row.status,
        "source": row.source,
        "use_count": row.use_count,
        "notes": row.notes,
        "created_by": row.created_by,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at,
        "retired_at": row.retired_at,
    }


def alias_targets() -> dict[str, Sequence[str]]:
    """What a reviewer may choose from, for the proposal form.

    Served rather than hard-coded in the browser for the reason every registry
    in this project is derived: a keyword added to ``intent`` appears here with
    no frontend change, and one removed stops being offered.
    """
    return {
        AliasKind.METRIC: sorted(METRIC_KEYWORDS),
        AliasKind.MODIFIER: sorted(MODIFIERS),
        AliasKind.GROUP_BY: sorted(group.value for group in GroupBy),
        AliasKind.ENTITY: sorted(key.value for key in BINDING_BY_TYPE),
    }


__all__ = [
    "Page",
    "ReviewRefused",
    "alias_active_key",
    "alias_targets",
    "approve_alias",
    "approve_example",
    "list_aliases",
    "list_examples",
    "list_signals",
    "propose_alias",
    "propose_example",
    "reject_alias",
    "reject_example",
    "retire_alias",
    "retire_example",
    "set_signal_status",
    "validate_alias",
]
