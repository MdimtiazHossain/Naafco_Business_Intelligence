"""The agent's master-data cache: what it saves, and what it must never cost.

Answering one question read every master dimension again — twenty-eight of the
forty-three statements one answer cost, on top of the seven that did the
business work. ``ai.masters`` loads them once instead.

The saving is worth nothing if a renamed record keeps its old name, so most of
this module is about invalidation rather than about speed. The one performance
test here asserts the *shape* of the win (no dimension is re-read) rather than a
duration, because a timing threshold on a shared machine fails for reasons that
have nothing to do with this code.
"""

from __future__ import annotations

from collections import Counter

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.ai import masters
from test_platform_api import auth, login, platform  # noqa: F401 - fixture reuse

WINDOW = "?date_from=2026-08-01&date_to=2026-08-31"


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Each test starts from a cold cache, and leaves one behind.

    The cache is process-local, so without this a test would inherit whatever
    the previous one loaded — which is the entire class of bug this module is
    about, reproduced in the test suite.
    """
    masters.invalidate()
    yield
    masters.invalidate()


def _dimension_reads(session: Session, work) -> int:
    """How many statements ``work`` issues against a ``dim_*`` table."""
    seen: list[str] = []
    bind = session.get_bind()

    def before(conn, cursor, statement, params, context, many):
        seen.append(statement)

    event.listen(bind, "before_cursor_execute", before)
    try:
        work()
    finally:
        event.remove(bind, "before_cursor_execute", before)

    def table(sql: str) -> str:
        text = " ".join(sql.split())
        return text.split(" FROM ", 1)[1].split()[0].strip('"') if " FROM " in text else ""

    return sum(n for name, n in Counter(table(s) for s in seen).items()
               if name.startswith("dim_"))


def test_the_masters_are_read_once_not_once_per_question(make_agent) -> None:
    """The second question re-reads no dimension at all."""
    agent = make_agent("ceo")

    first = _dimension_reads(agent.session, lambda: agent.chat("এই মাসের sales কত?"))
    second = _dimension_reads(agent.session, lambda: agent.chat("এই মাসের sales কত?"))

    assert first > 0, "the first question must actually load the masters"
    assert second == 0, f"{second} dimension reads on a cached question"


def test_a_renamed_master_is_visible_to_the_very_next_question(
    platform: TestClient,
) -> None:
    """The whole reason this cache is keyed on a generation and not on nothing.

    An administrator who renames a region expects the assistant to know the new
    name, and to stop knowing the old one. A cache that outlived the write would
    answer under a name the master data no longer holds — and keep doing it
    until the server was restarted.
    """
    admin = auth(login(platform, "root"))
    reader = auth(login(platform, "ceo"))

    def ask(question: str) -> dict:
        response = platform.post("/api/chat", json={"message": question},
                                 headers=reader)
        assert response.status_code == 200, response.text
        return response.json()

    # Load the cache under the old name, then rename to one that shares no
    # substring with it: a new name containing the old one would resolve against
    # the stale index by partial match, and the test would pass either way.
    assert ask("Khulna region এর sales কত?")["filters"]["region_codes"] == ["REG002"]

    renamed = platform.put("/api/master/dim_region/REG002", headers=admin,
                           json={"values": {"region_name": "Barishal"}})
    assert renamed.status_code == 200, renamed.text

    # The new name reaches the region...
    assert ask("Barishal region এর sales কত?")["filters"]["region_codes"] == ["REG002"]
    # ...and the old one no longer names anything, rather than quietly
    # filtering by a record the master data no longer describes that way.
    stale = ask("Khulna region এর sales কত?")
    assert not stale["filters"].get("region_codes"), stale["filters"]


def test_the_cache_is_not_shared_between_databases(make_agent, agent_engine) -> None:
    """Entries are held per engine, and weakly.

    ``queries.view`` keys its cache on ``id(bind)``, which is safe for a schema
    every database shares. Master *data* differs per database and an id is
    reusable once its engine is collected, so a throwaway test engine's rows
    could be served to whatever landed on the same address next.
    """
    agent = make_agent("ceo")
    agent.chat("এই মাসের sales কত?")

    entries = masters.CACHE._entries          # noqa: SLF001 - the property under test
    assert agent_engine in entries
    assert set(entries[agent_engine]) == {"master", "entity"}


def test_an_import_never_reads_the_cache(agent_engine) -> None:
    """The ETL builds its own index, and must.

    A run that adds master records has to see them: a snapshot taken before it
    began is the one thing an import must not be handed. This is asserted
    against the module rather than by running an import, because the property is
    "``etl`` does not import ``ai.masters``" and that is what it checks.
    """
    import app.etl.pipeline as pipeline
    import app.etl.mapping as mapping

    for module in (pipeline, mapping):
        assert "masters" not in dir(module), module.__name__


def test_a_stale_entry_is_reloaded_even_with_no_invalidation(make_agent) -> None:
    """The backstop for the writer this process cannot see.

    ``scripts/import_master_data.py`` runs in its own process and bumps nothing
    here, so an entry that nobody invalidated is still re-read eventually. The
    horizon is asserted as a bounded, non-zero number rather than by waiting for
    it: a test that slept for a minute would be a minute of everybody's time.
    """
    assert 0 < masters.RELOAD_AFTER_SECONDS <= 300

    agent = make_agent("ceo")
    agent.chat("এই মাসের sales কত?")
    assert _dimension_reads(agent.session,
                            lambda: agent.chat("এই মাসের sales কত?")) == 0

    # Age every entry past the horizon without touching the clock.
    with masters.CACHE._lock:                 # noqa: SLF001
        for kinds in masters.CACHE._entries.values():   # noqa: SLF001
            for kind, (generation, _, value) in list(kinds.items()):
                kinds[kind] = (generation, 0.0, value)

    assert _dimension_reads(agent.session,
                            lambda: agent.chat("এই মাসের sales কত?")) > 0


def test_a_cached_index_holds_no_session(make_agent) -> None:
    """The index is a snapshot, and must not pin the request that took it.

    ``MasterDataIndex`` reads its session only while loading; every other method
    answers from memory. A cached one that kept the reference would hold a
    request's ``Session`` — and its connection — for as long as the engine
    lives, which in a server is forever.
    """
    agent = make_agent("ceo")
    agent.chat("এই মাসের sales কত?")

    index = masters.master_index(agent.session)
    assert index.session is None
    # And it still answers, because answering never needed the session.
    assert index.ancestors_of("region_code", "REG001")
