"""The Data Upload Center API.

Every endpoint is gated by ``require_section("data_upload")``, so access is
governed by exactly the same permission chain as any report — a user without the
section gets ``403`` whether they click a button or type the URL.

Two endpoints are gated more tightly still:

* the rollback requires a super administrator *and* an explicit confirmation;
* the whole router refuses anything that is not ``.xlsx`` or ``.csv`` before a
  byte is parsed.
"""

from __future__ import annotations

import csv
import io
import logging
import uuid
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import desc, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..ai.permission_filter import UserContext
from ..auth import audit
from ..auth.permissions import require_section
from ..database.models_admin import (
    ImportMode,
    UploadBatch,
    UploadCategory,
    UploadError,
    UploadStatus,
)
from ..database.models_ai import AuditAction, Role
from ..security.sections import SectionKey
from ..upload import files as upload_files
from ..upload import jobs, service, templates
from ..upload.registry import catalogue, get_upload_type
from ..utils import progress as progress_registry
from ..utils.progress import Phase
from .deps import get_session, internal_error

logger = logging.getLogger("app.api.data_upload")

router = APIRouter(prefix="/api/data-upload", tags=["data-upload"])

#: The section every endpoint in this router requires.
SectionDep = Depends(require_section(SectionKey.DATA_UPLOAD))


def _upload_type_or_404(key: str):
    try:
        return get_upload_type(key)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


def _batch_or_404(session: Session, upload_id: int) -> UploadBatch:
    batch = session.get(UploadBatch, upload_id)
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Upload not found.")
    return batch


# ---------------------------------------------------------------------------
# Write failures
#
# An import is one transaction and SQLite permits one writer, so a batch row
# written on the request thread while a job is running can genuinely find the
# database locked. That is a *situation*, not a fault, and it used to reach the
# operator as an opaque 500 with nothing to act on.
# ---------------------------------------------------------------------------


#: The driver's own words for "another writer holds the lock". Matched on text
#: because SQLAlchemy raises one ``OperationalError`` class for every backend;
#: PostgreSQL never produces these strings, so the check is inert there and such
#: an error still falls through to the generic handler.
_LOCK_MARKERS = ("database is locked", "database is busy")


def _is_write_conflict(exc: BaseException) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    text = str(getattr(exc, "orig", exc)).lower()
    return any(marker in text for marker in _LOCK_MARKERS)


def _running_import(session: Session) -> UploadBatch | None:
    """The import currently holding the write lock, if one is ours.

    Read, not written — WAL lets a reader through while a writer works, which is
    the whole reason this lookup can run at all while the database is locked.
    Anyone's job qualifies: a second user's import blocks this write just as a
    user's own does, so naming only their own would explain nothing.
    """
    return session.execute(
        select(UploadBatch)
        .where(UploadBatch.status.in_(UploadStatus.ACTIVE))
        .order_by(UploadBatch.upload_id.desc())
        .limit(1)
    ).scalars().first()


def _upload_write_error(exc: Exception, *, session: Session, endpoint: str,
                        user: UserContext, file_name: str | None,
                        upload_id: int | None = None,
                        job_id: str | None = None) -> HTTPException:
    """Log an upload write failure in full; answer with something actionable.

    Every failure is logged with the same reference the caller is shown, so a
    report of "upload failed, reference 3f2a1b" maps to exactly one line in the
    server log — which is what the old generic message could not do. The stack
    trace stays server-side.
    """
    reference = uuid.uuid4().hex[:8]
    logger.exception(
        "upload write failed [ref=%s] endpoint=%s user_id=%s username=%s "
        "file=%s upload_id=%s job_id=%s error=%s: %s",
        reference, endpoint, user.user_id, user.username, file_name,
        upload_id, job_id, type(exc).__name__, exc,
    )

    if not _is_write_conflict(exc):
        return HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"Upload failed. Reason: unable to record the import job. "
            f"Reference: {reference}. Quote that reference when reporting this.",
        )

    # 409, not 500: nothing is broken and the operator's own next action fixes
    # it. Naming the blocking job matters because the Import Job ID is what the
    # progress dock and the history are keyed on, so they can go and watch it.
    running = _running_import(session)
    if running is not None:
        # The batch row is the authority for what was committed, but for a job
        # still in flight the live registry is what says where it actually is —
        # the same precedence ``jobs.job_state`` applies, and without it a job
        # busy writing rows would be reported to the operator as "queued".
        live = progress_registry.snapshot(running.upload_uuid)
        where = (live or {}).get("phase") or running.status
        detail = (
            f"An import is already running: {running.file_name} "
            f"(Import Job ID {running.upload_uuid}, {str(where).lower()}). "
            "Only one import can write at a time, so this upload was not "
            "started. Wait for that job to finish, then upload again — nothing "
            "was changed."
        )
    else:
        detail = (
            "The database is busy with another write and this upload was not "
            "started. Nothing was changed — try again in a moment."
        )
    return HTTPException(status.HTTP_409_CONFLICT, f"{detail} Reference: {reference}.")


# ---------------------------------------------------------------------------
# Catalogue and templates
# ---------------------------------------------------------------------------


@router.get("/types")
def upload_types(_: UserContext = SectionDep) -> dict[str, Any]:
    """Every upload type, grouped by category.

    Derived from the Phase 1 master schema and the Phase 2 dataset specs, so the
    UI can never offer an entity the backend does not actually accept.
    """
    return {
        **catalogue(),
        "limits": {
            "max_bytes": upload_files.MAX_UPLOAD_BYTES,
            "extensions": list(upload_files.ALLOWED_EXTENSIONS),
            "preview_rows": service.PREVIEW_ROWS,
        },
    }


@router.get("/types/{upload_type}")
def upload_type_detail(upload_type: str, _: UserContext = SectionDep) -> dict[str, Any]:
    """One upload type's full column contract."""
    return _upload_type_or_404(upload_type).to_dict()


@router.get("/types/{upload_type}/template")
def download_template(
    upload_type: str,
    request: Request,
    file_format: str = Query("xlsx", pattern="^(xlsx|csv)$", alias="format"),
    session: Session = Depends(get_session),
    user: UserContext = SectionDep,
) -> Response:
    """The blank template for one upload type.

    Contains headers, one illustrative example row and, for .xlsx, an
    instructions sheet. It never contains real business data.
    """
    spec = _upload_type_or_404(upload_type)
    try:
        content, filename, media_type = templates.build(spec, file_format)
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, f"template {upload_type}") from exc

    audit.record(session, action=AuditAction.TEMPLATE_DOWNLOADED,
                 user_id=user.user_id, username=user.username,
                 resource=f"template:{spec.key}",
                 ip_address=audit.client_ip(request),
                 detail={"format": file_format})
    session.commit()

    return Response(
        content=content, media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Upload -> validate -> preview
# ---------------------------------------------------------------------------


@router.post("/preview", status_code=status.HTTP_202_ACCEPTED)
async def preview_upload(
    request: Request,
    file: UploadFile = File(..., description="Excel (.xlsx) or CSV (.csv) file."),
    upload_type: str = Form(...),
    import_mode: str = Form(ImportMode.UPSERT),
    sheet_name: str | None = Form(None),
    date_format: str | None = Form(None),
    session: Session = Depends(get_session),
    user: UserContext = SectionDep,
) -> dict[str, Any]:
    """Accept a file and queue it for validation.

    Returns **202** as soon as the bytes are staged and the batch recorded — the
    reading, validation and master mapping happen on a background worker. Watch
    ``upload.job_id`` through ``GET /jobs/{job_id}``; the counts, the preview rows
    and every error are on the batch once it reaches a terminal status, and
    ``GET /history/{upload_id}`` serves them.

    Nothing is written to the warehouse by this call or by the job it starts.
    """
    spec = _upload_type_or_404(upload_type)
    if import_mode not in spec.supported_modes:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"'{import_mode}' is not available for {spec.label}. Supported: "
            f"{', '.join(spec.supported_modes)}.",
        )

    # Streaming 25 MB to disk is blocking work and this endpoint is ``async``,
    # so it goes off the event loop. Everything after it is a few short queries.
    try:
        stored = await run_in_threadpool(
            upload_files.store, file.file, file.filename, file.content_type
        )
    except upload_files.UploadFileError as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc
    finally:
        await file.close()

    try:
        batch = service.create_batch(
            session, spec, stored, import_mode=import_mode,
            user_id=user.user_id, username=user.username,
        )
        # Committed before the job is queued, not after: the worker opens its own
        # session and must be able to read this row the moment it starts.
        session.commit()
    except Exception as exc:  # noqa: BLE001
        # Rolled back before the staged file goes: the failed flush left this
        # session in a state where the lookup behind the 409 message could not
        # run, which is how a locked database used to become a second error.
        session.rollback()
        upload_files.discard(stored.path)
        raise _upload_write_error(
            exc, session=session, endpoint="POST /api/data-upload/preview",
            user=user, file_name=file.filename,
        ) from exc

    jobs.submit(jobs.VALIDATE, batch, ip_address=audit.client_ip(request),
                sheet_name=sheet_name, date_format=date_format)
    return {"upload": service.batch_to_dict(batch), "queued": True}


@router.post("/{upload_id}/commit", status_code=status.HTTP_202_ACCEPTED)
def commit_upload(
    upload_id: int,
    request: Request,
    confirm: bool = Query(False, description="Must be true; the import is not "
                                             "started without an explicit "
                                             "confirmation."),
    session: Session = Depends(get_session),
    user: UserContext = SectionDep,
) -> dict[str, Any]:
    """Queue a validated upload for import into the warehouse.

    Returns **202**; the import runs on a background worker as one transaction.
    Watch ``upload.job_id`` through ``GET /jobs/{job_id}`` and read the result
    from ``GET /history/{upload_id}`` once it is terminal.
    """
    batch = _batch_or_404(session, upload_id)
    if not confirm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Confirm the import before it runs: review the preview and the error "
            "report first.",
        )
    # An upload belongs to the person who made it. An administrator can see it
    # in the history, but committing someone else's staged file would let one
    # user's data land under another's audit trail.
    if batch.user_id is not None and batch.user_id != user.user_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This upload was created by another user. Upload the file yourself to "
            "import it.",
        )
    # Checked here rather than only in the worker so the caller learns
    # immediately that there is nothing to import, instead of being handed a job
    # that exists only to fail.
    if batch.status != UploadStatus.VALIDATED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This upload is {batch.status.lower()}; only a validated upload can "
            "be imported.",
        )

    spec = _upload_type_or_404(batch.upload_type)
    batch.status = UploadStatus.QUEUED
    batch.stage = Phase.QUEUED
    batch.progress_percent = 0
    # The same write conflict reaches here: marking this batch queued is a write
    # on the request thread, and another import may hold the lock. Unhandled it
    # became a 500 that left the batch VALIDATED with no explanation, so the
    # operator could not tell a refusal from a fault.
    try:
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        raise _upload_write_error(
            exc, session=session,
            endpoint=f"POST /api/data-upload/{upload_id}/commit",
            user=user, file_name=batch.file_name, upload_id=batch.upload_id,
            job_id=batch.upload_uuid,
        ) from exc

    jobs.submit(jobs.IMPORT, batch, ip_address=audit.client_ip(request))
    return {"upload": service.batch_to_dict(batch), "queued": True}


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@router.get("/jobs")
def list_jobs(
    session: Session = Depends(get_session),
    user: UserContext = SectionDep,
) -> dict[str, Any]:
    """This user's running imports, and the ones that just finished.

    What the upload dock polls. Scoped to the caller in the query — a job is
    someone's work in progress, and §14 says you see your own unless your role
    says otherwise, so a super administrator sees them all.
    """
    owner = None if user.role == Role.SUPER_ADMIN else user.user_id
    batches = jobs.active_jobs(session, user_id=owner)
    return {
        "jobs": [jobs.job_state(session, batch) for batch in batches],
        "active": sum(1 for b in batches if b.status in UploadStatus.ACTIVE),
    }


@router.get("/jobs/{job_id}")
def job_progress(
    job_id: str,
    session: Session = Depends(get_session),
    user: UserContext = SectionDep,
) -> dict[str, Any]:
    """How far one import has got.

    ``job_id`` is the batch's ``upload_uuid``. The batch row is the authority —
    it says who owns the job and how it ended — and the in-memory registry adds
    the live stage and row counts while the worker is still going.

    ``known: false`` means no such job, which is a normal answer rather than an
    error: the client stops watching instead of having to tell a 404 apart from
    a lost one.
    """
    batch = session.execute(
        select(UploadBatch).where(UploadBatch.upload_uuid == job_id)
    ).scalar_one_or_none()
    if batch is None:
        return {"known": False, "job_id": job_id}
    _require_job_access(batch, user)
    return {"known": True, **jobs.job_state(session, batch)}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: str,
    request: Request,
    session: Session = Depends(get_session),
    user: UserContext = SectionDep,
) -> dict[str, Any]:
    """Stop a running import.

    Cancellation is cooperative and backend-owned: this raises a flag, and the
    worker stops at its next checkpoint and rolls its transaction back. Because
    the run is one transaction, a cancelled import has written **nothing** — there
    is no half-loaded batch to clean up afterwards.

    Stopping takes effect at the next checkpoint rather than instantly. On SQLite
    a statement already executing cannot be interrupted, so the worst case is one
    write chunk — a second or so on a large file.

    Returns **409** if the run had already finished: the stored outcome stands,
    because it is what actually happened, and the response carries it so the
    caller can show the real result instead of a cancellation that never was.
    """
    batch = session.execute(
        select(UploadBatch).where(UploadBatch.upload_uuid == job_id)
    ).scalar_one_or_none()
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Import job not found.")
    # Cancelling is an action on someone's work, so it is gated exactly as
    # reading the job is: your own, or any if you are a super administrator.
    _require_job_access(batch, user)

    outcome = jobs.request_cancel(session, batch, by=user.username)
    if outcome == jobs.CancelOutcome.NOT_ACTIVE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This import is {batch.status.lower()} and is not running, so there "
            "is nothing to cancel.",
        )

    audit.record(session, action=AuditAction.DATA_IMPORT_CANCELLED,
                 user_id=user.user_id, username=user.username,
                 resource=f"upload:{batch.upload_type}", success=True,
                 ip_address=audit.client_ip(request),
                 detail={"upload_id": batch.upload_id, "job_id": job_id,
                         "outcome": outcome,
                         "stopped_at_percent": batch.progress_percent})
    session.commit()

    session.refresh(batch)
    return {"outcome": outcome, **jobs.job_state(session, batch)}


def _require_job_access(batch: UploadBatch, user: UserContext) -> None:
    """A job is visible to the person who started it, and to a super admin.

    Enforced server-side on every job read, so hiding the dock entry in the
    browser stays presentation and this stays the actual rule.
    """
    if user.role == Role.SUPER_ADMIN:
        return
    if batch.user_id is not None and batch.user_id != user.user_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This import belongs to another user.",
        )


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@router.get("/history")
def history(
    category: str | None = Query(None),
    upload_type: str | None = Query(None, max_length=48),
    batch_status: str | None = Query(None, alias="status", max_length=16),
    username: str | None = Query(None, max_length=64),
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
    _: UserContext = SectionDep,
) -> dict[str, Any]:
    """Upload history, newest first."""
    conditions = []
    if category:
        conditions.append(UploadBatch.category == category.upper())
    if upload_type:
        conditions.append(UploadBatch.upload_type == upload_type)
    if batch_status:
        conditions.append(UploadBatch.status == batch_status.upper())
    if username:
        conditions.append(UploadBatch.username == username)

    total = session.execute(
        select(func.count()).select_from(UploadBatch).where(*conditions)
    ).scalar_one()
    rows = session.execute(
        select(UploadBatch).where(*conditions)
        .order_by(desc(UploadBatch.upload_id)).limit(limit).offset(offset)
    ).scalars().all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "statuses": list(UploadStatus.ALL),
        "categories": list(UploadCategory.ALL),
        "batches": [service.batch_to_dict(batch) for batch in rows],
    }


@router.get("/history/{upload_id}")
def history_detail(
    upload_id: int,
    error_limit: int = Query(200, ge=1, le=1000),
    session: Session = Depends(get_session),
    _: UserContext = SectionDep,
) -> dict[str, Any]:
    """One upload's full result, including its errors."""
    batch = _batch_or_404(session, upload_id)
    errors = session.execute(
        select(UploadError).where(UploadError.upload_id == upload_id)
        .order_by(UploadError.row_number, UploadError.id).limit(error_limit)
    ).scalars().all()
    error_total = session.execute(
        select(func.count()).select_from(UploadError)
        .where(UploadError.upload_id == upload_id)
    ).scalar_one()

    return {
        "upload": service.batch_to_dict(batch),
        "upload_type": _upload_type_or_404(batch.upload_type).to_dict(),
        # The rows the validation job judged, stored on the batch because the
        # request that started that job returned before they existed. Absent for
        # a batch that never got as far as reading the file.
        "preview": batch.preview,
        "warnings": (batch.preview or {}).get("warnings", []),
        "error_total": error_total,
        "errors": [
            {
                "row": error.row_number,
                "column": error.column_name,
                "value": error.value,
                "error_code": error.error_code,
                "error": error.error_message,
                "suggested_fix": error.suggested_fix,
                "severity": error.severity,
                "category": error.error_category,
            }
            for error in errors
        ],
    }


@router.get("/history/{upload_id}/errors")
def download_error_report(
    upload_id: int,
    session: Session = Depends(get_session),
    _: UserContext = SectionDep,
) -> Response:
    """The error report as CSV: row, column, value, error, suggested fix."""
    batch = _batch_or_404(session, upload_id)
    rows = session.execute(
        select(UploadError).where(UploadError.upload_id == upload_id)
        .order_by(UploadError.row_number, UploadError.id)
    ).scalars().all()

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["Row Number", "Column", "Value", "Error Code", "Error",
                     "Suggested Fix", "Severity"])
    for error in rows:
        writer.writerow([
            error.row_number if error.row_number is not None else "",
            error.column_name or "",
            error.value or "",
            error.error_code,
            error.error_message,
            error.suggested_fix or "",
            error.severity,
        ])

    filename = f"upload_{batch.upload_id}_errors.csv"
    return Response(
        # utf-8-sig so Excel renders Bangla values in the report correctly.
        content=buffer.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/history/{upload_id}/rollback")
def rollback(
    upload_id: int,
    request: Request,
    confirm: bool = Query(False),
    session: Session = Depends(get_session),
    user: UserContext = SectionDep,
) -> dict[str, Any]:
    """Undo one transactional import.

    Restricted to a super administrator, requires explicit confirmation, and is
    audited. It removes only the fact rows this batch created — a transaction a
    later batch has since updated belongs to that batch and is left alone. A
    master-data upload cannot be rolled back at all: the upsert overwrote the
    previous values without keeping them, so an "undo" would be invention.
    """
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only a super administrator can roll back an import.",
        )
    if not confirm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Rolling back deletes the records this import loaded. Confirm to proceed.",
        )

    batch = _batch_or_404(session, upload_id)
    try:
        result = service.rollback_batch(session, batch, by=user.username)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        raise internal_error(exc, f"rollback upload {upload_id}") from exc

    audit.record(session, action=AuditAction.DATA_ROLLBACK, user_id=user.user_id,
                 username=user.username, resource=f"upload:{batch.upload_type}",
                 ip_address=audit.client_ip(request),
                 detail={"upload_id": upload_id, **result})
    session.commit()
    return {"upload": service.batch_to_dict(batch), **result}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("/summary")
def summary(
    session: Session = Depends(get_session),
    _: UserContext = SectionDep,
) -> dict[str, Any]:
    """Cards for the Data Upload dashboard."""
    try:
        return {
            **service.upload_summary(session),
            "master_types": sum(
                1 for group in catalogue()["categories"]
                if group["key"] == UploadCategory.MASTER for _ in group["types"]
            ),
            "recent": [
                service.batch_to_dict(batch)
                for batch in session.execute(
                    select(UploadBatch).order_by(desc(UploadBatch.upload_id)).limit(5)
                ).scalars().all()
            ],
        }
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "upload summary") from exc


@router.get("/failed-records")
def failed_records(
    upload_type: str | None = Query(None, max_length=48),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
    _: UserContext = SectionDep,
) -> dict[str, Any]:
    """Every failed record across uploads — the Failed Records tab."""
    conditions = []
    if upload_type:
        conditions.append(UploadBatch.upload_type == upload_type)

    statement = (
        select(UploadError, UploadBatch)
        .join(UploadBatch, UploadBatch.upload_id == UploadError.upload_id)
        .where(*conditions)
        .order_by(desc(UploadError.upload_id), UploadError.row_number)
    )
    total = session.execute(
        select(func.count()).select_from(UploadError)
        .join(UploadBatch, UploadBatch.upload_id == UploadError.upload_id)
        .where(*conditions)
    ).scalar_one()
    rows = session.execute(statement.limit(limit).offset(offset)).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "records": [
            {
                "upload_id": batch.upload_id,
                "upload_type": batch.upload_type,
                "file_name": batch.file_name,
                "uploaded_by": batch.username,
                "row": error.row_number,
                "column": error.column_name,
                "value": error.value,
                "error_code": error.error_code,
                "error": error.error_message,
                "suggested_fix": error.suggested_fix,
                "created_at": error.created_at,
            }
            for error, batch in rows
        ],
    }


__all__ = ["router"]
