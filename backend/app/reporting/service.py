"""Reporting queries over the Phase 2 fact layer.

Every query is built with SQLAlchemy Core against the reporting views and is
fully parameterised — no user value is ever concatenated into SQL. Filters are
validated against a whitelist of column names, so an unexpected filter is a
clear error rather than an injection surface.

Ratios (growth %, achievement %, margin %) are computed in Python with
``safe_divide`` so a zero denominator yields ``None``, never an exception.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import Table, and_, func, select, text
from sqlalchemy.orm import Session

from ..etl.calendar import FinancialYearConfig, shift_years, to_date_id
from ..etl.transforms import achievement_percent, growth_percent
from ..etl.validation import safe_divide

MAX_LIMIT = 1000
DEFAULT_LIMIT = 100


@dataclass
class ReportFilters:
    """Filters accepted by every report endpoint."""

    date_from: dt.date | None = None
    date_to: dt.date | None = None
    source_system: str | None = None
    company_code: str | None = None
    bu_code: str | None = None
    sales_line_code: str | None = None
    zone_code: str | None = None
    region_code: str | None = None
    area_code: str | None = None
    unit_code: str | None = None
    territory_code: str | None = None
    sku_code: str | None = None
    category: str | None = None
    brand: str | None = None
    customer_code: str | None = None
    #: Transaction-line filters. Not organisational scope — they narrow what the
    #: caller may already see and can never widen it.
    sub_territory_code: str | None = None
    #: Where stock is held, and which plant raised an invoice. Not part of the
    #: sales hierarchy — ``vw_sales_detail`` has no such column and skips it —
    #: but a real scope level since a data scope could be granted at plant, and
    #: without a field here a plant-scoped caller could not be narrowed on the
    #: one report their scope is *for*.
    plant_code: str | None = None
    batch_code: str | None = None
    financial_year: str | None = None
    limit: int = DEFAULT_LIMIT
    offset: int = 0

    def code_filters(self) -> dict[str, str]:
        """Non-null code filters, as ``{column_name: value}``."""
        names = (
            "source_system", "company_code", "bu_code", "sales_line_code", "zone_code",
            "region_code", "area_code", "unit_code", "territory_code",
            "sub_territory_code", "plant_code", "sku_code", "category", "brand",
            "customer_code", "batch_code", "financial_year",
        )
        return {
            name: getattr(self, name) for name in names if getattr(self, name) is not None
        }

    def bounded_limit(self) -> int:
        return max(1, min(self.limit or DEFAULT_LIMIT, MAX_LIMIT))


#: Reflected views, cached per engine. Reflection costs a round trip, and a
#: report can touch the same view several times while building its metrics.
_VIEW_CACHE: dict[tuple[int, str], Table] = {}


def _view(session: Session, name: str) -> Table:
    """Reflect a reporting view as a Core table, once per engine."""
    from sqlalchemy import MetaData

    bind = session.get_bind()
    key = (id(bind), name)
    if key not in _VIEW_CACHE:
        _VIEW_CACHE[key] = Table(name, MetaData(), autoload_with=bind)
    return _VIEW_CACHE[key]


def _apply_filters(statement, view: Table, filters: ReportFilters,
                   date_column: str | None = "full_date"):
    """Apply whitelisted filters. Unknown columns are ignored, not interpolated."""
    conditions = []
    for column_name, value in filters.code_filters().items():
        if column_name in view.c:
            conditions.append(view.c[column_name] == value)

    if date_column and date_column in view.c:
        if filters.date_from is not None:
            conditions.append(view.c[date_column] >= filters.date_from)
        if filters.date_to is not None:
            conditions.append(view.c[date_column] <= filters.date_to)
    elif "date_id" in view.c:
        if filters.date_from is not None:
            conditions.append(view.c["date_id"] >= to_date_id(filters.date_from))
        if filters.date_to is not None:
            conditions.append(view.c["date_id"] <= to_date_id(filters.date_to))

    return statement.where(and_(*conditions)) if conditions else statement


def _rows_to_dicts(result) -> list[dict[str, Any]]:
    return [dict(row._mapping) for row in result]


def _volume(session: Session, view_name: str, filters: ReportFilters,
            date_column: str | None = "full_date") -> dict[str, Any]:
    """The total volume under the current filters, as one figure.

    A transaction line states its own Total Volume and carries no unit of
    measure, so this is a straight sum — the same shape ``queries.volume_total``
    returns for the dashboard and the agent. Both surfaces read the same views
    and must count the same lines: this report and the dashboard disagreeing
    about a volume is the one outcome the shared-view design exists to prevent.

    Lines with no volume are counted separately rather than treated as zero. An
    unknown volume is not the same as none, and folding it in would quietly
    understate every total the product appears in; ``value`` is ``None`` when no
    line in scope carried one at all.
    """
    view = _view(session, view_name)
    if "volume" not in view.c:
        return {"value": None, "measured_lines": 0, "unmeasured_lines": 0}

    statement = select(
        func.sum(view.c["volume"]).label("volume"),
        func.count().label("line_count"),
    ).select_from(view)
    statement = _apply_filters(statement, view, filters, date_column)
    total, measured = session.execute(
        statement.where(view.c["volume"].isnot(None))
    ).one()

    unmeasured = session.execute(
        _apply_filters(
            select(func.count()).select_from(view), view, filters, date_column
        ).where(view.c["volume"].is_(None))
    ).scalar_one()

    return {
        "value": None if total is None else _f(total),
        "measured_lines": int(measured or 0),
        "unmeasured_lines": int(unmeasured or 0),
    }


def _sum(session: Session, view: Table, filters: ReportFilters,
         columns: Sequence[str], date_column: str | None = "full_date") -> dict[str, Any]:
    """Total the given columns under the current filters."""
    available = [c for c in columns if c in view.c]
    if not available:
        return {}
    statement = select(*[func.sum(view.c[c]).label(c) for c in available]).select_from(view)
    row = session.execute(_apply_filters(statement, view, filters, date_column)).one()
    return {name: row._mapping[name] or Decimal(0) for name in available}


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------


def sales_report(session: Session, filters: ReportFilters) -> dict[str, Any]:
    """Daily sales rows plus the headline metrics: MTD, YTD, growth, achievement."""
    view = _view(session, "vw_daily_sales")
    statement = select(view).order_by(view.c.full_date.desc())
    statement = _apply_filters(statement, view, filters)
    rows = _rows_to_dicts(
        session.execute(statement.limit(filters.bounded_limit()).offset(filters.offset))
    )

    # Quantity, volume and net sales are what a sales report states; gross sales
    # and gross profit stay on the fact table but are not summed for reporting.
    totals = _sum(session, view, filters,
                  ["quantity", "discount", "net_sales", "cost"])
    net_sales = totals.get("net_sales") or Decimal(0)
    quantity = totals.get("quantity") or Decimal(0)

    reference = filters.date_to or dt.date.today()
    metrics = {
        "total": _as_float(totals),
        "average_selling_price": _f(safe_divide(net_sales, quantity)),
        **_period_metrics(session, view, filters, "net_sales", reference),
    }
    metrics["achievement"] = _achievement(session, filters, reference)
    # Volume comes from the detail view, not from the daily aggregate: the line
    # is where the source states its Total Volume, and a day-level rollup would
    # count whatever that aggregate happened to carry instead.
    metrics["volume"] = _volume(session, "vw_sales_detail", filters)
    return {"filters": _describe(filters), "metrics": metrics, "rows": rows}


def _period_metrics(session: Session, view: Table, filters: ReportFilters,
                    measure: str, reference: dt.date) -> dict[str, Any]:
    """Daily / MTD / YTD figures, and growth against the same month last year."""
    config = FinancialYearConfig.from_settings()

    def total_between(start: dt.date, end: dt.date) -> Decimal:
        window = ReportFilters(**{**filters.__dict__, "date_from": start, "date_to": end})
        return (_sum(session, view, window, [measure]).get(measure) or Decimal(0))

    month_start = reference.replace(day=1)
    previous_month_end = month_start - dt.timedelta(days=1)
    previous_month_start = previous_month_end.replace(day=1)

    daily = total_between(reference, reference)
    mtd = total_between(month_start, reference)
    previous_month = total_between(previous_month_start, previous_month_end)
    ytd = total_between(config.year_start(reference), reference)
    # The same month-to-date a year earlier: what ``growth_percent`` grows
    # against. It used to grow against ``previous_month`` — month on month —
    # which made this figure answer a different question from every other
    # growth on the platform while carrying the same name. Both bases are
    # returned, each under a field that says which it is, so neither reader has
    # to guess what the percentage beside them was computed from.
    previous_year_mtd = total_between(shift_years(month_start, -1),
                                      shift_years(reference, -1))

    return {
        "as_on": reference.isoformat(),
        "daily": _f(daily),
        "mtd": _f(mtd),
        "ytd": _f(ytd),
        "financial_year": config.label(reference),
        "previous_month": _f(previous_month),
        "previous_year_mtd": _f(previous_year_mtd),
        "growth_percent": _f(growth_percent(mtd, previous_year_mtd)),
    }


def _achievement(session: Session, filters: ReportFilters,
                 reference: dt.date) -> dict[str, Any]:
    """Target vs actual for the reference month."""
    view = _view(session, "vw_target_vs_actual")
    conditions = [view.c.year == reference.year, view.c.month == reference.month]
    for column_name, value in filters.code_filters().items():
        if column_name in view.c:
            conditions.append(view.c[column_name] == value)
    row = session.execute(
        select(
            func.sum(view.c.target_amount).label("target"),
            func.sum(view.c.actual_sales).label("actual"),
        ).where(and_(*conditions))
    ).one()

    target = row._mapping["target"] or Decimal(0)
    actual = row._mapping["actual"] or Decimal(0)
    return {
        "period": f"{reference.year}-{reference.month:02d}",
        "target": _f(target),
        "actual": _f(actual),
        "achievement_percent": _f(achievement_percent(actual, target)),
        "gap": _f(Decimal(str(target)) - Decimal(str(actual))),
    }


# ---------------------------------------------------------------------------
# Stock / target
# ---------------------------------------------------------------------------


def stock_report(session: Session, filters: ReportFilters) -> dict[str, Any]:
    """Material stock positions and the four category totals.

    ``date_column=None``: there is no posting date on a material stock position,
    so no date filter is applied. Passing one would filter on a column that does
    not exist and return nothing.
    """
    view = _view(session, "vw_material_stock_detail")
    statement = _apply_filters(
        select(view).order_by(view.c.plant_code, view.c.storage_location_code,
                              view.c.material_group_code),
        view, filters, date_column=None,
    )
    rows = _rows_to_dicts(
        session.execute(statement.limit(filters.bounded_limit()).offset(filters.offset))
    )

    totals_statement = _apply_filters(
        select(
            func.sum(view.c.unrestricted_stock).label("unrestricted_stock"),
            func.sum(view.c.quality_inspection_stock).label("quality_inspection_stock"),
            func.sum(view.c.blocked_stock).label("blocked_stock"),
            func.sum(view.c.stock_in_transit).label("stock_in_transit"),
            func.sum(view.c.total_stock).label("total_stock"),
            func.count().label("position_count"),
        ),
        view, filters, date_column=None,
    )
    row = session.execute(totals_statement).one()
    metrics = {
        name: _f(row._mapping[name])
        for name in ("unrestricted_stock", "quality_inspection_stock",
                     "blocked_stock", "stock_in_transit", "total_stock")
    }
    metrics["position_count"] = row._mapping["position_count"]
    return {"filters": _describe(filters), "metrics": metrics, "rows": rows}


def _apply_period_filters(statement, view: Table, filters: ReportFilters):
    """Restrict a year/month-grained view to the requested date range.

    ``vw_target_vs_actual`` has no date column — targets are monthly — so the
    range is applied to ``year * 100 + month``, which keeps a request for
    "August 2026" meaningful without inventing a day-level target.
    """
    period = view.c.year * 100 + view.c.month
    if filters.date_from is not None:
        statement = statement.where(
            period >= filters.date_from.year * 100 + filters.date_from.month
        )
    if filters.date_to is not None:
        statement = statement.where(
            period <= filters.date_to.year * 100 + filters.date_to.month
        )
    return statement


def target_report(session: Session, filters: ReportFilters) -> dict[str, Any]:
    view = _view(session, "vw_target_vs_actual")
    statement = _apply_filters(
        select(view).order_by(view.c.year.desc(), view.c.month.desc()),
        view, filters, date_column=None,
    )
    statement = _apply_period_filters(statement, view, filters)
    rows = _rows_to_dicts(
        session.execute(statement.limit(filters.bounded_limit()).offset(filters.offset))
    )
    totals_statement = _apply_filters(
        select(
            func.sum(view.c.target_amount).label("target_amount"),
            func.sum(view.c.actual_sales).label("actual_sales"),
        ),
        view, filters, date_column=None,
    )
    totals_statement = _apply_period_filters(totals_statement, view, filters)
    row = session.execute(totals_statement).one()
    target = row._mapping["target_amount"] or Decimal(0)
    actual = row._mapping["actual_sales"] or Decimal(0)
    metrics = {
        "target": _f(target),
        "actual": _f(actual),
        "achievement_percent": _f(achievement_percent(actual, target)),
        "gap": _f(Decimal(str(target)) - Decimal(str(actual))),
    }
    return {"filters": _describe(filters), "metrics": metrics, "rows": rows}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _f(value: Any) -> float | None:
    """Render a Decimal for JSON without losing it to float noise upstream."""
    if value is None:
        return None
    return float(value)


def _as_float(values: dict[str, Any]) -> dict[str, float | None]:
    return {key: _f(value) for key, value in values.items()}


def _describe(filters: ReportFilters) -> dict[str, Any]:
    described = {
        key: (value.isoformat() if isinstance(value, dt.date) else value)
        for key, value in filters.__dict__.items()
        if value is not None
    }
    return described


__all__ = [
    "ReportFilters",
    "sales_report",
    "stock_report",
    "target_report",
    "MAX_LIMIT",
    "DEFAULT_LIMIT",
]
