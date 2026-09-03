"""Deleting a draft plan that was created and abandoned.

The rest of this package supersedes rather than removes, and that stays true.
Deletion is narrow on purpose: a plan that has been allocated, submitted or
locked has a history somebody may need to read, and only a plan that has been
none of those is a form filled in by mistake rather than a record.

Three promises are pinned.

**A typed country target does not block it.** Figures entered and never
allocated are a *draft* target — nothing downstream reads them — and treating
them as history would make a plan undeletable the moment anybody typed into it.
The distinction this package keeps is between a draft target and an allocated
or approved one, and only the second is a record.

**Everything the plan became does block it**, each refused by name, because an
allocation is undone by re-running, an approval by the person who gave it, and a
lock not at all.

**The audit trail outlives the plan.** ``target_audit`` holds it by a SET NULL
foreign key, so every entry survives the delete with its actor, action and
reason; only the link goes. That is what makes this safe to offer, and why it
needs no schema change.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models_ai import Role
from app.database.models_target import (
    ApprovalAction,
    TargetAllocation,
    TargetApproval,
    TargetAudit,
    TargetCountryLine,
    TargetPlan,
    TargetStatus,
    TargetVersion,
)
from app.targetmgmt import (
    country,
    engine as allocation_engine,
    plans as plan_service,
)
from app.targetmgmt.errors import PlanNotDeletable
from test_target_allocation import (  # noqa: F401  (fixtures reused wholesale)
    MATERIAL,
    SCOPE,
    _run,
    _seed_sales,
    _worker_engine,
    hierarchy,
    planned,
    with_history,
)


def _open(engine, planned):
    session = Session(engine)
    plan = plan_service.get_plan(session, planned["plan_id"])
    version = plan_service.get_version(session, planned["version_id"])
    return session, plan, version


def _plan_rows(session, plan_id) -> int:
    return session.execute(
        select(func.count(TargetPlan.plan_id))
        .where(TargetPlan.plan_id == plan_id)).scalar() or 0


# ---------------------------------------------------------------------------
# The ordinary case
# ---------------------------------------------------------------------------


def test_an_untouched_draft_plan_is_deletable(with_history, planned,
                                              users) -> None:
    session, plan, _ = _open(with_history, planned)
    assert plan_service.deletion_blockers(session, plan) == []

    result = plan_service.delete_plan(session, users["ceo"],
                                      plan_id=plan.plan_id)
    session.commit()

    assert result["versions_removed"] == 1
    assert _plan_rows(session, plan.plan_id) == 0
    session.close()


def test_a_typed_country_target_does_not_block_deletion(with_history, planned,
                                                        users) -> None:
    """A draft target has gone nowhere; nothing downstream reads it.

    Blocking on it would make a plan undeletable the moment anybody typed into
    it, which is the opposite of what deletion is for.
    """
    session, plan, version = _open(with_history, planned)
    country.set_lines(session, users["ceo"], version=version, plan=plan,
                      entries=[(MATERIAL, 120000)])
    session.commit()

    assert plan_service.deletion_blockers(session, plan) == []
    result = plan_service.delete_plan(session, users["ceo"],
                                      plan_id=plan.plan_id)
    session.commit()

    assert result["country_lines_removed"] == 1
    assert _plan_rows(session, plan.plan_id) == 0
    session.close()


def test_the_versions_and_their_lines_go_with_the_plan(with_history, planned,
                                                       users) -> None:
    """Versions are deleted explicitly — the plan's foreign key is RESTRICT, on
    purpose, so nothing can remove a plan and silently take them with it."""
    session, plan, version = _open(with_history, planned)
    country.set_lines(session, users["ceo"], version=version, plan=plan,
                      entries=[(MATERIAL, 120000)])
    session.commit()
    version_id = version.version_id

    plan_service.delete_plan(session, users["ceo"], plan_id=plan.plan_id)
    session.commit()

    assert session.execute(
        select(func.count(TargetVersion.version_id))
        .where(TargetVersion.version_id == version_id)).scalar() == 0
    assert session.execute(
        select(func.count(TargetCountryLine.line_id))
        .where(TargetCountryLine.version_id == version_id)).scalar() == 0
    session.close()


def test_the_audit_trail_outlives_the_plan(with_history, planned,
                                           users) -> None:
    """Only the link goes. The actor, the action and the reason stay."""
    session, plan, _ = _open(with_history, planned)
    plan_code = plan.plan_code

    plan_service.delete_plan(session, users["ceo"], plan_id=plan.plan_id)
    session.commit()

    entry = session.execute(
        select(TargetAudit).where(TargetAudit.action == "PLAN_DELETED")
    ).scalar_one()
    assert entry.node_label == plan_code
    assert entry.actor == users["ceo"].username
    assert entry.plan_id is None          # SET NULL, not a dangling row
    assert "draft" in (entry.reason or "").lower()

    # The plan's earlier entries survive too — creating it is still on record.
    created = session.execute(
        select(func.count(TargetAudit.audit_id))
        .where(TargetAudit.action == "PLAN_CREATED")).scalar()
    assert created >= 1
    session.close()


# ---------------------------------------------------------------------------
# What is refused, and why
# ---------------------------------------------------------------------------


def test_an_allocated_plan_is_refused(with_history, planned, users) -> None:
    session, plan, version = _open(with_history, planned)
    allocation_engine.persist(session, version=version,
                              result=_run(session, users, planned))
    session.commit()

    reasons = plan_service.deletion_blockers(session, plan)
    assert any("allocated rows" in reason for reason in reasons)
    with pytest.raises(PlanNotDeletable):
        plan_service.delete_plan(session, users["ceo"], plan_id=plan.plan_id)
    assert _plan_rows(session, plan.plan_id) == 1
    session.close()


def test_a_submitted_plan_is_refused(with_history, planned, users) -> None:
    session, plan, version = _open(with_history, planned)
    for step in (TargetStatus.ALLOCATION_IN_PROGRESS, TargetStatus.ALLOCATED,
                 TargetStatus.UNDER_REVIEW):
        plan_service.set_version_status(session, users["ceo"],
                                        version_id=version.version_id,
                                        new_status=step)
    session.commit()

    reasons = plan_service.deletion_blockers(session, plan)
    assert reasons
    with pytest.raises(PlanNotDeletable):
        plan_service.delete_plan(session, users["ceo"], plan_id=plan.plan_id)
    session.close()


def test_a_plan_carrying_an_approval_is_refused(with_history, planned,
                                                users) -> None:
    """An approval log is not something a delete may take with it."""
    session, plan, version = _open(with_history, planned)
    session.add(TargetApproval(
        version_id=version.version_id, action=ApprovalAction.SUBMITTED,
        actor="ceo", actor_role=Role.MANAGEMENT))
    session.commit()

    reasons = plan_service.deletion_blockers(session, plan)
    assert any("approval act" in reason for reason in reasons)
    with pytest.raises(PlanNotDeletable):
        plan_service.delete_plan(session, users["ceo"], plan_id=plan.plan_id)
    session.close()


def test_a_locked_plan_is_refused(with_history, planned, users) -> None:
    """Its figures are in fact_target and are what every report reads."""
    session, plan, version = _open(with_history, planned)
    version.locked_batch_id = "batch-0001"
    session.commit()

    reasons = plan_service.deletion_blockers(session, plan)
    assert any("fact_target" in reason for reason in reasons)
    with pytest.raises(PlanNotDeletable):
        plan_service.delete_plan(session, users["ceo"], plan_id=plan.plan_id)
    session.close()


def test_every_reason_is_reported_rather_than_the_first(with_history, planned,
                                                        users) -> None:
    """They are fixed in different places, so one merged sentence would send a
    reader to the wrong one."""
    session, plan, version = _open(with_history, planned)
    allocation_engine.persist(session, version=version,
                              result=_run(session, users, planned))
    version.locked_batch_id = "batch-0001"
    session.add(TargetApproval(
        version_id=version.version_id, action=ApprovalAction.SUBMITTED,
        actor="ceo", actor_role=Role.MANAGEMENT))
    session.commit()

    reasons = plan_service.deletion_blockers(session, plan)
    assert len(reasons) >= 3
    session.close()


def test_deleting_frees_the_scope_for_a_new_plan(with_history, planned,
                                                 users) -> None:
    """No uniqueness is left behind holding the scope hostage.

    A soft cancel would have kept the plan's (year, period, company, business
    unit, sales line) key, so the same scope could never be planned again. A
    delete frees it, which is the whole reason this is a delete.
    """
    session, plan, _ = _open(with_history, planned)
    scope = plan_service.PlanScope(
        financial_year=plan.financial_year, target_period=plan.target_period,
        company_code=plan.company_code, bu_code=plan.bu_code,
        sales_line_code=plan.sales_line_code)

    plan_service.delete_plan(session, users["ceo"], plan_id=plan.plan_id)
    session.commit()

    replacement = plan_service.create_plan(session, users["ceo"], scope=scope)
    session.commit()
    assert replacement.status == TargetStatus.DRAFT
    # The code is *not* reused. ``plan_code`` is what an audit entry names, and
    # the trail survives the delete — handing the number on would leave one
    # label pointing at two different plans.
    assert replacement.plan_code != plan.plan_code

    # SQLite reuses a row id once the highest row is gone, so the replacement
    # can legitimately land on the deleted plan's id. That is exactly why the
    # audit trail holds a plan by a **SET NULL** foreign key: the entries the
    # old plan wrote are detached at the delete and cannot be silently adopted
    # by whatever occupies that id next.
    orphaned = session.execute(
        select(TargetAudit).where(TargetAudit.action == "PLAN_DELETED")
    ).scalar_one()
    assert orphaned.plan_id is None
    assert orphaned.node_label == plan.plan_code
    session.close()


def test_a_missing_plan_is_not_found_rather_than_deleted(with_history,
                                                         users) -> None:
    from app.targetmgmt.errors import PlanNotFound

    with Session(with_history) as session:
        with pytest.raises(PlanNotFound):
            plan_service.delete_plan(session, users["ceo"], plan_id=999_999)
