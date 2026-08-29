"""Credit Control's read surface: KPIs, aging, exposure and the invoice list.

Everything here reads ``vw_credit_invoice_detail`` through the same
``ReportFilters`` every other report uses, so these figures and the Sales page
cannot disagree about which rows exist. Scope is either enforced or the request
is refused — see :func:`assert_scope_is_honourable`, which exists because this
view cannot yet express a scope stated above the customer's sub-territory.

**Why the status and aging rules are rebuilt here rather than read off the view.**
The view computes them against ``CURRENT_DATE``, which is right for the default
request and wrong for every other one — and ``as_on_date`` is a first-class
parameter, because "what did we look like at month end" is the question finance
actually brings. So the expressions below are **generated from
:data:`app.etl.credit.OVERDUE_BUCKETS`** for whatever date the caller asked
about. Generated, not transcribed: this is the third place the bucket rule
appears and the first two are already pinned against each other, so a hand-typed
copy here would be the one free to drift.

**No date arithmetic reaches the database.** A bucket bounded at *n* days overdue
is the same statement as a due date on or after ``as_on - n days``, and that
subtraction happens in Python before the query is built. So each boundary is a
plain comparison against a date literal — portable across both dialects with no
``julianday``/``INTERVAL`` split, and able to use the index on ``due_date``
instead of scanning to compute a difference per row.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import Table, and_, case, func, literal, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..etl import credit
from .service import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    ReportFilters,
    _apply_filters,
    _describe,
    _f,
    _rows_to_dicts,
    _view,
)

CREDIT_INVOICE_VIEW = "vw_credit_invoice_detail"
CREDIT_EXPOSURE_VIEW = "vw_customer_credit_exposure"
CREDIT_AGING_VIEW = "vw_credit_aging"

#: Columns the invoice list may be sorted by. A whitelist rather than a
#: pass-through: ``sort_by`` arrives from a query string, and interpolating it
#: would be an injection point in the one module that reads money.
SORTABLE_INVOICE_COLUMNS: tuple[str, ...] = (
    "invoice_no", "invoice_date", "due_date", "customer_code", "customer_name",
    "company_code", "plant_code", "credit_days", "invoice_value",
    "net_invoice_amount", "payment_amount", "discount_amount",
    "adjustment_amount", "balance_amount", "payment_mode", "clearing_date",
    "last_payment_date", "clearing_document",
)

SORTABLE_CUSTOMER_COLUMNS: tuple[str, ...] = (
    "customer_code", "customer_name", "company_code", "invoice_count",
    "total_invoice_amount", "net_invoice_amount", "payment_amount",
    "discount_amount", "adjustment_amount", "outstanding_amount",
    "overdue_amount", "overdue_invoice_count", "oldest_due_date",
    "last_payment_date",
)

#: Columns a free-text search looks in — identifiers and names only. A substring
#: match against an amount is meaningless, and against a date it is misleading.
INVOICE_SEARCH_COLUMNS: tuple[str, ...] = (
    "invoice_no", "customer_code", "customer_name", "clearing_document",
    "plant_code", "plant_name",
)


#: Organisational levels ``vw_credit_invoice_detail`` can actually filter on.
#:
#: An invoice states a company, a plant and a customer; the customer carries its
#: sub-territory. Everything above that — territory, unit, area, region, zone,
#: sales line, business unit — is reachable only by walking the master hierarchy,
#: and the view does not join it yet.
#:
#: This matters because ``_apply_filters`` **ignores** a filter naming a column
#: the view lacks, which is the right behaviour for an optional narrowing and
#: exactly the wrong one for a scope: a regional manager's scope would be
#: dropped in silence and they would be served the whole company's receivables.
#: :func:`assert_scope_is_honourable` refuses instead. Refusing is the same
#: answer ``enforce_report_scope`` already gives a caller whose scope it cannot
#: express — a 403 that names the problem beats a page of somebody else's debt.
SCOPE_LEVELS_HONOURED: frozenset[str] = frozenset({
    "company_code", "customer_code", "sub_territory_code",
})


class ScopeNotHonourable(Exception):
    """A caller's data scope names a level this view cannot filter on."""

    def __init__(self, levels: tuple[str, ...]) -> None:
        self.levels = levels
        super().__init__(
            "Credit Control cannot yet apply a data scope at: "
            + ", ".join(levels)
            + ". An invoice records its company, plant and customer, and the "
            "reporting view does not carry the sales hierarchy above the "
            "customer's sub-territory, so this scope cannot be enforced and the "
            "request is refused rather than answered with unscoped figures."
        )


def assert_scope_is_honourable(user: Any, filters: ReportFilters) -> None:
    """Refuse a request whose scope this view would silently drop.

    Unrestricted callers pass: they have no scope to lose. Everyone else is
    checked against what the view can express, *before* the query runs, so the
    refusal cannot be mistaken for an empty result.
    """
    if getattr(user, "is_unrestricted", False):
        return
    scope = getattr(user, "data_scope", None) or {}
    unusable = tuple(
        level for level in sorted(scope) if level not in SCOPE_LEVELS_HONOURED
    )
    if unusable:
        raise ScopeNotHonourable(unusable)


@dataclass(frozen=True)
class CreditQuery:
    """The parameters specific to a credit request, beside the shared filters."""

    as_on: dt.date
    due_soon_days: int
    #: The four narrowings that belong to a credit invoice and to nothing else,
    #: so they are not on the shared ``ReportFilters``. Two are stored columns
    #: and two are *derived for the requested date* — which is why they are
    #: applied through the same generated expressions the report reads, rather
    #: than against the view's own CURRENT_DATE columns.
    credit_days: int | None = None
    payment_mode: str | None = None
    credit_status: str | None = None
    aging_bucket: str | None = None
    search: str | None = None
    sort_by: str | None = None
    sort_dir: str = "asc"
    page: int = 1
    page_size: int = DEFAULT_LIMIT

    def bounded_page_size(self) -> int:
        return max(1, min(self.page_size or DEFAULT_LIMIT, MAX_LIMIT))

    def offset(self) -> int:
        return (max(1, self.page) - 1) * self.bounded_page_size()


def resolve_query(
    *,
    as_on: dt.date | None = None,
    due_soon_days: int | None = None,
    **kwargs: Any,
) -> CreditQuery:
    """Fill in the two defaults that come from configuration rather than the caller."""
    settings = get_settings()
    return CreditQuery(
        as_on=as_on or dt.date.today(),
        due_soon_days=(
            settings.credit_due_soon_days if due_soon_days is None else due_soon_days
        ),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The rules, as SQL, for an arbitrary reporting date
# ---------------------------------------------------------------------------


def filtered(statement, view: Table, filters: ReportFilters, query: "CreditQuery",
             date_column: str | None = "invoice_date"):
    """Apply the shared report filters *and* the credit-only ones, always both.

    Every query in this module goes through here rather than calling
    ``_apply_filters`` directly. A site applying only one of the two would answer
    a different question from the one asked, and the mistake would look like an
    ordinary wrong number rather than a missing filter.
    """
    return apply_credit_filters(
        _apply_filters(statement, view, filters, date_column=date_column),
        view, query,
    )


def apply_credit_filters(statement, view: Table, query: "CreditQuery"):
    """Narrow by the four credit-only filters, where the caller supplied them.

    Applied to every surface — the KPI bundle included. Filtering to OVER_DUE
    and seeing a one-slice status donut is the correct answer to what was asked;
    a bundle that ignored the filter would report a headline that disagreed with
    the table underneath it.
    """
    conditions = []
    if query.credit_days is not None:
        conditions.append(view.c.credit_days == query.credit_days)
    if query.payment_mode:
        conditions.append(view.c.payment_mode == query.payment_mode)
    if query.credit_status:
        conditions.append(
            credit_status_expression(view, query.as_on) == query.credit_status)
    if query.aging_bucket:
        conditions.append(
            aging_bucket_expression(view, query.as_on) == query.aging_bucket)
    return statement.where(and_(*conditions)) if conditions else statement


def _is_open(view: Table):
    """Money still owed. A cleared invoice — balance zero *or below* — is not."""
    return view.c.balance_amount > 0


def _is_overdue(view: Table, as_on: dt.date):
    return and_(_is_open(view), view.c.due_date < as_on)


def credit_status_expression(view: Table, as_on: dt.date):
    """CLEARED / OVER_DUE / NOT_YET_DUE for one invoice on one day."""
    return case(
        (view.c.balance_amount <= 0, literal(credit.STATUS_CLEARED)),
        (view.c.due_date < as_on, literal(credit.STATUS_OVER_DUE)),
        else_=literal(credit.STATUS_NOT_YET_DUE),
    )


def aging_bucket_expression(view: Table, as_on: dt.date):
    """Which aging bucket an invoice falls in, or NULL if it falls in none.

    Built by walking :data:`credit.OVERDUE_BUCKETS` in order and turning each
    upper bound into a date: "at most *n* days overdue" is "due on or after
    ``as_on - n``". The arms are evaluated in order, so each one only has to
    state its upper bound — the lower bound is whatever the previous arm
    rejected, exactly as the frozen SQL in revision 0031 is written.
    """
    arms: list[tuple[Any, Any]] = [
        # Cleared invoices age nowhere: aging measures money still owed, and
        # including settled rows would report debt already collected.
        (view.c.balance_amount <= 0, literal(None)),
        (view.c.due_date >= as_on, literal(credit.BUCKET_NOT_YET_DUE)),
    ]
    for code, _lower, upper in credit.OVERDUE_BUCKETS:
        if upper is None:
            continue  # the open-ended tail is the ELSE
        arms.append((view.c.due_date >= as_on - dt.timedelta(days=upper),
                     literal(code)))
    tail = credit.OVERDUE_BUCKETS[-1][0]
    return case(*arms, else_=literal(tail))


def days_overdue(due_date: dt.date | None, as_on: dt.date) -> int | None:
    """Days past due for one row, computed in Python from the date the view gave.

    In Python because the answer is per row and the input is already in hand —
    asking the database to subtract two dates would buy nothing and would
    reintroduce the dialect split the bucket expressions exist to avoid.
    """
    if due_date is None:
        return None
    return credit.days_overdue(due_date=due_date, as_on=as_on)


# ---------------------------------------------------------------------------
# The page bundle
# ---------------------------------------------------------------------------


def credit_control_report(
    session: Session, filters: ReportFilters, query: CreditQuery
) -> dict[str, Any]:
    """Everything the Credit Control page draws above its table, in one request.

    One bundle rather than six endpoints, for the reason ``/api/dashboard`` is
    one: six requests against the same filtered row set can arrive at six
    slightly different answers if a load lands between them, and a page whose
    KPI strip disagrees with its own chart is worse than a slower page.
    """
    view = _view(session, CREDIT_INVOICE_VIEW)
    as_on = query.as_on

    totals = session.execute(filtered(
        select(
            func.count().label("invoice_count"),
            func.sum(view.c.invoice_value).label("total_invoice_amount"),
            func.sum(view.c.net_invoice_amount).label("net_invoice_amount"),
            func.sum(view.c.payment_amount).label("payment_amount"),
            func.sum(view.c.discount_amount).label("discount_amount"),
            func.sum(view.c.adjustment_amount).label("adjustment_amount"),
            func.sum(case((_is_open(view), view.c.balance_amount), else_=0))
                .label("outstanding_amount"),
            func.sum(case((_is_open(view), 1), else_=0))
                .label("open_invoice_count"),
            func.sum(case((_is_overdue(view, as_on), view.c.balance_amount), else_=0))
                .label("overdue_amount"),
            func.sum(case((_is_overdue(view, as_on), 1), else_=0))
                .label("overdue_invoice_count"),
            # Due Soon is the near edge of what is *not yet* overdue: still open,
            # not yet past its date, and falling due inside the horizon. An
            # invoice already overdue is counted as overdue and not double-counted
            # here, which is what lets the two figures be added.
            func.sum(case((and_(
                _is_open(view),
                view.c.due_date >= as_on,
                view.c.due_date <= as_on + dt.timedelta(days=query.due_soon_days),
            ), view.c.balance_amount), else_=0)).label("due_soon_amount"),
            func.sum(case((and_(
                _is_open(view),
                view.c.due_date >= as_on,
                view.c.due_date <= as_on + dt.timedelta(days=query.due_soon_days),
            ), 1), else_=0)).label("due_soon_invoice_count"),
        ),
        view, filters, query,
    )).one()._mapping

    metrics: dict[str, Any] = {
        name: _f(totals[name]) for name in (
            "total_invoice_amount", "net_invoice_amount", "payment_amount",
            "discount_amount", "adjustment_amount", "outstanding_amount",
            "overdue_amount", "due_soon_amount",
        )
    }
    metrics.update({
        "invoice_count": totals["invoice_count"],
        "open_invoice_count": totals["open_invoice_count"] or 0,
        "overdue_invoice_count": totals["overdue_invoice_count"] or 0,
        "due_soon_invoice_count": totals["due_soon_invoice_count"] or 0,
    })
    # Shares are suppressed rather than rendered as 0%: a portfolio with nothing
    # outstanding has no overdue *proportion*, and "0%" would read as good news
    # about a book that does not exist.
    metrics["payment_rate_percent"] = _share(
        totals["payment_amount"], totals["net_invoice_amount"])
    metrics["overdue_share_percent"] = _share(
        totals["overdue_amount"], totals["outstanding_amount"])

    return {
        "filters": _describe(filters),
        "as_on_date": as_on.isoformat(),
        "due_soon_days": query.due_soon_days,
        "metrics": metrics,
        "aging": _aging(session, view, filters, query),
        "status": _status_split(session, view, filters, query),
        "top_overdue_customers": _top_overdue(session, view, filters, query),
        "outstanding_trend": _outstanding_trend(),
        "notes": _notes(session, view, filters, query),
    }


def _share(part: Any, whole: Any) -> float | None:
    """``part / whole * 100``, or ``None`` when the denominator is zero."""
    if part is None or whole in (None, 0):
        return None
    whole_value = float(whole)
    return None if whole_value == 0 else round(float(part) / whole_value * 100, 2)


def _aging(session: Session, view: Table, filters: ReportFilters,
           query: CreditQuery) -> list[dict[str, Any]]:
    """Outstanding money per bucket — **all eight**, in order, zeros included.

    The view produces no row for a bucket holding nothing, which is correct for
    a view and wrong for this response: "no invoices in 91-120" is a fact the
    screen has to state, and a gap in the chart would read as a rendering fault.
    So the query's answers are merged onto the full ordered list.
    """
    as_on = query.as_on
    bucket = aging_bucket_expression(view, as_on)
    rows = session.execute(filtered(
        select(
            bucket.label("aging_bucket"),
            func.count().label("invoice_count"),
            func.sum(view.c.balance_amount).label("outstanding_amount"),
        ).where(_is_open(view)).group_by(bucket),
        view, filters, query,
    )).all()

    found = {row._mapping["aging_bucket"]: row._mapping for row in rows}
    return [
        {
            "bucket": code,
            "invoice_count": found[code]["invoice_count"] if code in found else 0,
            "outstanding_amount": (
                _f(found[code]["outstanding_amount"]) if code in found else 0.0
            ),
        }
        for code in credit.AGING_BUCKETS
    ]


def _status_split(session: Session, view: Table, filters: ReportFilters,
                  query: CreditQuery) -> list[dict[str, Any]]:
    """Count and balance per credit status, all three present even at zero."""
    as_on = query.as_on
    status = credit_status_expression(view, as_on)
    rows = session.execute(filtered(
        select(
            status.label("credit_status"),
            func.count().label("invoice_count"),
            func.sum(case((_is_open(view), view.c.balance_amount), else_=0))
                .label("outstanding_amount"),
        ).group_by(status),
        view, filters, query,
    )).all()

    found = {row._mapping["credit_status"]: row._mapping for row in rows}
    return [
        {
            "status": code,
            "invoice_count": found[code]["invoice_count"] if code in found else 0,
            "outstanding_amount": (
                _f(found[code]["outstanding_amount"]) if code in found else 0.0
            ),
        }
        for code in credit.CREDIT_STATUSES
    ]


#: How many customers the overdue chart names. A chart is a ranking, not a list.
TOP_OVERDUE_LIMIT = 5


def _top_overdue(session: Session, view: Table, filters: ReportFilters,
                 query: CreditQuery) -> list[dict[str, Any]]:
    as_on = query.as_on
    overdue = _is_overdue(view, as_on)
    rows = session.execute(filtered(
        select(
            view.c.customer_code,
            view.c.customer_name,
            func.sum(view.c.balance_amount).label("overdue_amount"),
            func.count().label("invoice_count"),
        ).where(overdue).group_by(view.c.customer_code, view.c.customer_name)
        .order_by(func.sum(view.c.balance_amount).desc())
        .limit(TOP_OVERDUE_LIMIT),
        view, filters, query,
    )).all()
    return [
        {
            "customer_code": row._mapping["customer_code"],
            "customer_name": row._mapping["customer_name"],
            "overdue_amount": _f(row._mapping["overdue_amount"]),
            "invoice_count": row._mapping["invoice_count"],
        }
        for row in rows
    ]


def _outstanding_trend() -> dict[str, Any]:
    """Not available, and said so rather than approximated.

    A closing-outstanding series needs to know what was owed at each past month
    end, and this source cannot say. It states one aggregate ``payment_amount``
    per invoice and a single Last Payment Date — so for any month end before that
    date, how much of the payment had been made by then is simply unrecorded.
    Reconstructing it would mean assuming a payment pattern, and a chart of
    assumed history is indistinguishable on screen from a measured one.

    ``NOT_AVAILABLE`` rather than ``NO_DATA``: no file will fix this. It needs a
    payment-transaction extract, which is a different source, and until one
    exists the honest answer is that the platform holds no basis for the series.
    """
    return {
        "state": "NOT_AVAILABLE",
        "points": [],
        "reason": (
            "The source states one total payment per invoice and a single last "
            "payment date, so what was outstanding at a past month end cannot be "
            "reconstructed. A payment-transaction extract would be needed."
        ),
    }


def _notes(session: Session, view: Table, filters: ReportFilters,
           query: CreditQuery) -> list[dict[str, Any]]:
    """What the reader should know about the rows behind the figures.

    Currently one note: how many invoices carry a data-quality flag. It is
    surfaced on the page rather than left to the Data Quality screen because a
    negative balance changes what the outstanding total means, and somebody
    reading the total deserves to know it is in there.
    """
    flagged = session.execute(filtered(
        select(func.count()).select_from(view)
        .where(view.c.data_quality_flag.isnot(None)),
        view, filters, query,
    )).scalar() or 0
    if not flagged:
        return []
    return [{
        "code": "DATA_QUALITY_FLAGGED",
        "count": flagged,
        "message": (
            f"{flagged} invoice(s) carry a data-quality flag and are shown, not "
            "hidden. Their figures are the source's own."
        ),
    }]


# ---------------------------------------------------------------------------
# The two tables
# ---------------------------------------------------------------------------


def _jsonable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render every Decimal in a row as a float.

    Without this a money column arrives as the string ``"220000.0000"`` from the
    table endpoints while the same figure arrives as ``220000.0`` from the KPI
    bundle, because those go through :func:`_f` and these did not. One API
    returning a number two ways is a client-side bug waiting to happen.
    """
    return [
        {key: (float(value) if isinstance(value, Decimal) else value)
         for key, value in row.items()}
        for row in rows
    ]


def _paged(session: Session, statement, count_statement,
           query: CreditQuery) -> tuple[list[dict[str, Any]], int]:
    total = session.execute(count_statement).scalar() or 0
    size = query.bounded_page_size()
    rows = _jsonable(_rows_to_dicts(
        session.execute(statement.limit(size).offset(query.offset()))
    ))
    return rows, total


def _sorted(statement, view: Table, query: CreditQuery,
            allowed: tuple[str, ...], default: str):
    """Apply a whitelisted sort. An unknown column falls back, never interpolates."""
    column_name = query.sort_by if query.sort_by in allowed else default
    column = view.c[column_name]
    return statement.order_by(column.desc() if query.sort_dir == "desc" else column.asc())


def _searched(statement, view: Table, query: CreditQuery,
              columns: tuple[str, ...]):
    if not query.search or not query.search.strip():
        return statement
    pattern = f"%{query.search.strip()}%"
    return statement.where(or_(*[
        view.c[name].ilike(pattern) for name in columns if name in view.c
    ]))


def credit_invoices(session: Session, filters: ReportFilters,
                    query: CreditQuery) -> dict[str, Any]:
    """The invoice table: one row per invoice, server-paged and server-sorted."""
    view = _view(session, CREDIT_INVOICE_VIEW)
    as_on = query.as_on

    base = filtered(select(view), view, filters, query)
    base = _searched(base, view, query, INVOICE_SEARCH_COLUMNS)
    # ``select_from`` is not decoration: a bare ``count()`` infers its FROM from
    # the columns in the WHERE clause, and an unfiltered request has none — so
    # this counted one row of nothing and every page reported a total of 1.
    counted = filtered(select(func.count()).select_from(view), view, filters, query)
    counted = _searched(counted, view, query, INVOICE_SEARCH_COLUMNS)

    rows, total = _paged(
        session, _sorted(base, view, query, SORTABLE_INVOICE_COLUMNS, "due_date"),
        counted, query,
    )
    # The three date-relative columns are recomputed for the requested date. The
    # view answered for today, and this request may not be about today.
    for row in rows:
        row["days_overdue"] = days_overdue(row.get("due_date"), as_on)
        row["credit_status"] = credit.credit_status(
            balance_amount=row["balance_amount"], due_date=row["due_date"], as_on=as_on)
        row["aging_bucket"] = credit.aging_bucket(
            balance_amount=row["balance_amount"], due_date=row["due_date"], as_on=as_on)

    size = query.bounded_page_size()
    return {
        "filters": _describe(filters),
        "as_on_date": as_on.isoformat(),
        "rows": rows,
        "total": total,
        "page": max(1, query.page),
        "page_size": size,
        "total_pages": (total + size - 1) // size if total else 0,
    }


def credit_customers(session: Session, filters: ReportFilters,
                     query: CreditQuery) -> dict[str, Any]:
    """The Customer View: exposure per customer, per company.

    Read from ``vw_credit_invoice_detail`` and aggregated here rather than from
    ``vw_customer_credit_exposure``, because that view answers for today and this
    request may not be about today. The exposure view stays the cheap read for
    anything that genuinely wants the current position.

    Credit Exposure is each customer's **share of portfolio outstanding**. The
    Customer Master carries no credit limit, so a limit-versus-used exposure
    would have to be invented; the share is a real ratio of two figures this
    system holds.
    """
    view = _view(session, CREDIT_INVOICE_VIEW)
    as_on = query.as_on
    overdue = _is_overdue(view, as_on)

    grouped = (
        view.c.company_code, view.c.customer_code, view.c.customer_name,
        view.c.sub_territory_code,
    )
    base = filtered(
        select(
            *grouped,
            func.count().label("invoice_count"),
            func.sum(view.c.invoice_value).label("total_invoice_amount"),
            func.sum(view.c.net_invoice_amount).label("net_invoice_amount"),
            func.sum(view.c.payment_amount).label("payment_amount"),
            func.sum(view.c.discount_amount).label("discount_amount"),
            func.sum(view.c.adjustment_amount).label("adjustment_amount"),
            func.sum(case((_is_open(view), view.c.balance_amount), else_=0))
                .label("outstanding_amount"),
            func.sum(case((overdue, view.c.balance_amount), else_=0))
                .label("overdue_amount"),
            func.sum(case((overdue, 1), else_=0)).label("overdue_invoice_count"),
            func.min(case((overdue, view.c.due_date))).label("oldest_due_date"),
            func.max(view.c.last_payment_date).label("last_payment_date"),
        ).group_by(*grouped),
        view, filters, query,
    )
    base = _searched(base, view, query, INVOICE_SEARCH_COLUMNS)

    subquery = base.subquery()
    column_name = (
        query.sort_by if query.sort_by in SORTABLE_CUSTOMER_COLUMNS
        else "outstanding_amount"
    )
    column = subquery.c[column_name]
    statement = select(subquery).order_by(
        column.desc() if query.sort_dir != "asc" else column.asc())

    total = session.execute(
        select(func.count()).select_from(base.subquery())).scalar() or 0
    size = query.bounded_page_size()
    rows = _jsonable(_rows_to_dicts(
        session.execute(statement.limit(size).offset(query.offset()))))

    portfolio = sum(float(row["outstanding_amount"] or 0) for row in rows)
    total_outstanding = session.execute(filtered(
        select(func.sum(case((_is_open(view), view.c.balance_amount), else_=0)))
        .select_from(view),
        view, filters, query,
    )).scalar() or 0
    total_outstanding = float(total_outstanding)
    for row in rows:
        # Share of the whole filtered portfolio, not of this page — a customer's
        # exposure does not change because somebody turned to page two.
        row["credit_exposure_percent"] = (
            None if total_outstanding == 0
            else round(float(row["outstanding_amount"] or 0) / total_outstanding * 100, 2)
        )

    return {
        "filters": _describe(filters),
        "as_on_date": as_on.isoformat(),
        "rows": rows,
        "total": total,
        "page": max(1, query.page),
        "page_size": size,
        "total_pages": (total + size - 1) // size if total else 0,
        "portfolio_outstanding": total_outstanding,
        "page_outstanding": portfolio,
    }


def credit_invoice_detail(session: Session, company_code: str, invoice_no: str,
                          query: CreditQuery,
                          filters: ReportFilters) -> dict[str, Any] | None:
    """One invoice, for the detail panel. ``None`` when it is not in scope.

    The filters are applied here too, which is what makes an out-of-scope invoice
    indistinguishable from a non-existent one: a caller must not be able to
    confirm that a customer outside their region exists by guessing at invoice
    numbers.
    """
    view = _view(session, CREDIT_INVOICE_VIEW)
    statement = filtered(
        select(view).where(and_(
            view.c.company_code == company_code,
            view.c.invoice_no == invoice_no,
        )),
        view, filters, query, date_column=None,
    )
    row = session.execute(statement).first()
    if row is None:
        return None

    record = _jsonable([dict(row._mapping)])[0]
    as_on = query.as_on
    record["days_overdue"] = days_overdue(record.get("due_date"), as_on)
    record["credit_status"] = credit.credit_status(
        balance_amount=record["balance_amount"], due_date=record["due_date"],
        as_on=as_on)
    record["aging_bucket"] = credit.aging_bucket(
        balance_amount=record["balance_amount"], due_date=record["due_date"],
        as_on=as_on)
    return {
        "as_on_date": as_on.isoformat(),
        "invoice": record,
        # One payment event, and the panel says why. The source aggregates
        # payments, so listing "a payment" per invoice is the whole of the
        # history it can support; inventing a schedule would be fiction.
        "payment_events": _payment_events(record),
        "data_quality_flags": (
            record["data_quality_flag"].split("|")
            if record.get("data_quality_flag") else []
        ),
    }


def _payment_events(record: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    paid = float(record.get("payment_amount") or 0)
    if paid:
        # Reported even when the source states no Last Payment Date. Money was
        # posted against this invoice — the Financial Summary shows it — so a
        # timeline that omitted the event entirely would contradict the panel it
        # sits in. An undated event says the amount is known and the date is not,
        # which is the true statement.
        events.append({
            "date": record.get("last_payment_date"),
            "amount": _f(record["payment_amount"]),
            "kind": "PAYMENT",
            "note": (
                "Total posted in payment. The source states one aggregate figure "
                "and one last payment date, not individual transactions."
                if record.get("last_payment_date") else
                "Total posted in payment. The source states no last payment "
                "date, so this event cannot be placed on the timeline."
            ),
        })
    if record.get("clearing_date"):
        events.append({
            "date": record["clearing_date"],
            "amount": None,
            "kind": "CLEARING",
            "note": record.get("clearing_document") or "",
        })
    return events


__all__ = [
    "CREDIT_AGING_VIEW",
    "CREDIT_EXPOSURE_VIEW",
    "CREDIT_INVOICE_VIEW",
    "CreditQuery",
    "INVOICE_SEARCH_COLUMNS",
    "SORTABLE_CUSTOMER_COLUMNS",
    "SORTABLE_INVOICE_COLUMNS",
    "TOP_OVERDUE_LIMIT",
    "aging_bucket_expression",
    "credit_control_report",
    "credit_customers",
    "credit_invoice_detail",
    "credit_invoices",
    "credit_status_expression",
    "days_overdue",
    "resolve_query",
]
