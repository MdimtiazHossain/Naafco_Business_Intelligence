"""Credit Control endpoints.

Four reads, all guarded by the ``credit_control`` section and all scoped by
``enforce_report_scope`` — the same pair every ``/api/reports/*`` endpoint uses,
so a caller denied Credit Control gets 403 here exactly as they do from the page.

**Scope is enforced or the request is refused; it is never partially applied.**
An invoice records a company, a plant and a customer, and the reporting view
reaches the customer's sub-territory and no further, so a scope stated at region
or territory cannot be expressed here. ``_apply_filters`` would drop it without
comment and answer with the whole company's receivables, so ``_scoped`` returns
403 instead, naming the levels it could not honour. The section defaults to the
unrestricted roles, who have no scope to lose; a scoped user granted it meets
that refusal until the view carries the sales hierarchy above the customer.

Every one takes ``as_on_date``. Overdue is a function of a date, and "what did we
look like at month end" is a question finance actually asks; defaulting it to
today is a convenience, not an assumption baked into the figures.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..auth.permissions import require_section
from ..reporting.credit import (
    ScopeNotHonourable,
    assert_scope_is_honourable,
    credit_control_report,
    credit_customers,
    credit_invoice_detail,
    credit_invoices,
    resolve_query,
)
from ..reporting.service import DEFAULT_LIMIT, MAX_LIMIT, ReportFilters
from ..security.sections import SectionKey
from .deps import enforce_report_scope, get_session, internal_error, report_filters

router = APIRouter(prefix="/api/reports/credit-control", tags=["credit-control"])

#: Ascending or descending, and nothing else. The value reaches a whitelisted
#: ORDER BY, but validating it here means a typo is a 422 rather than a silent
#: fall back to ascending.
_SORT_DIRECTIONS = ("asc", "desc")


def _credit_query(
    as_on_date: dt.date | None = Query(
        None, description="Reporting date. Overdue is measured against it. "
                          "Defaults to today."),
    due_soon_days: int | None = Query(
        None, ge=1, le=365,
        description="How near a due date counts as Due Soon. Defaults to "
                    "CREDIT_DUE_SOON_DAYS."),
    credit_days: int | None = Query(
        None, ge=0, description="Exact credit term, in days."),
    payment_mode: str | None = Query(None, description="CASH or CREDIT."),
    credit_status: str | None = Query(
        None, description="NOT_YET_DUE, OVER_DUE or CLEARED — as at as_on_date."),
    aging_bucket: str | None = Query(
        None, description="One aging bucket — as at as_on_date."),
    search: str | None = Query(None, description="Substring of an identifier or name."),
    sort_by: str | None = Query(None, description="Whitelisted column; unknown falls back."),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
):
    return resolve_query(
        as_on=as_on_date, due_soon_days=due_soon_days, search=search,
        credit_days=credit_days, payment_mode=payment_mode,
        credit_status=credit_status, aging_bucket=aging_bucket,
        sort_by=sort_by, sort_dir=sort_dir, page=page, page_size=page_size,
    )


def _scoped(session: Session, user: UserContext,
            filters: ReportFilters) -> ReportFilters:
    """Apply the caller's scope, or refuse if this view cannot express it.

    The refusal is the point. ``_apply_filters`` drops a filter naming a column
    the view lacks, so a scope this report cannot honour would vanish in silence
    and serve a regional manager the whole company's receivables.
    """
    scoped = enforce_report_scope(session, user, filters)
    try:
        assert_scope_is_honourable(user, scoped)
    except ScopeNotHonourable as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return scoped


def _run(handler, session: Session, filters: ReportFilters, user: UserContext,
         name: str, *args: Any) -> Any:
    scoped = _scoped(session, user, filters)
    try:
        return handler(session, scoped, *args)
    except Exception as exc:  # noqa: BLE001 - internals never reach the client
        raise internal_error(exc, f"{name} report") from exc


@router.get("")
def get_credit_control(
    filters: ReportFilters = Depends(report_filters),
    query=Depends(_credit_query),
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.CREDIT_CONTROL)),
) -> dict[str, Any]:
    """The page bundle: KPIs, aging, status split, top overdue, trend and notes."""
    return _run(credit_control_report, session, filters, user, "credit control", query)


@router.get("/invoices")
def get_credit_invoices(
    filters: ReportFilters = Depends(report_filters),
    query=Depends(_credit_query),
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.CREDIT_CONTROL)),
) -> dict[str, Any]:
    """The invoice table, server-paged and server-sorted."""
    return _run(credit_invoices, session, filters, user, "credit invoices", query)


@router.get("/customers")
def get_credit_customers(
    filters: ReportFilters = Depends(report_filters),
    query=Depends(_credit_query),
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.CREDIT_CONTROL)),
) -> dict[str, Any]:
    """The Customer View: exposure per customer, per company."""
    return _run(credit_customers, session, filters, user, "credit customers", query)


@router.get("/invoices/{company_code}/{invoice_no}")
def get_credit_invoice(
    company_code: str,
    invoice_no: str,
    filters: ReportFilters = Depends(report_filters),
    query=Depends(_credit_query),
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.CREDIT_CONTROL)),
) -> dict[str, Any]:
    """One invoice for the detail panel.

    Addressed by **company and invoice number**, because that pair is what
    identifies an invoice — the number alone is unique only within its company,
    and a single-segment route would have had to pick one of two group companies'
    invoices arbitrarily.

    An invoice outside the caller's scope returns 404, not 403. The two are
    deliberately indistinguishable here: a 403 would confirm that the invoice
    exists, which is precisely what somebody probing another region's customers
    would be trying to learn.
    """
    scoped = _scoped(session, user, filters)
    try:
        found = credit_invoice_detail(session, company_code, invoice_no, query, scoped)
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "credit invoice") from exc
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found.")
    return found
