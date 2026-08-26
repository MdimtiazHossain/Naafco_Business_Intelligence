"""The approval path: validation, the single-active rule, and the section gate.

The guarantee under test is that nothing the agent reads got there without a
person putting it there, and that what they approved points at something real.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import vocabulary
from app.api.deps import get_session
from app.database.models_learning import (
    AgentTermAlias,
    AliasKind,
    LearningStatus,
    SignalStatus,
    SignalType,
)
from app.main import app

pytest.importorskip("multipart",
                    reason="python-multipart is required by FastAPI forms")


@pytest.fixture
def admin(agent_engine, users):
    """An administrator, which the shared user fixtures do not provide.

    ``AGENT_LEARNING`` is off by default for every role except the admin ones,
    which is the point of the section — so testing the gate needs somebody on
    each side of it, and the seeded users are all on the denied side.
    """
    from app.database.models_ai import AppUser, Role

    with Session(agent_engine) as session:
        user = AppUser(username="vocab_admin", display_name="Vocabulary Admin",
                       role=Role.ADMIN, data_scope=None, is_active=True)
        session.add(user)
        session.commit()
    return "vocab_admin"


@pytest.fixture
def client(agent_engine, users):
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


def head(user: str = "ceo") -> dict[str, str]:
    return {"X-User": user}


def a_region_code(agent_engine) -> str:
    from app.database.models import DimRegion

    with Session(agent_engine) as session:
        return session.execute(select(DimRegion.region_code)).scalars().first()


# --------------------------------------------------------------------------
# An alias may not point at something that does not exist
# --------------------------------------------------------------------------


def test_an_entity_alias_must_name_a_master_record(session, users) -> None:
    with pytest.raises(vocabulary.ReviewRefused) as raised:
        vocabulary.propose_alias(
            session, users["ceo"], phrase="chini", alias_kind=AliasKind.ENTITY,
            entity_type="region", entity_code="NOPE-999",
        )
    # The reason is the value here: a reviewer told this can fix it.
    assert "NOPE-999" in raised.value.user_message


def test_an_entity_alias_onto_a_real_record_is_accepted(
    session, users, agent_engine,
) -> None:
    row = vocabulary.propose_alias(
        session, users["ceo"], phrase="dhaka division",
        alias_kind=AliasKind.ENTITY, entity_type="region",
        entity_code=a_region_code(agent_engine),
    )
    assert row["status"] == LearningStatus.PROPOSED
    assert row["alias_kind"] == AliasKind.ENTITY


def test_a_metric_alias_must_name_a_keyword_the_agent_has(session, users) -> None:
    with pytest.raises(vocabulary.ReviewRefused) as raised:
        vocabulary.propose_alias(
            session, users["ceo"], phrase="bikroy", alias_kind=AliasKind.METRIC,
            target_keyword="turnover_total",
        )
    # Names what *is* allowed, so the reviewer can correct it in one step.
    assert "sales" in raised.value.user_message


def test_a_metric_alias_onto_a_real_keyword_is_accepted(session, users) -> None:
    row = vocabulary.propose_alias(
        session, users["ceo"], phrase="bikroy", alias_kind=AliasKind.METRIC,
        target_keyword="sales",
    )
    assert row["target_keyword"] == "sales"


def test_an_unknown_entity_type_is_refused(session, users) -> None:
    with pytest.raises(vocabulary.ReviewRefused):
        vocabulary.propose_alias(
            session, users["ceo"], phrase="x", alias_kind=AliasKind.ENTITY,
            entity_type="warehouse", entity_code="W1",
        )


def test_the_phrase_is_stored_as_the_key_the_resolver_will_look_up(
    session, users,
) -> None:
    """Normalised the same way a mined phrase is, or the two would never meet."""
    row = vocabulary.propose_alias(
        session, users["ceo"], phrase="  BIKROY  ?", alias_kind=AliasKind.METRIC,
        target_keyword="sales",
    )
    assert row["phrase"] == "bikroy"


# --------------------------------------------------------------------------
# Nothing is live until approved
# --------------------------------------------------------------------------


def test_a_proposal_is_inert(session, users) -> None:
    row = vocabulary.propose_alias(
        session, users["ceo"], phrase="bikroy", alias_kind=AliasKind.METRIC,
        target_keyword="sales",
    )
    session.flush()
    stored = session.get(AgentTermAlias, row["alias_id"])
    assert stored.status == LearningStatus.PROPOSED
    # NULL until approved: that is what leaves the phrase free.
    assert stored.active_key is None


def test_approving_makes_it_active_and_names_who_did_it(session, users) -> None:
    proposed = vocabulary.propose_alias(
        session, users["ceo"], phrase="bikroy", alias_kind=AliasKind.METRIC,
        target_keyword="sales",
    )
    row = vocabulary.approve_alias(session, users["ceo"], proposed["alias_id"])
    assert row["status"] == LearningStatus.ACTIVE
    assert row["approved_by"] == "ceo"
    assert row["approved_at"] is not None


def test_approving_twice_is_refused(session, users) -> None:
    proposed = vocabulary.propose_alias(
        session, users["ceo"], phrase="bikroy", alias_kind=AliasKind.METRIC,
        target_keyword="sales",
    )
    vocabulary.approve_alias(session, users["ceo"], proposed["alias_id"])
    with pytest.raises(vocabulary.ReviewRefused):
        vocabulary.approve_alias(session, users["ceo"], proposed["alias_id"])


# --------------------------------------------------------------------------
# One active meaning per phrase
# --------------------------------------------------------------------------


def _two_proposals(session, users) -> tuple[int, int]:
    first = vocabulary.propose_alias(
        session, users["ceo"], phrase="bikroy", alias_kind=AliasKind.METRIC,
        target_keyword="sales",
    )["alias_id"]
    second = vocabulary.propose_alias(
        session, users["ceo"], phrase="bikroy", alias_kind=AliasKind.METRIC,
        target_keyword="target",
    )["alias_id"]
    return first, second


def test_a_second_meaning_for_one_phrase_is_refused_and_names_the_incumbent(
    session, users,
) -> None:
    first, second = _two_proposals(session, users)
    vocabulary.approve_alias(session, users["ceo"], first)

    with pytest.raises(vocabulary.ReviewRefused) as raised:
        vocabulary.approve_alias(session, users["ceo"], second)
    assert "sales" in raised.value.user_message


def test_replacing_retires_the_incumbent_rather_than_deleting_it(
    session, users,
) -> None:
    first, second = _two_proposals(session, users)
    vocabulary.approve_alias(session, users["ceo"], first)
    vocabulary.approve_alias(session, users["ceo"], second, replace=True)
    session.flush()

    old = session.get(AgentTermAlias, first)
    new = session.get(AgentTermAlias, second)
    assert old.status == LearningStatus.RETIRED
    assert old.active_key is None          # freed, so the phrase can move on
    assert old.retired_at is not None      # and the record of it survives
    assert new.status == LearningStatus.ACTIVE
    assert new.active_key == vocabulary.alias_active_key(AliasKind.METRIC, "bikroy")


def test_a_retired_phrase_can_be_taught_again(session, users) -> None:
    """The reason ``active_key`` exists rather than a plain unique constraint."""
    first, second = _two_proposals(session, users)
    vocabulary.approve_alias(session, users["ceo"], first)
    vocabulary.retire_alias(session, users["ceo"], first, reason="wrong")

    row = vocabulary.approve_alias(session, users["ceo"], second)
    assert row["status"] == LearningStatus.ACTIVE


def test_retiring_something_not_active_is_refused(session, users) -> None:
    proposed = vocabulary.propose_alias(
        session, users["ceo"], phrase="bikroy", alias_kind=AliasKind.METRIC,
        target_keyword="sales",
    )
    with pytest.raises(vocabulary.ReviewRefused):
        vocabulary.retire_alias(session, users["ceo"], proposed["alias_id"])


def test_rejecting_keeps_the_row_so_it_is_not_proposed_again(
    session, users,
) -> None:
    proposed = vocabulary.propose_alias(
        session, users["ceo"], phrase="bikroy", alias_kind=AliasKind.METRIC,
        target_keyword="sales",
    )
    row = vocabulary.reject_alias(session, users["ceo"], proposed["alias_id"],
                                  reason="means something else here")
    assert row["status"] == LearningStatus.REJECTED
    assert session.get(AgentTermAlias, proposed["alias_id"]) is not None


# --------------------------------------------------------------------------
# Examples
# --------------------------------------------------------------------------


def test_an_example_must_name_a_tool_that_exists(session, users) -> None:
    with pytest.raises(vocabulary.ReviewRefused):
        vocabulary.propose_example(
            session, users["ceo"], question="aajker sales koto",
            tool_name="get_outstanding_summary",   # removed in revision 0020
        )


def test_an_example_onto_a_real_tool_is_accepted_and_inert(session, users) -> None:
    row = vocabulary.propose_example(
        session, users["ceo"], question="aajker sales koto",
        tool_name="get_sales_summary",
    )
    assert row["status"] == LearningStatus.PROPOSED
    assert row["use_count"] == 0


def test_only_one_example_may_answer_a_question(session, users) -> None:
    first = vocabulary.propose_example(
        session, users["ceo"], question="aajker sales koto",
        tool_name="get_sales_summary",
    )["example_id"]
    second = vocabulary.propose_example(
        session, users["ceo"], question="Aajker sales koto?",
        tool_name="get_sales_detail",
    )["example_id"]

    vocabulary.approve_example(session, users["ceo"], first)
    with pytest.raises(vocabulary.ReviewRefused):
        vocabulary.approve_example(session, users["ceo"], second)

    replaced = vocabulary.approve_example(session, users["ceo"], second,
                                          replace=True)
    assert replaced["status"] == LearningStatus.ACTIVE


# --------------------------------------------------------------------------
# The queue
# --------------------------------------------------------------------------


def test_the_queue_is_ordered_by_how_often_something_failed(session) -> None:
    from app.ai import mining

    for _ in range(3):
        mining.record(session, SignalType.UNKNOWN_INTENT, "frequent question")
    mining.record(session, SignalType.UNKNOWN_INTENT, "rare question")
    session.flush()

    page = vocabulary.list_signals(session)
    assert page.total == 2
    # A reviewer's time is scarce; the thing that fails most comes first.
    assert page.rows[0]["phrase"] == "frequent question"
    assert page.rows[0]["occurrences"] == 3


def test_proposing_from_a_signal_marks_that_signal_proposed(session, users) -> None:
    from app.ai import mining

    mining.record(session, SignalType.UNKNOWN_INTENT, "bikroy koto")
    session.flush()
    signal = vocabulary.list_signals(session).rows[0]
    assert signal["status"] == SignalStatus.NEW

    vocabulary.propose_alias(
        session, users["ceo"], phrase="bikroy", alias_kind=AliasKind.METRIC,
        target_keyword="sales", signal_id=signal["signal_id"],
    )
    session.flush()
    assert vocabulary.list_signals(session).rows[0]["status"] == SignalStatus.PROPOSED


# --------------------------------------------------------------------------
# The section gate
# --------------------------------------------------------------------------


def test_the_review_api_is_refused_without_the_section(client) -> None:
    """Off by default for everyone, including a regional manager."""
    response = client.get("/api/learning/signals", headers=head("dhaka_rm"))
    assert response.status_code == 403


def test_management_alone_does_not_hold_the_section(client) -> None:
    """Being senior is not the same as owning the vocabulary.

    The section is granted on its own, so the managing director is refused it
    until somebody grants it — exactly like Map Settings.
    """
    assert client.get("/api/learning/signals",
                      headers=head("ceo")).status_code == 403


def test_an_administrator_may_read_the_queue(client, admin) -> None:
    response = client.get("/api/learning/signals", headers=head(admin))
    assert response.status_code == 200, response.text
    assert "signals" in response.json()


def test_the_options_endpoint_is_derived_from_the_shipped_tables(
    client, admin,
) -> None:
    response = client.get("/api/learning/options", headers=head(admin))
    assert response.status_code == 200, response.text
    body = response.json()
    # Read from ``ai.intent``, so a keyword added there appears with no edit
    # to the browser.
    assert "sales" in body["alias_targets"][AliasKind.METRIC]
    assert set(body["alias_kinds"]) == set(AliasKind.ALL)


def test_an_approval_round_trip_over_the_api(client, admin, agent_engine) -> None:
    """Propose then approve, as two separate calls — there is no shortcut."""
    proposed = client.post(
        "/api/learning/aliases",
        json={"phrase": "bikroy", "alias_kind": "METRIC",
              "target_keyword": "sales"},
        headers=head(admin),
    )
    assert proposed.status_code == 201, proposed.text
    alias_id = proposed.json()["alias_id"]
    assert proposed.json()["status"] == LearningStatus.PROPOSED

    approved = client.post(f"/api/learning/aliases/{alias_id}/approve",
                           json={}, headers=head(admin))
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == LearningStatus.ACTIVE


def test_an_alias_pointing_nowhere_is_refused_over_the_api(client, admin) -> None:
    response = client.post(
        "/api/learning/aliases",
        json={"phrase": "bikroy", "alias_kind": "METRIC",
              "target_keyword": "not_a_metric"},
        headers=head(admin),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "REVIEW_REFUSED"


def test_a_second_meaning_conflicts_over_the_api(client, admin) -> None:
    def propose(keyword: str) -> int:
        return client.post(
            "/api/learning/aliases",
            json={"phrase": "bikroy", "alias_kind": "METRIC",
                  "target_keyword": keyword},
            headers=head(admin),
        ).json()["alias_id"]

    first, second = propose("sales"), propose("target")
    client.post(f"/api/learning/aliases/{first}/approve", json={},
                headers=head(admin))

    clash = client.post(f"/api/learning/aliases/{second}/approve", json={},
                        headers=head(admin))
    # 409, not 400: the request is well formed, the *state* refuses it.
    assert clash.status_code == 409

    replaced = client.post(f"/api/learning/aliases/{second}/approve",
                           json={"replace": True}, headers=head(admin))
    assert replaced.status_code == 200
