"""Target plans and their versions: the scope, the chain and the transitions.

A **plan** fixes what a target is for: a financial year, a period, a company, a
business unit and a sales line. There is exactly one plan per scope, and the
database says so — see ``uq_target_plan_scope``. A second target for the same
scope is a new *version* of the same plan, never a second plan nobody can tell
apart from the first.

A **version** is one revision of that plan's numbers. Creating a plan creates
V1 with it, because a plan with no version has nothing to hold a number and
every screen downstream would have to special-case its absence. Every later
version is created from the current one, **copies its country lines** and
becomes current in its place; the version it replaced keeps its status, its
numbers and its approvals untouched.

**What "current" means, and why it is a column.** The current version is the one
every screen defaults to and every new allocation writes into.
``target_version.current_plan_id`` holds the plan's id while a version is
current and NULL otherwise, under a plain unique constraint — so the database
refuses a second current version rather than trusting this module to be careful.
:func:`_make_current` clears the incumbent and flushes before claiming it,
because the constraint is checked on the flush and doing it the other way round
raises against a row we are about to release.

**Scope, and the level a plan actually carries.** A plan states a company, a
business unit and a sales line. It states no zone, region or territory, so a
regional manager's scope does not narrow the plan list — filtering on a level a
row does not carry would hide every plan from the very people who have to
review one. Their scope binds where their targets are read, which is the
allocation tree, not here. This is the same rule the filter engine follows: a
row only matches a filter on a level it actually carries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..database.models import (
    DimBusinessUnit,
    DimCompany,
    DimSalesLine,
)
from ..database.models_target import (
    TargetAudit,
    TargetAllocation,
    TargetApproval,
    TargetCountryLine,
    TargetPlan,
    TargetStatus,
    TargetVersion,
)
from ..etl.calendar import FinancialYearConfig
from . import audit as target_audit
from .errors import (
    PlanNotDeletable,
    InvalidTransition,
    PlanNotFound,
    PlanScopeConflict,
    ReasonRequired,
    ScopeMismatch,
    UnknownScopeCode,
    VersionFrozen,
    VersionNotFound,
)

#: Periods a plan may cover. ``FY`` is the whole financial year; the four
#: quarters are quarters *of the financial year*, so Q1 is July-September under
#: the default July start and January-March under a January one. Which months
#: each covers is derived from the configured start month, never written down.
TARGET_PERIODS: tuple[str, ...] = ("FY", "Q1", "Q2", "Q3", "Q4")

#: Which status may follow which.
#:
#: Written out rather than inferred from an ordering, because the workflow is
#: not a straight line: a rejection returns a version to its author, an approved
#: version can still be sent back, and a locked one goes nowhere at all. An
#: ordering would have to encode all three as exceptions anyway, and would read
#: as if the exceptions were the accidents rather than the rules.
ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    TargetStatus.DRAFT: (
        TargetStatus.ALLOCATION_IN_PROGRESS,
        TargetStatus.UNDER_REVIEW,
    ),
    TargetStatus.ALLOCATION_IN_PROGRESS: (
        TargetStatus.ALLOCATED,
        # An allocation run that fails leaves the version where it started,
        # rather than in a state nothing can act on.
        TargetStatus.DRAFT,
    ),
    TargetStatus.ALLOCATED: (
        TargetStatus.UNDER_REVIEW,
        # Re-running the allocation is ordinary: the country target changed, or
        # a factor was toggled.
        TargetStatus.ALLOCATION_IN_PROGRESS,
        TargetStatus.DRAFT,
    ),
    TargetStatus.UNDER_REVIEW: (
        TargetStatus.PARTIALLY_APPROVED,
        TargetStatus.APPROVED,
        TargetStatus.REJECTED,
    ),
    TargetStatus.PARTIALLY_APPROVED: (
        TargetStatus.APPROVED,
        TargetStatus.REJECTED,
        # A level below can still ask for a change after a level above signed
        # off; the version returns to review rather than jumping to approved.
        TargetStatus.UNDER_REVIEW,
    ),
    TargetStatus.APPROVED: (
        TargetStatus.LOCKED,
        # Approval is not irreversible until it is locked. Until then a later
        # approver may send the whole version back.
        TargetStatus.UNDER_REVIEW,
    ),
    TargetStatus.REJECTED: (
        TargetStatus.DRAFT,
        TargetStatus.ALLOCATION_IN_PROGRESS,
    ),
    # Terminal. A locked version is changed only by creating the next one, which
    # is a different plan-level act and does not move this version at all.
    TargetStatus.LOCKED: (),
    #: What a version becomes when a newer one takes its place while it was
    #: still being worked on. Distinct from LOCKED, which is a version that was
    #: approved and written into ``fact_target``.
    TargetStatus.REVISED: (),
}


@dataclass(frozen=True)
class PlanScope:
    """The five columns that identify a plan. Validated before anything is written."""

    financial_year: str
    target_period: str
    company_code: str
    bu_code: str
    sales_line_code: str


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_scope(session: Session, scope: PlanScope) -> None:
    """Refuse a scope the master data does not support.

    Checked here rather than left to a foreign key for the reason every
    master-side reference in this schema is checked in application code: the
    message can name *which* code and *which* master, and an integrity error
    cannot. Three checks, in the order a planner fills the form.
    """
    company = session.execute(
        select(DimCompany).where(DimCompany.company_code == scope.company_code)
    ).scalar_one_or_none()
    if company is None:
        raise UnknownScopeCode("Company", scope.company_code)

    unit = session.execute(
        select(DimBusinessUnit).where(DimBusinessUnit.bu_code == scope.bu_code)
    ).scalar_one_or_none()
    if unit is None:
        raise UnknownScopeCode("Business Unit", scope.bu_code)
    if unit.company_code != scope.company_code:
        raise ScopeMismatch("Business Unit", scope.bu_code, "Company",
                            scope.company_code)

    line = session.execute(
        select(DimSalesLine).where(
            DimSalesLine.sales_line_code == scope.sales_line_code)
    ).scalar_one_or_none()
    if line is None:
        raise UnknownScopeCode("Sales Line", scope.sales_line_code)
    if line.bu_code != scope.bu_code:
        raise ScopeMismatch("Sales Line", scope.sales_line_code,
                            "Business Unit", scope.bu_code)


def next_plan_code(session: Session, financial_year: str) -> str:
    """``TP-2026-001`` — sequential within the financial year that starts it.

    Derived from the plans already in that year rather than from a sequence, so
    the number a planner sees follows the plans they can see. It is a label, not
    a key: ``uq_target_plan_scope`` is what actually prevents a duplicate, so a
    collision here can only be a race and is resolved by trying the next number
    rather than by failing the creation.

    **A code is never reused, including one a deleted plan had.** The live table
    is not the whole record: deleting a draft plan frees its number, and handing
    that number to the next plan would leave the audit trail — which survives the
    delete and names the plan by code — pointing at two different plans with one
    label. So the codes an audit entry has already spoken for are counted as
    taken, which is what makes ``plan_code`` mean one thing forever.
    """
    digits = "".join(ch for ch in financial_year if ch.isdigit())[:4] or "0000"
    prefix = f"TP-{digits}-"
    used = {
        code for (code,) in session.execute(
            select(TargetPlan.plan_code).where(
                TargetPlan.plan_code.like(f"{prefix}%"))
        )
    }
    used |= {
        label for (label,) in session.execute(
            select(TargetAudit.node_label).where(
                TargetAudit.node_label.like(f"{prefix}%"))
        ) if label
    }
    for n in range(1, 1000):
        candidate = f"TP-{digits}-{n:03d}"
        if candidate not in used:
            return candidate
    # 999 plans in one financial year is not a case worth a special format; it
    # is a case worth saying so plainly.
    raise ValueError(f"no free plan code left for {financial_year}")


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def create_plan(session: Session, user: UserContext, *, scope: PlanScope,
                basis_financial_years: str | None = None) -> TargetPlan:
    """Create a plan and its first version, in one transaction.

    V1 is created with the plan and made current. A plan with no version has
    nowhere to put a number, so every screen downstream would have to handle its
    absence — and none of them would ever be right to, because the absence would
    only ever be this function's own gap.
    """
    validate_scope(session, scope)

    existing = session.execute(
        select(TargetPlan).where(
            TargetPlan.financial_year == scope.financial_year,
            TargetPlan.target_period == scope.target_period,
            TargetPlan.company_code == scope.company_code,
            TargetPlan.bu_code == scope.bu_code,
            TargetPlan.sales_line_code == scope.sales_line_code,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise PlanScopeConflict(existing.plan_code)

    plan = TargetPlan(
        plan_code=next_plan_code(session, scope.financial_year),
        financial_year=scope.financial_year,
        target_period=scope.target_period,
        company_code=scope.company_code,
        bu_code=scope.bu_code,
        sales_line_code=scope.sales_line_code,
        basis_financial_years=basis_financial_years,
        status=TargetStatus.DRAFT,
        created_by=user.username,
    )
    session.add(plan)
    session.flush()

    version = TargetVersion(
        plan_id=plan.plan_id,
        version_no=1,
        status=TargetStatus.DRAFT,
        current_plan_id=plan.plan_id,
        reason=None,
        created_by=user.username,
    )
    session.add(version)
    session.flush()

    target_audit.record(
        session, action=target_audit.TargetAction.PLAN_CREATED,
        plan_id=plan.plan_id, version_id=version.version_id,
        actor=user.username, actor_role=user.role,
        node_label=f"{plan.plan_code} · {plan.financial_year} {plan.target_period}",
        old_value=None, new_value="V1 · Draft",
    )
    return plan


def list_plans(session: Session, user: UserContext, *,
               financial_year: str | None = None,
               plan_status: str | None = None,
               company_code: str | None = None,
               sales_line_code: str | None = None,
               limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    """Plans the caller may see, newest financial year first.

    Scope narrows this only on the three levels a plan states. See the module
    docstring: a regional manager holds no company, business-unit or sales-line
    scope, so they see every plan and their own scope binds where their targets
    are read.
    """
    conditions = []
    if financial_year:
        conditions.append(TargetPlan.financial_year == financial_year)
    if plan_status:
        conditions.append(TargetPlan.status == plan_status)
    if company_code:
        conditions.append(TargetPlan.company_code == company_code)
    if sales_line_code:
        conditions.append(TargetPlan.sales_line_code == sales_line_code)

    for level, column in (("company_code", TargetPlan.company_code),
                          ("bu_code", TargetPlan.bu_code),
                          ("sales_line_code", TargetPlan.sales_line_code)):
        codes = user.data_scope.get(level)
        if codes:
            conditions.append(column.in_(codes))

    total = session.execute(
        select(func.count()).select_from(TargetPlan).where(*conditions)
    ).scalar_one()

    rows = session.execute(
        select(TargetPlan)
        .where(*conditions)
        .order_by(TargetPlan.financial_year.desc(), TargetPlan.plan_code.desc())
        .limit(limit).offset(offset)
    ).scalars().all()

    # One query for every plan's current version rather than one per plan: the
    # list shows the current version number and status on each row, and a lazy
    # load here would be a query per row on a page of fifty.
    current_by_plan = _current_versions(session, [p.plan_id for p in rows])
    return [plan_to_dict(plan, current_by_plan.get(plan.plan_id)) for plan in rows], total


def get_plan(session: Session, plan_id: int) -> TargetPlan:
    plan = session.get(TargetPlan, plan_id)
    if plan is None:
        raise PlanNotFound()
    return plan


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def list_versions(session: Session, plan_id: int) -> list[dict]:
    """Every version of a plan, newest first. Nothing is ever hidden here."""
    get_plan(session, plan_id)
    rows = session.execute(
        select(TargetVersion)
        .where(TargetVersion.plan_id == plan_id)
        .order_by(TargetVersion.version_no.desc())
    ).scalars().all()
    counts = _line_counts(session, [v.version_id for v in rows])
    return [version_to_dict(v, line_count=counts.get(v.version_id, 0)) for v in rows]


def current_version(session: Session, plan_id: int) -> TargetVersion | None:
    return session.execute(
        select(TargetVersion).where(TargetVersion.current_plan_id == plan_id)
    ).scalar_one_or_none()


def get_version(session: Session, version_id: int) -> TargetVersion:
    version = session.get(TargetVersion, version_id)
    if version is None:
        raise VersionNotFound()
    return version


def create_version(session: Session, user: UserContext, *, plan_id: int,
                   reason: str) -> TargetVersion:
    """Create the next version from the current one and make it current.

    The country lines are **copied**, not moved. That is what makes the new
    version a starting point rather than a blank page, and it is what leaves the
    version it came from complete: an approved V2 that had its numbers taken
    away by V3 would no longer be the thing that was approved.

    The previous version is marked ``REVISED`` only if it was still being worked
    on. One that was approved or locked keeps that status — what it *is* did not
    change because something newer exists, and rewriting it would erase the
    record of a target the business actually agreed to.
    """
    if not (reason or "").strip():
        raise ReasonRequired()

    plan = get_plan(session, plan_id)
    previous = current_version(session, plan_id)

    highest = session.execute(
        select(func.max(TargetVersion.version_no))
        .where(TargetVersion.plan_id == plan_id)
    ).scalar_one() or 0

    version = TargetVersion(
        plan_id=plan_id,
        version_no=highest + 1,
        status=TargetStatus.DRAFT,
        reason=reason.strip(),
        created_by=user.username,
    )
    session.add(version)
    session.flush()

    if previous is not None:
        _copy_country_lines(session, previous.version_id, version.version_id,
                            actor=user.username)
        if previous.status not in TargetStatus.FROZEN:
            previous.status = TargetStatus.REVISED

    _make_current(session, plan_id, version)

    target_audit.record(
        session, action=target_audit.TargetAction.VERSION_CREATED,
        plan_id=plan_id, version_id=version.version_id,
        actor=user.username, actor_role=user.role,
        node_label=f"{plan.plan_code} · {plan.financial_year}",
        old_value=f"V{previous.version_no}" if previous else None,
        new_value=f"V{version.version_no}",
        reason=reason.strip(),
    )
    return version


def set_version_status(session: Session, user: UserContext, *, version_id: int,
                       new_status: str, reason: str | None = None) -> TargetVersion:
    """Move a version through the workflow, or refuse to.

    Every transition is checked against :data:`ALLOWED_TRANSITIONS`, including
    the ones this module drives itself. A status set from inside is no more
    trustworthy than one asked for over HTTP — the point of the table is that
    it cannot be reached out of order.

    Setting a version to the status it already has is accepted and does nothing.
    That is not laxity: a retried request must not fail differently from the one
    that succeeded, and a no-op is the only honest result.
    """
    version = get_version(session, version_id)
    if version.status == new_status:
        return version
    if new_status not in ALLOWED_TRANSITIONS.get(version.status, ()):
        raise InvalidTransition(version.status, new_status)

    previous = version.status
    version.status = new_status
    now = datetime.now(timezone.utc)
    if new_status == TargetStatus.UNDER_REVIEW:
        version.submitted_at = now
    elif new_status == TargetStatus.APPROVED:
        version.approved_at = now
    elif new_status == TargetStatus.LOCKED:
        version.locked_at = now

    # The plan carries the current version's status so a plan list can be drawn
    # without reading every version. Only the current version speaks for the
    # plan: a superseded V2 moving does not change what the plan is doing now.
    plan = get_plan(session, version.plan_id)
    if version.current_plan_id == plan.plan_id:
        plan.status = new_status

    session.flush()
    target_audit.record(
        session,
        action=_AUDIT_ACTION_FOR_STATUS.get(
            new_status, target_audit.TargetAction.TARGET_SUBMITTED),
        plan_id=plan.plan_id, version_id=version.version_id,
        actor=user.username, actor_role=user.role,
        node_label=f"{plan.plan_code} · V{version.version_no}",
        old_value=previous,
        # A lock names the batch it wrote under. Not decoration: the batch is
        # how the rows a version produced are found in ``fact_target``, and the
        # trail is where somebody asking "which lock produced these figures?"
        # looks first. Every other transition has nothing to add here.
        new_value=(f"{new_status} · batch {version.locked_batch_id}"
                   if new_status == TargetStatus.LOCKED
                   and version.locked_batch_id else new_status),
        reason=reason,
    )
    return version


def assert_editable(version: TargetVersion) -> None:
    """Refuse a write to a version that has been approved or locked.

    Called by every path that changes a number. Kept here rather than repeated
    at each call site so "an approved target is never overwritten" is one
    sentence of code, not a rule each new endpoint has to remember.
    """
    if version.status in TargetStatus.FROZEN:
        raise VersionFrozen(f"Version V{version.version_no}", version.status)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def deletion_blockers(session: Session, plan: TargetPlan) -> list[str]:
    """Why this plan may not be deleted, one sentence per reason.

    Read-only, so a screen can decide whether to offer the control at all
    rather than offering one that refuses — the rule the rest of this package
    follows.

    **A typed country target is not a blocker, deliberately.** Figures somebody
    entered and never allocated are a *draft* target: they have gone nowhere,
    nothing downstream reads them, and treating them as a record would mean a
    plan became undeletable the moment anybody typed into it. The distinction
    this package keeps is between a draft target and an allocated or approved
    one, and only the second is history.
    """
    reasons: list[str] = []

    versions = session.execute(
        select(TargetVersion).where(TargetVersion.plan_id == plan.plan_id)
    ).scalars().all()

    if plan.status != TargetStatus.DRAFT:
        reasons.append(
            f"Its status is {plan.status.replace('_', ' ').lower()}; only a "
            f"draft plan can be deleted.")

    moved = sorted({version.status for version in versions
                    if version.status != TargetStatus.DRAFT})
    if moved:
        reasons.append(
            f"A version of it is {', '.join(s.replace('_', ' ').lower() for s in moved)}. "
            f"Supersede it with a new version instead.")

    if any(version.locked_batch_id for version in versions):
        reasons.append(
            "It has been locked, so its figures are in fact_target and are "
            "what every report reads.")

    version_ids = [version.version_id for version in versions] or [0]
    allocated = session.execute(
        select(func.count(TargetAllocation.allocation_id))
        .where(TargetAllocation.version_id.in_(version_ids))
    ).scalar() or 0
    if allocated:
        reasons.append(
            f"It has {allocated:,} allocated rows. Re-run the allocation or "
            f"start a new version rather than erasing this one.")

    approvals = session.execute(
        select(func.count(TargetApproval.approval_id))
        .where(TargetApproval.version_id.in_(version_ids))
    ).scalar() or 0
    if approvals:
        reasons.append(
            f"{approvals} approval act{'s have' if approvals != 1 else ' has'} "
            f"been recorded against it, and an approval log is not something a "
            f"delete may take with it.")

    return reasons


def delete_plan(session: Session, user: UserContext, *, plan_id: int) -> dict:
    """Remove a plan that was created and abandoned. Refuses anything else.

    Deletion is deliberately narrow. The rest of this package supersedes rather
    than removes, because a target that has been allocated, put to an approver
    or locked has a history somebody may need to read. A plan that has been none
    of those is not a record — it is a form filled in by mistake, and leaving it
    in the list forever helps nobody.

    **The audit trail outlives the plan.** ``target_audit.plan_id`` and
    ``version_id`` are ``SET NULL``, so every entry this plan wrote survives the
    delete with its actor, its action and its reason intact; only the link goes.
    That is what makes the removal safe to offer at all, and it is why this
    needs no schema change.

    The versions are deleted **first and explicitly**: ``target_version.plan_id``
    is ``RESTRICT``, on purpose, so nothing can remove a plan and silently take
    its versions with it. Their own children — country lines, allocations,
    revisions, adjustments, jobs — are ``CASCADE`` and go with them.
    """
    plan = get_plan(session, plan_id)
    if (reasons := deletion_blockers(session, plan)):
        raise PlanNotDeletable(plan.plan_code, reasons)

    versions = session.execute(
        select(TargetVersion).where(TargetVersion.plan_id == plan.plan_id)
    ).scalars().all()
    lines = session.execute(
        select(func.count(TargetCountryLine.line_id))
        .where(TargetCountryLine.version_id.in_(
            [version.version_id for version in versions] or [0]))
    ).scalar() or 0

    # Written *before* the rows go, so the entry exists even though its foreign
    # keys are about to be nulled. The trail is the whole record afterwards.
    target_audit.record(
        session, action=target_audit.TargetAction.PLAN_DELETED,
        plan_id=plan.plan_id, actor=user.username, actor_role=user.role,
        node_label=plan.plan_code,
        old_value=f"{plan.status} · {len(versions)} version(s) · "
                  f"{lines} country line(s)",
        new_value=None,
        reason="Draft plan deleted before it was allocated or approved.",
    )
    session.flush()

    for version in versions:
        # Released first: ``uq_target_version_current`` is checked on the flush,
        # and a delete that left the claim standing would collide with nothing
        # useful while being harder to read in a failure.
        version.current_plan_id = None
    session.flush()
    for version in versions:
        session.delete(version)
    session.flush()
    session.delete(plan)
    session.flush()

    return {
        "plan_code": plan.plan_code,
        "versions_removed": len(versions),
        "country_lines_removed": lines,
    }


def plan_to_dict(plan: TargetPlan, current: TargetVersion | None = None) -> dict:
    return {
        "plan_id": plan.plan_id,
        "plan_code": plan.plan_code,
        "financial_year": plan.financial_year,
        "target_period": plan.target_period,
        "period_label": period_label(plan.financial_year, plan.target_period),
        "company_code": plan.company_code,
        "bu_code": plan.bu_code,
        "sales_line_code": plan.sales_line_code,
        "basis_financial_years": plan.basis_financial_years,
        "status": plan.status,
        "created_by": plan.created_by,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "current_version_id": current.version_id if current else None,
        "current_version_no": current.version_no if current else None,
        "current_version_status": current.status if current else None,
    }


def version_to_dict(version: TargetVersion, line_count: int | None = None) -> dict:
    return {
        "version_id": version.version_id,
        "plan_id": version.plan_id,
        "version_no": version.version_no,
        "label": f"V{version.version_no}",
        "status": version.status,
        "is_current": version.current_plan_id is not None,
        "reason": version.reason,
        "created_by": version.created_by,
        "created_at": version.created_at.isoformat() if version.created_at else None,
        "submitted_at": version.submitted_at.isoformat() if version.submitted_at else None,
        "approved_at": version.approved_at.isoformat() if version.approved_at else None,
        "locked_at": version.locked_at.isoformat() if version.locked_at else None,
        "locked_batch_id": version.locked_batch_id,
        "country_line_count": line_count,
    }


#: Month abbreviations, indexed 0-11. Only ever used to *label* a derived
#: period; nothing is computed from this tuple.
_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def period_months_label(target_period: str) -> str:
    """``Q1 (Jul – Sep)`` — the months that period actually covers.

    Derived from the configured financial-year start month on every call rather
    than stored, because the financial year is configuration: a deployment that
    starts its year in January must read Q1 as January to March, and a stored
    label would go on saying July.
    """
    if target_period == "FY":
        return "FY (full year)"
    try:
        quarter = int(target_period[1:])
    except (ValueError, IndexError):
        return target_period
    config = FinancialYearConfig.from_settings()
    first = (config.start_month - 1 + (quarter - 1) * 3) % 12
    last = (first + 2) % 12
    return f"{target_period} ({_MONTH_NAMES[first]} – {_MONTH_NAMES[last]})"


def period_label(financial_year: str, target_period: str) -> str:
    """``FY 2026-27 · Q1 (Jul – Sep)`` — a plan's period, spelled in full."""
    if target_period == "FY":
        return financial_year
    return f"{financial_year} · {period_months_label(target_period)}"


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

#: Which business action a status change is recorded as. Statuses not named here
#: are ordinary submissions and fall back to ``TARGET_SUBMITTED``.
_AUDIT_ACTION_FOR_STATUS = {
    TargetStatus.UNDER_REVIEW: target_audit.TargetAction.TARGET_SUBMITTED,
    TargetStatus.APPROVED: target_audit.TargetAction.APPROVED,
    TargetStatus.REJECTED: target_audit.TargetAction.REJECTED,
    TargetStatus.LOCKED: target_audit.TargetAction.TARGET_LOCKED,
    TargetStatus.ALLOCATED: target_audit.TargetAction.ALLOCATION_GENERATED,
}


def _make_current(session: Session, plan_id: int,
                  version: TargetVersion) -> None:
    """Hand ``current`` from the incumbent to ``version``.

    Released and flushed **before** claimed. ``uq_target_version_current`` is
    checked on the flush, so claiming first would collide with the row we are
    about to release — a constraint doing exactly its job, on a state that was
    never meant to exist.
    """
    incumbent = current_version(session, plan_id)
    if incumbent is not None and incumbent.version_id != version.version_id:
        incumbent.current_plan_id = None
        session.flush()
    version.current_plan_id = plan_id
    session.flush()


def _copy_country_lines(session: Session, source_version_id: int,
                        target_version_id: int, *, actor: str | None) -> int:
    """Copy one version's typed country volumes onto the next. Returns the count."""
    rows = session.execute(
        select(TargetCountryLine).where(
            TargetCountryLine.version_id == source_version_id)
    ).scalars().all()
    for row in rows:
        session.add(TargetCountryLine(
            version_id=target_version_id,
            material_code=row.material_code,
            target_volume=row.target_volume,
            updated_by=actor,
        ))
    session.flush()
    return len(rows)


def _current_versions(session: Session,
                      plan_ids: list[int]) -> dict[int, TargetVersion]:
    if not plan_ids:
        return {}
    rows = session.execute(
        select(TargetVersion).where(TargetVersion.current_plan_id.in_(plan_ids))
    ).scalars().all()
    return {row.current_plan_id: row for row in rows if row.current_plan_id}


def _line_counts(session: Session, version_ids: list[int]) -> dict[int, int]:
    if not version_ids:
        return {}
    rows = session.execute(
        select(TargetCountryLine.version_id, func.count())
        .where(TargetCountryLine.version_id.in_(version_ids))
        .group_by(TargetCountryLine.version_id)
    ).all()
    return {version_id: count for version_id, count in rows}


def scope_options(session: Session) -> dict[str, Any]:
    """What the plan form may choose from, read from the master data.

    Derived rather than declared, the way every other registry in this project
    is: a company added to the master appears here with no change to this module
    and none in the browser, and one retired stops being offered.
    """
    companies = session.execute(
        select(DimCompany.company_code, DimCompany.company_name)
        .where(DimCompany.is_deleted.is_(False))
        .order_by(DimCompany.company_code)
    ).all()
    units = session.execute(
        select(DimBusinessUnit.bu_code, DimBusinessUnit.bu_name,
               DimBusinessUnit.company_code)
        .where(DimBusinessUnit.is_deleted.is_(False))
        .order_by(DimBusinessUnit.bu_code)
    ).all()
    lines = session.execute(
        select(DimSalesLine.sales_line_code, DimSalesLine.sales_line_name,
               DimSalesLine.bu_code)
        .where(DimSalesLine.is_deleted.is_(False))
        .order_by(DimSalesLine.sales_line_code)
    ).all()
    return {
        "companies": [{"code": c, "name": n} for c, n in companies],
        "business_units": [
            {"code": c, "name": n, "company_code": parent} for c, n, parent in units
        ],
        "sales_lines": [
            {"code": c, "name": n, "bu_code": parent} for c, n, parent in lines
        ],
        "periods": [
            {"code": period, "label": period_months_label(period)}
            for period in TARGET_PERIODS
        ],
        "financial_years": financial_year_options(session),
        "statuses": list(TargetStatus.ALL),
    }


def financial_year_options(session: Session) -> list[str]:
    """Financial years a plan may be created for, newest first.

    Computed from the configured calendar rather than listed anywhere: the
    financial year is configuration, and a hard-coded list would go on offering
    July-start years to a deployment that starts its year in January.

    Three years around today — last, this and next — because planning is
    forward-looking but a plan is sometimes built for a year already under way.
    Unioned with every year that already has a plan, so an existing plan's year
    never disappears from the control that would let you find it.
    """
    config = FinancialYearConfig.from_settings()
    today = datetime.now(timezone.utc).date()
    # The 1st of the month, not today's day-of-month: 29 February does not exist
    # in the year either side of a leap year, and ``replace`` would raise on it.
    years = {
        config.label(date(today.year + offset, today.month, 1))
        for offset in (-1, 0, 1)
    }
    years.update(
        year for (year,) in session.execute(select(TargetPlan.financial_year).distinct())
        if year
    )
    return sorted(years, reverse=True)


__all__ = [
    "TARGET_PERIODS",
    "ALLOWED_TRANSITIONS",
    "PlanScope",
    "validate_scope",
    "next_plan_code",
    "create_plan",
    "list_plans",
    "get_plan",
    "list_versions",
    "current_version",
    "get_version",
    "create_version",
    "set_version_status",
    "assert_editable",
    "plan_to_dict",
    "version_to_dict",
    "period_label",
    "period_months_label",
    "financial_year_options",
    "scope_options",
    "deletion_blockers",
    "delete_plan",
]
