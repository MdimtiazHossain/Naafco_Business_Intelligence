"""The upload workflow.

Two calls, matching the two decisions the user makes:

``validate_upload``
    Accept the file, stage it, read it, check it, and *report*. Nothing is
    written to the warehouse — for transactional data this is the Phase 2 ETL
    run in ``dry_run`` mode, which performs every step and rolls back. The
    resulting ``upload_batches`` row is ``VALIDATED`` (or ``FAILED``), holds the
    counts, the preview and every error, and keeps the staged file so the user
    can confirm without re-uploading.

``commit_upload``
    Re-run the same file for real, against the mode the user confirmed. A
    transactional commit goes through ``etl.pipeline.run_import`` unchanged, so
    staging, master mapping, hierarchy validation, business-key de-duplication
    and the fact tables behave exactly as for a scripted import.

Validating twice is deliberate. The warehouse can change between the two calls
(someone else may load the missing region), so the numbers shown at preview are
a report, never a promise, and the commit re-checks everything before writing.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..database.connection import get_engine
from ..database.models_admin import (
    SEVERITY_ERROR,
    ImportMode,
    UploadBatch,
    UploadCategory,
    UploadError,
    UploadStatus,
)
from ..database.models_warehouse import (
    FACT_MODEL_BY_DATA_TYPE,
    STAGING_MODEL_BY_DATA_TYPE,
    EtlImportBatch,
    EtlRejectedRecord,
)
from ..etl.pipeline import (
    ACCEPTED,
    LINE_REPORT_FIELDS,
    LOAD_MODE_INCREMENTAL,
    LOAD_MODE_INITIAL,
    ImportResult,
    run_import,
)
from ..etl.readers import SourceReader
from ..utils.progress import ImportCancelled, Phase, ProgressReporter
from . import master_loader
from .errors import (
    UploadIssue,
    failure_issue,
    hierarchy_fix,
    is_file_fault,
    suggested_fix,
)
from .files import StoredUpload, discard
from .registry import UploadType

logger = logging.getLogger("app.upload")

#: How many rows of the file the preview shows. The whole file is validated;
#: only this many rows are ever sent to the browser.
PREVIEW_ROWS = 50
#: How many errors are returned inline. The rest stay in ``upload_errors`` and
#: are available through the downloadable error report.
MAX_INLINE_ERRORS = 200

#: Upload import mode -> Phase 2 ETL load mode.
#:
#: ``INSERT`` maps to INITIAL, whose contract is "a business key already in the
#: warehouse is reported, not re-applied" — exactly insert-only semantics.
#: ``UPSERT`` maps to INCREMENTAL, which updates the existing row in place.
LOAD_MODE_BY_IMPORT_MODE = {
    ImportMode.INSERT: LOAD_MODE_INITIAL,
    ImportMode.UPSERT: LOAD_MODE_INCREMENTAL,
    ImportMode.UPDATE: LOAD_MODE_INCREMENTAL,
}

SOURCE_SYSTEM = "UPLOAD"


@dataclass
class UploadOutcome:
    """What the API returns for both validate and commit."""

    batch: UploadBatch
    preview: list[dict[str, Any]]
    columns: list[str]
    issues: list[UploadIssue]
    warnings: list[str]

    def to_dict(self, *, include_preview: bool = True) -> dict[str, Any]:
        payload = {
            "upload": batch_to_dict(self.batch),
            "warnings": self.warnings,
            "errors": [issue.to_dict() for issue in self.issues[:MAX_INLINE_ERRORS]],
            "error_count": len(self.issues),
            "error_truncated": len(self.issues) > MAX_INLINE_ERRORS,
        }
        if include_preview:
            payload["preview"] = {
                "columns": self.columns,
                "rows": self.preview,
                "shown": len(self.preview),
                "limit": PREVIEW_ROWS,
            }
        return payload


def batch_to_dict(batch: UploadBatch) -> dict[str, Any]:
    """The upload batch as the history, detail and job views present it.

    One shape for all three, so a job being watched and the same batch read back
    from the history a week later describe themselves with the same keys. The
    ``job_id`` is ``upload_uuid``: an upload has exactly one run, so it needs one
    identifier, not two that have to be kept in step.
    """
    return {
        "upload_id": batch.upload_id,
        "upload_uuid": batch.upload_uuid,
        #: The Import Job ID. An alias rather than a second column, because the
        #: two would name the same thing and could then disagree.
        "job_id": batch.upload_uuid,
        "category": batch.category,
        "upload_type": batch.upload_type,
        "file_name": batch.file_name,
        "file_size": batch.file_size,
        "import_mode": batch.import_mode,
        "status": batch.status,
        "user_id": batch.user_id,
        "username": batch.username,
        "started_at": batch.started_at,
        "validated_at": batch.validated_at,
        "completed_at": batch.completed_at,
        "cancelled_at": batch.cancelled_at,
        "cancelled_by": batch.cancelled_by,
        #: Where the run got to. Live progress comes from the job registry while
        #: a job is in memory; this is what is left of it afterwards, which is
        #: what lets the history say where a cancelled import stopped.
        "stage": batch.stage,
        "progress_percent": batch.progress_percent,
        "duration_seconds": _duration_seconds(batch),
        "totals": {
            "total_rows": batch.total_rows,
            "columns": batch.column_count,
            "valid_rows": batch.valid_rows,
            "invalid_rows": batch.invalid_rows,
            "duplicate_rows": batch.duplicate_rows,
            "inserted_rows": batch.inserted_rows,
            "updated_rows": batch.updated_rows,
        },
        "etl_batch_id": batch.etl_batch_id,
        "summary": batch.summary,
        "message": batch.message,
        "rolled_back_at": batch.rolled_back_at,
        "rolled_back_by": batch.rolled_back_by,
        "is_active": batch.status in UploadStatus.ACTIVE,
        "can_commit": batch.status == UploadStatus.VALIDATED,
        #: Only a run that is still going can be stopped. Whether *this* user may
        #: stop it is a separate question the endpoint answers, because the
        #: answer depends on who is asking and this function does not know.
        "can_cancel": batch.status in UploadStatus.ACTIVE,
        "can_rollback": (
            batch.category == UploadCategory.TRANSACTIONAL
            and batch.etl_batch_id is not None
            and batch.status in (UploadStatus.COMPLETED, UploadStatus.PARTIAL)
            and batch.rolled_back_at is None
        ),
    }


def _duration_seconds(batch: UploadBatch) -> float | None:
    """How long the run took, or ``None`` while it is still going.

    Reported rather than stored: it is ``completed_at - started_at`` and a
    derived column would be one more thing that can disagree with its inputs.
    The end is whichever terminal timestamp the batch actually reached, so a
    cancelled import reports the time up to the cancellation rather than nothing.
    """
    finished = batch.completed_at or batch.cancelled_at
    if finished is None or batch.started_at is None:
        return None
    return round((_aware(finished) - _aware(batch.started_at)).total_seconds(), 3)


def _aware(value: datetime) -> datetime:
    """Read a stored timestamp back as UTC.

    The columns are declared ``DateTime(timezone=True)``, but SQLite has no time
    zone type and hands back a naive value, while a timestamp still in the
    session from this request is the aware object that was just assigned to it.
    Subtracting one from the other raises. Everything here is written by
    :func:`_now`, which is UTC, so a naive value *is* UTC and saying so is a
    statement of fact rather than an assumption.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def create_batch(session: Session, upload_type: UploadType, stored: StoredUpload,
                 *, import_mode: str, user_id: int | None,
                 username: str | None) -> UploadBatch:
    """Record the upload and queue it. No file is read here.

    Split out of :func:`run_validation` because the two now happen in different
    places: the request creates this row so it can answer immediately with a job
    to watch, and a background worker does the reading afterwards. The row exists
    from the moment the bytes are accepted, so an upload is auditable even if the
    process dies before the work starts.

    ``upload_uuid`` is the Import Job ID. It is generated here rather than
    accepted from the client so that the identifier of a stored record is never
    something a caller chose.
    """
    batch = UploadBatch(
        upload_uuid=str(uuid.uuid4()),
        category=upload_type.category,
        upload_type=upload_type.key,
        file_name=stored.original_name,
        file_size=stored.size,
        content_type=stored.content_type,
        file_hash=stored.sha256,
        stored_path=str(stored.path),
        import_mode=import_mode,
        status=UploadStatus.QUEUED,
        stage=Phase.QUEUED,
        user_id=user_id,
        username=username,
    )
    session.add(batch)
    session.flush()
    return batch


def run_validation(session: Session, batch: UploadBatch, upload_type: UploadType,
                   stored: StoredUpload, *,
                   sheet_name: str | None = None,
                   date_format: str | None = None,
                   progress: ProgressReporter | None = None) -> UploadOutcome:
    """Read and check the staged file. Writes nothing to the warehouse.

    Runs on a background worker against its own session, so it owns the batch row
    for the duration and the caller commits when it returns.
    """
    progress = progress or ProgressReporter(None)
    import_mode = batch.import_mode
    batch.status = UploadStatus.VALIDATING
    session.flush()
    progress.counts(upload_id=batch.upload_id)

    try:
        if upload_type.category == UploadCategory.MASTER:
            outcome = _validate_master(session, batch, upload_type, stored,
                                       import_mode, sheet_name, progress)
        else:
            outcome = _validate_transaction(session, batch, upload_type, stored,
                                            import_mode, sheet_name, date_format,
                                            progress)
    except ImportCancelled:
        # Not a bad file — the user stopped it. Re-raised so the caller rolls the
        # transaction back and records CANCELLED; swallowing it here would report
        # a deliberate stop as an unreadable file.
        raise
    except Exception as exc:  # noqa: BLE001 - a bad file must not 500
        # Classified rather than assumed. This used to report *every* failure as
        # an unreadable file and put ``str(exc)`` in the downloadable CSV, so a
        # missing table reached the user as a live INSERT statement telling them
        # to re-save their spreadsheet — wrong twice over, and a leak besides.
        #
        # The log keeps the whole traceback either way, and is where the Import
        # Job ID in the user's message points.
        logger.exception(
            "upload %s failed for %s (%s)",
            batch.upload_uuid, stored.original_name,
            "bad file" if is_file_fault(exc) else "system error",
        )
        batch.status = UploadStatus.FAILED
        batch.completed_at = _now()
        batch.message, issue = failure_issue(exc, job_id=batch.upload_uuid)
        _store_issues(session, batch, [issue])
        discard(batch.stored_path)
        batch.stored_path = None
        progress.finish(message=batch.message, failed=True)
        return UploadOutcome(batch=batch, preview=[], columns=[], issues=[],
                             warnings=[])

    batch.validated_at = _now()
    batch.status = (
        UploadStatus.FAILED if batch.valid_rows == 0 else UploadStatus.VALIDATED
    )
    if batch.status == UploadStatus.FAILED:
        discard(batch.stored_path)
        batch.stored_path = None
    # Stored rather than returned. The request that queued this job answered long
    # ago, so the rows the operator reviews before confirming have to be fetched
    # from the batch afterwards; ``GET /history/{upload_id}`` serves them.
    batch.preview = {
        "columns": outcome.columns,
        "rows": outcome.preview,
        "shown": len(outcome.preview),
        "limit": PREVIEW_ROWS,
        "warnings": outcome.warnings,
    }
    progress.finish(
        failed=batch.status == UploadStatus.FAILED,
        message=batch.message,
        total_records=batch.total_rows or 0,
        processed_records=batch.total_rows or 0,
        valid_records=batch.valid_rows or 0,
        invalid_records=batch.invalid_rows or 0,
        failed_records=batch.invalid_rows or 0,
    )
    return outcome


def _validate_master(session: Session, batch: UploadBatch, upload_type: UploadType,
                     stored: StoredUpload, import_mode: str,
                     sheet_name: str | None,
                     progress: ProgressReporter) -> UploadOutcome:
    reader = stored.reader(sheet_name=sheet_name, progress=progress)
    validation = master_loader.validate(session, upload_type, reader, import_mode,
                                        progress)

    columns = [c.name for c in upload_type.columns]
    preview = [
        {"__row": row.row_number,
         "__status": "INVALID" if row.issues else ("DUPLICATE"
                                                   if row.action == "DUPLICATE"
                                                   else row.action),
         **{name: _cell(row.raw.get(name)) for name in validation.headers}}
        for row in validation.rows[:PREVIEW_ROWS]
    ]

    batch.total_rows = validation.total_rows
    batch.column_count = len(validation.headers)
    batch.valid_rows = len(validation.valid_rows)
    batch.invalid_rows = len(validation.invalid_row_numbers)
    batch.duplicate_rows = validation.duplicate_rows
    batch.message = validation.fatal
    batch.summary = {
        "unmapped_columns": validation.unmapped_columns,
        "missing_columns": validation.missing_columns,
        "by_error_code": _count(i.error_code for i in validation.all_issues),
        "actions": _count(r.action for r in validation.rows if r.ok),
    }

    issues = validation.all_issues
    _store_issues(session, batch, issues)

    warnings = []
    if validation.unmapped_columns:
        warnings.append(
            "These columns are not part of this upload type and will be ignored: "
            + ", ".join(validation.unmapped_columns)
        )
    return UploadOutcome(batch=batch, preview=preview,
                         columns=["__row", "__status", *validation.headers],
                         issues=issues, warnings=warnings)


def _validate_transaction(session: Session, batch: UploadBatch,
                          upload_type: UploadType, stored: StoredUpload,
                          import_mode: str, sheet_name: str | None,
                          date_format: str | None,
                          progress: ProgressReporter) -> UploadOutcome:
    """Dry-run the Phase 2 pipeline: every check, no writes."""
    # One reader, shared by all three consumers below. A reader parses its
    # source once, caches the rows and hands out a fresh iterator every time it
    # is asked — so headers, the preview and the pipeline read the file once
    # between them. They used to take a reader each, which meant parsing the
    # workbook three times: on a 100,000-row file that was two whole extra
    # reads, 57 of the 161 seconds a full upload took.
    #
    # Validation and commit still read it once each, and that pair is not
    # reducible: this run is a dry run that rolls its own work back, so the
    # commit has nothing to inherit and genuinely has to read the file again.
    reader = stored.reader(sheet_name=sheet_name, progress=progress)
    headers = [str(h) for h in reader.headers if h is not None]
    preview_rows = _read_preview(reader)

    # The ETL opens its own session against the same database. Committing the
    # batch row first releases this session's write lock, which SQLite requires
    # and PostgreSQL benefits from — and it means a crash mid-import still
    # leaves an auditable batch behind rather than nothing at all.
    session.commit()

    result = run_import(
        get_engine(), upload_type.data_type, reader,
        source_system=SOURCE_SYSTEM,
        load_mode=LOAD_MODE_BY_IMPORT_MODE.get(import_mode, LOAD_MODE_INCREMENTAL),
        date_format=date_format, dry_run=True, progress=progress,
    )
    issues = issues_from_result(result)

    batch.total_rows = result.total_rows
    batch.column_count = len(headers)
    batch.valid_rows = result.valid_rows
    batch.invalid_rows = result.rejected_rows
    batch.duplicate_rows = result.duplicate_rows
    batch.message = result.message
    batch.summary = {
        "unmapped_columns": result.unmapped_columns,
        "missing_columns": result.missing_columns,
        "by_error_code": result.error_counts,
        "by_category": result.category_counts,
        "business_key": upload_type.business_key_description,
    }
    _store_issues(session, batch, issues)

    invalid_rows = {issue.row_number for issue in issues if issue.row_number}
    # What the pipeline made of each line — the resolved identity codes, the
    # volume it computed, and the reason if it was turned away. Shown *before*
    # the raw file columns, because that is the order the question is asked in:
    # which line, what did we get, was it taken.
    resolved, resolved_columns = _resolved_preview_columns(result)
    preview = [
        {"__row": number,
         "__status": "INVALID" if number in invalid_rows else "VALID",
         **resolved.get(number, {}),
         **{header: _cell(values.get(header)) for header in headers}}
        for number, values in preview_rows
    ]

    warnings = []
    if result.unmapped_columns:
        warnings.append(
            "These columns are not part of this data type and will be ignored: "
            + ", ".join(result.unmapped_columns)
        )
    if result.duplicate_rows:
        warnings.append(
            f"{result.duplicate_rows} row(s) match a transaction already in the "
            f"warehouse ({upload_type.business_key_description})."
        )
    if result.volume_gaps:
        missing = sum(result.volume_gaps.values())
        warnings.append(
            f"{missing} row(s) were loaded without a Total Volume because the "
            "file did not supply one. The sales figures are unaffected and the "
            "volume is left empty rather than calculated from the quantity — "
            "add the column at source to report volume for these lines."
        )
    return UploadOutcome(batch=batch, preview=preview,
                         columns=["__row", "__status", *resolved_columns, *headers],
                         issues=issues, warnings=warnings)


def _resolved_preview_columns(
    result: ImportResult,
) -> tuple[dict[int, dict[str, Any]], list[str]]:
    """Line-level preview cells, keyed by source row number.

    Keys are ``__``-prefixed so they can never collide with a column name from
    the uploaded file, which is the same reason ``__row`` and ``__status`` are
    spelled that way.
    """
    if not result.lines:
        return {}, []

    names = [name for name in LINE_REPORT_FIELDS if name in result.lines[0].fields]
    columns = [f"__{name}" for name in (*names, "volume", "reason")]
    cells = {
        line.row_number: {
            **{f"__{name}": _cell(line.fields.get(name)) for name in names},
            "__volume": _cell(line.volume),
            "__reason": line.reason or ("Valid" if line.status == ACCEPTED else None),
        }
        for line in result.lines
    }
    return cells, columns


def _read_preview(reader: SourceReader) -> list[tuple[int, dict[str, Any]]]:
    """The first ``PREVIEW_ROWS`` rows, so a large file is never sent whole.

    Reads through the reader its caller is already holding rather than opening
    the staged file a second time. Stopping early is safe to do to a shared
    reader — ``__iter__`` returns a new iterator over the cached rows on every
    call — and note that it saves no *reading*: a reader parses its whole source
    before yielding a first row, so the break only avoids copying the rest.
    """
    rows: list[tuple[int, dict[str, Any]]] = []
    for source_row in reader:
        rows.append((source_row.row_number,
                     {str(k): v for k, v in source_row.values.items()}))
        if len(rows) >= PREVIEW_ROWS:
            break
    return rows


def issues_from_result(result: ImportResult) -> list[UploadIssue]:
    """Turn a pipeline result's rejections into user-facing issues.

    A dry run rolls its whole transaction back, so ``etl_rejected_records`` is
    empty afterwards. The pipeline therefore carries its rejections on the
    result object, and this is the one place that translates them — so a
    validation preview and a committed import explain a failure identically.
    """
    issues: list[UploadIssue] = []
    if result.missing_columns:
        issues.append(UploadIssue(
            row_number=1, column=", ".join(result.missing_columns), value=None,
            error_code="MISSING_SOURCE_COLUMN",
            message=result.message or "Required columns are missing.",
            suggested_fix=suggested_fix("MISSING_SOURCE_COLUMN"),
        ))
        return issues

    for rejection in result.rejections:
        issues.append(UploadIssue(
            row_number=rejection.row_number,
            column=rejection.field_name,
            value=rejection.field_value,
            error_code=rejection.error.code,
            message=rejection.message,
            suggested_fix=(suggested_fix(rejection.error.code)
                           or hierarchy_fix(rejection.error.code)),
            category=rejection.error.category,
        ))
    return issues


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


def commit_upload(session: Session, batch: UploadBatch, upload_type: UploadType,
                  *, sheet_name: str | None = None,
                  date_format: str | None = None,
                  progress: ProgressReporter | None = None) -> UploadOutcome:
    """Apply a validated upload for real.

    Accepts ``QUEUED`` as well as ``VALIDATED``: the endpoint checks the batch is
    validated *before* handing it to a worker and marks it queued in the same
    breath, so by the time this runs the status reflects the job rather than the
    validation. The check stays because this is also reachable directly, and an
    import of something that was never validated must still be refused.
    """
    progress = progress or ProgressReporter(None)
    if batch.status not in (UploadStatus.VALIDATED, UploadStatus.QUEUED):
        raise ValueError(
            f"This upload is {batch.status.lower()}; only a validated upload can be "
            "imported."
        )
    if not batch.stored_path or not Path(batch.stored_path).exists():
        raise ValueError(
            "The uploaded file is no longer available. Please upload it again."
        )

    stored = StoredUpload(
        path=Path(batch.stored_path), original_name=batch.file_name,
        extension=Path(batch.stored_path).suffix.lower(), size=batch.file_size,
        content_type=batch.content_type,
    )

    batch.status = UploadStatus.IMPORTING
    session.flush()
    progress.counts(upload_id=batch.upload_id)

    # Previous validation errors belong to the previous run; the commit
    # re-validates, so stale rows would double-count.
    session.execute(delete(UploadError).where(UploadError.upload_id == batch.upload_id))

    if upload_type.category == UploadCategory.MASTER:
        outcome = _commit_master(session, batch, upload_type, stored, sheet_name,
                                 progress)
    else:
        outcome = _commit_transaction(session, batch, upload_type, stored,
                                      sheet_name, date_format, progress)

    batch.completed_at = _now()
    if batch.valid_rows == 0:
        # Nothing reached the warehouse — the whole upload failed, even though
        # individual rows were read successfully.
        batch.status = UploadStatus.FAILED
    elif batch.invalid_rows:
        # Some rows loaded and some did not: PARTIAL, so the history makes the
        # difference visible rather than reporting a clean success.
        batch.status = UploadStatus.PARTIAL
    else:
        batch.status = UploadStatus.COMPLETED

    discard(batch.stored_path)
    batch.stored_path = None
    progress.finish(
        failed=batch.status == UploadStatus.FAILED,
        message=batch.message,
        total_records=batch.total_rows or 0,
        processed_records=batch.total_rows or 0,
        valid_records=batch.valid_rows or 0,
        invalid_records=batch.invalid_rows or 0,
        imported_records=(batch.inserted_rows or 0) + (batch.updated_rows or 0),
        failed_records=batch.invalid_rows or 0,
    )
    return outcome


def _commit_master(session: Session, batch: UploadBatch, upload_type: UploadType,
                   stored: StoredUpload, sheet_name: str | None,
                   progress: ProgressReporter) -> UploadOutcome:
    reader = stored.reader(sheet_name=sheet_name, progress=progress)
    validation = master_loader.validate(session, upload_type, reader,
                                        batch.import_mode, progress)
    load_result = master_loader.load(session, upload_type, validation, progress)

    batch.total_rows = validation.total_rows
    batch.column_count = len(validation.headers)
    batch.valid_rows = len(validation.valid_rows)
    batch.invalid_rows = len(validation.invalid_row_numbers)
    batch.duplicate_rows = validation.duplicate_rows
    batch.inserted_rows = load_result.inserted
    batch.updated_rows = load_result.updated
    batch.summary = {
        **(batch.summary or {}),
        "unmapped_columns": validation.unmapped_columns,
        "by_error_code": _count(i.error_code for i in validation.all_issues),
        "unchanged_rows": load_result.unchanged,
    }
    batch.message = (
        f"{load_result.inserted} inserted, {load_result.updated} updated, "
        f"{load_result.unchanged} already up to date."
    )
    issues = validation.all_issues
    _store_issues(session, batch, issues)
    return UploadOutcome(batch=batch, preview=[], columns=[], issues=issues,
                         warnings=[])


def _commit_transaction(session: Session, batch: UploadBatch,
                        upload_type: UploadType, stored: StoredUpload,
                        sheet_name: str | None,
                        date_format: str | None,
                        progress: ProgressReporter) -> UploadOutcome:
    # See ``_validate_transaction``: release the write lock before the ETL's own
    # session opens one.
    session.commit()

    result = run_import(
        get_engine(), upload_type.data_type,
        stored.reader(sheet_name=sheet_name, progress=progress),
        source_system=SOURCE_SYSTEM,
        load_mode=LOAD_MODE_BY_IMPORT_MODE.get(batch.import_mode,
                                               LOAD_MODE_INCREMENTAL),
        date_format=date_format, dry_run=False, progress=progress,
    )

    batch.etl_batch_id = result.batch_id
    batch.total_rows = result.total_rows
    batch.valid_rows = result.valid_rows
    batch.invalid_rows = result.rejected_rows
    batch.duplicate_rows = result.duplicate_rows
    batch.inserted_rows = result.inserted_rows
    batch.updated_rows = result.updated_rows
    batch.summary = {
        **(batch.summary or {}),
        "unmapped_columns": result.unmapped_columns,
        "by_error_code": result.error_counts,
        "by_category": result.category_counts,
        "etl_status": result.status,
    }
    batch.message = result.message or (
        f"{result.inserted_rows} inserted, {result.updated_rows} updated, "
        f"{result.rejected_rows} rejected."
    )

    issues = issues_from_result(result)
    _store_issues(session, batch, issues)
    return UploadOutcome(batch=batch, preview=[], columns=[], issues=issues,
                         warnings=[])


def issues_from_etl_batch(session: Session, etl_batch_id: int) -> list[UploadIssue]:
    """Read a Phase 2 batch's rejections and dress them for the file's author."""
    rows = session.execute(
        select(EtlRejectedRecord)
        .where(EtlRejectedRecord.batch_id == etl_batch_id)
        .order_by(EtlRejectedRecord.source_row_number, EtlRejectedRecord.id)
    ).scalars().all()
    return [
        UploadIssue(
            row_number=row.source_row_number,
            column=row.field_name,
            value=row.field_value,
            error_code=row.error_code,
            message=row.error_message,
            suggested_fix=suggested_fix(row.error_code) or hierarchy_fix(row.error_code),
            category=row.error_category,
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def rollback_batch(session: Session, batch: UploadBatch, *, by: str) -> dict[str, int]:
    """Remove the fact rows one transactional upload created.

    Deliberately narrow. Only rows carrying this batch's ``import_batch_id`` are
    removed, so a transaction that a *later* batch updated is left alone — its
    ``import_batch_id`` now names that later batch. Master-data uploads cannot be
    rolled back at all: an upsert overwrote the previous values without keeping
    them, so "undo" would mean inventing data.
    """
    if batch.category != UploadCategory.TRANSACTIONAL or batch.etl_batch_id is None:
        raise ValueError(
            "Only a transactional import can be rolled back. A master-data upload "
            "updates records in place and has no previous version to restore."
        )
    if batch.rolled_back_at is not None:
        raise ValueError("This upload has already been rolled back.")

    etl_batch = session.get(EtlImportBatch, batch.etl_batch_id)
    if etl_batch is None:
        raise ValueError("The import batch for this upload no longer exists.")

    fact_model = FACT_MODEL_BY_DATA_TYPE[etl_batch.data_type]
    staging_model = STAGING_MODEL_BY_DATA_TYPE[etl_batch.data_type]

    removed = session.execute(
        delete(fact_model.__table__)
        .where(fact_model.__table__.c.import_batch_id == batch.etl_batch_id)
    ).rowcount or 0
    session.execute(
        delete(staging_model.__table__)
        .where(staging_model.__table__.c.import_batch_id == batch.etl_batch_id)
    )

    etl_batch.status = "ROLLED_BACK"
    etl_batch.error_summary = {
        **(etl_batch.error_summary or {}),
        "rolled_back_by": by,
        "rolled_back_at": _now().isoformat(),
    }
    batch.status = UploadStatus.ROLLED_BACK
    batch.rolled_back_at = _now()
    batch.rolled_back_by = by
    batch.message = f"Rolled back: {removed} fact row(s) removed."
    return {"fact_rows_removed": removed, "etl_batch_id": batch.etl_batch_id}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store_issues(session: Session, batch: UploadBatch,
                  issues: list[UploadIssue]) -> None:
    if not issues:
        return
    session.add_all([
        UploadError(
            upload_id=batch.upload_id,
            row_number=issue.row_number,
            column_name=(issue.column or "")[:128] or None,
            value=issue.value,
            error_code=issue.error_code[:64],
            error_category=issue.category[:32],
            error_message=issue.message,
            suggested_fix=issue.suggested_fix,
            severity=issue.severity or SEVERITY_ERROR,
        )
        for issue in issues
    ])
    session.flush()


def _count(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _cell(value: Any) -> Any:
    """A preview cell: JSON-safe and bounded."""
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)[:200]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def upload_summary(session: Session) -> dict[str, Any]:
    """Counts for the Data Upload dashboard cards."""
    since = _now() - timedelta(days=1)
    by_status = dict(session.execute(
        select(UploadBatch.status, func.count()).group_by(UploadBatch.status)
    ).all())
    today = session.execute(
        select(func.count()).select_from(UploadBatch)
        .where(UploadBatch.started_at >= since)
    ).scalar_one()
    failed_rows = session.execute(
        select(func.coalesce(func.sum(UploadBatch.invalid_rows), 0))
    ).scalar_one()
    return {
        "total_uploads": sum(by_status.values()),
        "by_status": by_status,
        "pending_imports": by_status.get(UploadStatus.VALIDATED, 0)
                           + by_status.get(UploadStatus.UPLOADED, 0),
        "failed_imports": by_status.get(UploadStatus.FAILED, 0),
        "partial_imports": by_status.get(UploadStatus.PARTIAL, 0),
        "completed_imports": by_status.get(UploadStatus.COMPLETED, 0),
        "uploads_last_24h": today,
        "failed_records": int(failed_rows or 0),
    }


__all__ = [
    "PREVIEW_ROWS",
    "UploadOutcome",
    "validate_upload",
    "commit_upload",
    "rollback_batch",
    "batch_to_dict",
    "issues_from_etl_batch",
    "upload_summary",
    "LOAD_MODE_BY_IMPORT_MODE",
    "SOURCE_SYSTEM",
]
