"""Background import jobs.

An upload used to be a request that happened to take a long time. It is now a
job: the request accepts the file, records it and returns; a worker thread reads
it, validates it and — when the user confirms — loads it. That is what lets the
operator navigate away, watch the bar from any page, and stop a run that is going
wrong.

**Why threads and not a queue service.** Everything below this point is
synchronous SQLAlchemy, so a thread is the natural unit of one import; a broker
would add a service, a serialisation format and a deployment story to solve a
problem this project does not have. The pool is bounded and owned by the process,
and the job's durable state lives in ``upload_batches`` where it already lived.

**Why one worker by default.** SQLite permits a single writer. A second
concurrent import would not run faster — it would block on the write lock, inside
the database, where nobody can see it or cancel it. Queueing in the pool instead
keeps the wait visible and reportable, and ``IMPORT_WORKER_COUNT`` raises it for
PostgreSQL where concurrent writers are real.

**What the worker owns.** Its own ``Session``, opened from the engine and closed
when it finishes. The request's session is long gone by then, and the ETL opens
its own besides. If the worker raises, the batch is marked ``FAILED`` through a
*second*, clean session, because the first one is the one that just broke.

The engine is reached as ``connection.get_engine()`` rather than through a
``from ... import get_engine``, so that redirecting the connection module — which
is how the tests point at a throwaway warehouse — redirects this too. A binding
captured at import time would leave worker threads writing to whatever database
the process started with, which in a test run is the developer's own.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import connection
from ..database.models_admin import UploadBatch, UploadStatus
from ..database.models_ai import AuditAction
from ..utils import progress as progress_registry
from ..utils.progress import ImportCancelled, Phase, ProgressReporter
from . import service
from .files import StoredUpload, discard
from .registry import get_upload_type

logger = logging.getLogger("app.upload.jobs")

#: Job kinds. A batch is validated once and, if the user confirms, imported once.
VALIDATE = "VALIDATE"
IMPORT = "IMPORT"

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def get_executor() -> ThreadPoolExecutor:
    """The process-wide import pool, created on first use."""
    global _executor
    with _executor_lock:
        if _executor is None:
            workers = max(1, get_settings().import_worker_count)
            _executor = ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="import-job"
            )
            logger.info("import job pool started with %s worker(s)", workers)
        return _executor


def shutdown(wait: bool = False) -> None:
    """Stop accepting jobs. Called when the application shuts down.

    ``wait=False`` by default: a running import holds an uncommitted transaction,
    so letting the process exit rolls it back, which is the correct outcome for an
    interrupted run. Blocking on it would delay shutdown to reach the same state.
    The startup sweep marks whatever was in flight as ``FAILED``.
    """
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=wait, cancel_futures=True)
            _executor = None


# ---------------------------------------------------------------------------
# Submitting work
# ---------------------------------------------------------------------------


def submit(kind: str, batch: UploadBatch, *, ip_address: str | None = None,
           sheet_name: str | None = None,
           date_format: str | None = None) -> dict[str, Any]:
    """Queue one job and return the state a client should start watching.

    Only the batch's *identifiers* cross into the worker, never the ORM object:
    it belongs to the request's session, which will be closed before the worker
    runs. The worker loads its own copy.
    """
    reporter = progress_registry.start(
        batch.upload_uuid,
        operation=kind,
        upload_type=batch.upload_type,
        file_name=batch.file_name,
    )
    reporter.phase(Phase.QUEUED)
    reporter.counts(upload_id=batch.upload_id)

    upload_id = batch.upload_id
    job_id = batch.upload_uuid
    get_executor().submit(
        _guarded, kind, upload_id, job_id, reporter, ip_address, sheet_name,
        date_format,
    )
    return {"job_id": job_id, "upload_id": upload_id}


def _guarded(kind: str, upload_id: int, job_id: str, reporter: ProgressReporter,
             ip_address: str | None, sheet_name: str | None,
             date_format: str | None) -> None:
    """Run one job so that no failure can escape into the pool.

    A thread that raises loses its exception silently and — worse — leaves the
    batch stuck in a running status forever, which is exactly the state the whole
    design exists to avoid.
    """
    try:
        _run(kind, upload_id, job_id, reporter, ip_address, sheet_name, date_format)
    except ImportCancelled:
        # Expected control flow, not a failure. By the time it reaches here every
        # transaction the run opened has unwound, so nothing it was writing
        # survived — which is exactly what makes cancelling safe.
        logger.info("import job %s (%s) was cancelled", job_id, kind)
        _mark_cancelled(upload_id, reporter)
    except Exception:  # noqa: BLE001 - the last line of defence
        logger.exception("import job %s (%s) failed", job_id, kind)
        _mark_failed(upload_id, reporter)


def _run(kind: str, upload_id: int, job_id: str, reporter: ProgressReporter,
         ip_address: str | None, sheet_name: str | None,
         date_format: str | None) -> None:
    session = Session(bind=connection.get_engine(), expire_on_commit=False, future=True)
    try:
        batch = session.get(UploadBatch, upload_id)
        if batch is None:  # pragma: no cover - the row is created before submit
            logger.warning("import job %s has no batch %s", job_id, upload_id)
            reporter.finish(message="The upload record no longer exists.",
                            failed=True)
            return

        # A job cancelled while it was still queued has done nothing at all, so
        # the cheapest place to notice is before the first byte is read. Without
        # this it would run to its first checkpoint and unwind from there —
        # correct, but needlessly.
        reporter.checkpoint()

        spec = get_upload_type(batch.upload_type)
        stored = _stored_for(batch)

        if kind == VALIDATE:
            if stored is None:
                _fail(session, batch, reporter,
                      "The uploaded file is no longer available. Please upload it "
                      "again.")
                return
            outcome = service.run_validation(
                session, batch, spec, stored,
                sheet_name=sheet_name, date_format=date_format, progress=reporter,
            )
            # One action for both outcomes: the event is "a file was uploaded",
            # and whether it validated is carried by ``success`` and the counts.
            action = AuditAction.DATA_UPLOADED
        else:
            outcome = service.commit_upload(
                session, batch, spec, sheet_name=sheet_name,
                date_format=date_format, progress=reporter,
            )
            action = (AuditAction.DATA_IMPORTED
                      if batch.status in (UploadStatus.COMPLETED,
                                          UploadStatus.PARTIAL)
                      else AuditAction.DATA_IMPORT_FAILED)

        _persist_progress(batch, reporter)
        _audit(session, batch, action, ip_address)
        session.commit()
        logger.info("import job %s (%s) finished as %s with %s valid row(s)",
                    job_id, kind, batch.status, outcome.batch.valid_rows)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _stored_for(batch: UploadBatch) -> StoredUpload | None:
    """Rebuild the staged-file handle the worker needs from the batch row."""
    if not batch.stored_path:
        return None
    path = Path(batch.stored_path)
    if not path.exists():
        return None
    return StoredUpload(
        path=path,
        original_name=batch.file_name,
        extension=path.suffix.lower(),
        size=batch.file_size,
        content_type=batch.content_type,
        sha256=batch.file_hash,
    )


# ---------------------------------------------------------------------------
# Reading job state
# ---------------------------------------------------------------------------


def job_state(session: Session, batch: UploadBatch) -> dict[str, Any]:
    """One job as the dock and the poll endpoint render it.

    The batch row is the authority for identity, ownership and outcome; the
    in-memory registry adds live detail while the job is running. When the two
    disagree — a job that finished between the poll and this read — the batch
    wins, because it is what was actually committed.
    """
    payload = service.batch_to_dict(batch)
    live = progress_registry.snapshot(batch.upload_uuid)
    finished = batch.status in UploadStatus.TERMINAL

    if live and not finished:
        payload["stage"] = live["phase"]
        payload["progress_percent"] = live["percent"]
        payload["counts"] = live["counts"]
        # A stop has been asked for but the run has not unwound yet. Reported as
        # its own state so the UI can say "Cancelling…" rather than leaving the
        # button live and inviting a second press.
        if live.get("cancel_requested") and not live.get("done"):
            payload["stage"] = Phase.CANCELLING
            payload["can_cancel"] = False
    else:
        # Terminal, or the job has left memory: report the stored counts under
        # the same six names, so a caller never has to know which case it got.
        payload["counts"] = {
            "total_records": batch.total_rows or 0,
            "processed_records": batch.total_rows or 0,
            "valid_records": batch.valid_rows or 0,
            "invalid_records": batch.invalid_rows or 0,
            "imported_records": (batch.inserted_rows or 0) + (batch.updated_rows or 0),
            "failed_records": batch.invalid_rows or 0,
        }
        if finished:
            payload["progress_percent"] = 100
    payload["live"] = live is not None
    return payload


def active_jobs(session: Session, *, user_id: int | None,
                include_recent: bool = True) -> list[UploadBatch]:
    """This user's running jobs, plus the ones that just finished.

    Scoped by ``user_id`` in the query rather than filtered afterwards: the dock
    is a per-user view, and a job belonging to someone else should never be
    loaded only to be discarded. Recently finished jobs are included so the
    result of an import is still on screen a moment after it lands.
    """
    conditions = [UploadBatch.status.in_(UploadStatus.ACTIVE)]
    if include_recent:
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=get_settings().import_job_retention_seconds
        )
        conditions = [
            UploadBatch.status.in_(UploadStatus.ACTIVE)
            | (UploadBatch.started_at >= cutoff)
        ]
    statement = select(UploadBatch).where(*conditions)
    if user_id is not None:
        statement = statement.where(UploadBatch.user_id == user_id)
    statement = statement.order_by(UploadBatch.upload_id.desc()).limit(20)
    return list(session.execute(statement).scalars())


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class CancelOutcome:
    """What a cancel request achieved. Distinct values, because they differ.

    ``REQUESTED``   the run was going and has been asked to stop
    ``NOT_ACTIVE``  there is nothing running to stop — it finished, or it is a
                    validated batch sitting waiting for someone to confirm it
    ``NOT_RUNNING`` the status says active but no worker here has it: swept by a
                    restart, or being run by another process
    """

    REQUESTED = "REQUESTED"
    NOT_ACTIVE = "NOT_ACTIVE"
    NOT_RUNNING = "NOT_RUNNING"


def request_cancel(session: Session, batch: UploadBatch, *, by: str) -> str:
    """Ask a running import to stop.

    **The backend resolves the race, and the worker resolves it alone.** This
    only raises a flag and records who raised it; it never writes a terminal
    status. If the run commits in the moment between the check below and the
    flag being set, the flag is simply never observed and the import completes —
    the caller then sees ``COMPLETED``, which is what actually happened. The
    alternative, writing ``CANCELLED`` here, would let a request thread overwrite
    the outcome of a run that had already succeeded.

    ``cancelled_by`` is recorded now rather than by the worker, because this is
    the only place that knows *who* asked.
    """
    # Only a *running* job can be stopped. That is narrower than "not terminal":
    # a VALIDATED batch is finished work waiting for someone to confirm it, so
    # there is nothing in flight to cancel and saying otherwise would imply the
    # validation had been undone.
    if batch.status not in UploadStatus.ACTIVE:
        return CancelOutcome.NOT_ACTIVE

    asked = progress_registry.request_cancel(batch.upload_uuid)
    batch.cancelled_by = by
    session.commit()
    if not asked:
        # Nothing in this process is running it. Either it finished between the
        # check above and now, or a restart already swept it. The batch row says
        # which, and the caller reads it back.
        return CancelOutcome.NOT_RUNNING
    return CancelOutcome.REQUESTED


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def _fail(session: Session, batch: UploadBatch, reporter: ProgressReporter,
          message: str) -> None:
    batch.status = UploadStatus.FAILED
    batch.completed_at = _now()
    batch.message = message
    batch.stage = Phase.FAILED
    batch.progress_percent = 100
    reporter.finish(message=message, failed=True)
    session.commit()


def _mark_cancelled(upload_id: int, reporter: ProgressReporter) -> None:
    """Record a stopped run, on a clean session.

    The worker's own session was unwound by the exception, so this opens a fresh
    one — the same reason :func:`_mark_failed` does. Written here rather than by
    the endpoint that requested the stop, because the worker is the only thread
    that knows the run has actually finished unwinding; a status written the
    moment Cancel was pressed would claim the rollback had happened before it had.
    """
    message = (
        "Cancelled. The import was stopped before it finished and nothing was "
        "loaded — the whole run is one transaction, so it was rolled back."
    )
    try:
        with Session(bind=connection.get_engine(), expire_on_commit=False,
                     future=True) as session:
            batch = session.get(UploadBatch, upload_id)
            if batch is not None and batch.status not in UploadStatus.TERMINAL:
                live = progress_registry.snapshot(batch.upload_uuid)
                batch.status = UploadStatus.CANCELLED
                batch.cancelled_at = _now()
                batch.completed_at = _now()
                batch.message = message
                batch.stage = Phase.CANCELLED
                # The percentage it reached, kept rather than reset: "stopped at
                # 68%" is the useful thing to know about a cancelled import.
                if live is not None:
                    batch.progress_percent = live["percent"]
                discard(batch.stored_path)
                batch.stored_path = None
                session.commit()
    except Exception:  # noqa: BLE001 - nothing useful is left to try
        logger.exception("could not mark upload %s as cancelled", upload_id)
    reporter.finish(message=message, cancelled=True)


def _mark_failed(upload_id: int, reporter: ProgressReporter) -> None:
    """Record a crashed job on a clean session.

    Deliberately does not reuse the worker's session: whatever went wrong may
    have left it unusable, and a batch stuck in ``IMPORTING`` because the
    bookkeeping also failed is the worst of both outcomes.
    """
    message = "The import failed and nothing was loaded."
    try:
        # ``connection.get_engine()``, not a bare ``get_engine`` — this module
        # deliberately never imports the name (see the module docstring). It read
        # ``get_engine()`` here, which is not bound in this namespace, so every
        # crashed job raised NameError inside its own recovery path: the except
        # below swallowed it and the batch stayed ACTIVE forever, waiting for a
        # worker that had already died. That is the one way an upload could
        # genuinely hang, and it looked exactly like "the upload stopped".
        with Session(bind=connection.get_engine(), expire_on_commit=False,
                     future=True) as session:
            batch = session.get(UploadBatch, upload_id)
            if batch is not None and batch.status not in UploadStatus.TERMINAL:
                batch.status = UploadStatus.FAILED
                batch.completed_at = _now()
                batch.message = message
                batch.stage = Phase.FAILED
                batch.progress_percent = 100
                discard(batch.stored_path)
                batch.stored_path = None
                session.commit()
    except Exception:  # noqa: BLE001 - nothing useful is left to try
        logger.exception("could not mark upload %s as failed", upload_id)
    reporter.finish(message=message, failed=True)


def sweep_interrupted(session: Session) -> int:
    """Close out jobs a restart killed. Returns how many were swept.

    A worker thread dies with its process and its transaction dies with it, so
    nothing it was writing reached the warehouse. What it does leave behind is a
    batch row claiming to be running, which would otherwise sit in the dock
    forever waiting for a worker that no longer exists. Run at startup, before
    the first request.
    """
    stranded = list(session.execute(
        select(UploadBatch).where(UploadBatch.status.in_(UploadStatus.ACTIVE))
    ).scalars())
    for batch in stranded:
        batch.status = UploadStatus.FAILED
        batch.completed_at = _now()
        batch.stage = Phase.FAILED
        batch.message = (
            "This import was interrupted by a server restart and did not finish. "
            "Nothing was loaded — upload the file again."
        )
        discard(batch.stored_path)
        batch.stored_path = None
    return len(stranded)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _persist_progress(batch: UploadBatch, reporter: ProgressReporter) -> None:
    """Copy the job's final position onto the batch.

    Live progress is served from memory, but memory is not where the history
    reads from. Storing the last stage and percentage is what lets a cancelled or
    interrupted import say where it stopped long after the job is gone.
    """
    live = progress_registry.snapshot(reporter.job_id or "")
    if live is None:
        return
    batch.stage = live["phase"]
    batch.progress_percent = live["percent"]


def _audit(session: Session, batch: UploadBatch, action: str,
           ip_address: str | None) -> None:
    """Record the job's real outcome, not the fact that it was accepted."""
    from ..auth import audit

    audit.record(
        session, action=action, user_id=batch.user_id, username=batch.username,
        resource=f"upload:{batch.upload_type}",
        ip_address=ip_address,
        success=batch.status not in (UploadStatus.FAILED, UploadStatus.CANCELLED),
        detail={
            "upload_id": batch.upload_id,
            "job_id": batch.upload_uuid,
            "file_name": batch.file_name,
            "status": batch.status,
            "rows": batch.total_rows,
            "valid_rows": batch.valid_rows,
            "invalid_rows": batch.invalid_rows,
            "inserted": batch.inserted_rows,
            "updated": batch.updated_rows,
            "etl_batch_id": batch.etl_batch_id,
        },
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "VALIDATE",
    "IMPORT",
    "CancelOutcome",
    "submit",
    "job_state",
    "active_jobs",
    "request_cancel",
    "sweep_interrupted",
    "get_executor",
    "shutdown",
]
