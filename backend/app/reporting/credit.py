"""Credit Control's read surface: KPIs, aging, exposure and the invoice list.

Everything here reads ``vw_credit_invoice_detail`` through the same
``ReportFilters`` every other report uses, so these figures and the Sales page
cannot disagree about which rows exist. Scope is enforced by the one generic
mechanism — ``enforce_report_scope`` under ``queries.SCOPE_POLICY`` — because
since revision 0040 the view carries the whole sales hierarchy and there is no
level left for it to fail to express. The bespoke refusal this module used to
carry went with the gap it covered.

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

from sqlalchemy import Table, and_, case, distinct, func, literal, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..etl import credit
from ..security.scope import ORG
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

#: The levels a receivables report may be grouped by, shallowest first.
#:
#: Derived from the organisational chain rather than written out, so a level
#: added to the warehouse appears here without this list being edited — the rule
#: at the top of CLAUDE.md, applied to the one list on this page that a reader
#: picks from. ``customer_code`` is appended because a customer is where credit
#: exposure actually lives: it is the entity whose supply gets stopped.
EXPOSURE_LEVELS: tuple[str, ...] = tuple(ORG.code_fields()) + ("customer_code",)

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


#: The scope levels this view can be narrowed by — **all of them**, since 0040.
#:
#: This used to be a four-item literal and a refusal beside it. The view reached
#: the customer's sub-territory and no further, so a region-scoped caller's scope
#: named a column that was not there; ``_apply_filters`` drops such a filter in
#: silence, which is right for an optional narrowing and catastrophic for a
#: scope, so Credit Control refused the whole request instead.
#:
#: 0040 gave the view the sales hierarchy, and the refusal went with the reason
#: for it. What checks the scope now is the one generic mechanism every other
#: report uses — ``PermissionFilter.assert_scope_is_honourable`` against
#: ``queries.SCOPE_POLICY``, which reads the view's *own columns* rather than any
#: hand-written list. That is why this is derived and no longer declared: a list
#: of levels beside a view that has just gained six is precisely the stale name
#: this codebase's first rule is about.
#:
#: The policy stays REFUSE. It is inert while the view carries every level, and
#: it is what would catch the next report that does not.
SCOPE_LEVELS_HONOURED: frozenset[str] = frozenset({
    "company_code", "bu_code", "sales_line_code", "zone_code", "region_code",
    "area_code", "unit_code", "territory_code", "sub_territory_code",
    "customer_code", "plant_code",
})



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
    #: Which organisational level the hierarchy sections group by.
    #:
    #: A request parameter rather than a fixed level, because the question
    #: changes with who is asking: a managing director reads this page by region
    #: and an area manager reads their own areas by territory. Validated against
    #: :data:`EXPOSURE_LEVELS` in :func:`resolve_query` rather than interpolated,
    #: for the same reason ``GroupBy`` is an enum — a level that reached the
    #: query as text would be a column name a caller chose.
    group_level: str = "region_code"
    search: str | None = None
    sort_by: str | None = None
    sort_dir: str = "asc"
    page: int = 1
    page_size: int = DEFAULT_LIMIT

    def bounded_page_size(self) -> int:
        return max(1, min(self.page_size or DEFAULT_LIMIT, MAX_LIMIT))

    def offset(self) -> int:
        return (max(1, self.page) - 1) * self.bounded_page_size()


class UnknownGroupLevel(ValueError):
    """A grouping level this report does not offer.

    Refused rather than silently replaced with the default: a caller who asked
    for a breakdown by material and got one by region would be reading the right
    numbers under the wrong heading, which is worse than an error.
    """

    def __init__(self, level: str) -> None:
        self.level = level
        super().__init__(
            f"'{level}' is not a level Credit Control can group by. "
            f"Available: {', '.join(EXPOSURE_LEVELS)}."
        )


def resolve_query(
    *,
    as_on: dt.date | None = None,
    due_soon_days: int | None = None,
    group_level: str | None = None,
    **kwargs: Any,
) -> CreditQuery:
    """Fill in the defaults that come from configuration rather than the caller."""
    settings = get_settings()
    if group_level is not None and group_level not in EXPOSURE_LEVELS:
        raise UnknownGroupLevel(group_level)
    return CreditQuery(
        as_on=as_on or dt.date.today(),
        due_soon_days=(
            settings.credit_due_soon_days if due_soon_days is None else due_soon_days
        ),
        **({"group_level": group_level} if group_level else {}),
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


def _is_due_soon(view: Table, as_on: dt.date, horizon_days: int):
    """Open, not yet late, and falling due inside the horizon.

    The lower bound is ``>= as_on`` rather than ``> as_on``: an invoice due
    *today* is not overdue — ``credit.days_overdue`` is ``(as_on - due).days``
    and zero days late is NOT_YET_DUE — so it belongs here, and this is the
    boundary the whole module already agrees on.
    """
    return and_(
        _is_open(view),
        view.c.due_date >= as_on,
        view.c.due_date <= as_on + dt.timedelta(days=horizon_days),
    )


def _is_due_later(view: Table, as_on: dt.date, horizon_days: int):
    """Open and falling due beyond the horizon — the third of the three.

    **The partition is the point.** Overdue and Due Soon were reported without
    it, so the two largest figures on the page did not add up to the third and
    nothing said why. On the SPL extract the missing piece is ৳35.75 Cr, a third
    of the book: money that is neither late nor imminent, which the source's own
    ``od`` and ``maturity`` columns do not publish either.

    Every open invoice falls in exactly one of the three, because the bounds are
    a strict ordering on one column — so ``overdue + due_soon + due_later``
    equals ``outstanding`` by construction rather than by hope, and
    ``test_credit_api`` checks it rather than trusting it.
    """
    return and_(
        _is_open(view),
        view.c.due_date > as_on + dt.timedelta(days=horizon_days),
    )


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
            # Reported so the card above it is explicable. Returns are the one
            # column this source posts negative *and* subtracts, so they can push
            # the net figure above the gross one — and a reader looking at that
            # is owed the number that caused it rather than left to wonder.
            func.sum(view.c.return_amount).label("return_amount"),
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
            # The three-way partition of everything still owed, by when it falls
            # due. Overdue above; Due Soon is the near edge of what is not yet
            # late; Due Later is the rest. They are mutually exclusive and
            # exhaustive over the open book, which is what lets a reader add the
            # first two and know what is missing.
            func.sum(case((_is_due_soon(view, as_on, query.due_soon_days),
                           view.c.balance_amount), else_=0)).label("due_soon_amount"),
            func.sum(case((_is_due_soon(view, as_on, query.due_soon_days), 1),
                          else_=0)).label("due_soon_invoice_count"),
            func.sum(case((_is_due_later(view, as_on, query.due_soon_days),
                           view.c.balance_amount), else_=0)).label("due_later_amount"),
            func.sum(case((_is_due_later(view, as_on, query.due_soon_days), 1),
                          else_=0)).label("due_later_invoice_count"),
        ),
        view, filters, query,
    )).one()._mapping

    metrics: dict[str, Any] = {
        name: _f(totals[name]) for name in (
            "total_invoice_amount", "net_invoice_amount", "return_amount",
            "outstanding_amount", "overdue_amount", "due_soon_amount",
            "due_later_amount",
        )
    }
    # The deductions are read straight through, and that is a change worth
    # knowing about: this used to negate them here.
    #
    # The warehouse stores one canonical sign — a deduction is the amount by
    # which that component *reduced* the balance — so a payment is already the
    # positive figure a card headed "Total Payment" wants, and a debit note that
    # increased what is owed is already the negative one. Nothing on this path
    # knows or needs to know which extract a row came from; the sign was settled
    # once, at the load, by ``etl.credit.canonical_deductions``.
    #
    # Flipping it here was correct while one file existed and became wrong the
    # moment a second one posted its payments the other way up. The rule now
    # lives in one place instead of two, which is the point.
    metrics.update({
        name: _float(totals[name])
        for name in ("payment_amount", "discount_amount", "adjustment_amount")
    })
    metrics.update({
        "invoice_count": totals["invoice_count"],
        "open_invoice_count": totals["open_invoice_count"] or 0,
        "overdue_invoice_count": totals["overdue_invoice_count"] or 0,
        "due_soon_invoice_count": totals["due_soon_invoice_count"] or 0,
        "due_later_invoice_count": totals["due_later_invoice_count"] or 0,
    })
    # Shares are suppressed rather than rendered as 0%: a portfolio with nothing
    # outstanding has no overdue *proportion*, and "0%" would read as good news
    # about a book that does not exist.
    metrics["payment_rate_percent"] = _share(
        metrics["payment_amount"], totals["net_invoice_amount"])
    metrics["overdue_share_percent"] = _share(
        totals["overdue_amount"], totals["outstanding_amount"])

    return {
        "filters": _describe(filters),
        "as_on_date": as_on.isoformat(),
        "due_soon_days": query.due_soon_days,
        "metrics": metrics,
        "aging": _aging(session, view, filters, query),
        # The hierarchy sections. One bundle rather than four more endpoints,
        # for the reason this is a bundle at all: four requests against the same
        # filtered rows can arrive at four slightly different answers if a load
        # lands between them, and a matrix that disagrees with the chart above it
        # is worse than a slower page.
        "aging_by_level": _aging_by_level(session, view, filters, query,
                                          query.group_level),
        "exposure_by_level": _exposure_by_level(session, view, filters, query,
                                                query.group_level),
        "due_profile": _due_profile(session, view, filters, query),
        "status": _status_split(session, view, filters, query),
        "top_overdue_customers": _top_overdue(session, view, filters, query),
        "outstanding_trend": _outstanding_trend(),
        "notes": _notes(session, view, filters, query),
    }


def _float(value: Any) -> float | None:
    """A stored figure as a float, keeping its sign.

    The sign is the figure's meaning and is never adjusted here. It was made to
    mean one thing at load time — positive reduced the balance, negative
    increased it — so a payment comes back positive, and a debit adjustment comes
    back negative because that is what it did.
    """
    return None if value is None else float(value)


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
    """Count, balance and invoice value per credit status, all three at zero too.

    **``invoice_amount`` is here because ``outstanding_amount`` cannot describe
    CLEARED.** A cleared invoice has a balance of zero or below by definition, so
    the outstanding figure for that status is always 0.0 — which left the split
    reporting a status with a count and no money at all, and no way to see how
    much had actually been settled. The two answer different questions: what is
    still owed in each state, and how much was billed to get there.

    ``invoice_amount`` is the **net** invoice value — what was billed less what
    came back — because that is the figure every other total on this page nets
    against, and mixing gross into one row of a split would be the only gross
    figure on the surface.
    """
    as_on = query.as_on
    status = credit_status_expression(view, as_on)
    rows = session.execute(filtered(
        select(
            status.label("credit_status"),
            func.count().label("invoice_count"),
            func.sum(case((_is_open(view), view.c.balance_amount), else_=0))
                .label("outstanding_amount"),
            func.sum(view.c.net_invoice_amount).label("invoice_amount"),
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
            "invoice_amount": (
                _f(found[code]["invoice_amount"]) if code in found else 0.0
            ),
        }
        for code in credit.CREDIT_STATUSES
    ]


#: How many customers the overdue chart names. A chart is a ranking, not a list.
TOP_OVERDUE_LIMIT = 5


def _top_overdue(session: Session, view: Table, filters: ReportFilters,
                 query: CreditQuery) -> list[dict[str, Any]]:
    """The worst overdue customers, and **where each of them is**.

    The territory joined these rows in revision 0040, and it is not decoration:
    the action this chart leads to is somebody going to see the customer, and
    until the view carried a hierarchy the page could name the debt but not the
    person whose patch it sits in. Territory rather than region because it is the
    level a single visit happens at; the region is carried beside it so a
    regional manager reading their own filtered page still recognises the rows.

    Grouped on the customer alone. Adding the place columns to the GROUP BY
    would split a customer who trades across two territories into two rows and
    quietly drop both below the cut — so the place is taken as the MAX over the
    customer's own invoices, which is that customer's territory in every case
    the master data allows and a stable choice in any case it does not.
    """
    as_on = query.as_on
    overdue = _is_overdue(view, as_on)
    rows = session.execute(filtered(
        select(
            view.c.customer_code,
            view.c.customer_name,
            func.max(view.c.territory_code).label("territory_code"),
            func.max(view.c.territory_name).label("territory_name"),
            func.max(view.c.region_code).label("region_code"),
            func.max(view.c.region_name).label("region_name"),
            func.sum(view.c.balance_amount).label("overdue_amount"),
            func.count().label("invoice_count"),
            func.max(view.c.days_overdue).label("max_days_overdue"),
        ).where(overdue).group_by(view.c.customer_code, view.c.customer_name)
        .order_by(func.sum(view.c.balance_amount).desc())
        .limit(TOP_OVERDUE_LIMIT),
        view, filters, query,
    )).all()
    return [
        {
            "customer_code": row._mapping["customer_code"],
            "customer_name": row._mapping["customer_name"],
            "territory_code": row._mapping["territory_code"],
            "territory_name": row._mapping["territory_name"] or UNASSIGNED,
            "region_code": row._mapping["region_code"],
            "region_name": row._mapping["region_name"] or UNASSIGNED,
            "overdue_amount": _f(row._mapping["overdue_amount"]),
            "invoice_count": row._mapping["invoice_count"],
            "max_days_overdue": row._mapping["max_days_overdue"],
        }
        for row in rows
    ]



# ---------------------------------------------------------------------------
# The hierarchy sections
#
# Everything below became possible in revision 0040, which put the sales
# hierarchy on ``vw_credit_invoice_detail``. Before it a receivables report
# could say what was owed and how late, and nothing at all about *where* — so
# the one question a regional manager brings to this page had no answer on it.
# ---------------------------------------------------------------------------


#: What a level is called when a row has no value for it.
#:
#: Rendered rather than dropped. A row with no region is money somebody is owed,
#: and silently omitting it from a breakdown would make the parts add up to less
#: than the total with nothing on screen to explain the gap.
UNASSIGNED = "(unassigned)"

#: How many groups a ranked breakdown names before it stops being a ranking.
EXPOSURE_LIMIT = 20


def _label_column(view: Table, level: str):
    """The human name beside a code, where the view carries one.

    ``region_code`` has ``region_name``; ``customer_code`` has ``customer_name``.
    A level whose name column the view lacks falls back to the code, which is
    what the Customers page did for two revisions before ``dim_customer``
    existed — a code is a poor label and an honest one.
    """
    name = level.replace("_code", "_name")
    return view.c[name] if name in view.c else view.c[level]


def _grouped(session: Session, view: Table, filters: ReportFilters,
             query: CreditQuery, level: str, columns: list, *, where=None,
             order_by=None, limit: int | None = None):
    """One grouped read of the credit view, filtered and scoped like every other."""
    code = view.c[level]
    label = _label_column(view, level)
    statement = select(code.label("code"), label.label("name"), *columns)
    if where is not None:
        statement = statement.where(where)
    statement = statement.group_by(code, label)
    if order_by is not None:
        statement = statement.order_by(order_by)
    if limit is not None:
        statement = statement.limit(limit)
    return session.execute(filtered(statement, view, filters, query)).all()


def _aging_by_level(session: Session, view: Table, filters: ReportFilters,
                    query: CreditQuery, level: str) -> dict[str, Any]:
    """The aging matrix: one row per group, one column per bucket.

    Returned as a matrix rather than as a list of (group, bucket, amount)
    triples, because a matrix is what the page draws and flattening it in the
    browser would put the bucket order — which is a business rule — on the wrong
    side of the API. Every bucket is present on every row, zeros included, for
    the reason ``_aging`` fills them: "nothing in 91-120" is a fact the screen
    has to state, and a ragged row would read as a rendering fault.

    Groups are ordered by what they are worth rather than alphabetically: the
    question this answers is which region is carrying the debt.
    """
    as_on = query.as_on
    bucket = aging_bucket_expression(view, as_on)
    code = view.c[level]
    label = _label_column(view, level)
    rows = session.execute(filtered(
        select(
            code.label("code"), label.label("name"),
            bucket.label("aging_bucket"),
            func.count().label("invoice_count"),
            func.sum(view.c.balance_amount).label("outstanding_amount"),
        ).where(_is_open(view)).group_by(code, label, bucket),
        view, filters, query,
    )).all()

    groups: dict[Any, dict[str, Any]] = {}
    for row in rows:
        record = row._mapping
        group = groups.setdefault(record["code"], {
            "code": record["code"],
            "name": record["name"] or record["code"] or UNASSIGNED,
            "outstanding_amount": 0.0,
            "invoice_count": 0,
            "amounts": {code_: 0.0 for code_ in credit.AGING_BUCKETS},
            "counts": {code_: 0 for code_ in credit.AGING_BUCKETS},
        })
        amount = _f(record["outstanding_amount"]) or 0.0
        group["amounts"][record["aging_bucket"]] = amount
        group["counts"][record["aging_bucket"]] = record["invoice_count"]
        group["outstanding_amount"] += amount
        group["invoice_count"] += record["invoice_count"]

    ordered = sorted(groups.values(), key=lambda g: -g["outstanding_amount"])
    return {
        "level": level,
        # The bucket order travels with the data, so the page renders whatever
        # the business rule currently says rather than a list of its own.
        "buckets": list(credit.AGING_BUCKETS),
        "rows": [
            {
                "code": group["code"],
                "name": group["name"],
                "outstanding_amount": round(group["outstanding_amount"], 2),
                "invoice_count": group["invoice_count"],
                "amounts": [group["amounts"][code_] for code_ in credit.AGING_BUCKETS],
                "counts": [group["counts"][code_] for code_ in credit.AGING_BUCKETS],
            }
            for group in ordered
        ],
    }


def _exposure_by_level(session: Session, view: Table, filters: ReportFilters,
                       query: CreditQuery, level: str) -> dict[str, Any]:
    """What each group is owed, how much of it is late, and by how many invoices.

    The overdue *share* is carried per group and suppressed rather than rendered
    as zero where a group has nothing outstanding — a group with no receivables
    has no overdue proportion, and 0% would read as good news about a book that
    does not exist. Same rule as the headline metric, applied per row.
    """
    as_on = query.as_on
    rows = _grouped(
        session, view, filters, query, level,
        [
            func.sum(case((_is_open(view), view.c.balance_amount), else_=0))
                .label("outstanding_amount"),
            func.sum(case((_is_overdue(view, as_on), view.c.balance_amount), else_=0))
                .label("overdue_amount"),
            func.sum(case((_is_open(view), 1), else_=0)).label("open_invoice_count"),
            func.count().label("invoice_count"),
            func.count(distinct(view.c.customer_code)).label("customer_count"),
        ],
        order_by=func.sum(case((_is_open(view), view.c.balance_amount), else_=0)).desc(),
        limit=EXPOSURE_LIMIT,
    )
    return {
        "level": level,
        "limit": EXPOSURE_LIMIT,
        "rows": [
            {
                "code": row._mapping["code"],
                "name": row._mapping["name"] or row._mapping["code"] or UNASSIGNED,
                "outstanding_amount": _f(row._mapping["outstanding_amount"]),
                "overdue_amount": _f(row._mapping["overdue_amount"]),
                "overdue_share_percent": _share(
                    row._mapping["overdue_amount"], row._mapping["outstanding_amount"]),
                "open_invoice_count": row._mapping["open_invoice_count"] or 0,
                "invoice_count": row._mapping["invoice_count"],
                "customer_count": row._mapping["customer_count"],
            }
            for row in rows
        ],
    }


def _due_profile(session: Session, view: Table, filters: ReportFilters,
                 query: CreditQuery) -> dict[str, Any]:
    """When money that is **not yet late** falls due. The aging chart's other half.

    Aging looks backwards and is what a collections team chases; this looks
    forwards and is what they plan. The SPL extract is why it is worth drawing:
    ৳35.75 Cr of that book falls due beyond the snapshot month and appears in
    neither of the source's own ``od`` and ``maturity`` columns, so a page built
    from those alone simply loses a third of the portfolio.

    Overdue money is deliberately **not** a bucket here. It has seven of its own
    on the aging chart, and repeating it would put the same taka on two charts
    that a reader would then be tempted to add. ``overdue_amount`` is returned
    beside the buckets so the two can be related without being mixed.
    """
    as_on = query.as_on
    arms: list[tuple[Any, Any]] = []
    for code, upper in credit.DUE_BUCKETS:
        if upper is None:
            continue  # the open-ended tail is the ELSE
        arms.append((view.c.due_date <= as_on + dt.timedelta(days=upper),
                     literal(code)))
    bucket = case(*arms, else_=literal(credit.DUE_BUCKETS[-1][0]))

    rows = session.execute(filtered(
        select(
            bucket.label("due_bucket"),
            func.count().label("invoice_count"),
            func.sum(view.c.balance_amount).label("due_amount"),
        ).where(and_(_is_open(view), view.c.due_date >= as_on)).group_by(bucket),
        view, filters, query,
    )).all()
    found = {row._mapping["due_bucket"]: row._mapping for row in rows}

    overdue = session.execute(filtered(
        select(func.sum(case((_is_overdue(view, as_on), view.c.balance_amount),
                             else_=0)).label("overdue_amount")),
        view, filters, query,
    )).scalar()

    return {
        "as_on_date": as_on.isoformat(),
        # Stated so the chart can say what it is *not* showing, rather than
        # leaving a reader to assume these buckets cover the whole book.
        "overdue_amount": _f(overdue),
        "buckets": [
            {
                "bucket": code,
                "invoice_count": found[code]["invoice_count"] if code in found else 0,
                "due_amount": (
                    _f(found[code]["due_amount"]) if code in found else 0.0
                ),
            }
            for code in credit.DUE_BUCKET_CODES
        ],
    }

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
            # Read through with its sign, like every other deduction on this
            # surface: what is stored is the amount by which the payment reduced
            # the balance, so an ordinary payment is already positive here.
            "amount": _float(record["payment_amount"]),
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
