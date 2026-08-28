"""Locking: the moment an agreed target becomes *the* target.

Every other module here builds a proposal. This is the one that writes into
``fact_target``, which is what the Target page, the exports and the AI agent all
read — so from the instant a version is locked, "the target" means one thing
across the whole platform and there is nothing left to disagree about.

**Approval is a judgement; locking is the act that freezes figures.** They are
deliberately two steps by the same role at two moments. Until the lock an
approved version can still be sent back and a corrected transfer price still
corrects every report; after it, the derived quantity and value are stored on
the fact and a later price change cannot rewrite what was agreed. That is the
whole reason ``target_quantity`` and ``target_amount`` live on ``fact_target``
and not on ``target_country_line``.

**Five conditions, checked before anything is written, each reported by name.**

*Approved.* Anything else has not been agreed.

*Balanced, and no revision open.* Both were gates on the last approval, and both
are re-checked here rather than trusted: an allocation can be re-run, and a
figure that no longer reconciles must not be frozen because it reconciled once.

*Every material carries a conversion factor and a transfer price.* These are
**advisory for allocation and blocking for the lock**, and the difference is not
an inconsistency: the engine allocates *volume*, which needs neither, while a
locked row states a quantity and an amount that cannot exist without them.
``fact_target.target_amount`` is NOT NULL, so a missing input would have to be
written as zero — and a zero target is a real instruction that must stay
distinguishable from an absent one.

*Every terminal node reaches territory or below.* This is not a rule invented
here: ``datasets.TARGET_ORG_LEVELS`` is ``(territory_code, sub_territory_code)``,
because a target *is* set for a territory and everything above one is derived
from the master hierarchy. A Target file stating no territory is rejected by the
ETL, and a locked row is the same fact by a different route, so it is refused for
the same reason — a target above territory would appear in the country total and
in no territory's total, which is the "doesn't add up" failure this module
exists to prevent. The nodes are named, because the fix is in the master data.

**Only terminal nodes are written.** They sum to the country target exactly —
that is what reconciliation proves — so writing every level would multiply the
target by the depth of the tree. The organisational codes come from each row's
**stored parent chain**, not from re-walking the masters, for the same reason
the review tree does: a territory moved to another region next month must not
retroactively reshape a target somebody has already locked.

**A re-locked plan restates its own rows and voids what it dropped.** The
business key is the target's grain, so V2 of a plan produces the same keys as V1
and updates them in place — one authoritative number per grain, which is the
point. A key V1 wrote and V2 does not is **voided**, never deleted: the row, its
provenance and its batch survive, and every reporting view filters ``is_void``,
so it leaves the reports in the same instant without leaving the record.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..database.models import (
    DimArea,
    DimBusinessUnit,
    DimCompany,
    DimMaterial,
    DimRegion,
    DimSalesLine,
    DimSubTerritory,
    DimTerritory,
    DimUnit,
    DimZone,
)
from ..database.models_target import (
    ApprovalAction,
    TargetAllocation,
    TargetApproval,
    TargetLevel,
    TargetPlan,
    TargetStatus,
    TargetVersion,
)
from ..database.models_warehouse import (
    DimCustomer,
    DimDate,
    EtlImportBatch,
    FactTarget,
)
from ..etl.datasets import get_dataset
from . import country, matrix, plans as plan_service
from . import approvals as approval_service
from . import reconcile
from .errors import TargetManagementError

#: What a locked row records as its origin. Not ``DEMO`` and not the name of a
#: file: these rows were not imported, they were *decided* here, and a reader
#: asking where a target came from should be sent to the plan rather than to a
#: spreadsheet nobody can find.
SOURCE_SYSTEM = "TARGET_MGMT"

#: The dataset whose business key defines a target's grain. Read from the spec
#: rather than restated, so a lock and a Target-file upload can never key the
#: same target two different ways.
TARGET_DATASET = get_dataset("target")

#: level -> (model, code attribute). Used to turn the codes stored on an
#: allocation row's parent chain into the surrogate keys the fact carries.
_DIMENSIONS: tuple[tuple[str, Any, str, str], ...] = (
    (TargetLevel.COMPANY, DimCompany, "company_code", "company_id"),
    (TargetLevel.ZONE, DimZone, "zone_code", "zone_id"),
    (TargetLevel.REGION, DimRegion, "region_code", "region_id"),
    (TargetLevel.AREA, DimArea, "area_code", "area_id"),
    (TargetLevel.UNIT, DimUnit, "unit_code", "unit_id"),
    (TargetLevel.TERRITORY, DimTerritory, "territory_code", "territory_id"),
    (TargetLevel.SUB_TERRITORY, DimSubTerritory, "sub_territory_code",
     "sub_territory_id"),
    (TargetLevel.CUSTOMER, DimCustomer, "customer_code", "customer_id"),
)


class NotLockable(TargetManagementError):
    """The version has not been approved, or has already been locked."""

    code = "TARGET_NOT_LOCKABLE"

    def __init__(self, status: str) -> None:
        super().__init__(
            f"a {status} version cannot be locked",
            user_message=(
                f"This target is {status.replace('_', ' ').lower()}. Only an "
                f"approved target can be locked, and a locked one is changed by "
                f"creating the next version."
            ),
            details={"status": status},
        )


class LockBlocked(TargetManagementError):
    """One or more conditions the frozen figures depend on are unmet.

    Each is its own sentence. They fail for different reasons and are fixed in
    different places — a missing transfer price is a Material Master upload, an
    unbalanced allocation is a re-run, a shallow branch is customer mapping —
    and one merged message would send a reader to the wrong one.
    """

    code = "TARGET_LOCK_BLOCKED"

    def __init__(self, reasons: list[str]) -> None:
        super().__init__(
            "; ".join(reasons),
            user_message="This target cannot be locked yet. " + " ".join(reasons),
            details={"reasons": reasons},
        )


class LockNotPermitted(TargetManagementError):
    """Only the top of the approval chain locks.

    The last signature and the lock are the same person's two acts, and giving
    the lock to anybody lower would let a target be frozen by somebody who could
    not have approved it.
    """

    code = "TARGET_LOCK_NOT_PERMITTED"

    def __init__(self, role: str, expected: str | None) -> None:
        super().__init__(
            f"{role} may not lock",
            user_message=(
                f"Locking a target is held by "
                f"{(expected or 'the top of the approval chain').replace('_', ' ').title()}"
                f", the last step of the approval chain."
            ),
            details={"role": role, "expected": expected},
        )


# ---------------------------------------------------------------------------
# The pre-flight
# ---------------------------------------------------------------------------


def _terminal_nodes(rows: list[TargetAllocation]) -> list[TargetAllocation]:
    """Rows whose node has no children in this allocation.

    Terminal rather than "the deepest level", because a real hierarchy is
    ragged: one region reaches a customer while another stops at territory
    because no sub-territory exists under it, and volume that comes to rest at
    the bottom of a short branch is as final as volume that reaches a customer.
    The same rule :mod:`reconcile` checks by.
    """
    parents = {(row.parent_level, row.parent_code) for row in rows
               if row.parent_code}
    return [row for row in rows if (row.level, row.node_code) not in parents]


def _derivation_inputs(session: Session,
                       material_codes: set[str]) -> dict[str, tuple]:
    rows = session.execute(
        select(DimMaterial.material_code, DimMaterial.conversion_factor,
               DimMaterial.transfer_price)
        .where(DimMaterial.material_code.in_(material_codes))
    ).all()
    return {code: (factor, price) for code, factor, price in rows}


def blockers(session: Session, *, plan: TargetPlan,
             version: TargetVersion) -> list[str]:
    """Everything standing between this version and ``fact_target``.

    Read-only and cheap enough for the screen to call on every load, so a
    planner sees what to fix before they press the button rather than after.
    """
    reasons: list[str] = []

    rows = list(session.execute(
        select(TargetAllocation).where(
            TargetAllocation.version_id == version.version_id)
    ).scalars())
    if not rows:
        return ["This version has no allocation to lock."]

    reasons.extend(approval_service.final_blockers(session, version.version_id))

    # Re-checked rather than trusted from the approval: an allocation can be
    # re-run after it was signed, and a figure that no longer reconciles must
    # not be frozen because it reconciled once.
    lines = country.list_lines(session, version.version_id)
    verdict = reconcile.check(
        session, version_id=version.version_id,
        country_target={line.material_code: Decimal(str(line.target_volume))
                        for line in lines if line.target_volume})
    if not verdict.balanced:
        reasons.append(
            f"Reconciliation does not balance at {len(verdict.mismatches)} "
            f"node{'s' if len(verdict.mismatches) != 1 else ''}.")

    inputs = _derivation_inputs(session, {row.material_code for row in rows})
    missing_factor = sorted(
        code for code in {row.material_code for row in rows}
        if inputs.get(code, (None, None))[0] in (None, 0))
    missing_price = sorted(
        code for code in {row.material_code for row in rows}
        if inputs.get(code, (None, None))[1] is None)
    if missing_factor:
        reasons.append(
            f"No Conversion Factor for {', '.join(missing_factor[:5])}"
            f"{'…' if len(missing_factor) > 5 else ''} — a locked target states "
            f"a quantity, which cannot be derived without it.")
    if missing_price:
        reasons.append(
            f"No Transfer Price for {', '.join(missing_price[:5])}"
            f"{'…' if len(missing_price) > 5 else ''} — a locked target states "
            f"an amount, which cannot be derived without it.")

    shallow = sorted({
        f"{row.level} {row.node_code}" for row in _terminal_nodes(rows)
        if TargetLevel.ORDERED.index(row.level)
        < TargetLevel.ORDERED.index(TargetLevel.TERRITORY)
    })
    if shallow:
        reasons.append(
            f"{len(shallow)} branch{'es' if len(shallow) != 1 else ''} stop "
            f"above territory ({', '.join(shallow[:5])}"
            f"{'…' if len(shallow) > 5 else ''}). A target above territory "
            f"would count in the country total and in no territory's total. "
            f"A target is set for a territory: extend the hierarchy beneath "
            f"them, or narrow the plan.")

    months = {row.target_month for row in rows}
    known = set(_date_ids(session, months))
    if (unknown := sorted(months - known)):
        reasons.append(
            f"The date dimension has no rows for {', '.join(unknown[:5])}"
            f"{'…' if len(unknown) > 5 else ''}. Run build_dim_date before "
            f"locking.")
    return reasons


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


def lock(session: Session, user: UserContext, *, plan: TargetPlan,
         version: TargetVersion, comment: str | None = None) -> dict[str, Any]:
    """Write the agreed allocation into ``fact_target``, in one transaction.

    Returns what was written, so the screen can report it rather than implying
    it. The caller owns the commit — the fact rows, the batch, the version's new
    status and its audit entry live or die together, which is what stops a
    half-locked target existing at all.
    """
    if version.status != TargetStatus.APPROVED:
        raise NotLockable(version.status)

    top = _top_of_chain(session)
    if top is not None and user.role != top:
        raise LockNotPermitted(user.role, top)

    if (reasons := blockers(session, plan=plan, version=version)):
        raise LockBlocked(reasons)

    rows = list(session.execute(
        select(TargetAllocation).where(
            TargetAllocation.version_id == version.version_id)
    ).scalars())
    chain = _parent_chain(rows)
    inputs = _derivation_inputs(session, {row.material_code for row in rows})
    surrogates = _surrogate_keys(session)
    dates = _date_ids(session, {row.target_month for row in rows})

    batch = EtlImportBatch(
        batch_uuid=str(uuid.uuid4()),
        source_type="TARGET_PLAN",
        source_system=SOURCE_SYSTEM,
        # Who locked it is on the version, on the approval log and on the audit
        # entry; the batch carries no actor column, and inventing a place to put
        # one here would be a fourth answer to a question already answered.
        source_file=f"{plan.plan_code} · V{version.version_no} · {user.username}",
        data_type="target",
        load_mode="UPSERT",
        status="STARTED",
    )
    session.add(batch)
    session.flush()

    existing = _existing_rows(session, plan)
    written: dict[str, FactTarget] = {}
    inserted = updated = 0

    for row in _terminal_nodes(rows):
        record = _record(row, chain, plan)
        key = TARGET_DATASET.build_business_key(record, SOURCE_SYSTEM)
        factor, price = inputs[row.material_code]
        volume = Decimal(str(row.current_volume))
        quantity = volume / Decimal(str(factor))
        amount = quantity * Decimal(str(price))

        fact = existing.get(key) or written.get(key)
        if fact is None:
            fact = FactTarget(business_key=key)
            session.add(fact)
            inserted += 1
        else:
            updated += 1
        written[key] = fact

        fact.date_id = dates[row.target_month]
        fact.target_month = row.target_month
        fact.financial_year = plan.financial_year
        fact.material_code = row.material_code
        fact.customer_code = record.get("customer_code")
        fact.sales_force_code = None
        fact.target_volume = volume
        fact.target_quantity = quantity
        fact.target_amount = amount
        fact.source_system = SOURCE_SYSTEM
        fact.source_file = batch.source_file
        fact.source_transaction_id = f"V{version.version_id}"
        fact.import_batch_id = batch.batch_id
        # Un-voided on purpose: a grain this plan dropped and has now restated
        # is a live target again, and leaving the flag set would hide a figure
        # somebody just approved.
        fact.is_void = False
        fact.voided_at = None
        for level, _model, code_field, id_field in _DIMENSIONS:
            code = record.get(code_field)
            setattr(fact, id_field,
                    surrogates[level].get(code) if code else None)
        fact.business_unit_id = surrogates["business_unit"].get(plan.bu_code)
        fact.sales_line_id = surrogates["sales_line"].get(plan.sales_line_code)

    voided = _void_dropped(session, existing, set(written))

    batch.status = "COMPLETED"
    batch.completed_at = datetime.now(timezone.utc)
    batch.total_rows = len(written)
    batch.successful_rows = len(written)
    batch.inserted_rows = inserted
    batch.updated_rows = updated

    # The batch is set **before** the status change, so the single audit entry
    # ``set_version_status`` writes can name it. Writing a second entry here
    # would put the same act in the trail twice, and a planner counting locks
    # would count one too many.
    version.locked_batch_id = batch.batch_uuid
    plan_service.set_version_status(
        session, user, version_id=version.version_id,
        new_status=TargetStatus.LOCKED, reason=comment)
    session.add(TargetApproval(
        version_id=version.version_id, action=ApprovalAction.LOCKED,
        actor=user.username, actor_role=user.role, comment=comment))
    session.flush()

    return {
        "batch_uuid": batch.batch_uuid,
        "rows_written": len(written),
        "rows_inserted": inserted,
        "rows_updated": updated,
        "rows_voided": voided,
        "status": TargetStatus.LOCKED,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _top_of_chain(session: Session) -> str | None:
    """The role that signs last, and therefore the one that locks.

    ``None`` when nothing is configured, which leaves the section permission as
    the only gate rather than refusing everybody — an empty chain is an
    administrator's problem to fix, not a reason to make the target unlockable.
    """
    approvers = matrix.approvers(session)
    return approvers[-1].role if approvers else None


def _parent_chain(rows: list[TargetAllocation]) -> dict[tuple[str, str],
                                                        tuple[str, str] | None]:
    """``(level, code) -> parent``, from the allocation's own stored snapshot.

    Read back rather than rebuilt from the organisational masters, for the
    reason the review tree gives: a territory moved to another region next month
    must not retroactively reshape a target somebody locked.
    """
    return {
        (row.level, row.node_code): ((row.parent_level, row.parent_code)
                                     if row.parent_code else None)
        for row in rows
    }


def _record(row: TargetAllocation,
            chain: dict[tuple[str, str], tuple[str, str] | None],
            plan: TargetPlan) -> dict[str, Any]:
    """One terminal node as the flat record a target's business key is built from.

    Every level the node sits under is filled in by walking the stored chain; a
    level the branch never reached stays absent, which is what makes a
    territory-terminal target carry no sub-territory rather than a guessed one.
    """
    record: dict[str, Any] = {
        "financial_year": plan.financial_year,
        "target_month": row.target_month,
        "material_code": row.material_code,
        "sales_force_code": None,
        "company_code": plan.company_code,
    }
    current: tuple[str, str] | None = (row.level, row.node_code)
    seen: set[tuple[str, str]] = set()
    while current is not None and current not in seen:
        seen.add(current)
        record[f"{current[0]}_code"] = current[1]
        current = chain.get(current)
    return record


def _surrogate_keys(session: Session) -> dict[str, dict[str, int]]:
    """``level -> {code: surrogate id}``, one small query per dimension.

    Retired records are included. A target allocated to a node the master has
    since retired still belongs to that node — retiring removes a record from
    *selection*, not from history — and dropping the link would leave the fact
    unable to say where it sat.
    """
    keys: dict[str, dict[str, int]] = {}
    for level, model, code_field, id_field in _DIMENSIONS:
        keys[level] = {
            code: surrogate for code, surrogate in session.execute(
                select(getattr(model, code_field),
                       getattr(model, id_field.replace("_id", "_id"))))
            if code
        }
    keys["business_unit"] = {
        code: surrogate for code, surrogate in session.execute(
            select(DimBusinessUnit.bu_code, DimBusinessUnit.business_unit_id))
        if code
    }
    keys["sales_line"] = {
        code: surrogate for code, surrogate in session.execute(
            select(DimSalesLine.sales_line_code, DimSalesLine.sales_line_id))
        if code
    }
    return keys


def _date_ids(session: Session, months: set[str]) -> dict[str, int]:
    """``YYYY-MM -> date_id`` for the first of each month.

    A target is a month, not a day, and the platform's rule is that it resolves
    to the **1st** — so a window that excludes the 1st legitimately reports no
    target rather than a partial one.

    Matched on ``year`` and ``month`` rather than on a stored label, because
    ``dim_date`` holds no such label; deriving one on the fly here and comparing
    strings would be a second spelling of the period that could drift from the
    dimension's own.
    """
    if not months:
        return {}
    pairs = set()
    for month in months:
        try:
            year, number = month.split("-")
            pairs.add((int(year), int(number)))
        except (ValueError, AttributeError):
            # An unparsable month is simply absent, which the caller reports as
            # a missing date row — the honest answer, and never a guessed one.
            continue
    if not pairs:
        return {}
    rows = session.execute(
        select(DimDate.year, DimDate.month, DimDate.date_id)
        .where(and_(DimDate.day == 1,
                    DimDate.year.in_({year for year, _ in pairs}),
                    DimDate.month.in_({number for _, number in pairs})))
    ).all()
    return {f"{year:04d}-{number:02d}": date_id
            for year, number, date_id in rows if (year, number) in pairs}


def _existing_rows(session: Session,
                   plan: TargetPlan) -> dict[str, FactTarget]:
    """Every fact row a previous version of *this plan* wrote, by business key.

    Scoped to the plan's own batches rather than to the whole table: a target
    loaded from a file for the same grain is somebody else's row, and silently
    taking ownership of it would make a locked plan responsible for figures it
    never produced.
    """
    batch_uuids = [
        uuid_value for (uuid_value,) in session.execute(
            select(TargetVersion.locked_batch_id).where(
                TargetVersion.plan_id == plan.plan_id,
                TargetVersion.locked_batch_id.is_not(None)))
    ]
    if not batch_uuids:
        return {}
    batch_ids = [
        batch_id for (batch_id,) in session.execute(
            select(EtlImportBatch.batch_id).where(
                EtlImportBatch.batch_uuid.in_(batch_uuids)))
    ]
    if not batch_ids:
        return {}
    return {
        row.business_key: row for row in session.execute(
            select(FactTarget).where(FactTarget.import_batch_id.in_(batch_ids))
        ).scalars()
    }


def _void_dropped(session: Session, existing: dict[str, FactTarget],
                  written: set[str]) -> int:
    """Void every grain a previous version wrote that this one does not restate.

    Voided, never deleted: the row, its provenance and its batch survive, and
    every reporting view filters ``is_void``, so it leaves the dashboard, the
    exports and the agent in the same instant without leaving the record.
    """
    now = datetime.now(timezone.utc)
    voided = 0
    for key, row in existing.items():
        if key in written or row.is_void:
            continue
        row.is_void = True
        row.voided_at = now
        voided += 1
    return voided


def state(session: Session, *, plan: TargetPlan,
          version: TargetVersion) -> dict[str, Any]:
    """What the Versions screen needs to draw the lock control honestly."""
    reasons = ([] if version.status != TargetStatus.APPROVED
               else blockers(session, plan=plan, version=version))
    return {
        "lockable": version.status == TargetStatus.APPROVED and not reasons,
        "blockers": reasons,
        "locked_batch_id": version.locked_batch_id,
        "locked_at": version.locked_at.isoformat() if version.locked_at else None,
        "locks_role": _top_of_chain(session),
    }


__all__ = [
    "SOURCE_SYSTEM",
    "NotLockable",
    "LockBlocked",
    "LockNotPermitted",
    "blockers",
    "lock",
    "state",
]
