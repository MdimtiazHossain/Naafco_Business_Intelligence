"""Transaction import endpoints — the machine-to-machine import surface.

``POST /api/import/{data_type}`` accepts either an uploaded Excel/CSV file or a
JSON body of records. The JSON path is the same seam a future SAP or Sales
Force App integration uses, so those systems need no new endpoint shape.

This is *not* the Data Upload Center. The upload centre
(``/api/data-upload/*``) adds the human workflow — templates, a preview, an
explicit confirmation, an error report and an upload history — on top of the
same ETL pipeline. These endpoints stay for integrations, but Phase 4 closes
the hole they had: they required no authentication at all, and now require the
**Data Upload** section like everything else that writes to the warehouse.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..ai.permission_filter import UserContext
from ..auth import audit
from ..auth.permissions import require_section
from ..database.connection import get_engine
from ..database.models_ai import AuditAction
from ..etl.datasets import DATA_TYPES
from ..etl.pipeline import (
    LOAD_MODE_INCREMENTAL,
    LOAD_MODE_INITIAL,
    LOAD_MODE_REPROCESS,
    run_import,
)
from ..etl.readers import RecordsSourceReader, reader_for_file
from ..security.sections import SectionKey
from .deps import get_session, internal_error

router = APIRouter(prefix="/api/import", tags=["import"])

#: Writing to the warehouse requires the Data Upload section, whether the caller
#: is a person or an integration holding a service account's token.
UploadDep = Depends(require_section(SectionKey.DATA_UPLOAD))

ALLOWED_SUFFIXES = {".xlsx", ".xlsm", ".csv", ".tsv", ".txt"}
LOAD_MODES = {LOAD_MODE_INITIAL, LOAD_MODE_INCREMENTAL, LOAD_MODE_REPROCESS}


class RecordsImportRequest(BaseModel):
    """JSON import payload — the integration seam for SAP / Sales Force App."""

    records: list[dict[str, Any]] = Field(..., description="Rows as key/value objects.")
    source_system: str = Field("API", description="e.g. SAP, SALES_APP, DEMO.")
    source_name: str = Field("api", description="Logical name recorded on the batch.")
    load_mode: str = Field(LOAD_MODE_INCREMENTAL)
    date_format: str | None = Field(
        None, description="e.g. 'DD/MM/YYYY'. Omit only when dates are unambiguous."
    )
    dry_run: bool = False


def _validate(data_type: str, load_mode: str) -> None:
    if data_type not in DATA_TYPES:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Unknown data type '{data_type}'. Supported: {', '.join(DATA_TYPES)}.",
        )
    if load_mode not in LOAD_MODES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown load mode '{load_mode}'. Supported: {', '.join(sorted(LOAD_MODES))}.",
        )


@router.post("/{data_type}", status_code=status.HTTP_201_CREATED)
async def import_file(
    data_type: str,
    http_request: Request,
    file: UploadFile = File(..., description="Excel or CSV transaction file."),
    source_system: str = Form("MANUAL"),
    load_mode: str = Form(LOAD_MODE_INCREMENTAL),
    date_format: str | None = Form(None),
    sheet_name: str | None = Form(None),
    dry_run: bool = Form(False),
    session: Session = Depends(get_session),
    user: UserContext = UploadDep,
) -> dict[str, Any]:
    """Import an uploaded transaction file through the full ETL pipeline."""
    _validate(data_type, load_mode)

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"Unsupported file type '{suffix}'. Allowed: "
            f"{', '.join(sorted(ALLOWED_SUFFIXES))}.",
        )

    # The upload is staged in a temp file so the reader can seek it; the
    # original file name is what gets recorded for audit.
    #
    # Staging and the pipeline are both blocking and a large file keeps them busy
    # for minutes. This endpoint is ``async``, so running them inline would hold
    # the event loop for the whole import and make the server unresponsive to
    # every other request until it finished.
    def _run() -> Any:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / Path(file.filename).name
            with path.open("wb") as handle:
                shutil.copyfileobj(file.file, handle)
            reader = reader_for_file(path, sheet_name=sheet_name)
            return run_import(
                get_engine(), data_type, reader,
                source_system=source_system, load_mode=load_mode,
                date_format=date_format, dry_run=dry_run,
            )

    try:
        result = await run_in_threadpool(_run)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - never leak internals
        raise internal_error(exc, f"import {data_type}") from exc

    payload = result.to_dict()
    payload["source_file"] = file.filename
    _audit(session, http_request, user, data_type, result, file.filename)
    return payload


def _audit(session: Session, request: Request, user: UserContext, data_type: str,
           result, source: str | None) -> None:
    """Record who loaded what. Never records the rows themselves."""
    audit.record(
        session, action=AuditAction.DATA_IMPORTED, user_id=user.user_id,
        username=user.username, resource=f"import:{data_type}",
        ip_address=audit.client_ip(request),
        success=result.status not in ("FAILED",),
        detail={"source": source, "batch_id": result.batch_id,
                "status": result.status, "total_rows": result.total_rows,
                "valid_rows": result.valid_rows,
                "rejected_rows": result.rejected_rows},
    )
    session.commit()


@router.post("/{data_type}/records", status_code=status.HTTP_201_CREATED)
def import_records(
    data_type: str,
    http_request: Request,
    request: RecordsImportRequest = Body(...),
    session: Session = Depends(get_session),
    user: UserContext = UploadDep,
) -> dict[str, Any]:
    """Import records supplied as JSON (SAP / Sales Force App integration seam)."""
    _validate(data_type, request.load_mode)
    if not request.records:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No records were supplied.")

    reader = RecordsSourceReader(
        request.records, source_name=request.source_name, source_type="API"
    )
    try:
        result = run_import(
            get_engine(), data_type, reader,
            source_system=request.source_system, load_mode=request.load_mode,
            date_format=request.date_format, dry_run=request.dry_run,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, f"import {data_type} records") from exc
    _audit(session, http_request, user, data_type, result, request.source_name)
    return result.to_dict()
