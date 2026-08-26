"""Failure mining: what counts as a signal, what it is keyed on, and dedup."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import mining
from app.api.deps import get_session
from app.database.models_learning import (
    AgentLearningSignal,
    SignalStatus,
    SignalType,
)
from app.main import app

pytest.importorskip("multipart",
                    reason="python-multipart is required by FastAPI forms")


# --------------------------------------------------------------------------
# The dedup key — pure, so no database is needed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("  Sales   koto?  ", "sales koto"),
    ("SALES KOTO", "sales koto"),
    ("sales koto???", "sales koto"),
    ("sales koto.", "sales koto"),
])
def test_phrases_that_are_one_problem_share_one_key(text, expected) -> None:
    assert mining.normalize_phrase(text) == expected


def test_bangla_is_not_rewritten_by_the_key(self=None) -> None:
    """Only the trailing danda goes; Bangla is caseless, so nothing folds."""
    assert mining.normalize_phrase("আজকের সেলস কত?") == "আজকের সেলস কত"
    assert mining.normalize_phrase("আজকের সেলস কত।") == "আজকের সেলস কত"


def test_an_empty_phrase_has_no_key(self=None) -> None:
    assert mining.normalize_phrase("   ") is None
    assert mining.normalize_phrase("???") is None
    assert mining.normalize_phrase(None) is None


def test_a_very_long_phrase_is_bounded(self=None) -> None:
    """The column is bounded because it carries a unique constraint."""
    key = mining.normalize_phrase("x " * 400)
    assert key is not None and len(key) <= mining.MAX_PHRASE


# --------------------------------------------------------------------------
# Which signal a turn produces
# --------------------------------------------------------------------------


def test_an_unclassified_question_is_recorded_whole() -> None:
    assert mining.signals_for_turn(
        "koto tk baki ache", intent="UNKNOWN", error_code=None,
        error_details=None, empty_result=False,
    ) == [(SignalType.UNKNOWN_INTENT, "koto tk baki ache")]


def test_an_unresolved_entity_is_recorded_as_the_term_not_the_sentence() -> None:
    """The term is what somebody can teach; the sentence around it is noise."""
    assert mining.signals_for_turn(
        "chinir sales koto", intent="UNKNOWN", error_code="ENTITY_NOT_FOUND",
        error_details={"term": "chini"}, empty_result=False,
    ) == [(SignalType.ENTITY_NOT_FOUND, "chini")]


def test_an_unresolved_entity_with_no_term_records_nothing() -> None:
    """Without the term there is nothing here a person could act on."""
    assert mining.signals_for_turn(
        "chinir sales koto", intent="UNKNOWN", error_code="ENTITY_NOT_FOUND",
        error_details=None, empty_result=False,
    ) == []


def test_a_turn_produces_at_most_one_signal() -> None:
    """An unresolved entity also has an UNKNOWN intent; only the useful half is kept."""
    signals = mining.signals_for_turn(
        "chinir sales", intent="UNKNOWN", error_code="ENTITY_NOT_FOUND",
        error_details={"term": "chini"}, empty_result=True,
    )
    assert len(signals) == 1
    assert signals[0][0] == SignalType.ENTITY_NOT_FOUND


def test_an_empty_result_is_its_own_kind_of_signal() -> None:
    """Understood perfectly, no data behind it — worth eyes, but not a fault."""
    assert mining.signals_for_turn(
        "sales for khulna", intent="SALES_SUMMARY", error_code=None,
        error_details=None, empty_result=True,
    ) == [(SignalType.ZERO_ROWS, "sales for khulna")]


def test_a_good_answer_produces_nothing() -> None:
    assert mining.signals_for_turn(
        "total sales", intent="SALES_SUMMARY", error_code=None,
        error_details=None, empty_result=False,
    ) == []


# --------------------------------------------------------------------------
# A scalar answer is not an empty one
# --------------------------------------------------------------------------


class _Result:
    def __init__(self, value=None, values=None, rows=None):
        self.value = value
        self.values = values or {}
        self.rows = rows or []


class _Answer:
    def __init__(self, results):
        self.results = results


def test_a_single_number_answer_is_not_treated_as_empty() -> None:
    """Otherwise every KPI answer would file a ZERO_ROWS signal."""
    assert mining.result_is_empty(_Answer([_Result(value=1_234.0)])) is False
    assert mining.result_is_empty(_Answer([_Result(values={"net_sales": 1})])) is False


def test_a_result_with_nothing_in_it_is_empty() -> None:
    assert mining.result_is_empty(_Answer([_Result()])) is True


def test_no_result_at_all_is_not_an_empty_result() -> None:
    """A turn that never ran a tool failed some other way and is mined as that."""
    assert mining.result_is_empty(_Answer([])) is False


# --------------------------------------------------------------------------
# Counting, against a real database
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
        session = Session(bind=agent_engine, expire_on_commit=False, future=True)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _session_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def signals(engine) -> list[AgentLearningSignal]:
    with Session(engine) as session:
        return list(session.execute(select(AgentLearningSignal)).scalars().all())


def test_the_same_failure_twice_is_one_row_counted_twice(session) -> None:
    mining.record(session, SignalType.UNKNOWN_INTENT, "koto tk baki ache")
    mining.record(session, SignalType.UNKNOWN_INTENT, "Koto tk baki ache?")
    session.flush()

    rows = list(session.execute(select(AgentLearningSignal)).scalars().all())
    assert len(rows) == 1
    assert rows[0].occurrences == 2
    assert rows[0].status == SignalStatus.NEW


def test_the_untouched_original_is_kept_beside_the_key(session) -> None:
    mining.record(session, SignalType.UNKNOWN_INTENT, "  Koto TK baki ache?  ")
    session.flush()
    row = session.execute(select(AgentLearningSignal)).scalars().one()
    assert row.normalized_phrase == "koto tk baki ache"
    # What was really typed, so a reviewer is not shown only the reduced key.
    assert "TK" in (row.raw_sample or "")


def test_the_same_phrase_failing_two_ways_is_two_signals(session) -> None:
    mining.record(session, SignalType.UNKNOWN_INTENT, "sales for khulna")
    mining.record(session, SignalType.ZERO_ROWS, "sales for khulna")
    session.flush()
    rows = list(session.execute(select(AgentLearningSignal)).scalars().all())
    assert len(rows) == 2
    assert {row.signal_type for row in rows} == {
        SignalType.UNKNOWN_INTENT, SignalType.ZERO_ROWS,
    }


def test_a_blank_phrase_records_nothing(session) -> None:
    assert mining.record(session, SignalType.UNKNOWN_INTENT, "   ") is False
    session.flush()
    assert session.execute(select(AgentLearningSignal)).scalars().all() == []


# --------------------------------------------------------------------------
# The live hooks
# --------------------------------------------------------------------------


def test_asking_something_the_agent_cannot_classify_files_a_signal(
    client, agent_engine,
) -> None:
    response = client.post(
        "/api/chat",
        json={"message": "koto tk baki ache purono customer der kache"},
        headers={"X-User": "ceo"},
    )
    assert response.status_code == 200

    recorded = signals(agent_engine)
    assert recorded, "an unclassified question should reach the review queue"
    assert {row.signal_type for row in recorded} <= set(SignalType.ALL)


def test_a_good_answer_leaves_the_queue_alone(client, agent_engine) -> None:
    response = client.post("/api/chat",
                           json={"message": "total sales this month"},
                           headers={"X-User": "ceo"})
    assert response.status_code == 200
    assert [row for row in signals(agent_engine)
            if row.signal_type == SignalType.UNKNOWN_INTENT] == []


def test_a_thumbs_down_files_the_question_that_earned_it(
    client, agent_engine,
) -> None:
    answer = client.post("/api/chat",
                         json={"message": "total sales this month"},
                         headers={"X-User": "ceo"}).json()
    client.post("/api/chat/feedback",
                json={"message_id": answer["message_id"], "rating": "DOWN"},
                headers={"X-User": "ceo"})

    negative = [row for row in signals(agent_engine)
                if row.signal_type == SignalType.NEGATIVE_FEEDBACK]
    assert len(negative) == 1
    # The *question*, not the answer: it is the half a reviewer can act on.
    assert negative[0].normalized_phrase == "total sales this month"


def test_a_thumbs_up_files_nothing(client, agent_engine) -> None:
    answer = client.post("/api/chat",
                         json={"message": "total sales this month"},
                         headers={"X-User": "ceo"}).json()
    client.post("/api/chat/feedback",
                json={"message_id": answer["message_id"], "rating": "UP"},
                headers={"X-User": "ceo"})
    assert [row for row in signals(agent_engine)
            if row.signal_type == SignalType.NEGATIVE_FEEDBACK] == []


def test_toggling_a_thumb_does_not_inflate_the_count(client, agent_engine) -> None:
    """Only a verdict *becoming* negative counts."""
    answer = client.post("/api/chat",
                         json={"message": "total sales this month"},
                         headers={"X-User": "ceo"}).json()
    for rating in ("DOWN", "UP", "DOWN", "DOWN"):
        client.post("/api/chat/feedback",
                    json={"message_id": answer["message_id"], "rating": rating},
                    headers={"X-User": "ceo"})

    negative = [row for row in signals(agent_engine)
                if row.signal_type == SignalType.NEGATIVE_FEEDBACK]
    assert len(negative) == 1
    assert negative[0].occurrences == 2  # two transitions into DOWN, not four


def test_the_history_sweep_finds_turns_taken_before_mining_existed(
    client, agent_engine, monkeypatch,
) -> None:
    """The live hook sees nothing that happened before it shipped.

    Simulated by turning the hook off for the conversation, then sweeping.
    """
    monkeypatch.setattr(mining, "mine_turn", lambda *a, **k: 0)
    client.post(
        "/api/chat",
        json={"message": "koto tk baki ache purono customer der kache"},
        headers={"X-User": "ceo"},
    )
    monkeypatch.undo()
    assert signals(agent_engine) == []

    with Session(agent_engine) as session:
        counts = mining.mine_history(session)
        session.commit()

    assert counts, "the stored turn should have been mined"
    assert signals(agent_engine)


def test_the_history_sweep_attributes_a_question_to_its_own_answer(
    client, agent_engine, monkeypatch,
) -> None:
    """Two turns in one conversation must not borrow each other's question."""
    monkeypatch.setattr(mining, "mine_turn", lambda *a, **k: 0)
    first = client.post(
        "/api/chat",
        json={"message": "koto tk baki ache purono customer der kache"},
        headers={"X-User": "ceo"},
    ).json()
    client.post(
        "/api/chat",
        json={"message": "ei mashe ki rokom obostha bepar ta",
              "conversation_id": first["conversation_id"]},
        headers={"X-User": "ceo"},
    )
    monkeypatch.undo()

    with Session(agent_engine) as session:
        mining.mine_history(session)
        session.commit()

    phrases = {row.normalized_phrase for row in signals(agent_engine)}
    # Each answer was mined against the question directly above it, so both
    # questions appear rather than one of them twice.
    assert "koto tk baki ache purono customer der kache" in phrases


def test_mining_never_costs_the_user_their_answer(
    client, agent_engine, monkeypatch,
) -> None:
    """A signal that cannot be written must not take the turn down with it."""
    def explode(*args, **kwargs):
        raise RuntimeError("signal table unavailable")

    monkeypatch.setattr(mining, "record", explode)
    # A question that *does* produce a signal, so the broken write is actually
    # reached — asking something answerable would never call ``record`` and the
    # test would pass without exercising anything.
    response = client.post(
        "/api/chat",
        json={"message": "koto tk baki ache purono customer der kache"},
        headers={"X-User": "ceo"},
    )
    assert response.status_code == 200
    assert response.json()["answer"]
    assert signals(agent_engine) == []
