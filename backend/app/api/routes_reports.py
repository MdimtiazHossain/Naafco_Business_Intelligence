"""Reporting endpoints.

These are the read surface the Phase 3 AI agent will call. Every endpoint takes
the same filter set and returns ``{filters, metrics, rows}`` so a generated
query never has to special-case a report.

Phase 4 closes the gap these endpoints were left with: each one now requires an
authenticated caller who holds the matching **section**, and the caller's **data
scope** is applied to the filters before the query runs. A user denied Stock gets
``403`` from ``/api/reports/stock`` exactly as they do from the Stock page.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..auth.permissions import require_section
from ..reporting.service import (
    ReportFilters,
    sales_report,
    stock_report,
    target_report,
)
from ..security.sections import SectionKey
from .deps import enforce_report_scope, get_session, internal_error, report_filters

router = APIRouter(prefix="/api/reports", tags=["reports"])


#: The view each report reads, for the scope check. Deliberately the *narrow*
#: view each handler actually queries rather than the wide detail view of the
#: same dataset: ``sales_report`` reads ``vw_daily_sales`` and ``target_report``
#: reads ``vw_target_vs_actual``, and it is exactly the columns those two lack
#: that used to make a scope vanish here.
_SCOPE_VIEW: dict[str, str] = {
    "sales": "vw_daily_sales",
    "stock": "vw_material_stock_detail",
    "target": "vw_target_vs_actual",
}


def _run(handler, session: Session, filters: ReportFilters, user: UserContext,
         name: str) -> dict[str, Any]:
    scoped = enforce_report_scope(session, user, filters, _SCOPE_VIEW[name])
    try:
        return handler(session, scoped)
    except Exception as exc:  # noqa: BLE001 - internals never reach the client
        raise internal_error(exc, f"{name} report") from exc


@router.get("/sales")
def get_sales(
    filters: ReportFilters = Depends(report_filters),
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.SALES)),
) -> dict[str, Any]:
    """Daily sales with MTD / YTD / growth / achievement metrics."""
    return _run(sales_report, session, filters, user, "sales")


@router.get("/stock")
def get_stock(
    filters: ReportFilters = Depends(report_filters),
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.STOCK)),
) -> dict[str, Any]:
    """Material stock positions with the four category totals and shelf-life dates."""
    return _run(stock_report, session, filters, user, "stock")


@router.get("/target")
def get_target(
    filters: ReportFilters = Depends(report_filters),
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.TARGET)),
) -> dict[str, Any]:
    """Target vs actual with achievement % and gap."""
    return _run(target_report, session, filters, user, "target")
