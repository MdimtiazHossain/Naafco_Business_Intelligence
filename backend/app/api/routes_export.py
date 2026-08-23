"""Report export endpoint.

``POST /api/reports/export`` renders already-validated report data as Excel, CSV
or PDF. Rows come either from the request body (echoed back from a chat answer)
or from a stored assistant message the caller owns — never from a fresh,
unrestricted query, so export cannot sidestep the permission scope.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..ai.export import MAX_EXPORT_ROWS, ReportMeta, render, supports_bangla_pdf
from ..ai.permission_filter import UserContext
from ..ai.schemas import ExportRequest
from ..auth import audit
from ..config import get_settings
from ..database.models_ai import AuditAction, ChatMessage
from .deps import get_current_user, get_session, internal_error

logger = logging.getLogger("app.api.export")

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.post("/export")
def export_report(
    request: ExportRequest,
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> Response:
    """Export report rows as ``xlsx``, ``csv`` or ``pdf``."""
    rows: list[dict[str, Any]] = list(request.rows)

    if not rows and request.message_id is not None:
        rows = _rows_from_message(session, user, request.message_id)

    if len(rows) > MAX_EXPORT_ROWS:
        rows = rows[:MAX_EXPORT_ROWS]
        logger.info("export truncated to %s rows for user %s", MAX_EXPORT_ROWS,
                    user.user_id)

    settings = get_settings()
    meta = ReportMeta(
        company_name=settings.company_name,
        report_name=request.report_name or request.title,
        date_range=request.date_range,
        filters=request.filters,
        generated_by=user.display_name or user.username,
        generated_at=dt.datetime.now(),
        currency=settings.currency_code,
        note=(None if request.format != "pdf" or supports_bangla_pdf()
              else "Bangla text is transliterated in PDF; use Excel or CSV for "
                   "Bangla names."),
    )
    kpis = [(str(pair[0]), str(pair[1])) for pair in request.kpis if len(pair) >= 2]

    try:
        payload, content_type = render(
            request.format, rows, request.title, meta=meta, kpis=kpis,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "report export") from exc

    filename = _filename(request.title, request.format)
    audit.record(session, action=AuditAction.EXPORT, user_id=user.user_id,
                 username=user.username, resource=request.report_name or request.title,
                 detail={"format": request.format, "rows": len(rows),
                         "filters": request.filters})
    session.commit()
    logger.info("export user=%s format=%s rows=%s", user.user_id, request.format,
                len(rows))
    return Response(
        content=payload,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _rows_from_message(session: Session, user: UserContext,
                       message_id: int) -> list[dict[str, Any]]:
    """Rows from a stored assistant turn, if it belongs to the caller."""
    message = session.get(ChatMessage, message_id)
    if message is None or message.user_id != user.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Report not found.")
    query = message.structured_query or {}
    rows = query.get("rows") if isinstance(query, dict) else None
    return list(rows) if isinstance(rows, list) else []


def _filename(title: str, export_format: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in title).strip()
    safe = (safe or "report").replace(" ", "_")[:60]
    return f"{safe}_{dt.date.today().isoformat()}.{export_format}"
