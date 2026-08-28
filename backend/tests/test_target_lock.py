"""Locking a target, and the trail that records every decision behind it.

The lock is the one act in this module that reaches outside it: from the instant
it lands, ``fact_target`` — and therefore the Target page, every export and the
AI assistant — is reading these figures. So the promises pinned here are about
what gets written and what refuses to be.

**Only terminal nodes are written, and they sum to the country target exactly.**
Writing every level would multiply the target by the depth of the tree; that the
total comes back equal to what somebody typed is the whole point of
reconciliation being a gate.

**Five conditions, each reported by name.** Approved; balanced; no open
revision; every material carrying a conversion factor and a transfer price; every
branch reaching territory. They are fixed in five different places, so they are
never merged into one message.

**A re-locked plan restates its own rows and voids what it dropped.** The
business key is the target's grain, so a later version updates in place — one
authoritative number — and a grain it no longer states is voided rather than
deleted, which removes it from every report without removing it from the record.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models import DimMaterial, DimTerritory
from app.database.models_ai import Role
from app.database.models_target import (
    ApprovalAction,
    RevisionStatus,
    TargetRevision,
    TargetAllocation,
    TargetApproval,
    TargetAudit,
    TargetLevel,
    TargetStatus,
    TargetVersion,
)
from app.database.models_warehouse import DimDate, EtlImportBatch, FactTarget
from app.targetmgmt import (
    approvals,
    audit as target_audit,
    engine as allocation_engine,
    lock as target_lock,
    plans as plan_service,
    revisions,
)
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
from test_target_approvals import chain_users  # noqa: F401


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lockable(with_history, users, planned, chain_users):
    """An approved version, ready for the last act.

    The shared agent fixture already completes the Khulna branch down to a
    territory, so every branch reaches the level a target is set for and the
    pre-flight passes on the hierarchy. Nothing is added here — a fixture that
    quietly repaired the master data would hide whether the real one is
    lockable.
    """
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
        for role in (Role.UNIT_MANAGER, Role.AREA_MANAGER,
                     Role.REGIONAL_MANAGER, Role.ZONE_MANAGER,
                     Role.BUSINESS_UNIT_HEAD, Role.MANAGEMENT):
            approvals.approve(session, chain_users[role], plan=plan,
                              version=version)
            session.commit()
    return planned


def _open(engine, planned):
    session = Session(engine)
    version = plan_service.get_version(session, planned["version_id"])
    plan = plan_service.get_plan(session, planned["plan_id"])
    return session, plan, version


def _plan_facts(session, plan):
    """The live ``fact_target`` rows *this plan* wrote, and nobody else's.

    Scoped to the plan's own locked batches, exactly as ``lock._existing_rows``
    scopes: the shared fixture seeds target rows of its own, and a test that
    summed the whole table would be measuring those too — and would pass or fail
    on how many the fixture happened to seed.
    """
    batch_uuids = [
        value for (value,) in session.execute(
            select(TargetVersion.locked_batch_id).where(
                TargetVersion.plan_id == plan.plan_id,
                TargetVersion.locked_batch_id.is_not(None)))
    ]
    if not batch_uuids:
        return []
    batch_ids = [
        value for (value,) in session.execute(
            select(EtlImportBatch.batch_id).where(
                EtlImportBatch.batch_uuid.in_(batch_uuids)))
    ]
    return list(session.execute(
        select(FactTarget).where(FactTarget.is_void.is_(False),
                                 FactTarget.import_batch_id.in_(batch_ids))
    ).scalars())


# ---------------------------------------------------------------------------
# The pre-flight
# ---------------------------------------------------------------------------


def test_an_unapproved_version_cannot_be_locked(with_history, users, planned,
                                                chain_users) -> None:
    """Anything short of approved has not been agreed."""
    session, plan, version = _open(with_history, planned)
    with pytest.raises(target_lock.NotLockable):
        target_lock.lock(session, chain_users[Role.MANAGEMENT], plan=plan,
                         version=version)
    session.close()


def test_only_the_top_of_the_chain_locks(with_history, lockable,
                                         chain_users) -> None:
    """The last signature and the lock are one person's two acts.

    Giving the lock to anybody lower would let a target be frozen by somebody
    who could not have approved it.
    """
    session, plan, version = _open(with_history, lockable)
    with pytest.raises(target_lock.LockNotPermitted) as caught:
        target_lock.lock(session, chain_users[Role.REGIONAL_MANAGER], plan=plan,
                         version=version)
    assert "Management" in caught.value.user_message
    session.close()


def test_a_branch_stopping_above_territory_is_refused(with_history, users,
                                                      planned,
                                                      chain_users) -> None:
    """A target is set for a territory — the ETL's own rule for a Target file.

    ``datasets.TARGET_ORG_LEVELS`` is ``(territory_code, sub_territory_code)``,
    so a Target *file* stating no territory is rejected by the pipeline; a
    locked row is the same fact by a different route and is refused for the same
    reason. Constructed by removing the Khulna branch below its unit, which is
    what an allocation over a hierarchy with no territory mapped under that unit
    produces. The branch then terminates at unit, and a row written there would
    count in the country total and in no territory's total. (The master row
    itself is not deleted: the facts hold foreign keys on it, and rightly.)
    """
    session, plan, version = _open(with_history, planned)
    result = _run(session, users, planned)
    allocation_engine.persist(session, version=version, result=result)
    session.execute(TargetAllocation.__table__.delete().where(
        TargetAllocation.version_id == version.version_id,
        TargetAllocation.parent_code == "UN002"))
    session.commit()

    reasons = target_lock.blockers(session, plan=plan, version=version)
    assert any("above territory" in reason for reason in reasons)
    assert any("UN002" in reason for reason in reasons)
    session.close()


def test_a_missing_conversion_factor_blocks_the_lock(with_history, lockable,
                                                     chain_users) -> None:
    """Advisory for allocation, blocking for the lock, and that is not a conflict.

    The engine allocates volume, which needs neither derivation input. A locked
    row states a quantity and an amount, which cannot exist without them — and
    ``target_amount`` is NOT NULL, so a missing input would have to be written
    as zero, which is a real instruction rather than an absent one.
    """
    session, plan, version = _open(with_history, lockable)
    material = session.execute(
        select(DimMaterial).where(DimMaterial.material_code == MATERIAL)
    ).scalar_one()
    material.conversion_factor = None
    session.commit()

    reasons = target_lock.blockers(session, plan=plan, version=version)
    assert any("Conversion Factor" in reason for reason in reasons)
    with pytest.raises(target_lock.LockBlocked):
        target_lock.lock(session, chain_users[Role.MANAGEMENT], plan=plan,
                         version=version)
    session.close()


def test_a_missing_transfer_price_blocks_the_lock(with_history, lockable,
                                                  chain_users) -> None:
    session, plan, version = _open(with_history, lockable)
    material = session.execute(
        select(DimMaterial).where(DimMaterial.material_code == MATERIAL)
    ).scalar_one()
    material.transfer_price = None
    session.commit()
    assert any("Transfer Price" in reason for reason in
               target_lock.blockers(session, plan=plan, version=version))
    session.close()


def test_an_unbalanced_allocation_blocks_the_lock(with_history, lockable,
                                                  chain_users) -> None:
    """Re-checked here rather than trusted from the approval.

    An allocation can be re-run after it was signed, and a figure that no longer
    reconciles must not be frozen because it reconciled once.
    """
    session, plan, version = _open(with_history, lockable)
    row = session.execute(
        select(TargetAllocation).where(
            TargetAllocation.version_id == version.version_id,
            TargetAllocation.level == TargetLevel.REGION).limit(1)
    ).scalar_one()
    row.current_volume = Decimal(str(row.current_volume)) + Decimal("500")
    session.commit()

    assert any("balance" in reason for reason in
               target_lock.blockers(session, plan=plan, version=version))
    session.close()


def test_an_open_revision_blocks_the_lock(with_history, lockable,
                                          chain_users) -> None:
    """Belt and braces, and deliberately so.

    An approved version takes no *new* revision — ``revisions.REVISABLE``
    excludes it — and approval is itself blocked while one is open, so in
    ordinary use this state cannot arise. The row is therefore written directly,
    because the point is that the lock checks for itself rather than trusting
    that the step before it did.
    """
    session, plan, version = _open(with_history, lockable)
    anchor = session.execute(
        select(TargetAllocation).where(
            TargetAllocation.version_id == version.version_id).limit(1)
    ).scalar_one()
    session.add(TargetRevision(
        allocation_id=anchor.allocation_id, version_id=version.version_id,
        level=anchor.level, node_code=anchor.node_code,
        system_volume=Decimal("100"), requested_volume=Decimal("120"),
        reason="Raised before approval.", status=RevisionStatus.PENDING))
    session.commit()

    assert any("revision request" in reason for reason in
               target_lock.blockers(session, plan=plan, version=version))
    session.close()


def test_a_month_the_date_dimension_lacks_blocks_the_lock(with_history,
                                                          lockable) -> None:
    """A fact needs a ``date_id``; the honest answer is to name the month.

    A real state — a financial year nobody has run ``build_dim_date`` for yet —
    constructed by pointing one allocation row at a month beyond the dimension
    rather than by deleting date rows the sales facts reference.
    """
    session, plan, version = _open(with_history, lockable)
    row = session.execute(
        select(TargetAllocation).where(
            TargetAllocation.version_id == version.version_id).limit(1)
    ).scalar_one()
    row.target_month = "2099-01"
    session.commit()
    reasons = target_lock.blockers(session, plan=plan, version=version)
    assert any("date dimension" in reason and "2099-01" in reason
               for reason in reasons)
    session.close()


def test_the_state_reports_lockable_and_who_holds_it(with_history, lockable,
                                                     chain_users) -> None:
    session, plan, version = _open(with_history, lockable)
    state = target_lock.state(session, plan=plan, version=version)
    assert state["lockable"] is True
    assert state["blockers"] == []
    assert state["locks_role"] == Role.MANAGEMENT
    assert state["locked_batch_id"] is None
    session.close()


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


def test_the_locked_total_equals_the_country_target(with_history, lockable,
                                                    chain_users) -> None:
    """Only terminal nodes are written, and they sum to what somebody typed.

    Writing every level would multiply the target by the depth of the tree.
    """
    session, plan, version = _open(with_history, lockable)
    target_lock.lock(session, chain_users[Role.MANAGEMENT], plan=plan,
                     version=version)
    session.commit()

    total = sum(float(row.target_volume) for row in _plan_facts(session, plan))
    assert total == pytest.approx(120000.0)
    session.close()


def test_quantity_and_value_are_derived_and_frozen(with_history, lockable,
                                                   chain_users) -> None:
    """The reason they live on the fact and not on the country line.

    Until the lock a corrected transfer price corrects every report; after it,
    a later price change cannot rewrite what was agreed.
    """
    session, plan, version = _open(with_history, lockable)
    target_lock.lock(session, chain_users[Role.MANAGEMENT], plan=plan,
                     version=version)
    session.commit()

    rows = _plan_facts(session, plan)
    # 120,000 volume at a conversion factor of 0.5, priced at 240.
    assert sum(float(row.target_quantity) for row in rows) ==         pytest.approx(240000.0)
    assert sum(float(row.target_amount) for row in rows) ==         pytest.approx(240000.0 * 240)

    material = session.execute(
        select(DimMaterial).where(DimMaterial.material_code == MATERIAL)
    ).scalar_one()
    material.transfer_price = Decimal("500")
    session.commit()
    unchanged = sum(float(row.target_amount)
                    for row in _plan_facts(session, plan))
    assert unchanged == pytest.approx(240000.0 * 240)
    session.close()


def test_every_locked_row_names_its_territory(with_history, lockable,
                                              chain_users) -> None:
    """The org chain comes from the allocation's own stored parents."""
    session, plan, version = _open(with_history, lockable)
    target_lock.lock(session, chain_users[Role.MANAGEMENT], plan=plan,
                     version=version)
    session.commit()

    rows = _plan_facts(session, plan)
    assert rows
    assert all(row.territory_id is not None for row in rows)
    assert all(row.company_id is not None for row in rows)
    session.close()


def test_a_branch_that_stops_at_sub_territory_carries_no_customer(
        with_history, lockable, chain_users) -> None:
    """Absent rather than guessed. Khulna has no customers mapped beneath it."""
    session, plan, version = _open(with_history, lockable)
    target_lock.lock(session, chain_users[Role.MANAGEMENT], plan=plan,
                     version=version)
    session.commit()

    rows = _plan_facts(session, plan)
    assert any(row.customer_code is None for row in rows)
    assert any(row.customer_code is not None for row in rows)
    session.close()


def test_the_lock_records_a_batch_and_the_version_names_it(with_history,
                                                           lockable,
                                                           chain_users) -> None:
    """The batch is how the rows a version produced are found afterwards."""
    session, plan, version = _open(with_history, lockable)
    result = target_lock.lock(session, chain_users[Role.MANAGEMENT], plan=plan,
                              version=version)
    session.commit()

    assert version.locked_batch_id == result["batch_uuid"]
    batch = session.execute(
        select(EtlImportBatch).where(
            EtlImportBatch.batch_uuid == result["batch_uuid"])
    ).scalar_one()
    assert batch.data_type == "target"
    assert batch.source_system == target_lock.SOURCE_SYSTEM
    assert batch.status == "COMPLETED"
    assert batch.successful_rows == result["rows_written"]
    assert all(row.import_batch_id == batch.batch_id
               for row in _plan_facts(session, plan))
    session.close()


def test_locking_moves_the_version_and_logs_the_act(with_history, lockable,
                                                    chain_users) -> None:
    session, plan, version = _open(with_history, lockable)
    target_lock.lock(session, chain_users[Role.MANAGEMENT], plan=plan,
                     version=version, comment="Agreed at the planning meeting.")
    session.commit()

    assert version.status == TargetStatus.LOCKED
    assert version.locked_at is not None
    act = session.execute(
        select(TargetApproval).where(
            TargetApproval.action == ApprovalAction.LOCKED)
    ).scalar_one()
    assert act.comment == "Agreed at the planning meeting."
    session.close()


def test_the_lock_writes_exactly_one_trail_entry_naming_its_batch(
        with_history, lockable, chain_users) -> None:
    """One act, one entry. Two would make a planner counting locks count wrong."""
    session, plan, version = _open(with_history, lockable)
    result = target_lock.lock(session, chain_users[Role.MANAGEMENT], plan=plan,
                              version=version)
    session.commit()

    entries = session.execute(
        select(TargetAudit).where(
            TargetAudit.action == target_audit.TargetAction.TARGET_LOCKED)
    ).scalars().all()
    assert len(entries) == 1
    assert result["batch_uuid"] in entries[0].new_value
    assert entries[0].old_value == TargetStatus.APPROVED
    session.close()


def test_a_locked_version_cannot_be_locked_again(with_history, lockable,
                                                 chain_users) -> None:
    """Terminal. The next change is a new version, not a second lock."""
    session, plan, version = _open(with_history, lockable)
    target_lock.lock(session, chain_users[Role.MANAGEMENT], plan=plan,
                     version=version)
    session.commit()
    with pytest.raises(target_lock.NotLockable):
        target_lock.lock(session, chain_users[Role.MANAGEMENT], plan=plan,
                         version=version)
    session.close()


def test_relocking_a_plan_restates_its_rows_rather_than_duplicating_them(
        with_history, lockable, users, chain_users) -> None:
    """The business key is the target's grain — one authoritative number.

    A second version of the same plan writes the same keys and updates them in
    place. Inserting instead would double every target the plan states.
    """
    session, plan, version = _open(with_history, lockable)
    first = target_lock.lock(session, chain_users[Role.MANAGEMENT], plan=plan,
                             version=version)
    session.commit()
    before = len(_plan_facts(session, plan))
    assert before == first["rows_written"]

    second = plan_service.create_version(
        session, users["ceo"], plan_id=plan.plan_id,
        reason="Country target raised after the September review.")
    session.commit()
    result = _run(session, users, {"plan_id": plan.plan_id,
                                   "version_id": second.version_id})
    allocation_engine.persist(session, version=second, result=result)
    for step in (TargetStatus.ALLOCATION_IN_PROGRESS, TargetStatus.ALLOCATED):
        plan_service.set_version_status(session, users["ceo"],
                                        version_id=second.version_id,
                                        new_status=step)
    approvals.submit(session, users["ceo"], plan=plan, version=second)
    session.commit()
    for role in (Role.UNIT_MANAGER, Role.AREA_MANAGER, Role.REGIONAL_MANAGER,
                 Role.ZONE_MANAGER, Role.BUSINESS_UNIT_HEAD, Role.MANAGEMENT):
        approvals.approve(session, chain_users[role], plan=plan, version=second)
        session.commit()

    again = target_lock.lock(session, chain_users[Role.MANAGEMENT], plan=plan,
                             version=second)
    session.commit()

    assert again["batch_uuid"] != first["batch_uuid"]
    assert again["rows_inserted"] == 0
    assert again["rows_updated"] == before
    assert len(_plan_facts(session, plan)) == before
    total = sum(float(row.target_volume) for row in _plan_facts(session, plan))
    assert total == pytest.approx(120000.0)
    session.close()


# ---------------------------------------------------------------------------
# The trail
# ---------------------------------------------------------------------------


def test_the_trail_records_every_decision_newest_first(with_history, lockable,
                                                       chain_users) -> None:
    session, plan, version = _open(with_history, lockable)
    target_lock.lock(session, chain_users[Role.MANAGEMENT], plan=plan,
                     version=version)
    session.commit()

    trail = target_audit.trail(session, plan_id=plan.plan_id)
    actions = [row["action"] for row in trail["rows"]]
    assert actions[0] == target_audit.TargetAction.TARGET_LOCKED
    assert target_audit.TargetAction.PLAN_CREATED in actions
    assert trail["total"] == len(trail["rows"])
    session.close()


def test_the_trail_filters_on_the_server(with_history, lockable,
                                         chain_users) -> None:
    """A plan's trail grows without bound; hiding rows in the browser would get
    slower exactly as the record gets more valuable."""
    session, plan, version = _open(with_history, lockable)
    target_lock.lock(session, chain_users[Role.MANAGEMENT], plan=plan,
                     version=version)
    session.commit()

    locked = target_audit.trail(
        session, plan_id=plan.plan_id,
        action=target_audit.TargetAction.TARGET_LOCKED)
    assert locked["total"] == 1
    assert locked["rows"][0]["action"] == target_audit.TargetAction.TARGET_LOCKED
    session.close()


def test_the_trail_reports_a_total_beside_its_page(with_history, lockable,
                                                   chain_users) -> None:
    """A filter that found forty and one that found forty thousand must differ."""
    session, plan, version = _open(with_history, lockable)
    page = target_audit.trail(session, plan_id=plan.plan_id, limit=2)
    assert len(page["rows"]) == 2
    assert page["total"] > 2
    session.close()


def test_the_trail_offers_the_actions_it_knows(with_history, lockable) -> None:
    """Declared once, so the filter cannot offer an action nothing writes."""
    session, plan, version = _open(with_history, lockable)
    trail = target_audit.trail(session, plan_id=plan.plan_id)
    assert set(trail["actions"]) == set(target_audit.TargetAction.ALL)
    session.close()
