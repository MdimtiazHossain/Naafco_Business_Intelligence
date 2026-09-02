"""The master data the agent resolves against, loaded once instead of per question.

Answering one question used to cost about forty-three statements, of which seven
did the business work. The rest were the master dimensions being read again:
:class:`~app.etl.mapping.MasterDataIndex` (about sixteen queries) and
:class:`~app.ai.entity_resolver.EntityIndex` (about a dozen more), both rebuilt
by every ``Orchestrator``, and an ``Orchestrator`` is built per chat turn.

``MasterDataIndex`` says what it is for in its own docstring — "an in-memory
snapshot of the master dimensions **for one ETL run**". Loading it once and
resolving thousands of rows against it is exactly right for an import; paying
for it once per *question* is the misuse, not the class.

This is nearly invisible on SQLite, where a statement is a function call, and it
is the whole cost on the pooled Postgres deployment, where each one is a network
round trip. ``CLAUDE.md`` records the identical finding about the master-data
upload — 4,012 statements to load 2,000 customers — for the same reason.

**Cached on a generation counter, with time only as a backstop.** A renamed
customer must reach every report at once, so a write bumps the generation and
every cached index becomes unreachable in the same instant. The counter is
bumped *after* the write's commit: bumping before it would let a concurrent
question load the uncommitted state and cache it under the new generation, which
is the stale window the counter exists to avoid.

The counter reaches every writer this process can see. It cannot reach
``scripts/import_master_data.py``, which runs in its own — so
:data:`RELOAD_AFTER_SECONDS` bounds how long that one can go unnoticed. Without
it a workbook loaded against a running server would be invisible until the
server was restarted, which is a worse failure than the cost this module saves.

**Keyed on the engine object, weakly.** ``queries.view`` keys its reflection
cache on ``id(bind)``, which is safe there because every database in this
project reflects the same schema. Master *data* differs per database, and an id
is reusable once its engine is collected — a throwaway test engine's rows could
be served to the next one that happened to land on the same address. A
``WeakKeyDictionary`` ties each entry to the life of its engine instead, so an
engine that goes away takes its masters with it.

The ETL keeps building its own index directly and is deliberately not routed
through here: a run that adds master records must see them, and a snapshot from
before the run began is the one thing it must not have.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from typing import Any, Callable

from sqlalchemy.orm import Session

from ..etl.mapping import MasterDataIndex
from .entity_resolver import EntityIndex

logger = logging.getLogger(__name__)


#: How long an entry may survive without being re-read, in seconds.
#:
#: The generation counter is what makes an in-process write visible *at once*,
#: and it is the mechanism that matters. This is a backstop for the one writer
#: it cannot reach: ``scripts/import_master_data.py`` runs in its own process,
#: so an administrator loading a Master Data workbook against a running server
#: bumps nothing here, and without a second mechanism the server would serve the
#: pre-import masters until it was restarted. That is a worse failure than the
#: cost this module exists to save.
#:
#: So: correctness on every path the process can see, and a bounded staleness of
#: a minute on the one it cannot. ``ai.lexicon`` says "keyed on a generation
#: counter, not on time" and is right to — every writer of *its* data is a
#: request this process handled.
RELOAD_AFTER_SECONDS = 60.0


class _MasterCache:
    """A process-local cache with generation-based invalidation.

    The third of its shape in this package — ``map.resolver`` and ``ai.lexicon``
    each hold one — kept separate rather than shared because each owns what
    invalidates it, and a single cache would make one subsystem's write clear
    another's entries.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 1
        self._entries: "weakref.WeakKeyDictionary[Any, dict[str, tuple[int, float, Any]]]" = (
            weakref.WeakKeyDictionary()
        )

    @property
    def generation(self) -> int:
        return self._generation

    def get_or_load(self, bind: Any, kind: str, load: Callable[[], Any]) -> Any:
        """The cached value for this engine, or a freshly loaded one.

        Loading happens outside the lock: it issues queries, and holding a lock
        across them would serialise every first question after a write.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(bind, {}).get(kind)
            if entry is not None:
                generation, loaded_at, value = entry
                if generation == self._generation and (
                        now - loaded_at) < RELOAD_AFTER_SECONDS:
                    return value
            generation = self._generation

        value = load()

        with self._lock:
            # Only store it if nothing invalidated while we were loading. A
            # value read before a write must never be filed under the
            # generation that write produced.
            if generation == self._generation:
                self._entries.setdefault(bind, {})[kind] = (
                    generation, time.monotonic(), value)
        return value

    def invalidate(self) -> int:
        """Bump the generation. Every cached index becomes unreachable."""
        with self._lock:
            self._generation += 1
            self._entries.clear()
            return self._generation


CACHE = _MasterCache()


def _load_master_index(session: Session) -> MasterDataIndex:
    """Build the index, then let go of the session that built it.

    ``MasterDataIndex`` keeps ``self.session`` and reads it only inside
    ``_load`` — every one of its eleven other methods answers from the
    dictionaries in memory. That is fine for an ETL run, which throws the index
    away with the session; it is not fine for a cached one, which would pin a
    request's ``Session`` — and the connection behind it — for as long as its
    engine lives.

    Dropping it is safe *because* of that split, and this is the line that would
    break loudly if a future method ever went back to the database: it would
    find ``None`` rather than a session quietly belonging to somebody else's
    request.
    """
    index = MasterDataIndex(session)
    index.session = None            # type: ignore[assignment]
    return index


def master_index(session: Session) -> MasterDataIndex:
    """The organisational and item masters, for scope and hierarchy questions."""
    return CACHE.get_or_load(session.get_bind(), "master",
                             lambda: _load_master_index(session))


def entity_index(session: Session) -> EntityIndex:
    """Every master record by code and by name, for entity resolution."""
    return CACHE.get_or_load(session.get_bind(), "entity",
                             lambda: EntityIndex.load(session))


def invalidate() -> int:
    """Called by every path that writes master data, **after its commit**.

    A renamed or newly loaded master record is expected on the next question,
    not on the next restart. Two callers reach it: the master upload job and the
    Data Management editor's five write routes. The command-line import is the
    third writer and cannot call this — it is a separate process — which is what
    :data:`RELOAD_AFTER_SECONDS` is for.

    Nothing else creates a dimension row. The ETL rejects a fact naming a master
    it lacks rather than inventing one, so the list of writers stays short
    enough to be kept right.
    """
    generation = CACHE.invalidate()
    logger.debug("master data cache invalidated, generation=%s", generation)
    return generation


__all__ = ["CACHE", "RELOAD_AFTER_SECONDS", "master_index",
           "entity_index", "invalidate"]
