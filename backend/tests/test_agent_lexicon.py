"""Approved vocabulary in use: what it may change, and what it may never touch.

The whole subsystem rests on two claims — an alias never outranks the master
data, and nothing is read until a person approves it. Those are what this file
exercises, alongside the cache that has to make an approval visible at once.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import lexicon, vocabulary
from app.ai.entity_resolver import EntityResolver
from app.ai.exceptions import EntityNotFoundError
from app.ai.intent import detect_intent
from app.ai.lexicon import Lexicon
from app.api.deps import get_session
from app.database.models import DimRegion
from app.database.models_learning import AliasKind
from app.main import app

pytest.importorskip("multipart",
                    reason="python-multipart is required by FastAPI forms")


@pytest.fixture(autouse=True)
def clean_cache():
    """The lexicon cache is process-local, so tests must not inherit each other."""
    lexicon.invalidate_cache()
    yield
    lexicon.invalidate_cache()


@pytest.fixture
def a_region(agent_engine) -> tuple[str, str]:
    with Session(agent_engine) as db:
        row = db.execute(select(DimRegion.region_code, DimRegion.region_name)).first()
    return row[0], row[1]


# --------------------------------------------------------------------------
# The classifier
# --------------------------------------------------------------------------


def test_a_learned_word_is_understood() -> None:
    assert detect_intent("bikroy koto").intent.value == "UNKNOWN"
    taught = detect_intent("bikroy koto", Lexicon(metrics={"bikroy": "sales"}))
    assert taught.intent.value == "SALES_SUMMARY"


def test_teaching_one_question_does_not_change_the_next() -> None:
    """The shipped tables are module-level and shared by every request.

    Widening one in place would leak a deployment's vocabulary into every
    later call and make the classifier depend on whichever question ran first.
    """
    detect_intent("bikroy koto", Lexicon(metrics={"bikroy": "sales"}))
    assert detect_intent("bikroy koto").intent.value == "UNKNOWN"


def test_a_shipped_keyword_still_works_alongside_a_learned_one() -> None:
    taught = Lexicon(metrics={"bikroy": "sales"})
    assert detect_intent("total sales this month", taught).intent.value == (
        "SALES_SUMMARY"
    )


def test_a_learned_grouping_noun_still_needs_a_grouping_marker() -> None:
    """Teaching a word does not make it a breakdown.

    It makes it a word the existing rules can recognise — and the existing rule
    is that a bare noun stays a filter.
    """
    taught = Lexicon(group_by={"elaka": "area"})
    grouped = detect_intent("elaka wise sales", taught)
    assert any(group.value == "area" for group in grouped.group_by)

    bare = detect_intent("elaka sales", taught)
    assert not any(group.value == "area" for group in bare.group_by)


def test_an_alias_onto_a_target_that_does_not_exist_is_ignored() -> None:
    """Belt and braces: approval refuses these, and the reader ignores them."""
    taught = Lexicon(metrics={"bikroy": "not_a_metric"})
    assert detect_intent("bikroy koto", taught).intent.value == "UNKNOWN"


# --------------------------------------------------------------------------
# The entity resolver
# --------------------------------------------------------------------------


def test_a_learned_phrase_finds_a_master_record(session, a_region) -> None:
    code, _name = a_region
    plain = EntityResolver(session)
    with pytest.raises(EntityNotFoundError):
        plain.resolve_term("cholti anchol")

    taught = EntityResolver(session, lexicon=Lexicon(
        entities={"cholti anchol": ("region", code)},
    ))
    assert taught.resolve_term("cholti anchol").code == code


def test_an_alias_never_outranks_the_master_data(session, a_region) -> None:
    """A name the master knows resolves to the master's record, not the alias.

    The alias is consulted only after every master route has come back empty,
    so this holds by construction rather than by convention.
    """
    code, name = a_region
    other = session.execute(
        select(DimRegion.region_code).where(DimRegion.region_code != code)
    ).scalars().first()
    if other is None:
        pytest.skip("only one region in the seeded master data")

    hijacked = EntityResolver(session, lexicon=Lexicon(
        # Points the master's own region name at a *different* region.
        entities={name.lower(): ("region", other)},
    ))
    assert hijacked.resolve_term(name).code == code


def test_an_alias_pointing_at_a_record_that_has_gone_resolves_to_nothing(
    session,
) -> None:
    """The code is looked up in the index, never trusted.

    A record retired since the alias was approved is simply not there, so the
    phrase finds nothing rather than a dangling reference.
    """
    resolver = EntityResolver(session, lexicon=Lexicon(
        entities={"kichu ekta": ("region", "REG-DOES-NOT-EXIST")},
    ))
    with pytest.raises(EntityNotFoundError):
        resolver.resolve_term("kichu ekta")


def test_an_alias_is_ignored_when_a_different_entity_type_was_asked_for(
    session, a_region,
) -> None:
    from app.ai.schemas import EntityType

    code, _ = a_region
    resolver = EntityResolver(session, lexicon=Lexicon(
        entities={"cholti anchol": ("region", code)},
    ))
    assert resolver.candidates("cholti anchol", EntityType.CUSTOMER) == []


# --------------------------------------------------------------------------
# Only approved rows are read
# --------------------------------------------------------------------------


def test_a_proposal_is_not_in_the_lexicon(session, users) -> None:
    vocabulary.propose_alias(
        session, users["ceo"], phrase="bikroy", alias_kind=AliasKind.METRIC,
        target_keyword="sales",
    )
    session.flush()
    lexicon.invalidate_cache()
    assert lexicon.load(session).metrics == {}


def test_approving_puts_it_in_and_retiring_takes_it_out(session, users) -> None:
    proposed = vocabulary.propose_alias(
        session, users["ceo"], phrase="bikroy", alias_kind=AliasKind.METRIC,
        target_keyword="sales",
    )
    vocabulary.approve_alias(session, users["ceo"], proposed["alias_id"])
    session.flush()
    lexicon.invalidate_cache()
    assert lexicon.load(session).metrics == {"bikroy": "sales"}

    vocabulary.retire_alias(session, users["ceo"], proposed["alias_id"])
    session.flush()
    lexicon.invalidate_cache()
    # Deactivation is immediate and reversible: the row is still there, and the
    # reader simply stops seeing it.
    assert lexicon.load(session).metrics == {}


def test_an_entity_alias_missing_its_code_is_dropped_on_load() -> None:
    class _Row:
        alias_kind = AliasKind.ENTITY
        phrase = "kichu"
        entity_type = "region"
        entity_code = None
        target_keyword = None

    assert lexicon.build([_Row()]).entities == {}


# --------------------------------------------------------------------------
# The cache
# --------------------------------------------------------------------------


def test_the_cache_answers_without_a_second_query(session, users) -> None:
    proposed = vocabulary.propose_alias(
        session, users["ceo"], phrase="bikroy", alias_kind=AliasKind.METRIC,
        target_keyword="sales",
    )
    vocabulary.approve_alias(session, users["ceo"], proposed["alias_id"])
    session.commit()

    lexicon.invalidate_cache()
    first = lexicon.load(session)
    second = lexicon.load(session)
    assert first is second           # the same object, not an equal one
    assert first.metrics == {"bikroy": "sales"}


def test_invalidating_forces_a_reload(session, users) -> None:
    """A reviewer who approves a term expects the assistant to know it.

    A time-based cache would leave a window where it did not, with no way to
    tell a failed approval from one that had simply not landed.
    """
    before = lexicon.load(session)
    proposed = vocabulary.propose_alias(
        session, users["ceo"], phrase="bikroy", alias_kind=AliasKind.METRIC,
        target_keyword="sales",
    )
    vocabulary.approve_alias(session, users["ceo"], proposed["alias_id"])
    session.commit()

    lexicon.invalidate_cache()
    after = lexicon.load(session)
    assert after is not before
    assert after.metrics == {"bikroy": "sales"}


def test_an_unreadable_vocabulary_does_not_break_a_question() -> None:
    """A question answerable from the master data alone must still be answered."""
    class _Broken:
        def execute(self, *args, **kwargs):
            raise RuntimeError("table gone")

    lexicon.invalidate_cache()
    assert lexicon.load(_Broken()) is lexicon.EMPTY


# --------------------------------------------------------------------------
# End to end, through the API
# --------------------------------------------------------------------------


@pytest.fixture
def client(agent_engine, users, monkeypatch):
    import app.ai.agent as agent_module
    from conftest_phase3 import TODAY

    original_init = agent_module.BusinessIntelligenceAgent.__init__

    def pinned_init(self, session, user, llm=None, today=None):
        original_init(self, session, user, llm=llm, today=today or TODAY)

    monkeypatch.setattr(agent_module.BusinessIntelligenceAgent, "__init__",
                        pinned_init)

    def _session_override():
        db = Session(bind=agent_engine, expire_on_commit=False, future=True)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = _session_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def admin(agent_engine, users) -> str:
    from app.database.models_ai import AppUser, Role

    with Session(agent_engine) as db:
        db.add(AppUser(username="lex_admin", display_name="Lexicon Admin",
                       role=Role.ADMIN, data_scope=None, is_active=True))
        db.commit()
    return "lex_admin"


def test_approving_a_term_changes_the_next_answer(client, admin) -> None:
    """The whole point, end to end: teach a word, ask again, get an answer."""
    def ask() -> dict:
        return client.post("/api/chat", json={"message": "bikroy koto"},
                           headers={"X-User": "ceo"}).json()

    assert ask()["intent"] == "UNKNOWN"

    proposed = client.post(
        "/api/learning/aliases",
        json={"phrase": "bikroy", "alias_kind": "METRIC",
              "target_keyword": "sales"},
        headers={"X-User": admin},
    )
    assert proposed.status_code == 201, proposed.text

    # Still unknown: proposing is not approving.
    assert ask()["intent"] == "UNKNOWN"

    approved = client.post(
        f"/api/learning/aliases/{proposed.json()['alias_id']}/approve",
        json={}, headers={"X-User": admin},
    )
    assert approved.status_code == 200, approved.text

    # No restart, no wait: the approval bumped the cache generation.
    assert ask()["intent"] == "SALES_SUMMARY"


def test_retiring_a_term_takes_effect_at_once(client, admin) -> None:
    proposed = client.post(
        "/api/learning/aliases",
        json={"phrase": "bikroy", "alias_kind": "METRIC",
              "target_keyword": "sales"},
        headers={"X-User": admin},
    ).json()
    client.post(f"/api/learning/aliases/{proposed['alias_id']}/approve",
                json={}, headers={"X-User": admin})

    def ask() -> dict:
        return client.post("/api/chat", json={"message": "bikroy koto"},
                           headers={"X-User": "ceo"}).json()

    assert ask()["intent"] == "SALES_SUMMARY"

    retired = client.post(f"/api/learning/aliases/{proposed['alias_id']}/retire",
                          json={}, headers={"X-User": admin})
    assert retired.status_code == 200, retired.text
    assert ask()["intent"] == "UNKNOWN"
