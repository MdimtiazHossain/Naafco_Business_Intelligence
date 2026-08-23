"""ETL batch and data-quality endpoints.

Every endpoint requires the **Data Quality** section. The batch history
names source files and rejection reasons, which is operational detail about
the warehouse rather than a business report, so it is gated in its own right
rather than riding on a reporting permission.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database.models_warehouse import (
    STATUS_PENDING_SOURCE_DATA,
    EtlImportBatch,
    MasterSourceStatus,
)
from ..etl.datasets import DATASETS, DATA_TYPES
from ..etl.errors import ALL_ERRORS
from ..etl.quality import (
    batch_quality,
    line_report,
    quality_overview,
    rejected_records,
    transaction_quality,
)
from ..ai.permission_filter import UserContext
from ..auth.permissions import require_section
from ..security.sections import SectionKey
from .deps import get_session, internal_error

router = APIRouter(prefix="/api", tags=["etl"])

#: Every route here is gated by the Data Quality section.
QualityDep = Depends(require_section(SectionKey.DATA_QUALITY))


@router.get("/etl/batches")
def list_batches(
    data_type: str | None = Query(None),
    source_system: str | None = Query(None),
    batch_status: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
    _: UserContext = QualityDep,
) -> dict[str, Any]:
    """Import batches, newest first."""
    conditions = []
    if data_type:
        conditions.append(EtlImportBatch.data_type == data_type)
    if source_system:
        conditions.append(EtlImportBatch.source_system == source_system)
    if batch_status:
        conditions.append(EtlImportBatch.status == batch_status)

    try:
        total = session.execute(
            select(func.count()).select_from(EtlImportBatch).where(*conditions)
        ).scalar_one()
        batches = session.execute(
            select(EtlImportBatch).where(*conditions)
            .order_by(EtlImportBatch.batch_id.desc())
            .limit(limit).offset(offset)
        ).scalars().all()
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "list batches") from exc

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "batches": [_batch_dict(batch) for batch in batches],
    }


@router.get("/etl/batches/{batch_id}")
def get_batch(batch_id: int, session: Session = Depends(get_session),
              _: UserContext = QualityDep) -> dict[str, Any]:
    """One batch with its rejection breakdown."""
    batch = session.get(EtlImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Batch {batch_id} not found.")
    payload = _batch_dict(batch)
    payload["quality"] = batch_quality(session, batch_id)
    return payload


@router.get("/data-quality/master-mapping")
def master_mapping_status(
    session: Session = Depends(get_session),
    _: UserContext = QualityDep,
) -> dict[str, Any]:
    """Master Data Mapping Status: how much of the new links could be derived.

    Declared *before* ``/data-quality/{batch_id}``: FastAPI matches routes in
    declaration order, so a literal path that could also read as a parameter
    has to come first or the parameterised one swallows it and fails trying to
    parse "master-mapping" as an integer.

    Read-only and computed on demand — it writes nothing, so it can be
    refreshed while deciding what to do about what it reports. Applying the
    mapping is a deliberate, separate act:
    ``python scripts/map_master_data.py --apply``.

    Under Data Quality rather than Administration because that is what it is: a
    report about how complete and how consistent the master data is, alongside
    the batch rejections and the source-status registry.
    """
    from ..datamgmt import mapping

    try:
        return mapping.summary(session)
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "master mapping status") from exc


@router.get("/data-quality/{batch_id}/validation")
def get_transaction_validation(
    batch_id: int,
    session: Session = Depends(get_session),
    _: UserContext = QualityDep,
) -> dict[str, Any]:
    """Validation results for one batch: valid, duplicate, invalid, missing.

    Declared before ``/data-quality/{batch_id}`` for the same reason
    ``master-mapping`` is — a literal segment has to be offered to the router
    ahead of the pattern that could also swallow it.
    """
    report = transaction_quality(session, batch_id)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Batch {batch_id} not found.")
    return report


@router.get("/data-quality/{batch_id}/lines")
def get_batch_lines(
    batch_id: int,
    line_status: str | None = Query(
        None, alias="status", pattern="^(ACCEPTED|REJECTED)$",
        description="Show only accepted or only rejected lines."),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
    _: UserContext = QualityDep,
) -> dict[str, Any]:
    """Every line of a batch with its identity, volume and verdict.

    The duplicate report: accepted and rejected lines in source order, so the
    two rows of the same invoice that differ only by batch code can be seen to
    have been treated differently and why.
    """
    report = line_report(session, batch_id, status=line_status,
                         limit=limit, offset=offset)
    if report is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Batch {batch_id} not found, or its data type has no staged lines.")
    return report


@router.get("/data-quality/{batch_id}")
def get_batch_quality(
    batch_id: int,
    include_records: bool = Query(False, description="Include rejected rows."),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    error_code: str | None = Query(None),
    session: Session = Depends(get_session),
    _: UserContext = QualityDep,
) -> dict[str, Any]:
    """Data-quality summary for one batch: totals and every rejection reason."""
    summary = batch_quality(session, batch_id)
    if summary is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Batch {batch_id} not found.")
    if include_records:
        summary["rejected_records"] = rejected_records(
            session, batch_id, limit=limit, offset=offset, error_code=error_code
        )
    return summary


@router.get("/data-quality")
def get_quality_overview(
    data_type: str | None = Query(None),
    source_system: str | None = Query(None),
    session: Session = Depends(get_session),
    _: UserContext = QualityDep,
) -> dict[str, Any]:
    """Data quality aggregated across every batch."""
    return quality_overview(session, data_type=data_type, source_system=source_system)


@router.get("/data-quality/master-sources/status")
def master_source_status(session: Session = Depends(get_session),
                         _: UserContext = QualityDep) -> dict[str, Any]:
    """Which master dimensions have a real source and which are still pending."""
    rows = session.execute(select(MasterSourceStatus)).scalars().all()
    return {
        "sources": [
            {
                "table_name": row.table_name,
                "status": row.status,
                "source_description": row.source_description,
                "note": row.note,
            }
            for row in rows
        ],
        "pending": [
            row.table_name for row in rows if row.status == STATUS_PENDING_SOURCE_DATA
        ],
    }


@router.get("/etl/datasets")
def list_datasets(_: UserContext = QualityDep) -> dict[str, Any]:
    """The supported data types, their fields and their business keys."""
    return {
        "data_types": list(DATA_TYPES),
        "datasets": [
            {
                "data_type": dataset.data_type,
                "staging_table": dataset.staging_table,
                "fact_table": dataset.fact_table,
                "description": dataset.description,
                "business_key": dataset.business_key_definition,
                "date_field": dataset.date_field,
                "required_fields": list(dataset.required_fields),
                "fields": [
                    {
                        "name": f.name,
                        "kind": f.kind,
                        "required": f.required,
                        "allow_negative": f.allow_negative,
                        "aliases": list(f.aliases),
                        "description": f.description,
                    }
                    for f in dataset.fields
                ],
            }
            for dataset in DATASETS
        ],
    }


@router.get("/etl/error-codes")
def list_error_codes(_: UserContext = QualityDep) -> list[dict[str, str]]:
    """The rejection catalogue."""
    return [
        {"error_code": spec.code, "category": spec.category, "description": spec.description}
        for spec in sorted(ALL_ERRORS, key=lambda s: (s.category, s.code))
    ]


def _batch_dict(batch: EtlImportBatch) -> dict[str, Any]:
    return {
        "batch_id": batch.batch_id,
        "batch_uuid": batch.batch_uuid,
        "data_type": batch.data_type,
        "source_type": batch.source_type,
        "source_system": batch.source_system,
        "source_file": batch.source_file,
        "load_mode": batch.load_mode,
        "status": batch.status,
        "started_at": batch.started_at,
        "completed_at": batch.completed_at,
        "total_rows": batch.total_rows,
        "successful_rows": batch.successful_rows,
        "failed_rows": batch.failed_rows,
        "duplicate_rows": batch.duplicate_rows,
        "inserted_rows": batch.inserted_rows,
        "updated_rows": batch.updated_rows,
        "error_summary": batch.error_summary,
    }
