"""Target Management: plans, versions and the rules that hold them together.

Three things are being pinned here, and they are the promises the rest of the
module will be built on:

* **one plan per scope** — a changed target is a new version, not a second plan;
* **an approved version is never overwritten** — a new version copies its
  predecessor's numbers and leaves that predecessor exactly as it was;
* **the workflow is a graph, not a wish** — an illegal status change is refused
  by the service, not merely undrawn by the browser.

The section is off by default for every role, so these tests sign in as an
administrator. That is itself asserted: a Management user who has not been
granted the section is refused, which is the point of separating "read a
target" from "set one".
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.auth.security import hash_password
from app.database.models_ai import AppUser, AuditAction, AuditLog, Role
from app.database.models_target import (
    TargetAudit,
    TargetCountryLine,
    TargetPlan,
    TargetStatus,
    TargetVersion,
)
from app.main import app
from app.targetmgmt import plans as plan_service
from app.targetmgmt.errors import (
    InvalidTransition,
    PlanScopeConflict,
    ReasonRequired,
    ScopeMismatch,
    UnknownScopeCode,
)

PASSWORD = "Correct-Horse-9"

SCOPE = {
    "financial_year": "FY 2026-27",
    "target_period": "Q1",
    "company_code": "C001",
    "bu_code": "BU001",
    "sales_line_code": "SL001",
}


@pytest.fixture
def tm_client(agent_engine, users):
    """A signed-in client, plus an administrator who holds the section.

    ``root`` is SUPER_ADMIN, which is the only role the Target Management
    section is on for by default. ``ceo`` keeps the role the shared fixture gave
    it, so the refusal test below exercises the real default rather than a
    contrived one.
    """
    with Session(agent_engine) as session:
        for user in session.query(AppUser).all():
            user.password_hash = hash_password(PASSWORD)
        session.add(AppUser(username="root", display_name="Administrator",
                            role=Role.SUPER_ADMIN, is_active=True,
                            password_hash=hash_password(PASSWORD)))
        session.commit()

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


def _token(client: TestClient, username: str = "root") -> dict[str, str]:
    response = client.post("/api/auth/login",
                           json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_plan(client: TestClient, headers: dict[str, str], **overrides):
    body = {**SCOPE, **overrides}
    return client.post("/api/target-management/plans", json=body, headers=headers)


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def test_the_section_is_refused_without_a_token(tm_client: TestClient) -> None:
    assert tm_client.get("/api/target-management/plans").status_code == 401


def test_setting_a_target_is_not_the_same_grant_as_reading_one(
        tm_client: TestClient) -> None:
    """A Management user holds Target and does not hold Target Management.

    The whole reason this is a separate section: seeing achievement against a
    target and deciding the target the sales force is measured on are different
    privileges, and one must not imply the other.
    """
    headers = _token(tm_client, "ceo")
    assert tm_client.get("/api/pages/target", headers=headers).status_code == 200
    assert tm_client.get("/api/target-management/plans",
                         headers=headers).status_code == 403


def test_options_report_what_this_caller_may_do(tm_client: TestClient) -> None:
    headers = _token(tm_client)
    body = tm_client.get("/api/target-management/options",
                         headers=headers).json()
    assert body["actions"]["CREATE"] is True
    # An administrator configures the run and never signs off on a number.
    assert body["actions"]["APPROVE"] is False


def test_options_are_read_from_the_master_data(tm_client: TestClient) -> None:
    """Derived, not declared: a company added to the master appears here."""
    headers = _token(tm_client)
    body = tm_client.get("/api/target-management/options",
                         headers=headers).json()
    assert [c["code"] for c in body["companies"]] == ["C001"]
    assert [u["code"] for u in body["business_units"]] == ["BU001"]
    assert body["sales_lines"][0]["bu_code"] == "BU001"
    assert [p["code"] for p in body["periods"]] == ["FY", "Q1", "Q2", "Q3", "Q4"]


def test_the_quarter_labels_follow_the_configured_financial_year(
        tm_client: TestClient) -> None:
    """Q1 is July under a July start. Derived, so a January start would say January."""
    headers = _token(tm_client)
    periods = {
        p["code"]: p["label"]
        for p in tm_client.get("/api/target-management/options",
                               headers=headers).json()["periods"]
    }
    assert periods["Q1"] == "Q1 (Jul – Sep)"
    assert periods["Q3"] == "Q3 (Jan – Mar)"


# ---------------------------------------------------------------------------
# Creating a plan
# ---------------------------------------------------------------------------


def test_creating_a_plan_creates_its_first_version(tm_client: TestClient,
                                                   agent_engine) -> None:
    headers = _token(tm_client)
    response = _create_plan(tm_client, headers)
    assert response.status_code == 201, response.text
    plan = response.json()["plan"]
    assert plan["plan_code"] == "TP-2026-001"
    assert plan["current_version_no"] == 1
    assert plan["status"] == TargetStatus.DRAFT

    with Session(agent_engine) as session:
        version = session.execute(select(TargetVersion)).scalar_one()
        assert version.version_no == 1
        # Current, and claiming it through the column the constraint watches.
        assert version.current_plan_id == plan["plan_id"]


def test_a_second_plan_for_the_same_scope_is_refused(tm_client: TestClient) -> None:
    headers = _token(tm_client)
    assert _create_plan(tm_client, headers).status_code == 201
    conflict = _create_plan(tm_client, headers)
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["error_code"] == "TARGET_PLAN_SCOPE_CONFLICT"
    # The refusal names the plan that holds the scope, so it can be found.
    assert "TP-2026-001" in detail["message"]


def test_a_different_period_is_a_different_plan(tm_client: TestClient) -> None:
    headers = _token(tm_client)
    assert _create_plan(tm_client, headers, target_period="Q1").status_code == 201
    second = _create_plan(tm_client, headers, target_period="Q2")
    assert second.status_code == 201
    assert second.json()["plan"]["plan_code"] == "TP-2026-002"


def test_an_unknown_company_is_named_rather_than_masked(
        tm_client: TestClient) -> None:
    headers = _token(tm_client)
    response = _create_plan(tm_client, headers, company_code="C999")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error_code"] == "TARGET_SCOPE_CODE_UNKNOWN"
    assert "C999" in detail["message"]


def test_a_sales_line_under_another_business_unit_is_refused(
        agent_engine, users) -> None:
    """Checked in the service, so a caller bypassing the API is refused too."""
    with Session(agent_engine) as session:
        from app.database.models import DimBusinessUnit

        session.add(DimBusinessUnit(bu_code="BU002", bu_name="Other",
                                    company_code="C001"))
        session.commit()
        scope = plan_service.PlanScope(
            financial_year="FY 2026-27", target_period="Q1",
            company_code="C001", bu_code="BU002", sales_line_code="SL001",
        )
        with pytest.raises(ScopeMismatch):
            plan_service.validate_scope(session, scope)


def test_a_business_unit_under_another_company_is_refused(agent_engine,
                                                          users) -> None:
    with Session(agent_engine) as session:
        from app.database.models import DimCompany

        session.add(DimCompany(company_code="C002", company_name="Other Ltd."))
        session.commit()
        scope = plan_service.PlanScope(
            financial_year="FY 2026-27", target_period="Q1",
            company_code="C002", bu_code="BU001", sales_line_code="SL001",
        )
        with pytest.raises(ScopeMismatch):
            plan_service.validate_scope(session, scope)


def test_an_unknown_sales_line_is_refused(agent_engine, users) -> None:
    with Session(agent_engine) as session:
        scope = plan_service.PlanScope(
            financial_year="FY 2026-27", target_period="Q1",
            company_code="C001", bu_code="BU001", sales_line_code="SL999",
        )
        with pytest.raises(UnknownScopeCode):
            plan_service.validate_scope(session, scope)


def test_creating_a_plan_writes_both_audit_trails(tm_client: TestClient,
                                                  agent_engine) -> None:
    """The business trail a planner reads, and the security trail an admin does.

    Two rows, on purpose: neither answers the other's question.
    """
    headers = _token(tm_client)
    _create_plan(tm_client, headers)
    with Session(agent_engine) as session:
        business = session.execute(select(TargetAudit)).scalars().all()
        assert [row.action for row in business] == ["PLAN_CREATED"]
        assert business[0].actor == "root"
        assert business[0].actor_role == Role.SUPER_ADMIN

        security = session.execute(
            select(AuditLog).where(AuditLog.action == AuditAction.TARGET_PLAN_CREATED)
        ).scalars().all()
        assert len(security) == 1
        assert security[0].username == "root"


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def test_a_new_version_copies_the_country_volumes(tm_client: TestClient,
                                                  agent_engine) -> None:
    """A new version starts from what was there, not from a blank page."""
    headers = _token(tm_client)
    plan_id = _create_plan(tm_client, headers).json()["plan"]["plan_id"]
    with Session(agent_engine) as session:
        version_id = session.execute(select(TargetVersion.version_id)).scalar_one()
        session.add(TargetCountryLine(version_id=version_id,
                                      material_code="MAT-1001",
                                      target_volume=1500))
        session.commit()

    response = tm_client.post(
        f"/api/target-management/plans/{plan_id}/versions",
        json={"reason": "Herbicide demand review."}, headers=headers)
    assert response.status_code == 201, response.text

    with Session(agent_engine) as session:
        lines = session.execute(select(TargetCountryLine)).scalars().all()
        assert len(lines) == 2
        assert {float(line.target_volume) for line in lines} == {1500.0}


def test_the_previous_version_keeps_its_own_numbers(tm_client: TestClient,
                                                    agent_engine) -> None:
    """Copied, not moved. A version whose numbers left it is not what was approved."""
    headers = _token(tm_client)
    plan_id = _create_plan(tm_client, headers).json()["plan"]["plan_id"]
    with Session(agent_engine) as session:
        first = session.execute(select(TargetVersion.version_id)).scalar_one()
        session.add(TargetCountryLine(version_id=first, material_code="MAT-1001",
                                      target_volume=1500))
        session.commit()

    tm_client.post(f"/api/target-management/plans/{plan_id}/versions",
                   json={"reason": "Raised after actuals closed."}, headers=headers)

    with Session(agent_engine) as session:
        kept = session.execute(
            select(TargetCountryLine).where(TargetCountryLine.version_id == first)
        ).scalars().all()
        assert len(kept) == 1
        assert float(kept[0].target_volume) == 1500.0


def test_the_new_version_becomes_current(tm_client: TestClient,
                                         agent_engine) -> None:
    headers = _token(tm_client)
    plan_id = _create_plan(tm_client, headers).json()["plan"]["plan_id"]
    tm_client.post(f"/api/target-management/plans/{plan_id}/versions",
                   json={"reason": "Second pass."}, headers=headers)

    with Session(agent_engine) as session:
        current = session.execute(
            select(TargetVersion).where(TargetVersion.current_plan_id == plan_id)
        ).scalars().all()
        assert len(current) == 1
        assert current[0].version_no == 2


def test_a_superseded_draft_is_marked_revised(tm_client: TestClient,
                                              agent_engine) -> None:
    headers = _token(tm_client)
    plan_id = _create_plan(tm_client, headers).json()["plan"]["plan_id"]
    tm_client.post(f"/api/target-management/plans/{plan_id}/versions",
                   json={"reason": "Second pass."}, headers=headers)
    with Session(agent_engine) as session:
        first = session.execute(
            select(TargetVersion).where(TargetVersion.version_no == 1)
        ).scalar_one()
        assert first.status == TargetStatus.REVISED


def test_a_superseded_approved_version_keeps_its_status(agent_engine,
                                                        users) -> None:
    """What it *is* did not change because something newer exists.

    Rewriting an approved version's status would erase the record of a target
    the business actually agreed to — the exact thing versioning exists for.
    """
    with Session(agent_engine) as session:
        user = users["ceo"]
        plan = plan_service.create_plan(
            session, user,
            scope=plan_service.PlanScope(**SCOPE),
        )
        first = plan_service.current_version(session, plan.plan_id)
        first.status = TargetStatus.APPROVED
        session.flush()

        plan_service.create_version(session, user, plan_id=plan.plan_id,
                                    reason="Next cycle.")
        session.commit()

        kept = session.execute(
            select(TargetVersion).where(TargetVersion.version_no == 1)
        ).scalar_one()
        assert kept.status == TargetStatus.APPROVED


def test_a_version_requires_a_reason(tm_client: TestClient) -> None:
    headers = _token(tm_client)
    plan_id = _create_plan(tm_client, headers).json()["plan"]["plan_id"]
    response = tm_client.post(f"/api/target-management/plans/{plan_id}/versions",
                              json={"reason": "   "}, headers=headers)
    # 422 from the schema or 409 from the service — both refuse it, and the
    # service refuses it even for a caller that never touched the schema.
    assert response.status_code in (409, 422)


def test_the_service_refuses_a_blank_reason_too(agent_engine, users) -> None:
    with Session(agent_engine) as session:
        plan = plan_service.create_plan(
            session, users["ceo"], scope=plan_service.PlanScope(**SCOPE))
        with pytest.raises(ReasonRequired):
            plan_service.create_version(session, users["ceo"],
                                        plan_id=plan.plan_id, reason="")


def test_a_plan_that_does_not_exist_answers_404(tm_client: TestClient) -> None:
    """"There is no such plan" is a different problem from "that plan says no"."""
    headers = _token(tm_client)
    assert tm_client.get("/api/target-management/plans/9999",
                         headers=headers).status_code == 404


def test_a_write_against_a_missing_plan_answers_404_too(
        tm_client: TestClient) -> None:
    """The read and write paths share one rule, so the answer does not depend
    on the verb."""
    headers = _token(tm_client)
    response = tm_client.post("/api/target-management/plans/9999/versions",
                              json={"reason": "Nowhere to put this."},
                              headers=headers)
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "TARGET_PLAN_NOT_FOUND"


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


def test_a_legal_transition_is_accepted(tm_client: TestClient) -> None:
    headers = _token(tm_client)
    plan = _create_plan(tm_client, headers).json()["plan"]
    response = tm_client.patch(
        f"/api/target-management/versions/{plan['current_version_id']}/status",
        json={"status": "UNDER_REVIEW"}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["version"]["status"] == "UNDER_REVIEW"


def test_an_illegal_transition_is_refused(tm_client: TestClient) -> None:
    """A draft cannot jump straight to locked, whatever the caller asks for."""
    headers = _token(tm_client)
    plan = _create_plan(tm_client, headers).json()["plan"]
    response = tm_client.patch(
        f"/api/target-management/versions/{plan['current_version_id']}/status",
        json={"status": "LOCKED"}, headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "TARGET_INVALID_TRANSITION"


def test_a_locked_version_goes_nowhere(agent_engine, users) -> None:
    with Session(agent_engine) as session:
        plan = plan_service.create_plan(
            session, users["ceo"], scope=plan_service.PlanScope(**SCOPE))
        version = plan_service.current_version(session, plan.plan_id)
        version.status = TargetStatus.LOCKED
        session.flush()
        with pytest.raises(InvalidTransition):
            plan_service.set_version_status(
                session, users["ceo"], version_id=version.version_id,
                new_status=TargetStatus.UNDER_REVIEW)


def test_setting_the_status_it_already_has_is_a_no_op(agent_engine,
                                                      users) -> None:
    """A retried request must not fail differently from the one that succeeded."""
    with Session(agent_engine) as session:
        plan = plan_service.create_plan(
            session, users["ceo"], scope=plan_service.PlanScope(**SCOPE))
        version = plan_service.current_version(session, plan.plan_id)
        again = plan_service.set_version_status(
            session, users["ceo"], version_id=version.version_id,
            new_status=TargetStatus.DRAFT)
        assert again.status == TargetStatus.DRAFT


def test_the_plan_follows_its_current_version(tm_client: TestClient,
                                              agent_engine) -> None:
    headers = _token(tm_client)
    plan = _create_plan(tm_client, headers).json()["plan"]
    tm_client.patch(
        f"/api/target-management/versions/{plan['current_version_id']}/status",
        json={"status": "UNDER_REVIEW"}, headers=headers)
    with Session(agent_engine) as session:
        stored = session.get(TargetPlan, plan["plan_id"])
        assert stored.status == TargetStatus.UNDER_REVIEW


def test_a_superseded_version_does_not_speak_for_the_plan(agent_engine,
                                                          users) -> None:
    """Only the current version says what the plan is doing now."""
    with Session(agent_engine) as session:
        plan = plan_service.create_plan(
            session, users["ceo"], scope=plan_service.PlanScope(**SCOPE))
        first = plan_service.current_version(session, plan.plan_id)
        plan_service.create_version(session, users["ceo"],
                                    plan_id=plan.plan_id, reason="Next.")
        session.flush()
        # The superseded V1 is REVISED, which is terminal — so nudge it back to
        # a state it can legally leave, then move it, and check the plan did not
        # follow.
        first.status = TargetStatus.DRAFT
        session.flush()
        plan_service.set_version_status(
            session, users["ceo"], version_id=first.version_id,
            new_status=TargetStatus.UNDER_REVIEW)
        assert plan.status == TargetStatus.DRAFT


def test_a_status_change_is_audited_with_both_values(agent_engine,
                                                     users) -> None:
    with Session(agent_engine) as session:
        plan = plan_service.create_plan(
            session, users["ceo"], scope=plan_service.PlanScope(**SCOPE))
        version = plan_service.current_version(session, plan.plan_id)
        plan_service.set_version_status(
            session, users["ceo"], version_id=version.version_id,
            new_status=TargetStatus.UNDER_REVIEW, reason="Ready for review.")
        session.commit()

        entry = session.execute(
            select(TargetAudit).where(TargetAudit.action == "TARGET_SUBMITTED")
        ).scalar_one()
        assert entry.old_value == TargetStatus.DRAFT
        assert entry.new_value == TargetStatus.UNDER_REVIEW
        assert entry.reason == "Ready for review."


# ---------------------------------------------------------------------------
# Editability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [TargetStatus.APPROVED, TargetStatus.LOCKED])
def test_a_frozen_version_refuses_a_write(agent_engine, users, status) -> None:
    """The module's central promise, in one function every write path calls."""
    from app.targetmgmt.errors import VersionFrozen

    with Session(agent_engine) as session:
        plan = plan_service.create_plan(
            session, users["ceo"], scope=plan_service.PlanScope(**SCOPE))
        version = plan_service.current_version(session, plan.plan_id)
        version.status = status
        with pytest.raises(VersionFrozen):
            plan_service.assert_editable(version)


@pytest.mark.parametrize("status", [TargetStatus.DRAFT, TargetStatus.ALLOCATED,
                                    TargetStatus.REJECTED])
def test_an_open_version_accepts_a_write(agent_engine, users, status) -> None:
    with Session(agent_engine) as session:
        plan = plan_service.create_plan(
            session, users["ceo"], scope=plan_service.PlanScope(**SCOPE))
        version = plan_service.current_version(session, plan.plan_id)
        version.status = status
        plan_service.assert_editable(version)  # does not raise


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_a_regional_scope_does_not_hide_the_plan_list(agent_engine,
                                                      users) -> None:
    """A plan states no region, so a regional scope cannot narrow it.

    Filtering on a level the row does not carry would hide every plan from the
    very people who have to review one. Their scope binds where their *targets*
    are read, not here.
    """
    with Session(agent_engine) as session:
        plan_service.create_plan(session, users["ceo"],
                                 scope=plan_service.PlanScope(**SCOPE))
        session.commit()
        rows, total = plan_service.list_plans(session, users["dhaka_rm"])
        assert total == 1
        assert rows[0]["plan_code"] == "TP-2026-001"


def test_a_company_scope_does_narrow_the_plan_list(agent_engine, users) -> None:
    """A plan *does* state a company, so a company scope binds."""
    from app.ai.permission_filter import UserContext

    with Session(agent_engine) as session:
        plan_service.create_plan(session, users["ceo"],
                                 scope=plan_service.PlanScope(**SCOPE))
        session.commit()
        elsewhere = UserContext(
            user_id=999, username="other", role=Role.REGIONAL_MANAGER,
            data_scope={"company_code": ["C002"]},
        )
        _, total = plan_service.list_plans(session, elsewhere)
        assert total == 0


# ---------------------------------------------------------------------------
# Plan codes
# ---------------------------------------------------------------------------


def test_plan_codes_are_sequential_within_the_financial_year(agent_engine,
                                                             users) -> None:
    with Session(agent_engine) as session:
        codes = []
        for period in ("Q1", "Q2", "Q3"):
            plan = plan_service.create_plan(
                session, users["ceo"],
                scope=plan_service.PlanScope(**{**SCOPE, "target_period": period}))
            codes.append(plan.plan_code)
        assert codes == ["TP-2026-001", "TP-2026-002", "TP-2026-003"]


def test_the_scope_conflict_is_raised_before_a_code_is_burnt(agent_engine,
                                                             users) -> None:
    """A refused creation must not consume a plan number.

    The conflict is raised before the code is allocated, so the plan that comes
    after a refusal is 002 rather than 003 — a planner should not see a gap in
    the sequence where somebody's mistake used to be.
    """
    with Session(agent_engine) as session:
        plan_service.create_plan(session, users["ceo"],
                                 scope=plan_service.PlanScope(**SCOPE))
        session.commit()

    with Session(agent_engine) as session:
        with pytest.raises(PlanScopeConflict):
            plan_service.create_plan(session, users["ceo"],
                                     scope=plan_service.PlanScope(**SCOPE))
        session.rollback()

    with Session(agent_engine) as session:
        plan = plan_service.create_plan(
            session, users["ceo"],
            scope=plan_service.PlanScope(**{**SCOPE, "target_period": "Q2"}))
        assert plan.plan_code == "TP-2026-002"
