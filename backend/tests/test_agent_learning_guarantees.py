"""The guarantees the whole learning subsystem rests on.

The per-step files cover each piece as it was built. This one covers the claims
that span them — the ones a future change is most likely to break without any
single module's own tests noticing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import lexicon, mining, vocabulary
from app.ai.entity_resolver import EntityResolver
from app.ai.exceptions import AmbiguousEntityError
from app.ai.intent import detect_intent
from app.ai.lexicon import LearnedExample, Lexicon
from app.api.deps import get_session
from app.database.models import DimRegion, DimTerritory
from app.database.models_admin import UserSectionPermission
from app.database.models_ai import AppUser, Role
from app.database.models_learning import (
    AgentFeedback,
    AgentLearningSignal,
    AliasKind,
    SignalType,
)
from app.main import app

pytest.importorskip("multipart",
                    reason="python-multipart is required by FastAPI forms")


@pytest.fixture(autouse=True)
def clean_cache():
    lexicon.invalidate_cache()
    yield
    lexicon.invalidate_cache()


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


def make_admin(agent_engine, username: str, *,
               actions: dict[str, str] | None = None) -> str:
    """An administrator, optionally with action-level overrides on the section."""
    with Session(agent_engine) as db:
        user = AppUser(username=username, display_name=username,
                       role=Role.ADMIN, data_scope=None, is_active=True)
        db.add(user)
        db.flush()
        if actions is not None:
            db.add(UserSectionPermission(
                user_id=user.user_id, section_key="agent_learning",
                access="ALLOW", actions=actions, updated_by="test",
            ))
        db.commit()
    return username


# ---------------------------------------------------------------------------
# Inert by default
# ---------------------------------------------------------------------------


def test_with_nothing_approved_the_agent_behaves_exactly_as_before(
    client, agent_engine,
) -> None:
    """The strongest guarantee: this subsystem changes nothing until used.

    A deployment that never opens the review screen must get the same answers
    it got before any of this existed.
    """
    assert lexicon.load(Session(bind=agent_engine)).size == 0

    known = client.post("/api/chat", json={"message": "total sales this month"},
                        headers={"X-User": "ceo"})
    assert known.status_code == 200
    assert known.json()["intent"] == "SALES_SUMMARY"

    unknown = client.post("/api/chat", json={"message": "bikroy koto"},
                          headers={"X-User": "ceo"})
    assert unknown.json()["intent"] == "UNKNOWN"


def test_the_shipped_classifier_is_unchanged_without_a_lexicon() -> None:
    """``detect_intent`` with no lexicon must be the function it always was."""
    for question, expected in [
        ("total sales this month", "SALES_SUMMARY"),
        ("region wise sales", "REGION_PERFORMANCE"),
        ("bikroy koto", "UNKNOWN"),
    ]:
        assert detect_intent(question).intent.value == expected
        # Passing an empty lexicon must be identical to passing none at all.
        assert detect_intent(question, Lexicon()).intent.value == expected


# ---------------------------------------------------------------------------
# Ambiguity survives the alias fallback
# ---------------------------------------------------------------------------


def test_an_ambiguous_master_term_is_still_refused_when_aliases_exist(
    session, agent_engine,
) -> None:
    """Adding a vocabulary must not turn a refusal into a guess.

    A term matching two master records is the case this system exists to refuse
    rather than resolve, and the alias fallback sits *after* the master routes —
    so it must never be reached for a term the master already matched twice.
    """
    shared = "Ambiguity Test Name"
    # Two *existing* master rows renamed to collide, rather than two new ones:
    # these tables sit in a hierarchy with non-null parents, and inventing
    # partial rows would test the fixture rather than the resolver.
    with Session(agent_engine) as db:
        region = db.execute(select(DimRegion)).scalars().first()
        territory = db.execute(select(DimTerritory)).scalars().first()
        assert region is not None and territory is not None
        region_code = region.region_code
        region.region_name = shared
        territory.territory_name = shared
        db.commit()

    resolver = EntityResolver(session, lexicon=Lexicon(
        entities={shared.lower(): ("region", region_code)},
    ))
    with pytest.raises(AmbiguousEntityError):
        resolver.resolve_term(shared)


# ---------------------------------------------------------------------------
# The action split is real
# ---------------------------------------------------------------------------


def test_a_reviewer_with_view_only_cannot_propose_or_approve(
    client, agent_engine,
) -> None:
    """VIEW, CREATE and EDIT are separate on this section for a reason.

    Somebody may be shown the queue — to understand what is failing — without
    being trusted to change what the assistant reads.
    """
    reader = make_admin(agent_engine, "queue_reader",
                        actions={"CREATE": "DENY", "EDIT": "DENY"})

    assert client.get("/api/learning/signals",
                      headers={"X-User": reader}).status_code == 200

    proposed = client.post(
        "/api/learning/aliases",
        json={"phrase": "bikroy", "alias_kind": "METRIC",
              "target_keyword": "sales"},
        headers={"X-User": reader},
    )
    assert proposed.status_code == 403


def test_a_reviewer_who_may_propose_still_may_not_approve(
    client, agent_engine,
) -> None:
    """Proposing and approving are separate acts, and separately granted."""
    proposer = make_admin(agent_engine, "queue_proposer",
                          actions={"CREATE": "ALLOW", "EDIT": "DENY"})

    proposed = client.post(
        "/api/learning/aliases",
        json={"phrase": "bikroy", "alias_kind": "METRIC",
              "target_keyword": "sales"},
        headers={"X-User": proposer},
    )
    assert proposed.status_code == 201, proposed.text

    approved = client.post(
        f"/api/learning/aliases/{proposed.json()['alias_id']}/approve",
        json={}, headers={"X-User": proposer},
    )
    assert approved.status_code == 403


# ---------------------------------------------------------------------------
# Counting is not vote-stuffing
# ---------------------------------------------------------------------------


def test_a_signal_race_counts_rather_than_duplicating(session) -> None:
    """Two writers on one new phrase: the loser increments, it does not fail.

    Simulated by writing the row underneath the miner, which is what the loser
    of the race sees when it reaches its insert.
    """
    mining.record(session, SignalType.UNKNOWN_INTENT, "concurrent phrase")
    session.flush()
    mining.record(session, SignalType.UNKNOWN_INTENT, "concurrent phrase")
    session.flush()

    rows = list(session.execute(select(AgentLearningSignal)).scalars().all())
    assert len(rows) == 1
    assert rows[0].occurrences == 2


def test_one_readers_verdict_is_not_shown_to_another(client, agent_engine) -> None:
    """A rating is one person's opinion.

    Showing a thumb the reader did not press would misreport it as theirs.
    """
    answer = client.post("/api/chat", json={"message": "total sales this month"},
                         headers={"X-User": "ceo"}).json()
    client.post("/api/chat/feedback",
                json={"message_id": answer["message_id"], "rating": "UP"},
                headers={"X-User": "ceo"})

    with Session(agent_engine) as db:
        stored = db.execute(select(AgentFeedback)).scalars().all()
    assert len(stored) == 1

    # The other user cannot even see the conversation, let alone its verdict.
    other = client.get(f"/api/chat/conversations/{answer['conversation_id']}",
                       headers={"X-User": "dhaka_rm"})
    assert other.status_code == 404


# ---------------------------------------------------------------------------
# Nothing learned reaches a number
# ---------------------------------------------------------------------------


def test_a_learned_alias_does_not_change_any_figure(client, agent_engine,
                                                     users) -> None:
    """The line this subsystem may never cross.

    Teaching a synonym for "sales" changes which question is understood. The
    figure behind the answer is produced by the same tool over the same view
    under the same permission filter, so it must come back identical.
    """
    baseline = client.post("/api/chat",
                           json={"message": "total sales this month"},
                           headers={"X-User": "ceo"}).json()

    with Session(agent_engine, expire_on_commit=False) as db:
        proposed = vocabulary.propose_alias(
            db, users["ceo"], phrase="bikroy", alias_kind=AliasKind.METRIC,
            target_keyword="sales",
        )
        vocabulary.approve_alias(db, users["ceo"], proposed["alias_id"])
        db.commit()
    lexicon.invalidate_cache()

    after = client.post("/api/chat",
                        json={"message": "total sales this month"},
                        headers={"X-User": "ceo"}).json()

    assert after["intent"] == baseline["intent"]
    assert after["data"].get("value") == baseline["data"].get("value")
    assert after["data"].get("values") == baseline["data"].get("values")


def test_a_learned_alias_cannot_widen_a_users_scope(client, agent_engine,
                                                    users) -> None:
    """Scope is applied after the tool is chosen, so vocabulary cannot reach it.

    A regional manager who learns a new word for a region they may not see is
    still refused that region's data.
    """
    with Session(agent_engine) as db:
        other = db.execute(
            select(DimRegion.region_code)
            .where(DimRegion.region_code != "REG001")
        ).scalars().first()
    if other is None:
        pytest.skip("only one region in the seeded master data")

    with Session(agent_engine, expire_on_commit=False) as db:
        proposed = vocabulary.propose_alias(
            db, users["ceo"], phrase="onno anchol", alias_kind=AliasKind.ENTITY,
            entity_type="region", entity_code=other,
        )
        vocabulary.approve_alias(db, users["ceo"], proposed["alias_id"])
        db.commit()
    lexicon.invalidate_cache()

    # dhaka_rm is scoped to REG001 and has just been given a word for another
    # region. The word resolves; the data does not.
    answer = client.post("/api/chat",
                         json={"message": "onno anchol er sales koto"},
                         headers={"X-User": "dhaka_rm"}).json()
    rows = answer.get("data", {}).get("rows") or []
    assert all(row.get("code") != other for row in rows)


# ---------------------------------------------------------------------------
# Retiring is complete
# ---------------------------------------------------------------------------


def test_retiring_removes_every_trace_from_what_the_agent_reads(
    session, users,
) -> None:
    """Across all four lookups, not just the one the alias was proposed for."""
    proposed = vocabulary.propose_alias(
        session, users["ceo"], phrase="elaka", alias_kind=AliasKind.GROUP_BY,
        target_keyword="area",
    )
    vocabulary.approve_alias(session, users["ceo"], proposed["alias_id"])
    session.commit()
    lexicon.invalidate_cache()
    assert lexicon.load(session).group_by == {"elaka": "area"}

    vocabulary.retire_alias(session, users["ceo"], proposed["alias_id"])
    session.commit()
    lexicon.invalidate_cache()

    loaded = lexicon.load(session)
    assert loaded.size == 0
    assert detect_intent("elaka wise sales", loaded).group_by == []


def test_an_example_for_a_tool_that_has_gone_is_not_used(session, users) -> None:
    """A tool removed from the registry must not stay reachable through a bank."""
    from app.ai.orchestrator import Orchestrator
    from app.ai.schemas import Intent, StructuredQuery

    orchestrator = Orchestrator(session, users["ceo"])
    orchestrator.lexicon = Lexicon(examples={
        "total sales this month": LearnedExample(
            1, "get_outstanding_summary",  # removed in revision 0020
            "SALES_SUMMARY", "total sales this month",
        ),
    })
    query = StructuredQuery(intent=Intent.SALES_SUMMARY, language="en")
    chosen = orchestrator._select_tool(query, "total sales this month", ())
    assert chosen != "get_outstanding_summary"
    assert chosen in __import__("app.ai.tools", fromlist=["REGISTRY"]).REGISTRY
