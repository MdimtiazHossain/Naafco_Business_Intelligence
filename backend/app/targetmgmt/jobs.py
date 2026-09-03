"""Running an allocation in the background, and reporting where it got to.

An allocation writes tens of thousands of rows and reads two years of sales to
decide what they should be. Doing that inside a request would hold a worker for
the whole run, and on a full-year plan across a real customer base it is not a
slow request — it is a request that never comes back.

So the endpoint records a job, hands it to a worker and answers **202**; the
browser polls :func:`state`. That is the same shape the upload centre uses, and
for the same reasons — a thread rather than a broker because everything below
here is synchronous SQLAlchemy, and one worker by default because SQLite permits
a single writer and a second concurrent run would block invisibly inside the
database rather than queue visibly here.

**What the worker owns.** Its own ``Session``, opened from the engine and closed
when it finishes. The request's session is long gone by then. If the worker
raises, the job is marked ``FAILED`` through a *second*, clean session, because
the first one is the one that just broke.

**Why progress is durable here and in memory for an upload.** The expensive half
of an allocation is pure reading, so a progress write holds no lock and blocks
nobody; the ETL's is one long write transaction, where the same write would be
invisible until commit and would block on SQLite besides. The consequence is a
better guarantee: allocation progress survives a page reload and a process
restart, where an in-flight upload's degrades to "no detail".

The engine is reached through ``connection.get_engine()`` rather than a captured
binding, so redirecting the connection module — which is how the tests point at
a throwaway warehouse — redirects the workers too.
"""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..config import get_settings
from ..database import connection
from ..database.models_target import (
    AllocationJobStatus,
    TargetAllocationJob,
    TargetLevel,
    TargetPlan,
    TargetStatus,
    TargetVersion,
)
from . import audit as target_audit
from . import (
    country,
    engine as allocation_engine,
    factors,
    plans,
    readiness,
    reconcile,
)
from .errors import TargetManagementError

logger = logging.getLogger("app.targetmgmt.jobs")

_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()

#: Roughly what fraction of a run each stage represents.
#:
#: Declared rather than derived from row counts, because the stages are not
#: comparable in units — "read two years of sales" and "write 40,000 rows" have
#: no common denominator. These are the proportions a planner actually
#: experiences, and a percentage that moves smoothly is worth more than one that
#: is arithmetically defensible and jumps from 5% to 95%.
STAGE_WEIGHT: dict[str, tuple[int, int]] = {
    "HISTORICAL_ANALYSIS": (0, 20),
    "MATERIAL_ALLOCATION": (20, 30),
    "MONTHLY_ALLOCATION": (30, 45),
    "CUSTOMER_ALLOCATION": (45, 85),
    "RECONCILIATION": (85, 95),
    "FINALIZATION": (95, 100),
}


def get_executor() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=max(1, get_settings().target_allocation_worker_count),
                thread_name_prefix="target-allocation",
            )
        return _executor


def shutdown(wait: bool = False) -> None:
    """Stop accepting allocation work on the way down.

    Not waited on by default: a running allocation holds an uncommitted
    transaction only during its final persist, so letting the process exit rolls
    that back — which is the right outcome for a run that was interrupted, and
    the same one blocking would reach more slowly. :func:`sweep_interrupted`
    closes the job row on the way back up.
    """
    global _executor
    with _lock:
        if _executor is not None:
            _executor.shutdown(wait=wait)
            _executor = None


def submit(session: Session, user: UserContext, *, plan: TargetPlan,
           version: TargetVersion,
           settings: factors.FactorSettings) -> TargetAllocationJob:
    """Record a job, move the version, and hand the work to a worker.

    The version moves to ``ALLOCATION_IN_PROGRESS`` here rather than in the
    worker, and that ordering matters: the transition is validated against the
    workflow table, so a version that may not be allocated is refused *before* a
    thread is started and while the caller still has a request to answer.
    """
    plans.assert_editable(version)

    # The size check happens **here**, before the version moves and before a
    # thread is started. A run refused halfway through leaves a planner watching
    # a progress bar for something that was never going to finish, and leaves
    # the version parked in ALLOCATION_IN_PROGRESS until the worker unwinds it.
    # Refusing in the request keeps the existing version untouched and usable,
    # which is what the caller asked for.
    lines = country.list_lines(session, version.version_id)
    priced = [line for line in lines if line.target_volume]
    level, _ = readiness.deepest_reachable_level(session)
    projection = readiness.project(session, plan=plan, version=version,
                                   level=level, material_count=len(priced))
    if projection["exceeds"]:
        raise allocation_engine.AllocationTooLarge(projection)

    plans.set_version_status(session, user, version_id=version.version_id,
                             new_status=TargetStatus.ALLOCATION_IN_PROGRESS)

    job = TargetAllocationJob(
        job_uuid=str(uuid.uuid4()),
        plan_id=plan.plan_id,
        version_id=version.version_id,
        status=AllocationJobStatus.QUEUED,
        current_stage=None,
        progress_percent=0,
        rows_processed=0,
        settings=settings.to_dict(),
        projected_rows=projection["projected_rows"],
        allocation_level=level,
        requested_by=user.username,
    )
    session.add(job)
    session.flush()

    target_audit.record(
        session, action=target_audit.TargetAction.ALLOCATION_GENERATED,
        plan_id=plan.plan_id, version_id=version.version_id,
        actor=user.username, actor_role=user.role,
        node_label=f"{plan.plan_code} · V{version.version_no}",
        old_value=None, new_value="Allocation requested",
    )

    job_uuid = job.job_uuid
    version_id = version.version_id
    user_copy = UserContext(
        user_id=user.user_id, username=user.username, role=user.role,
        display_name=user.display_name, data_scope=dict(user.data_scope),
    )
    # Submitted after the caller commits, not here: a worker that opened its own
    # session before this transaction committed would not see the job row it is
    # meant to work on. ``run_after_commit`` is called by the route.
    session.info.setdefault("_pending_allocations", []).append(
        (job_uuid, version_id, user_copy, settings)
    )
    return job


def run_after_commit(session: Session) -> None:
    """Start the workers for jobs recorded in this session. Called post-commit.

    Separated from :func:`submit` so the thread never races the transaction that
    created its job row — the classic version of this bug is a worker that
    reports "job not found" on a fast machine and works fine on a slow one.
    """
    pending = session.info.pop("_pending_allocations", [])
    for job_uuid, version_id, user, settings in pending:
        get_executor().submit(_guarded, job_uuid, version_id, user, settings)


def _guarded(job_uuid: str, version_id: int, user: UserContext,
             settings: factors.FactorSettings) -> None:
    """Run one allocation, and make sure the job row never lies about it."""
    try:
        _run(job_uuid, version_id, user, settings)
    except TargetManagementError as exc:
        logger.info("allocation %s refused: %s", job_uuid, exc.user_message)
        _mark_failed(job_uuid, exc.user_message)
    except Exception as exc:  # noqa: BLE001 - a worker must never die silently
        logger.exception("allocation %s failed", job_uuid)
        _mark_failed(
            job_uuid,
            "The allocation stopped unexpectedly. Nothing was written; the "
            "target is unchanged and the run can be started again.",
            internal=str(exc),
        )


def _run(job_uuid: str, version_id: int, user: UserContext,
         settings: factors.FactorSettings) -> None:
    with Session(connection.get_engine(), expire_on_commit=False) as session:
        job = _load(session, job_uuid)
        if job is None:
            logger.warning("allocation job %s vanished before it started", job_uuid)
            return

        version = plans.get_version(session, version_id)
        plan = plans.get_plan(session, version.plan_id)

        job.status = AllocationJobStatus.PROCESSING
        job.started_at = datetime.now(timezone.utc)
        job.current_stage = allocation_engine.STAGE_KEYS[0]
        session.commit()

        def report(stage: str, done: int, total: int) -> None:
            """Write progress straight to the job row.

            Safe *because* the compute phase holds no write lock — see the
            module docstring. Each call is its own short transaction, so a poll
            on another connection sees it immediately.
            """
            low, high = STAGE_WEIGHT.get(stage, (0, 100))
            fraction = (done / total) if total else 0
            job.current_stage = stage
            job.progress_percent = int(low + (high - low) * min(1.0, fraction))
            session.commit()

        result = allocation_engine.plan_allocation(
            session, user, plan=plan, version=version, settings=settings,
            progress=report,
        )

        if not result.allocatable:
            # The honest stop. Not a failure, and not an allocation.
            job.status = AllocationJobStatus.NO_HISTORY
            job.current_stage = allocation_engine.STAGE_KEYS[0]
            job.progress_percent = 100
            job.total_rows = 0
            job.sales_rows_found = result.sales_rows_found
            job.allocation_level = result.allocation_level
            job.completed_at = datetime.now(timezone.utc)
            job.error_message = result.reason
            job.result = _no_history_result(session, result, plan)
            plans.set_version_status(
                session, user, version_id=version.version_id,
                new_status=TargetStatus.DRAFT,
                reason="Allocation found no sales history to allocate by.")
            session.commit()
            return

        job.total_rows = result.row_count
        job.current_stage = "FINALIZATION"
        job.progress_percent = STAGE_WEIGHT["FINALIZATION"][0]
        session.commit()

        written = allocation_engine.persist(session, version=version,
                                            result=result)

        country_target = {
            line.material_code: _decimal(line.target_volume)
            for line in country.list_lines(session, version.version_id)
            if line.target_volume
        }
        verdict = reconcile.check(session, version_id=version.version_id,
                                  country_target=country_target)

        if not verdict.balanced:
            # Refuse to keep an allocation that does not add up. Rolling back
            # leaves the previous allocation — or none — intact, which is a
            # state a planner can act on; a stored unbalanced one is not.
            session.rollback()
            _mark_failed(
                job_uuid,
                "The generated allocation did not reconcile, so nothing was "
                "written. The target is unchanged.",
                internal=str(verdict.to_dict()),
            )
            return

        warnings = list(result.warnings)
        if result.allocation_level != TargetLevel.CUSTOMER:
            # Not a failure, and not a detail. A planner who asked for a
            # customer-level target and received a sub-territory one has to be
            # told, in the run's own status rather than in a footnote.
            warnings.append(
                f"Allocation reached {result.allocation_level.replace('_', ' ')} "
                f"level, not customer. The hierarchy below it is not available "
                f"in the master data."
            )
        if result.equal_split_nodes:
            warnings.append(
                f"{len(result.equal_split_nodes)} node(s) were split evenly "
                f"because no factor could distinguish their children."
            )

        job.rows_processed = written
        job.progress_percent = 100
        job.warning_count = len(warnings)
        job.allocation_level = result.allocation_level
        job.sales_rows_found = result.sales_rows_found
        job.status = (AllocationJobStatus.COMPLETED_WITH_WARNINGS if warnings
                      else AllocationJobStatus.COMPLETED)
        job.completed_at = datetime.now(timezone.utc)
        job.result = _completed_result(result, verdict, warnings)

        plans.set_version_status(session, user, version_id=version.version_id,
                                 new_status=TargetStatus.ALLOCATED)
        target_audit.record(
            session, action=target_audit.TargetAction.ALLOCATION_GENERATED,
            plan_id=plan.plan_id, version_id=version.version_id,
            actor=user.username, actor_role=user.role,
            node_label=f"{plan.plan_code} · V{version.version_no}",
            old_value=None,
            new_value=f"{written:,} rows across {result.customer_count:,} customers",
        )
        session.commit()


def _completed_result(result: allocation_engine.AllocationPlan,
                      verdict: reconcile.Reconciliation,
                      warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "reconciliation": verdict.to_dict(),
        "allocation_level": result.allocation_level,
        "adjustments": result.adjustments,
        "sales_rows_found": result.sales_rows_found,
        "months": result.months,
        "materials": result.materials,
        "material_count": len(result.materials),
        "node_count": result.node_count,
        "customer_count": result.customer_count,
        "row_count": result.row_count,
        "seasonality": result.seasonality,
        "factors": result.factor_availability,
        "equal_split_nodes": result.equal_split_nodes,
        "warnings": warnings if warnings is not None else result.warnings,
    }


def _no_history_result(session: Session,
                       result: allocation_engine.AllocationPlan,
                       plan: TargetPlan) -> dict[str, Any]:
    """What the screen shows instead of a completed allocation.

    Deliberately shaped as an explanation rather than an empty success: the
    rows found, the period checked, and which factors could not be calculated.
    A planner should close this screen understanding that the engine is waiting
    for transactional data, not that it ran and produced nothing.
    """
    from . import history as history_module

    return {
        "reconciliation": None,
        "sales_rows_found": result.sales_rows_found,
        "allocation_level": result.allocation_level,
        "adjustments": [],
        "basis_years": history_module.basis_years(plan),
        "months": result.months,
        "materials": result.materials,
        "material_count": len(result.materials),
        "node_count": result.node_count,
        "customer_count": result.customer_count,
        "row_count": 0,
        "factors": result.factor_availability,
        "warnings": [],
    }


def _mark_failed(job_uuid: str, message: str, *,
                 internal: str | None = None) -> None:
    """Record a failure through a clean session, and free the version.

    A second session on purpose: the one that raised may be in a state where
    nothing further can be written, and a job stuck at ``PROCESSING`` forever is
    worse than a job that says it failed.

    The version is returned to ``DRAFT`` so it is workable again. Leaving it at
    ``ALLOCATION_IN_PROGRESS`` would strand it in a state nothing can act on and
    no screen can leave.
    """
    try:
        with Session(connection.get_engine(), expire_on_commit=False) as session:
            job = _load(session, job_uuid)
            if job is None:
                return
            job.status = AllocationJobStatus.FAILED
            job.completed_at = datetime.now(timezone.utc)
            job.error_count = job.error_count + 1
            job.error_message = message
            if internal:
                logger.error("allocation %s: %s", job_uuid, internal)

            version = session.get(TargetVersion, job.version_id)
            if version is not None and version.status == \
                    TargetStatus.ALLOCATION_IN_PROGRESS:
                version.status = TargetStatus.DRAFT
                plan = session.get(TargetPlan, version.plan_id)
                if plan is not None and version.current_plan_id == plan.plan_id:
                    plan.status = TargetStatus.DRAFT
            session.commit()
    except Exception:  # noqa: BLE001 - reporting a failure must not raise
        logger.exception("could not record failure for allocation %s", job_uuid)


def _load(session: Session, job_uuid: str) -> TargetAllocationJob | None:
    return session.execute(
        select(TargetAllocationJob).where(
            TargetAllocationJob.job_uuid == job_uuid)
    ).scalar_one_or_none()


def state(session: Session, job_uuid: str) -> dict[str, Any] | None:
    """One job as the browser polls it."""
    job = _load(session, job_uuid)
    return to_dict(job) if job is not None else None


def latest_for_version(session: Session,
                       version_id: int) -> TargetAllocationJob | None:
    return session.execute(
        select(TargetAllocationJob)
        .where(TargetAllocationJob.version_id == version_id)
        .order_by(TargetAllocationJob.job_id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _summary(job: TargetAllocationJob) -> dict[str, Any]:
    """What one finished run came to, in the terms a planner asks about.

    Composed from the columns the job already carries rather than from new
    ones — none of these needed storing, because each is either a figure the
    run recorded or a difference between two timestamps.

    **Generated and saved are the same number, and both are reported anyway.**
    ``persist`` writes every row the engine produced or raises; it never keeps
    part of a run. Showing one figure would leave a reader wondering whether the
    other differed, and showing them equal says plainly that nothing was lost on
    the way to the database.

    **Duplicates rejected is always zero, and that is a fact about the schema
    rather than about this run.** ``uq_target_allocation_node`` covers the full
    grain — version, level, node, material, month — and the version's previous
    rows are deleted before the insert, so a duplicate cannot be written and is
    never silently discarded. The line is kept because "0 duplicates" and "we
    did not look" are different claims.
    """
    seconds = None
    if job.started_at and job.completed_at:
        seconds = round((job.completed_at - job.started_at).total_seconds(), 1)

    generated = job.rows_processed or 0
    return {
        "rows_generated": generated,
        #: Equal to ``rows_generated`` by construction; see the docstring.
        "rows_saved": generated,
        "duplicates_rejected": 0,
        "validation_failures": job.error_count or 0,
        "warnings": job.warning_count or 0,
        "projected_rows": job.projected_rows,
        "allocation_level": job.allocation_level,
        "sales_rows_found": job.sales_rows_found,
        "processing_seconds": seconds,
    }


def to_dict(job: TargetAllocationJob) -> dict[str, Any]:
    return {
        "job_id": job.job_uuid,
        "plan_id": job.plan_id,
        "version_id": job.version_id,
        "status": job.status,
        "current_stage": job.current_stage,
        "current_stage_label": allocation_engine.STAGE_LABEL.get(
            job.current_stage or "", None),
        "stages": [
            {"key": key, "label": label} for key, label in allocation_engine.STAGES
        ],
        "progress_percent": job.progress_percent,
        "rows_processed": job.rows_processed,
        "total_rows": job.total_rows,
        "projected_rows": job.projected_rows,
        "allocation_level": job.allocation_level,
        "warning_count": job.warning_count,
        "sales_rows_found": job.sales_rows_found,
        "error_count": job.error_count,
        "error_message": job.error_message,
        "settings": job.settings,
        "result": job.result,
        "requested_by": job.requested_by,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "summary": _summary(job),
    }


def history(session: Session, *, version_id: int | None = None,
            plan_id: int | None = None, limit: int = 50,
            offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Every allocation run, newest first.

    A run is a record of work, so a failed one is as worth keeping as a
    successful one — the question a planner brings to this list is usually "why
    did the last attempt not work", and a list that showed only successes could
    not answer it.
    """
    from sqlalchemy import func as sa_func

    conditions = []
    if version_id is not None:
        conditions.append(TargetAllocationJob.version_id == version_id)
    if plan_id is not None:
        conditions.append(TargetAllocationJob.plan_id == plan_id)

    total = session.execute(
        select(sa_func.count()).select_from(TargetAllocationJob)
        .where(*conditions)
    ).scalar_one()

    rows = session.execute(
        select(TargetAllocationJob)
        .where(*conditions)
        .order_by(TargetAllocationJob.job_id.desc())
        .limit(limit).offset(offset)
    ).scalars().all()
    return [summary(job) for job in rows], total


def summary(job: TargetAllocationJob) -> dict[str, Any]:
    """One run as the history table lists it — counts, not the whole result."""
    return {
        "job_id": job.job_uuid,
        "plan_id": job.plan_id,
        "version_id": job.version_id,
        "status": job.status,
        "started_by": job.requested_by,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "projected_rows": job.projected_rows,
        "generated_rows": job.rows_processed,
        "allocation_level": job.allocation_level,
        # The three-way distinction the specification asks for: a run with no
        # sales at all, one with sales, and one that never got far enough to
        # look. NULL is the third, and is not the same as zero.
        "sales_rows_found": job.sales_rows_found,
        "sales_data_available": (None if job.sales_rows_found is None
                                 else job.sales_rows_found > 0),
        "error_count": job.error_count,
        "warning_count": job.warning_count,
        "error_message": job.error_message,
    }


def sweep_interrupted(session: Session) -> int:
    """Fail jobs a restart killed, before the first request is served.

    A worker thread dies with its process, and its transaction dies with it, so
    nothing an interrupted allocation was writing ever reached the database.
    What it leaves behind is a job row still claiming to be running, which would
    sit at 40% forever waiting for a worker that no longer exists. Sweeping at
    startup is what turns "stuck" into "failed, run it again".
    """
    stuck = session.execute(
        select(TargetAllocationJob).where(
            TargetAllocationJob.status.in_(AllocationJobStatus.OPEN))
    ).scalars().all()

    for job in stuck:
        job.status = AllocationJobStatus.FAILED
        job.completed_at = datetime.now(timezone.utc)
        job.error_message = (
            "The server restarted while this allocation was running. Nothing "
            "was written — run it again."
        )
        version = session.get(TargetVersion, job.version_id)
        if version is not None and version.status == \
                TargetStatus.ALLOCATION_IN_PROGRESS:
            version.status = TargetStatus.DRAFT
    return len(stuck)


def _decimal(value: Any):
    from decimal import Decimal

    return Decimal(str(value or 0))


__all__ = [
    "STAGE_WEIGHT",
    "get_executor",
    "shutdown",
    "submit",
    "run_after_commit",
    "state",
    "history",
    "summary",
    "latest_for_version",
    "to_dict",
    "sweep_interrupted",
]
