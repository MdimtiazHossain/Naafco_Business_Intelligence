"""Data-quality reporting over import batches.

Answers the question the business actually asks after an import: *how many rows
made it, how many did not, and exactly why not* — broken down by the categories
in ``errors.ErrorCategory``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database.models_warehouse import (
    FACT_MODEL_BY_DATA_TYPE,
    STAGING_MODEL_BY_DATA_TYPE,
    STAGING_VALID,
    EtlImportBatch,
    EtlRejectedRecord,
)
from .errors import ERROR_BY_CODE, ErrorCategory

#: Categories always present in a summary, so a consumer can rely on the shape.
CATEGORY_ORDER: tuple[str, ...] = (
    ErrorCategory.DUPLICATE,
    ErrorCategory.INVALID_MASTER,
    ErrorCategory.INVALID_HIERARCHY,
    ErrorCategory.INVALID_DATE,
    ErrorCategory.INVALID_NUMERIC,
    ErrorCategory.MISSING_REQUIRED_FIELD,
    ErrorCategory.STRUCTURE,
)


def batch_quality(session: Session, batch_id: int) -> dict[str, Any] | None:
    """Data-quality summary for one import batch."""
    batch = session.get(EtlImportBatch, batch_id)
    if batch is None:
        return None

    by_code = dict(
        session.execute(
            select(EtlRejectedRecord.error_code, func.count())
            .where(EtlRejectedRecord.batch_id == batch_id)
            .group_by(EtlRejectedRecord.error_code)
        ).all()
    )
    by_category = dict(
        session.execute(
            select(EtlRejectedRecord.error_category, func.count())
            .where(EtlRejectedRecord.batch_id == batch_id)
            .group_by(EtlRejectedRecord.error_category)
        ).all()
    )

    total = batch.total_rows or 0
    rejected = batch.failed_rows or 0
    return {
        "batch_id": batch.batch_id,
        "batch_uuid": batch.batch_uuid,
        "data_type": batch.data_type,
        "source_system": batch.source_system,
        "source_type": batch.source_type,
        "source_file": batch.source_file,
        "load_mode": batch.load_mode,
        "status": batch.status,
        "started_at": batch.started_at,
        "completed_at": batch.completed_at,
        "totals": {
            "total_imported": total,
            "valid": batch.successful_rows or 0,
            "rejected": rejected,
            "duplicate": batch.duplicate_rows or 0,
            "inserted": batch.inserted_rows or 0,
            "updated": batch.updated_rows or 0,
            "rejection_rate_percent": round(rejected / total * 100, 2) if total else 0.0,
        },
        "by_category": {category: by_category.get(category, 0) for category in CATEGORY_ORDER},
        "by_error_code": [
            {
                "error_code": code,
                "count": count,
                "category": ERROR_BY_CODE[code].category if code in ERROR_BY_CODE else None,
                "description": (
                    ERROR_BY_CODE[code].description if code in ERROR_BY_CODE else None
                ),
            }
            for code, count in sorted(by_code.items(), key=lambda kv: -kv[1])
        ],
    }


def rejected_records(session: Session, batch_id: int, *, limit: int = 100,
                     offset: int = 0, error_code: str | None = None) -> dict[str, Any]:
    """Paginated rejected rows for a batch, newest first."""
    conditions = [EtlRejectedRecord.batch_id == batch_id]
    if error_code:
        conditions.append(EtlRejectedRecord.error_code == error_code)

    total = session.execute(
        select(func.count()).select_from(EtlRejectedRecord).where(*conditions)
    ).scalar_one()
    rows = session.execute(
        select(EtlRejectedRecord).where(*conditions)
        .order_by(EtlRejectedRecord.id)
        .limit(limit).offset(offset)
    ).scalars().all()

    return {
        "batch_id": batch_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "records": [
            {
                "id": row.id,
                "source_row_number": row.source_row_number,
                "error_code": row.error_code,
                "error_category": row.error_category,
                "error_message": row.error_message,
                "field_name": row.field_name,
                "field_value": row.field_value,
                "raw_data": row.raw_data,
                "created_at": row.created_at,
            }
            for row in rows
        ],
    }


def quality_overview(session: Session, data_type: str | None = None,
                     source_system: str | None = None) -> dict[str, Any]:
    """Aggregate data quality across every batch, optionally filtered."""
    conditions = []
    if data_type:
        conditions.append(EtlImportBatch.data_type == data_type)
    if source_system:
        conditions.append(EtlImportBatch.source_system == source_system)

    totals = session.execute(
        select(
            func.count(EtlImportBatch.batch_id),
            func.coalesce(func.sum(EtlImportBatch.total_rows), 0),
            func.coalesce(func.sum(EtlImportBatch.successful_rows), 0),
            func.coalesce(func.sum(EtlImportBatch.failed_rows), 0),
            func.coalesce(func.sum(EtlImportBatch.duplicate_rows), 0),
        ).where(*conditions)
    ).one()

    category_query = (
        select(EtlRejectedRecord.error_category, func.count())
        .join(EtlImportBatch, EtlImportBatch.batch_id == EtlRejectedRecord.batch_id)
        .where(*conditions)
        .group_by(EtlRejectedRecord.error_category)
    )
    by_category = dict(session.execute(category_query).all())

    batch_count, total_rows, valid_rows, failed_rows, duplicate_rows = totals
    return {
        "filters": {"data_type": data_type, "source_system": source_system},
        "batches": batch_count,
        "totals": {
            "total_imported": int(total_rows),
            "valid": int(valid_rows),
            "rejected": int(failed_rows),
            "duplicate": int(duplicate_rows),
            "rejection_rate_percent": (
                round(int(failed_rows) / int(total_rows) * 100, 2) if total_rows else 0.0
            ),
        },
        "by_category": {category: by_category.get(category, 0) for category in CATEGORY_ORDER},
    }


# ---------------------------------------------------------------------------
# Line-level reporting
# ---------------------------------------------------------------------------

#: Identity columns a line report shows, in display order. Only the ones the
#: staging table actually has are reported, so each data type is described in
#: its own terms rather than padded out with empty sales columns.
LINE_COLUMNS: tuple[str, ...] = (
    "company_code", "invoice_no", "invoice_line_no",
    "customer_code", "material_code", "batch_code", "quantity",
)

#: Error codes that mean "this line names something the master data does not
#: have", grouped by what is missing. Used by the counters in
#: :func:`transaction_quality`.
MISSING_MASTER_CODES: dict[str, tuple[str, ...]] = {
    "missing_material": ("INVALID_MATERIAL_CODE",),
    "missing_customer": ("INVALID_CUSTOMER_CODE",),
}


def _staging_model(data_type: str):
    return STAGING_MODEL_BY_DATA_TYPE.get(data_type)


def line_report(session: Session, batch_id: int, *, status: str | None = None,
                limit: int = 100, offset: int = 0) -> dict[str, Any] | None:
    """Every line of one batch with its verdict — the duplicate report.

    Reads back what the import wrote rather than recomputing it: staging holds
    one row per source line with the identity codes and the verdict, the
    rejection table holds the reason, and the fact table holds the volume that
    was actually stored. Joining the three is the only way to show an accepted
    and a rejected line side by side, which is what makes the report readable —
    "row 25 rejected as a duplicate, row 26 accepted" says far more than a
    list of failures on its own.

    ``status`` filters to ``ACCEPTED`` or ``REJECTED``. Paginated, because a
    batch may hold a million lines.
    """
    batch = session.get(EtlImportBatch, batch_id)
    if batch is None:
        return None

    model = _staging_model(batch.data_type)
    if model is None:
        return None

    columns = [name for name in LINE_COLUMNS
               if name in model.__table__.columns.keys()]

    conditions = [model.import_batch_id == batch_id]
    if status == "ACCEPTED":
        conditions.append(model.validation_status == STAGING_VALID)
    elif status == "REJECTED":
        conditions.append(model.validation_status != STAGING_VALID)

    total = session.execute(
        select(func.count()).select_from(model).where(*conditions)
    ).scalar_one()
    rows = session.execute(
        select(model).where(*conditions)
        .order_by(model.source_row_number)
        .limit(limit).offset(offset)
    ).scalars().all()

    row_numbers = [row.source_row_number for row in rows]
    reasons = _reasons_by_row(session, batch_id, row_numbers)
    volumes = _volumes_by_row(session, batch.data_type, batch_id, row_numbers)

    records = []
    for row in rows:
        number = row.source_row_number
        accepted = row.validation_status == STAGING_VALID
        code, message = reasons.get(number, (row.validation_error, None))
        volume = volumes.get(number)
        records.append({
            "row": number,
            **{name: getattr(row, name) for name in columns},
            "volume": None if volume is None else str(volume),
            "status": "ACCEPTED" if accepted else "REJECTED",
            "error_code": None if accepted else code,
            "reason": message or ("Valid" if accepted else code),
        })

    return {
        "batch_id": batch_id,
        "data_type": batch.data_type,
        "columns": ["row", *columns, "volume", "status",
                    "error_code", "reason"],
        "total": total,
        "limit": limit,
        "offset": offset,
        "status": status,
        "records": records,
    }


def _reasons_by_row(session: Session, batch_id: int,
                    row_numbers: list[int]) -> dict[int, tuple[str, str]]:
    """First rejection reason per source row. One line, one headline reason."""
    if not row_numbers:
        return {}
    rows = session.execute(
        select(EtlRejectedRecord.source_row_number, EtlRejectedRecord.error_code,
               EtlRejectedRecord.error_message)
        .where(EtlRejectedRecord.batch_id == batch_id)
        .where(EtlRejectedRecord.source_row_number.in_(row_numbers))
        .order_by(EtlRejectedRecord.id)
    ).all()
    reasons: dict[int, tuple[str, str]] = {}
    for number, code, message in rows:
        reasons.setdefault(number, (code, message))
    return reasons


def _volumes_by_row(session: Session, data_type: str, batch_id: int,
                    row_numbers: list[int]) -> dict[int, Any]:
    """The total volume stored for each accepted line, from the fact table."""
    model = FACT_MODEL_BY_DATA_TYPE.get(data_type)
    if model is None or not row_numbers:
        return {}
    if "volume" not in model.__table__.columns.keys():
        return {}
    rows = session.execute(
        select(model.source_row_number, model.volume)
        .where(model.import_batch_id == batch_id)
        .where(model.source_row_number.in_(row_numbers))
    ).all()
    return {number: volume for number, volume in rows}


def transaction_quality(session: Session, batch_id: int) -> dict[str, Any] | None:
    """Validation results for one transaction batch, counter by counter.

    Answers the questions the business asks of an import in the order it asks
    them: how many lines, how many took, how many collided, how many were
    wrong, and then specifically *what* was missing — material, customer, batch,
    invoice line, quantity, Total Volume or net sales.

    "Missing" is not the same as "rejected", and the two are counted apart. A
    line with no batch code is perfectly valid — the key falls back to the
    material — but a file where every batch code is empty is worth knowing
    about. The
    same goes for the volume: those rows loaded, and their sales figures are
    right; only the volume is absent, and ``missing_volume`` is the number that
    tells an operator to fix the column at source rather than expect the
    importer to invent it.
    """
    batch = session.get(EtlImportBatch, batch_id)
    if batch is None:
        return None

    model = _staging_model(batch.data_type)
    staging_columns = set(model.__table__.columns.keys()) if model else set()

    by_category = dict(
        session.execute(
            select(EtlRejectedRecord.error_category,
                   func.count(func.distinct(EtlRejectedRecord.source_row_number)))
            .where(EtlRejectedRecord.batch_id == batch_id)
            .group_by(EtlRejectedRecord.error_category)
        ).all()
    )
    by_code = dict(
        session.execute(
            select(EtlRejectedRecord.error_code,
                   func.count(func.distinct(EtlRejectedRecord.source_row_number)))
            .where(EtlRejectedRecord.batch_id == batch_id)
            .group_by(EtlRejectedRecord.error_code)
        ).all()
    )

    rejected = batch.failed_rows or 0
    duplicate_rejected = by_category.get(ErrorCategory.DUPLICATE, 0)

    counts: dict[str, int] = {
        "total_lines": batch.total_rows or 0,
        "valid_lines": batch.successful_rows or 0,
        # Lines turned away as duplicates, and lines that matched a transaction
        # already in the warehouse. Under INCREMENTAL the second group is
        # updated rather than rejected, so the two are not the same number.
        "duplicate_lines": duplicate_rejected,
        "matched_existing_lines": batch.duplicate_rows or 0,
        "invalid_lines": max(rejected - duplicate_rejected, 0),
        "hierarchy_mapping_conflict": by_category.get(
            ErrorCategory.INVALID_HIERARCHY, 0),
    }
    for name, codes in MISSING_MASTER_CODES.items():
        counts[name] = sum(by_code.get(code, 0) for code in codes)

    # Blank-but-allowed fields, counted from staging rather than from
    # rejections: nothing was rejected for these, which is exactly why they
    # need reporting. These describe the file, so every line counts.
    #
    # Quantity, Total Volume and net sales are counted here together because
    # they are the three measures every report is built on. Quantity and net
    # sales are required and so should always read zero; a non-zero count means
    # a required-field rejection happened and points at which column caused it.
    for name, column in (("missing_batch", "batch_code"),
                         ("missing_invoice_line", "invoice_line_no"),
                         ("missing_quantity", "quantity"),
                         ("missing_net_sales", "net_sales")):
        counts[name] = (
            _blank_count(session, model, batch_id, column)
            if column in staging_columns else 0
        )

    counts["missing_volume"] = _missing_volume_count(
        session, batch.data_type, batch_id)
    summary = batch.error_summary or {}
    gaps = summary.get("volume_gaps") or {}

    total = counts["total_lines"]
    return {
        "batch_id": batch_id,
        "data_type": batch.data_type,
        "source_file": batch.source_file,
        "status": batch.status,
        "counts": counts,
        "volume_gaps": gaps,
        "by_category": {c: by_category.get(c, 0) for c in CATEGORY_ORDER},
        "rejection_rate_percent": (
            round(rejected / total * 100, 2) if total else 0.0),
    }


def _blank_count(session: Session, model: Any, batch_id: int, column: str) -> int:
    """How many staged lines left this column empty.

    Counted across every line of the batch regardless of verdict: this
    describes the *file*, not the import. A file where nothing carries an
    invoice line number is worth reporting whether or not those lines loaded.
    """
    target = getattr(model, column)
    return session.execute(
        select(func.count()).select_from(model)
        .where(model.import_batch_id == batch_id)
        .where((target.is_(None)) | (target == ""))
    ).scalar_one()


def _missing_volume_count(session: Session, data_type: str, batch_id: int) -> int:
    model = FACT_MODEL_BY_DATA_TYPE.get(data_type)
    if model is None or "volume" not in model.__table__.columns.keys():
        return 0
    return session.execute(
        select(func.count()).select_from(model)
        .where(model.import_batch_id == batch_id)
        .where(model.volume.is_(None))
    ).scalar_one()


__all__ = [
    "batch_quality",
    "rejected_records",
    "quality_overview",
    "line_report",
    "transaction_quality",
    "CATEGORY_ORDER",
    "LINE_COLUMNS",
]
