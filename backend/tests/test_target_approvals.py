"""Revising a figure, and signing a target off.

The three promises this step makes, pinned here.

**A revision is a request, and asking is not changing.** Nothing moves until an
approver decides; the request keeps the system figure, the requested figure and
the approved figure apart, and its adjustment limit *routes* it rather than
refusing it.

**An approved revision is funded by the node's siblings.** The country target
does not move, every level still reconciles exactly, and the node's own subtree
is re-split in proportion to what it held — so a decision changes one branch
without quietly rewriting the plan around it.

**The chain runs bottom-up and in order.** A step approves only once the steps
beneath it have; deciding a revision is not signing off on the version; and the
last step is gated on reconciliation, open revisions and the chain itself, each
reported separately because each is fixed somewhere else.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.permission_filter import UserContext
from app.database.models_ai import Role
from app.database.models_target import (
    ApprovalAction,
    RevisionStatus,
    TargetAdjustment,
    TargetAllocation,
    TargetApproval,
    TargetApprovalMatrix,
    TargetAudit,
    TargetLevel,
    TargetStatus,
)
from app.targetmgmt import (
    approvals,
    country,
    engine as allocation_engine,
    matrix,
    plans as plan_service,
    reconcile,
    revisions,
)
from app.targetmgmt.errors import ReasonRequired
from test_target_allocation import (  # noqa: F401  (fixtures reused wholesale)
    CUSTOMERS,
    MATERIAL,
    SCOPE,
    _run,
    _seed_sales,
    _worker_engine,
    hierarchy,
    planned,
    with_history,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def chain_users():
    """One user per step of the default chain, each scoped to their own branch.

    Every restricted role carries a real data scope, because a restricted role
    with none at all can see nothing — the platform rule this module respects
    rather than routes around. Management is unrestricted, which is what
    ``Role.UNRESTRICTED`` means and why it alone is given no scope.
    """
    return {
        Role.MANAGEMENT: UserContext(user_id=1, username="ceo",
                                     role=Role.MANAGEMENT),
        Role.BUSINESS_UNIT_HEAD: UserContext(
            user_id=2, username="buh", role=Role.BUSINESS_UNIT_HEAD,
            data_scope={"company_code": ["C001"]}),
        Role.ZONE_MANAGER: UserContext(
            user_id=3, username="zm", role=Role.ZONE_MANAGER,
            data_scope={"zone_code": ["Z001"]}),
        Role.REGIONAL_MANAGER: UserContext(
            user_id=4, username="rm", role=Role.REGIONAL_MANAGER,
            data_scope={"region_code": ["REG001"]}),
        Role.AREA_MANAGER: UserContext(
            user_id=5, username="am", role=Role.AREA_MANAGER,
            data_scope={"area_code": ["AR001"]}),
        Role.UNIT_MANAGER: UserContext(
            user_id=6, username="um", role=Role.UNIT_MANAGER,
            data_scope={"unit_code": ["UN001"]}),
        Role.SALES_OFFICER: UserContext(
            user_id=7, username="so", role=Role.SALES_OFFICER,
            data_scope={"sub_territory_code": ["STR001"]}),
        Role.ADMIN: UserContext(user_id=8, username="admin", role=Role.ADMIN),
    }


@pytest.fixture
def submitted(with_history, users, planned, chain_users):
    """An allocated version that has been put in front of the chain."""
    with Session(with_history) as session:
        version = plan_service.get_version(session, planned["version_id"])
        plan = plan_service.get_plan(session, planned["plan_id"])
        result = _run(session, users, planned)
        allocation_engine.persist(session, version=version, result=result)
        for step in (TargetStatus.ALLOCATION_IN_PROGRESS, TargetStatus.ALLOCATED):
            plan_service.set_version_status(
                session, users["ceo"], version_id=version.version_id,
                new_status=step)
        approvals.submit(session, users["ceo"], plan=plan, version=version)
        session.commit()
    return planned


def _open(engine, planned):
    session = Session(engine)
    version = plan_service.get_version(session, planned["version_id"])
    plan = plan_service.get_plan(session, planned["plan_id"])
    return session, plan, version


def _totals(session, version_id) -> dict[tuple[str, str], Decimal]:
    rows = session.execute(
        select(TargetAllocation.level, TargetAllocation.node_code,
               func.sum(TargetAllocation.current_volume))
        .where(TargetAllocation.version_id == version_id)
        .group_by(TargetAllocation.level, TargetAllocation.node_code)
    ).all()
    return {(level, code): Decimal(str(total)) for level, code, total in rows}


def _raise(session, plan, version, user, **kwargs):
    defaults = dict(level=TargetLevel.CUSTOMER, node_code="CUST-001",
                    requested_volume="20000", reason="Two new outlets opened.")
    defaults.update(kwargs)
    return revisions.request(session, user, plan=plan, version=version,
                             **defaults)


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


def test_the_chain_runs_bottom_up(with_history) -> None:
    """Sequence 1 is the sub-territory, not Management.

    A target is allocated downwards and reviewed *upwards*: the person who has
    to hit the number sees it first, and the CEO signs last. Revision 0027
    seeded this the other way round and 0030 corrects it.
    """
    with Session(with_history) as session:
        chain = matrix.chain(session)
    assert [row.role for row in chain] == [
        Role.SALES_OFFICER, Role.TERRITORY_MANAGER, Role.UNIT_MANAGER,
        Role.AREA_MANAGER, Role.REGIONAL_MANAGER, Role.ZONE_MANAGER,
        Role.BUSINESS_UNIT_HEAD, Role.MANAGEMENT,
    ]


def test_administrators_sit_outside_the_chain(with_history) -> None:
    """NULL is outside it, never step zero at the head of it."""
    with Session(with_history) as session:
        rows = matrix.load(session)
        assert rows[Role.ADMIN].approval_sequence is None
        assert rows[Role.ADMIN].in_chain is False
        assert Role.ADMIN not in {row.role for row in matrix.chain(session)}


def test_a_step_can_sit_in_the_chain_without_approving(with_history) -> None:
    """A target is *questioned* at the sales officer's level, not signed there."""
    with Session(with_history) as session:
        officer = matrix.for_role(session, Role.SALES_OFFICER)
        assert officer.in_chain is True
        assert officer.can_approve is False
        assert officer.can_revise is True
        assert Role.SALES_OFFICER not in {
            row.role for row in matrix.approvers(session)}


def test_unlimited_and_zero_are_different_settings(with_history) -> None:
    """NULL is unlimited; 0 is "may not change a figure at all"."""
    with Session(with_history) as session:
        rows = matrix.load(session)
    assert rows[Role.MANAGEMENT].adjustment_limit_percent is None
    assert rows[Role.MANAGEMENT].to_dict()["limit_label"] == "Unlimited"
    assert rows[Role.SALES_OFFICER].adjustment_limit_percent == 0.0
    assert rows[Role.SALES_OFFICER].to_dict()["limit_label"] == "±0%"


def test_a_role_approves_at_its_level_and_below(with_history) -> None:
    """A region's figure *is* the sum of the areas inside it.

    Refusing the regional manager the area beneath would make the region
    unapprovable by the only person who can actually see it.
    """
    with Session(with_history) as session:
        rm = matrix.for_role(session, Role.REGIONAL_MANAGER)
    assert matrix.may_act_on(rm, TargetLevel.REGION) is True
    assert matrix.may_act_on(rm, TargetLevel.CUSTOMER) is True
    assert matrix.may_act_on(rm, TargetLevel.ZONE) is False


def test_escalation_goes_to_the_lowest_step_that_covers_it(with_history) -> None:
    """Not to the top. A 12% change is the zone manager's, not the CEO's."""
    with Session(with_history) as session:
        officer = matrix.for_role(session, Role.SALES_OFFICER)
        assert matrix.escalation_target(session, officer, 4.0) == Role.UNIT_MANAGER
        assert matrix.escalation_target(session, officer, 12.0) == Role.ZONE_MANAGER
        assert matrix.escalation_target(session, officer, 400.0) == Role.MANAGEMENT


def test_the_matrix_refuses_two_roles_at_one_step(with_history, users) -> None:
    """"Who signs next?" must have one answer."""
    with Session(with_history) as session:
        with pytest.raises(matrix.DuplicateMatrixSequence):
            matrix.update(session, users["ceo"], [
                {"role": Role.ZONE_MANAGER, "approval_sequence": 5},
            ])


def test_the_matrix_refuses_a_level_the_allocator_cannot_reach(
        with_history, users) -> None:
    """A stale name in configuration routes a target to a node that cannot exist."""
    with Session(with_history) as session:
        with pytest.raises(matrix.UnknownMatrixLevel):
            matrix.update(session, users["ceo"], [
                {"role": Role.ZONE_MANAGER, "hierarchy_level": "warehouse"},
            ])


def test_a_role_absent_from_the_payload_is_left_alone(with_history,
                                                      users) -> None:
    """Omission is not deletion.

    A half-loaded form must not be able to empty the workflow, and an approval
    chain with no rows is not a blank slate — it is one nobody can approve in.
    """
    with Session(with_history) as session:
        before = len(matrix.load(session))
        matrix.update(session, users["ceo"],
                      [{"role": Role.ZONE_MANAGER, "is_active": False}])
        session.commit()
        after = matrix.load(session)
    assert len(after) == before
    assert after[Role.ZONE_MANAGER].is_active is False
    assert after[Role.REGIONAL_MANAGER].is_active is True


def test_deactivating_removes_a_role_from_the_chain_reversibly(with_history,
                                                               users) -> None:
    with Session(with_history) as session:
        matrix.update(session, users["ceo"],
                      [{"role": Role.ZONE_MANAGER, "is_active": False}])
        session.commit()
        assert Role.ZONE_MANAGER not in {r.role for r in matrix.chain(session)}
        matrix.update(session, users["ceo"],
                      [{"role": Role.ZONE_MANAGER, "is_active": True}])
        session.commit()
        assert Role.ZONE_MANAGER in {r.role for r in matrix.chain(session)}


def test_editing_the_matrix_is_audited(with_history, users) -> None:
    """Configuration is a decision, and the trail says what it was before."""
    with Session(with_history) as session:
        matrix.update(session, users["ceo"], [
            {"role": Role.AREA_MANAGER, "adjustment_limit_percent": 25.0,
             "reason": "Wider discretion agreed for FY27."},
        ])
        session.commit()
        entry = session.execute(
            select(TargetAudit).where(TargetAudit.action == "MATRIX_UPDATED")
        ).scalars().all()[-1]
    assert entry.node_label == Role.AREA_MANAGER
    assert "±10%" in entry.old_value
    assert "±25%" in entry.new_value
    assert entry.reason == "Wider discretion agreed for FY27."


# ---------------------------------------------------------------------------
# Raising a revision
# ---------------------------------------------------------------------------


def test_a_revision_keeps_three_volumes_apart(with_history, submitted,
                                              chain_users) -> None:
    """"What the engine said" and "what we agreed" are different questions."""
    session, plan, version = _open(with_history, submitted)
    revision = _raise(session, plan, version, chain_users[Role.SALES_OFFICER])
    session.commit()
    assert revision.system_volume > 0
    assert float(revision.requested_volume) == 20000.0
    assert revision.approved_volume is None
    session.close()


def test_a_revision_without_a_reason_is_refused(with_history, submitted,
                                                chain_users) -> None:
    session, plan, version = _open(with_history, submitted)
    with pytest.raises(ReasonRequired):
        _raise(session, plan, version, chain_users[Role.SALES_OFFICER],
               reason="   ")
    session.close()


def test_an_unreadable_volume_is_refused_by_name(with_history, submitted,
                                                 chain_users) -> None:
    """``12,5OO`` is not 125. The no-invented-data invariant, applied to one cell."""
    session, plan, version = _open(with_history, submitted)
    with pytest.raises(revisions.InvalidRevisionVolume) as caught:
        _raise(session, plan, version, chain_users[Role.SALES_OFFICER],
               requested_volume="12,5OO")
    assert "not a number" in caught.value.user_message
    session.close()


def test_a_negative_volume_takes_its_own_message(with_history, submitted,
                                                 chain_users) -> None:
    """"Unreadable" and "negative" send a reader to different places."""
    session, plan, version = _open(with_history, submitted)
    with pytest.raises(revisions.InvalidRevisionVolume) as caught:
        _raise(session, plan, version, chain_users[Role.SALES_OFFICER],
               requested_volume="-500")
    assert "cannot be negative" in caught.value.user_message
    session.close()


def test_a_thousands_separator_is_read(with_history, submitted,
                                       chain_users) -> None:
    session, plan, version = _open(with_history, submitted)
    revision = _raise(session, plan, version, chain_users[Role.SALES_OFFICER],
                      requested_volume="20,000")
    assert float(revision.requested_volume) == 20000.0
    session.close()


def test_a_change_beyond_the_limit_escalates_rather_than_refusing(
        with_history, submitted, chain_users) -> None:
    """The limit routes the request. Escalation is a status, not a flag.

    Losing the distinction would make the adjustment limit unauditable — nobody
    could tell afterwards whether a request went the ordinary way or was routed
    past somebody.
    """
    session, plan, version = _open(with_history, submitted)
    revision = _raise(session, plan, version, chain_users[Role.SALES_OFFICER])
    assert revision.status == RevisionStatus.ESCALATED
    assert revision.escalated_to_role == Role.MANAGEMENT
    assert revision.change_percent > 0
    session.close()


def test_a_change_inside_the_limit_is_an_ordinary_request(
        with_history, submitted, chain_users) -> None:
    session, plan, version = _open(with_history, submitted)
    system = revisions.node_volume(session, version_id=version.version_id,
                                   level=TargetLevel.REGION, node_code="REG001")
    revision = _raise(session, plan, version, chain_users[Role.REGIONAL_MANAGER],
                      level=TargetLevel.REGION, node_code="REG001",
                      requested_volume=str(float(system) * 1.05))
    assert revision.status == RevisionStatus.PENDING
    assert revision.escalated_to_role is None
    session.close()


def test_a_node_outside_the_callers_scope_is_refused(with_history, submitted,
                                                     chain_users) -> None:
    """A sub-territory officer does not revise the whole region above them."""
    session, plan, version = _open(with_history, submitted)
    with pytest.raises(revisions.RevisionOutOfScope):
        _raise(session, plan, version, chain_users[Role.SALES_OFFICER],
               level=TargetLevel.REGION, node_code="REG001")
    session.close()


def test_a_node_the_allocation_does_not_contain_is_refused(
        with_history, submitted, chain_users) -> None:
    session, plan, version = _open(with_history, submitted)
    with pytest.raises(revisions.NodeNotAllocated):
        _raise(session, plan, version, chain_users[Role.MANAGEMENT],
               node_code="CUST-NOPE")
    session.close()


def test_a_frozen_version_takes_no_revision(with_history, users, planned,
                                            chain_users) -> None:
    """An approved target is changed by creating the next version."""
    session, plan, version = _open(with_history, planned)
    with pytest.raises(revisions.NotRevisable):
        _raise(session, plan, version, chain_users[Role.MANAGEMENT])
    session.close()


def test_raising_a_revision_is_audited(with_history, submitted,
                                       chain_users) -> None:
    session, plan, version = _open(with_history, submitted)
    _raise(session, plan, version, chain_users[Role.SALES_OFFICER])
    session.commit()
    entry = session.execute(
        select(TargetAudit).where(TargetAudit.action == "REVISION_REQUESTED")
    ).scalars().one()
    assert "CUST-001" in entry.node_label
    assert entry.reason == "Two new outlets opened."
    session.close()


# ---------------------------------------------------------------------------
# Deciding one
# ---------------------------------------------------------------------------


def test_an_approved_revision_is_funded_by_the_siblings(with_history, submitted,
                                                        chain_users) -> None:
    """Whatever the node gains, its siblings give up. The parent does not move.

    This is the identity the whole module rests on: the country target is what
    somebody typed, and no decision below it may change that figure.
    """
    session, plan, version = _open(with_history, submitted)
    before = _totals(session, version.version_id)
    revision = _raise(session, plan, version, chain_users[Role.SALES_OFFICER])
    session.commit()

    revisions.decide(session, chain_users[Role.MANAGEMENT], plan=plan,
                     version=version, revision_id=revision.revision_id,
                     approve=True)
    session.commit()
    after = _totals(session, version.version_id)

    assert after[(TargetLevel.CUSTOMER, "CUST-001")] == Decimal("20000")
    assert after[(TargetLevel.COMPANY, "C001")] == \
        before[(TargetLevel.COMPANY, "C001")]
    assert after[(TargetLevel.SUB_TERRITORY, "STR001")] == \
        before[(TargetLevel.SUB_TERRITORY, "STR001")]
    session.close()


def test_the_tree_still_reconciles_after_a_revision(with_history, submitted,
                                                    chain_users) -> None:
    """Exactly, with zero tolerance — the same check a run has to pass."""
    session, plan, version = _open(with_history, submitted)
    revision = _raise(session, plan, version, chain_users[Role.SALES_OFFICER])
    session.commit()
    revisions.decide(session, chain_users[Role.MANAGEMENT], plan=plan,
                     version=version, revision_id=revision.revision_id,
                     approve=True)
    session.commit()

    lines = country.list_lines(session, version.version_id)
    verdict = reconcile.check(
        session, version_id=version.version_id,
        country_target={line.material_code: Decimal(str(line.target_volume))
                        for line in lines if line.target_volume})
    assert verdict.balanced is True
    assert verdict.mismatches == []
    session.close()


def test_an_approver_may_grant_a_different_figure(with_history, submitted,
                                                  chain_users) -> None:
    """Granting half of what was asked is a real outcome and is recorded as one."""
    session, plan, version = _open(with_history, submitted)
    revision = _raise(session, plan, version, chain_users[Role.SALES_OFFICER])
    session.commit()
    revisions.decide(session, chain_users[Role.MANAGEMENT], plan=plan,
                     version=version, revision_id=revision.revision_id,
                     approve=True, approved_volume="17000")
    session.commit()

    assert float(revision.approved_volume) == 17000.0
    assert float(revision.requested_volume) == 20000.0
    totals = _totals(session, version.version_id)
    assert totals[(TargetLevel.CUSTOMER, "CUST-001")] == Decimal("17000")
    session.close()


def test_a_decision_beyond_the_deciders_own_limit_is_refused(
        with_history, submitted, chain_users) -> None:
    """Checked on what they *granted*, which is what the limit is about."""
    session, plan, version = _open(with_history, submitted)
    revision = _raise(session, plan, version, chain_users[Role.MANAGEMENT])
    session.commit()
    with pytest.raises(revisions.RevisionExceedsLimit):
        revisions.decide(session, chain_users[Role.UNIT_MANAGER], plan=plan,
                         version=version, revision_id=revision.revision_id,
                         approve=True)
    session.close()


def test_a_rejected_revision_moves_nothing(with_history, submitted,
                                           chain_users) -> None:
    session, plan, version = _open(with_history, submitted)
    before = _totals(session, version.version_id)
    revision = _raise(session, plan, version, chain_users[Role.SALES_OFFICER])
    session.commit()
    revisions.decide(session, chain_users[Role.MANAGEMENT], plan=plan,
                     version=version, revision_id=revision.revision_id,
                     approve=False, comment="Not this year.")
    session.commit()

    assert revision.status == RevisionStatus.REJECTED
    assert revision.approved_volume is None
    assert _totals(session, version.version_id) == before
    session.close()


def test_a_decided_revision_cannot_be_decided_again(with_history, submitted,
                                                    chain_users) -> None:
    """A second decision is a new request, not an edit of the first."""
    session, plan, version = _open(with_history, submitted)
    revision = _raise(session, plan, version, chain_users[Role.SALES_OFFICER])
    session.commit()
    revisions.decide(session, chain_users[Role.MANAGEMENT], plan=plan,
                     version=version, revision_id=revision.revision_id,
                     approve=True)
    session.commit()
    with pytest.raises(revisions.RevisionClosed):
        revisions.decide(session, chain_users[Role.MANAGEMENT], plan=plan,
                         version=version, revision_id=revision.revision_id,
                         approve=False)
    session.close()


def test_an_approved_revision_survives_a_re_run(with_history, submitted,
                                                chain_users) -> None:
    """Kept as a standing adjustment, so re-allocating does not discard it.

    The same failure ``target_adjustment`` was introduced to prevent for
    management adjustments — which is why a revision reuses that mechanism
    rather than inventing a second one.
    """
    session, plan, version = _open(with_history, submitted)
    revision = _raise(session, plan, version, chain_users[Role.SALES_OFFICER])
    session.commit()
    revisions.decide(session, chain_users[Role.MANAGEMENT], plan=plan,
                     version=version, revision_id=revision.revision_id,
                     approve=True)
    session.commit()

    standing = session.execute(
        select(TargetAdjustment).where(
            TargetAdjustment.version_id == version.version_id)
    ).scalars().all()
    assert len(standing) == 1
    assert standing[0].node_code == "CUST-001"
    assert float(standing[0].adjustment_volume) > 0
    assert f"Revision {revision.revision_id}" in standing[0].reason
    session.close()


def test_deciding_a_revision_is_not_signing_off_the_version(
        with_history, submitted, chain_users) -> None:
    """The distinction the approval table was built with: a node act names a node.

    An approver who decides a request and is then told they have already
    approved the version would have had their signature taken without giving it.
    """
    session, plan, version = _open(with_history, submitted)
    revision = _raise(session, plan, version, chain_users[Role.SALES_OFFICER])
    session.commit()
    revisions.decide(session, chain_users[Role.MANAGEMENT], plan=plan,
                     version=version, revision_id=revision.revision_id,
                     approve=True)
    session.commit()

    state = approvals.state(session, chain_users[Role.MANAGEMENT],
                            version_id=version.version_id)
    assert state["already_acted"] is None
    assert all(not step["approved"] for step in state["steps"])
    session.close()


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


def _approve_up_to(session, plan, version, chain_users, role):
    order = [Role.UNIT_MANAGER, Role.AREA_MANAGER, Role.REGIONAL_MANAGER,
             Role.ZONE_MANAGER, Role.BUSINESS_UNIT_HEAD, Role.MANAGEMENT]
    outcome = None
    for step in order:
        outcome = approvals.approve(session, chain_users[step], plan=plan,
                                    version=version)
        session.commit()
        if step == role:
            break
    return outcome


def test_only_an_allocated_version_can_be_submitted(with_history, users,
                                                    planned) -> None:
    session, plan, version = _open(with_history, planned)
    with pytest.raises(approvals.NotSubmittable):
        approvals.submit(session, users["ceo"], plan=plan, version=version)
    session.close()


def test_approving_out_of_turn_names_who_it_is_with(with_history, submitted,
                                                    chain_users) -> None:
    """"Waiting on the Area Manager" is actionable; a generic refusal is not."""
    session, plan, version = _open(with_history, submitted)
    with pytest.raises(approvals.OutOfTurn) as caught:
        approvals.approve(session, chain_users[Role.MANAGEMENT], plan=plan,
                          version=version)
    assert "Unit Manager" in caught.value.user_message
    assert caught.value.details["waiting_on"][0] == Role.UNIT_MANAGER
    session.close()


def test_a_role_outside_the_chain_approves_nothing(with_history, submitted,
                                                   chain_users) -> None:
    """An administrator legitimately holds the section and signs nothing."""
    session, plan, version = _open(with_history, submitted)
    with pytest.raises(matrix.NotInApprovalChain):
        approvals.approve(session, chain_users[Role.ADMIN], plan=plan,
                          version=version)
    session.close()


def test_a_partly_signed_version_is_partially_approved(with_history, submitted,
                                                       chain_users) -> None:
    """A real state describing a target part-way up the chain, not a near-miss."""
    session, plan, version = _open(with_history, submitted)
    outcome = _approve_up_to(session, plan, version, chain_users,
                             Role.REGIONAL_MANAGER)
    assert outcome["status"] == TargetStatus.PARTIALLY_APPROVED
    assert "Zone Manager" in outcome["blockers"][0]
    session.close()


def test_the_last_signature_approves_the_version(with_history, submitted,
                                                 chain_users) -> None:
    session, plan, version = _open(with_history, submitted)
    outcome = _approve_up_to(session, plan, version, chain_users,
                             Role.MANAGEMENT)
    assert outcome["status"] == TargetStatus.APPROVED
    assert outcome["blockers"] == []
    assert plan_service.get_version(
        session, version.version_id).status == TargetStatus.APPROVED
    session.close()


def test_the_same_role_cannot_approve_twice(with_history, submitted,
                                            chain_users) -> None:
    session, plan, version = _open(with_history, submitted)
    approvals.approve(session, chain_users[Role.UNIT_MANAGER], plan=plan,
                      version=version)
    session.commit()
    with pytest.raises(approvals.AlreadyActed):
        approvals.approve(session, chain_users[Role.UNIT_MANAGER], plan=plan,
                          version=version)
    session.close()


def test_an_open_revision_blocks_final_approval(with_history, submitted,
                                                chain_users) -> None:
    """Reported as its own sentence, because it is fixed somewhere else."""
    session, plan, version = _open(with_history, submitted)
    _raise(session, plan, version, chain_users[Role.SALES_OFFICER])
    session.commit()

    with pytest.raises(approvals.ApprovalBlocked) as caught:
        _approve_up_to(session, plan, version, chain_users, Role.MANAGEMENT)
    assert any("revision request" in reason
               for reason in caught.value.details["reasons"])
    session.close()


def test_an_unbalanced_allocation_blocks_final_approval(with_history, submitted,
                                                        chain_users) -> None:
    """The check has to be capable of failing, or it proves nothing."""
    session, plan, version = _open(with_history, submitted)
    row = session.execute(
        select(TargetAllocation).where(
            TargetAllocation.version_id == version.version_id,
            TargetAllocation.level == TargetLevel.REGION).limit(1)
    ).scalar_one()
    row.current_volume = Decimal(str(row.current_volume)) + Decimal("250")
    session.commit()

    with pytest.raises(approvals.ApprovalBlocked) as caught:
        _approve_up_to(session, plan, version, chain_users, Role.MANAGEMENT)
    assert any("balance" in reason for reason in caught.value.details["reasons"])
    session.close()


def test_a_rejection_needs_a_reason(with_history, submitted,
                                    chain_users) -> None:
    """A rejection with none says the target is wrong and nothing about what to fix."""
    session, plan, version = _open(with_history, submitted)
    with pytest.raises(ReasonRequired):
        approvals.reject(session, chain_users[Role.REGIONAL_MANAGER], plan=plan,
                         version=version, comment="  ")
    session.close()


def test_a_rejection_sends_the_version_back_to_its_author(
        with_history, submitted, chain_users) -> None:
    session, plan, version = _open(with_history, submitted)
    outcome = approvals.reject(session, chain_users[Role.REGIONAL_MANAGER],
                               plan=plan, version=version,
                               comment="Dhaka's growth assumption is too high.")
    session.commit()
    assert outcome["status"] == TargetStatus.REJECTED
    assert plan_service.get_version(
        session, version.version_id).status == TargetStatus.REJECTED
    session.close()


def test_an_approved_version_can_be_sent_back_before_it_is_locked(
        with_history, submitted, chain_users) -> None:
    """Approval is a judgement, not an irreversible act. Locking is the latter."""
    session, plan, version = _open(with_history, submitted)
    _approve_up_to(session, plan, version, chain_users, Role.MANAGEMENT)
    outcome = approvals.send_back(session, chain_users[Role.MANAGEMENT],
                                  plan=plan, version=version,
                                  comment="One region needs re-checking.")
    session.commit()
    assert outcome["status"] == TargetStatus.UNDER_REVIEW
    session.close()


def test_every_act_is_recorded_and_nothing_is_updated(with_history, submitted,
                                                      chain_users) -> None:
    """Append-only: an approver who changes their mind adds a row.

    Both rows stay, which is the only way the sequence a target actually
    travelled can be read back.
    """
    session, plan, version = _open(with_history, submitted)
    _approve_up_to(session, plan, version, chain_users, Role.MANAGEMENT)
    approvals.send_back(session, chain_users[Role.MANAGEMENT], plan=plan,
                        version=version, comment="Re-checking Khulna.")
    session.commit()

    log = approvals.history(session, version.version_id)
    actions = [act["action"] for act in log]
    assert actions[0] == ApprovalAction.SUBMITTED
    assert actions.count(ApprovalAction.APPROVED) == 6
    assert actions[-1] == ApprovalAction.REVISION_REQUESTED
    session.close()


def test_an_approval_records_the_sequence_held_at_the_time(
        with_history, submitted, chain_users) -> None:
    """A matrix reordered next year must not rewrite last year's order."""
    session, plan, version = _open(with_history, submitted)
    approvals.approve(session, chain_users[Role.UNIT_MANAGER], plan=plan,
                      version=version)
    session.commit()
    act = session.execute(
        select(TargetApproval).where(
            TargetApproval.actor_role == Role.UNIT_MANAGER)
    ).scalar_one()
    assert act.approval_sequence == 3

    session.execute(
        TargetApprovalMatrix.__table__.update()
        .where(TargetApprovalMatrix.role == Role.UNIT_MANAGER)
        .values(approval_sequence=9))
    session.commit()
    session.refresh(act)
    assert act.approval_sequence == 3
    session.close()


# ---------------------------------------------------------------------------
# My Approvals
# ---------------------------------------------------------------------------


def test_the_queue_shows_a_version_and_says_whose_turn_it_is(
        with_history, submitted, chain_users) -> None:
    """Shown even before it is yours — a target that vanished would be worse."""
    session, plan, version = _open(with_history, submitted)
    queue = approvals.queue(session, chain_users[Role.MANAGEMENT])
    assert len(queue["versions"]) == 1
    assert queue["versions"][0]["is_my_turn"] is False
    assert Role.UNIT_MANAGER in queue["versions"][0]["waiting_on"]
    session.close()


def test_the_queue_marks_it_when_the_turn_arrives(with_history, submitted,
                                                  chain_users) -> None:
    session, plan, version = _open(with_history, submitted)
    queue = approvals.queue(session, chain_users[Role.UNIT_MANAGER])
    assert queue["versions"][0]["is_my_turn"] is True
    session.close()


def test_an_approved_version_leaves_the_queue(with_history, submitted,
                                              chain_users) -> None:
    session, plan, version = _open(with_history, submitted)
    approvals.approve(session, chain_users[Role.UNIT_MANAGER], plan=plan,
                      version=version)
    session.commit()
    queue = approvals.queue(session, chain_users[Role.UNIT_MANAGER])
    assert queue["versions"] == []
    session.close()


def test_an_escalated_request_goes_to_the_named_role_and_nobody_else(
        with_history, submitted, chain_users) -> None:
    """That is what escalation *is*. Offering it elsewhere would undo the routing."""
    session, plan, version = _open(with_history, submitted)
    _raise(session, plan, version, chain_users[Role.SALES_OFFICER])
    session.commit()

    assert len(approvals.queue(session, chain_users[Role.MANAGEMENT])
               ["revisions"]) == 1
    assert approvals.queue(session, chain_users[Role.REGIONAL_MANAGER])[
        "revisions"] == []
    session.close()


def test_an_ordinary_request_reaches_any_approver_at_or_above_the_node(
        with_history, submitted, chain_users) -> None:
    session, plan, version = _open(with_history, submitted)
    system = revisions.node_volume(session, version_id=version.version_id,
                                   level=TargetLevel.REGION, node_code="REG001")
    _raise(session, plan, version, chain_users[Role.REGIONAL_MANAGER],
           level=TargetLevel.REGION, node_code="REG001",
           requested_volume=str(float(system) * 1.05))
    session.commit()

    assert len(approvals.queue(session, chain_users[Role.REGIONAL_MANAGER])
               ["revisions"]) == 1
    assert len(approvals.queue(session, chain_users[Role.ZONE_MANAGER])
               ["revisions"]) == 1
    session.close()


def test_a_role_outside_the_chain_is_told_why_its_queue_is_empty(
        with_history, submitted, chain_users) -> None:
    """An empty list with no explanation would read as a fault."""
    session, plan, version = _open(with_history, submitted)
    queue = approvals.queue(session, chain_users[Role.ADMIN])
    assert queue["versions"] == []
    assert queue["revisions"] == []
    assert "not part of the approval chain" in queue["notes"][0]
    assert queue["matrix"] is None
    session.close()
