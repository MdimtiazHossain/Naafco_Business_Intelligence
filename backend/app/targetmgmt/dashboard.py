"""The Target Management dashboard: where every plan has got to, and what is stuck.

Read-only, and deliberately narrow about what it claims. Everything here is
either a **count of workflow state** or a **figure somebody typed** — never a
scoped aggregate, because a single headline volume would mean something
different to every reader and there would be no honest label for it.

**Nothing on this screen is a second opinion about a number.** The country
target comes from ``target_country_line``, which is what a person entered; the
allocated total comes from the stored allocation; achievement against a locked
target is the Target *page's* question and is deliberately absent here. A
dashboard that recomputed a figure another screen already reports is how two
screens come to disagree.

**Scope narrows the plan list on the three levels a plan states — company,
business unit, sales line — and nothing else.** A regional manager holds none of
those, so they see every plan, exactly as :func:`plans.list_plans` says. Their
scope binds where their *targets* are read, which is the review tree. This is
the same rule, delegated rather than restated.

**A count of zero is a measurement; an unknown is not a count.** A plan with no
country target has ``country_volume`` of ``None`` rather than 0 — nobody has
typed one — and the screen renders that as ``n/a``. A plan whose country target
is genuinely zero is a real, if unusual, instruction and reads as ``0``.

**"Stuck" is defined, not implied.** A plan is only ever reported as needing
attention for a stated reason — allocated but never submitted, under review with
nobody's turn open, approved but not locked — and the reason travels with it, so
the screen never shows a red count a reader cannot act on.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..database.models_target import (
    AllocationJobStatus,
    TargetAllocation,
    TargetAllocationJob,
    TargetCountryLine,
    TargetLevel,
    TargetPlan,
    TargetRevision,
    TargetStatus,
    TargetVersion,
    RevisionStatus,
)
from . import approvals as approval_service
from . import audit as target_audit
from . import plans as plan_service

#: Statuses grouped for the strip along the top. The groups are stages of one
#: journey rather than arbitrary buckets, which is why a plan appears in exactly
#: one of them and the four counts add up to the total.
STAGE_DRAFTING = "drafting"
STAGE_ALLOCATED = "allocated"
STAGE_IN_APPROVAL = "in_approval"
STAGE_LOCKED = "locked"

#: **Every status appears exactly once**, which is what makes the four counts add
#: up to the plans behind them. ``REVISED`` sits with the drafting stage: it
#: marks a version a newer one replaced, so a *current* version should never
#: hold it — but a status silently matching no group would drop its plan out of
#: the strip while leaving it in the list, and a headline that quietly fails to
#: add up is worse than one that files an odd case under the least wrong
#: heading. Of the four, drafting is that heading: not allocated, not in
#: approval, not locked.
STAGES: dict[str, tuple[str, ...]] = {
    STAGE_DRAFTING: (TargetStatus.DRAFT, TargetStatus.ALLOCATION_IN_PROGRESS,
                     TargetStatus.REJECTED, TargetStatus.REVISED),
    STAGE_ALLOCATED: (TargetStatus.ALLOCATED,),
    STAGE_IN_APPROVAL: (TargetStatus.UNDER_REVIEW,
                        TargetStatus.PARTIALLY_APPROVED,
                        TargetStatus.APPROVED),
    STAGE_LOCKED: (TargetStatus.LOCKED,),
}

#: What a plan can be waiting on, and the sentence that says so. Declared here
#: rather than built in a conditional chain so the screen and the tests read the
#: same list, and so adding one is a line rather than a branch.
ATTENTION_REASONS: dict[str, str] = {
    "not_allocated":
        "Allocated nothing yet — the country target is set but the engine has "
        "not been run.",
    "not_submitted":
        "Allocated but never submitted, so nobody has been asked to approve it.",
    "open_revisions":
        "Revision requests are still open, and final approval waits on them.",
    "approved_not_locked":
        "Approved but not locked, so the agreed figures are not yet the target "
        "anybody reports against.",
    "last_run_failed":
        "The last allocation run did not complete.",
}


def _current_versions(session: Session,
                      plan_ids: list[int]) -> dict[int, TargetVersion]:
    if not plan_ids:
        return {}
    rows = session.execute(
        select(TargetVersion).where(TargetVersion.current_plan_id.in_(plan_ids))
    ).scalars().all()
    return {row.plan_id: row for row in rows}


def _country_volumes(session: Session,
                     version_ids: list[int]) -> dict[int, Decimal | None]:
    """Typed country volume per version, or ``None`` where no line exists.

    ``None`` rather than zero, and the distinction is the point: nobody having
    typed a target is not the same as somebody having typed nothing.
    """
    if not version_ids:
        return {}
    rows = session.execute(
        select(TargetCountryLine.version_id,
               func.sum(TargetCountryLine.target_volume))
        .where(TargetCountryLine.version_id.in_(version_ids))
        .group_by(TargetCountryLine.version_id)
    ).all()
    return {version_id: (None if total is None else Decimal(str(total)))
            for version_id, total in rows}


def _allocated_volumes(session: Session,
                       version_ids: list[int]) -> dict[int, Decimal]:
    """Allocated volume per version, taken from the **root** of each tree.

    Summing every allocation row would add each figure once per level it appears
    at and report a target several times over. The root is the one row whose
    total is the whole tree, which is exactly what reconciliation guarantees.
    """
    if not version_ids:
        return {}
    rows = session.execute(
        select(TargetAllocation.version_id,
               func.sum(TargetAllocation.current_volume))
        .where(TargetAllocation.version_id.in_(version_ids),
               TargetAllocation.parent_code.is_(None))
        .group_by(TargetAllocation.version_id)
    ).all()
    return {version_id: Decimal(str(total or 0)) for version_id, total in rows}


def _open_revisions(session: Session,
                    version_ids: list[int]) -> dict[int, int]:
    if not version_ids:
        return {}
    rows = session.execute(
        select(TargetRevision.version_id,
               func.count(TargetRevision.revision_id))
        .where(TargetRevision.version_id.in_(version_ids),
               TargetRevision.status.in_(RevisionStatus.OPEN))
        .group_by(TargetRevision.version_id)
    ).all()
    return {version_id: int(count) for version_id, count in rows}


def _last_jobs(session: Session,
               version_ids: list[int]) -> dict[int, TargetAllocationJob]:
    if not version_ids:
        return {}
    rows = session.execute(
        select(TargetAllocationJob)
        .where(TargetAllocationJob.version_id.in_(version_ids))
        .order_by(TargetAllocationJob.job_id)
    ).scalars().all()
    return {row.version_id: row for row in rows}


def _attention(version: TargetVersion, *, allocated: Decimal | None,
               open_revisions: int, job: TargetAllocationJob | None,
               country: Decimal | None) -> list[str]:
    """Why this plan needs somebody, as a list of declared reasons.

    A plan with nothing wrong returns an empty list, which is what lets the
    screen show a count a reader can act on rather than a red number that
    turns out to mean nothing when they open it.
    """
    reasons: list[str] = []
    if version.status in (TargetStatus.DRAFT, TargetStatus.REJECTED) \
            and country is not None and not allocated:
        reasons.append("not_allocated")
    if version.status == TargetStatus.ALLOCATED:
        reasons.append("not_submitted")
    if open_revisions:
        reasons.append("open_revisions")
    if version.status == TargetStatus.APPROVED:
        reasons.append("approved_not_locked")
    if job is not None and job.status in (AllocationJobStatus.FAILED,
                                          AllocationJobStatus.CANCELLED):
        reasons.append("last_run_failed")
    return reasons


def summary(session: Session, user: UserContext, *,
            financial_year: str | None = None,
            limit: int = 12) -> dict[str, Any]:
    """Everything the dashboard draws, in one call.

    One call because every panel is a different view of the same small set of
    plans, and four round trips would let them disagree with each other while
    the page was still loading.
    """
    rows, total = plan_service.list_plans(
        session, user, financial_year=financial_year, limit=200)
    plan_ids = [row["plan_id"] for row in rows]

    plans_by_id = {
        plan.plan_id: plan for plan in session.execute(
            select(TargetPlan).where(TargetPlan.plan_id.in_(plan_ids or [0]))
        ).scalars()
    }
    current = _current_versions(session, plan_ids)
    version_ids = [version.version_id for version in current.values()]
    country = _country_volumes(session, version_ids)
    allocated = _allocated_volumes(session, version_ids)
    open_revisions = _open_revisions(session, version_ids)
    jobs = _last_jobs(session, version_ids)

    stages = {name: 0 for name in STAGES}
    entries: list[dict[str, Any]] = []
    for plan_id in plan_ids:
        plan = plans_by_id.get(plan_id)
        version = current.get(plan_id)
        if plan is None or version is None:
            continue
        for name, statuses in STAGES.items():
            if version.status in statuses:
                stages[name] += 1
                break
        else:  # pragma: no cover - unreachable while STAGES covers every status
            # A status nothing groups would leave this plan in the list and out
            # of the strip. Counted rather than dropped, and the test below
            # pins that STAGES stays exhaustive so this stays unreachable.
            stages[STAGE_DRAFTING] += 1

        country_volume = country.get(version.version_id)
        allocated_volume = allocated.get(version.version_id)
        reasons = _attention(
            version, allocated=allocated_volume,
            open_revisions=open_revisions.get(version.version_id, 0),
            job=jobs.get(version.version_id), country=country_volume)
        entries.append({
            "plan": plan_service.plan_to_dict(plan, version),
            "version": plan_service.version_to_dict(version),
            "country_volume": (None if country_volume is None
                               else float(country_volume)),
            "allocated_volume": (None if allocated_volume is None
                                 else float(allocated_volume)),
            #: How much of the typed target the allocation actually placed.
            #: ``None`` when there is no country target to measure against —
            #: never 0%, which would read as "allocated nothing".
            "allocated_percent": (
                None if not country_volume
                else float((allocated_volume or Decimal(0))
                           / country_volume * 100)),
            "open_revisions": open_revisions.get(version.version_id, 0),
            "attention": reasons,
            "locked_at": (version.locked_at.isoformat()
                          if version.locked_at else None),
        })

    queue = approval_service.queue(session, user)
    return {
        "stages": stages,
        "plan_count": total,
        "financial_year": financial_year,
        "financial_years": plan_service.financial_year_options(session),
        "plans": entries[:limit],
        "attention": [entry for entry in entries if entry["attention"]][:limit],
        "attention_reasons": ATTENTION_REASONS,
        "my_queue": {
            "versions": len([row for row in queue["versions"]
                             if row["is_my_turn"]]),
            "revisions": len(queue["revisions"]),
            "notes": queue["notes"],
            "role": queue["role"],
        },
        "recent": target_audit.trail(session, limit=8)["rows"],
        "levels": list(TargetLevel.ORDERED),
    }


__all__ = ["STAGES", "ATTENTION_REASONS", "summary"]
