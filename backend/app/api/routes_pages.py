"""One endpoint per report page, plus a paginated transaction table.

Each page endpoint composes the Phase 3 tools into exactly the KPIs, charts and
tables that page renders — one HTTP round trip per page, and not one line of
business arithmetic in the browser.

The transaction table is server-side paginated against the Phase 3 detail views:
the fact tables are never shipped to the client.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_, asc, desc, func, or_, select
from sqlalchemy.orm import Session

from ..ai import queries as q
from ..ai.permission_filter import UserContext
from ..ai.schemas import GroupBy, ScopeFilters
from ..auth import audit
from ..database.models_ai import AuditAction
from ..auth.permissions import FORBIDDEN_MESSAGE, has_section, require_section
from ..reporting import columns as report_columns
from ..security.sections import SectionKey
from .deps import get_current_user, get_session, internal_error
from .routes_dashboard import date_range_params, run, scope_filters, tool_context

logger = logging.getLogger("app.api.pages")

router = APIRouter(prefix="/api/pages", tags=["pages"])

MAX_PAGE_SIZE = 200


def _audit(session: Session, request: Request, user: UserContext, page: str,
           date_range) -> None:
    audit.record(session, action=AuditAction.VIEW_REPORT, user_id=user.user_id,
                 username=user.username, resource=page,
                 ip_address=audit.client_ip(request),
                 detail={"period": date_range.label})
    session.commit()


def _page(handler, *, page: str):
    """Wrap a page builder with uniform auditing and error masking."""
    def wrapped(request: Request, date_range, filters, session, user, **extra):
        try:
            ctx = tool_context(session, user)
            payload = handler(ctx, date_range, filters, **extra)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise internal_error(exc, f"{page} page") from exc
        _audit(session, request, user, page, date_range)
        return {
            "period": date_range.model_dump(mode="json"),
            "filters": {k: v for k, v in filters.model_dump(mode="json").items() if v},
            **payload,
        }
    return wrapped


def _compare(date_range) -> dict[str, str]:
    return {
        "compare_from": (date_range.compare_from or date_range.date_from).isoformat(),
        "compare_to": (date_range.compare_to or date_range.date_to).isoformat(),
    }


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------


@router.get("/sales")
def sales_page(
    request: Request,
    date_range=Depends(date_range_params),
    filters: ScopeFilters = Depends(scope_filters),
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.SALES)),
) -> dict[str, Any]:
    """KPIs, trends and the four performance tables of the sales page."""
    def build(ctx, date_range, filters):
        summary = run(ctx, "get_sales_summary", date_range, filters)
        growth = run(ctx, "get_sales_growth", date_range, filters, **_compare(date_range))
        span = (date_range.date_to - date_range.date_from).days
        return {
            "summary": summary,
            "growth": growth,
            "daily_trend": run(ctx, "get_sales_trend", date_range, filters,
                               granularity="day", limit=200),
            "monthly_trend": run(ctx, "get_sales_trend", date_range, filters,
                                 granularity="month", limit=60) if span > 31 else None,
            "target_vs_actual": run(ctx, "get_sales_achievement", date_range, filters,
                                    group_by=GroupBy.REGION.value, limit=20),
            "region_performance": run(ctx, "get_region_performance", date_range, filters,
                                      group_by=GroupBy.REGION.value, limit=20),
            # The sales page's general performance breakdown is brand-wise.
            # Material-level sales sit on the Material Analysis page.
            "brand_performance": run(ctx, "get_material_brand_performance",
                                     date_range, filters,
                                     group_by=GroupBy.MATERIAL_BRAND.value,
                                     limit=20),
            "customer_performance": run(ctx, "get_customer_performance", date_range,
                                        filters, group_by=GroupBy.CUSTOMER.value,
                                        limit=20),
            "salesforce_performance": run(ctx, "get_salesforce_performance", date_range,
                                          filters, group_by=GroupBy.SALES_FORCE.value,
                                          limit=20),
            "territory_performance": run(ctx, "get_territory_performance", date_range,
                                         filters, group_by=GroupBy.TERRITORY.value,
                                         limit=20),
        }
    return _page(build, page="sales")(request, date_range, filters, session, user)


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------


@router.get("/stock")
def stock_page(
    request: Request,
    date_range=Depends(date_range_params),
    filters: ScopeFilters = Depends(scope_filters),
    expiring_within_days: int | None = Query(None, ge=0, le=3650),
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.STOCK)),
) -> dict[str, Any]:
    """Material stock: the four categories, five breakdowns and expiry.

    Every figure comes from the same tools the agent uses, so the page and a
    chat answer cannot disagree. The date range is accepted because the shared
    filter bar always sends one, and deliberately does not filter: stock is a
    current position and the source carries no posting date.

    The material breakdown is capped tighter than the other four. Plants,
    storage locations, material groups and material brands are counted in tens;
    materials are counted in thousands, and a table nobody can read is not a
    report. The rows are ranked by total stock, so the cap keeps the ones that
    matter.
    """
    def build(ctx, date_range, filters, horizon=expiring_within_days):
        expiry_args = {"expiring_within_days": horizon} if horizon else {}
        return {
            "summary": run(ctx, "get_stock_summary", date_range, filters),
            "by_plant": run(ctx, "get_stock_by_plant", date_range, filters,
                            limit=50),
            "by_storage_location": run(ctx, "get_stock_by_storage_location",
                                       date_range, filters, limit=50),
            "by_material": run(ctx, "get_stock_by_material", date_range, filters,
                               limit=25),
            "by_material_group": run(ctx, "get_stock_by_material_group",
                                     date_range, filters, limit=50),
            "by_material_brand": run(ctx, "get_stock_by_material_brand",
                                     date_range, filters, limit=50),
            "expiry": run(ctx, "get_stock_expiry", date_range, filters,
                          **expiry_args),
            "expiring": run(ctx, "get_expiring_stock", date_range, filters,
                            limit=100, **expiry_args),
        }
    return _page(build, page="stock")(request, date_range, filters, session, user)


# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------


@router.get("/target")
def target_page(
    request: Request,
    date_range=Depends(date_range_params),
    filters: ScopeFilters = Depends(scope_filters),
    below_percent: float | None = Query(None, ge=0, le=1000),
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.TARGET)),
) -> dict[str, Any]:
    def build(ctx, date_range, filters, below=below_percent):
        extra = {"below_percent": below} if below is not None else {}
        return {
            "summary": run(ctx, "get_target_achievement", date_range, filters,
                           group_by=GroupBy.REGION.value, limit=50, **extra),
            "region_achievement": run(ctx, "get_target_achievement", date_range, filters,
                                      group_by=GroupBy.REGION.value, limit=50),
            "territory_achievement": run(ctx, "get_target_achievement", date_range,
                                         filters, group_by=GroupBy.TERRITORY.value,
                                         limit=50),
            # Achievement by brand — *not* ``get_target_achievement`` with a
            # brand group_by. A brand is an attribute of the material, so a
            # brand with two targets and three sales lines would meet itself six
            # times in a raw-to-raw join and report inflated figures on both
            # sides. ``get_material_brand_target_performance`` aggregates each
            # side to brand independently and joins the two summaries, which is
            # the same construction (and the same numbers) as the dashboard's
            # Top Brands table.
            "brand_achievement": run(ctx, "get_material_brand_target_performance",
                                     date_range, filters,
                                     group_by=GroupBy.MATERIAL_BRAND.value,
                                     limit=50),
            "gap": run(ctx, "get_target_gap", date_range, filters,
                       group_by=GroupBy.REGION.value, limit=50),
        }
    return _page(build, page="target")(request, date_range, filters, session, user)


# ---------------------------------------------------------------------------
# Performance drill-down
# ---------------------------------------------------------------------------

#: Drill-down chain. Each level's report links to the next one down.
DRILL_CHAIN: tuple[str, ...] = (
    "zone", "region", "area", "unit", "territory", "sub_territory",
)

_PERFORMANCE_TOOL = {
    "zone": "get_zone_performance",
    "region": "get_region_performance",
    "area": "get_area_performance",
    "unit": "get_unit_performance",
    "territory": "get_territory_performance",
    "sub_territory": "get_sub_territory_performance",
    "sales_force": "get_salesforce_performance",
    "material_brand": "get_material_brand_performance",
    "material_group": "get_material_group_performance",
    "material": "get_material_performance",
    "customer": "get_customer_performance",
}


@router.get("/performance")
def performance_page(
    request: Request,
    level: str = Query("region"),
    date_range=Depends(date_range_params),
    filters: ScopeFilters = Depends(scope_filters),
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.PERFORMANCE)),
) -> dict[str, Any]:
    """Performance at one hierarchy level, with the next drill-down level named.

    Drilling in simply adds that level's code to the filters, so RBAC applies to
    a drill-down exactly as it does to any other report.
    """
    if level not in _PERFORMANCE_TOOL:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown level '{level}'.")

    def build(ctx, date_range, filters, level=level, limit=limit):
        performance = run(ctx, _PERFORMANCE_TOOL[level], date_range, filters,
                          group_by=level, limit=limit)
        next_level = None
        if level in DRILL_CHAIN:
            index = DRILL_CHAIN.index(level)
            if index + 1 < len(DRILL_CHAIN):
                next_level = DRILL_CHAIN[index + 1]
        achievement = None
        if level in ("region", "territory", "area", "zone"):
            achievement = run(ctx, "get_target_achievement", date_range, filters,
                              group_by=level, limit=limit)
        return {
            "level": level,
            "next_level": next_level,
            "drill_chain": list(DRILL_CHAIN),
            "performance": performance,
            "achievement": achievement,
        }
    return _page(build, page="performance")(request, date_range, filters, session, user)


# ---------------------------------------------------------------------------
# Materials and customers
# ---------------------------------------------------------------------------


#: The analysis levels the Material Analysis page offers, and how each is
#: produced — the Material Master's three, finest first. ``material`` is the
#: default because it is the grain the sales data is stated at.
MATERIAL_ANALYSIS_LEVELS: dict[str, tuple[str, str]] = {
    "material": ("get_material_performance", GroupBy.MATERIAL.value),
    "material_brand": ("get_material_brand_performance",
                       GroupBy.MATERIAL_BRAND.value),
    "material_group": ("get_material_group_performance",
                       GroupBy.MATERIAL_GROUP.value),
}


@router.get("/materials")
def materials_page(
    request: Request,
    level: str = Query("material",
                       description="material, material_brand or material_group."),
    date_range=Depends(date_range_params),
    filters: ScopeFilters = Depends(scope_filters),
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.MATERIALS)),
) -> dict[str, Any]:
    """Material analytics at material, brand or group level.

    This is the one page that still ranks individual materials, and it keeps
    doing so: the brand-wise ranking that answers a general item question
    elsewhere is the *overview* of the business, not a reason to lose the
    ability to ask which particular material is selling.

    When exactly one material brand is in scope the response also carries the
    brand breakdown — groups, materials, monthly trend, territories and
    customers — composed from the same Phase 3 tools with the brand added to the
    filters.
    """
    if level not in MATERIAL_ANALYSIS_LEVELS:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Unknown analysis level '{level}'. "
            f"Choose one of: {', '.join(MATERIAL_ANALYSIS_LEVELS)}.")

    def build(ctx, date_range, filters, level=level, limit=limit):
        tool, group_by = MATERIAL_ANALYSIS_LEVELS[level]
        performance = run(ctx, tool, date_range, filters, group_by=group_by,
                          limit=limit)
        # Growth needs the same tool over the comparison period — a second call,
        # not a second implementation.
        previous = run(ctx, tool, _shift(date_range), filters, group_by=group_by,
                       limit=200)
        previous_by_code = {r.get("code"): r for r in (previous.get("rows") or [])}

        # No stock columns here, even though sales and stock now share the
        # Material Code and could be joined on it. A stock position carries no
        # posting date, so its figure is today's regardless of the period this
        # page is showing — putting it beside a period's sales would read as a
        # measurement of that period. The Stock page answers stock questions.
        rows = []
        for row in performance.get("rows") or []:
            before = previous_by_code.get(row.get("code")) or {}
            rows.append({
                **row,
                "previous_net_sales": before.get("net_sales"),
                "growth_percent": _growth(row.get("net_sales"), before.get("net_sales")),
            })
        ranked = sorted(rows, key=lambda r: r.get("net_sales") or 0, reverse=True)
        return {
            "level": level,
            "levels": list(MATERIAL_ANALYSIS_LEVELS),
            "materials": {**performance, "rows": rows},
            "top": ranked[:10],
            "bottom": ranked[-10:][::-1] if len(ranked) > 10 else [],
            "brand_detail": _brand_detail(ctx, date_range, filters),
        }
    return _page(build, page="materials")(request, date_range, filters, session, user)


def _brand_detail(ctx, date_range, filters: ScopeFilters) -> dict[str, Any] | None:
    """The Brand Analysis breakdown, when the filters name exactly one brand.

    Every panel is an existing tool called with the brand already in the
    filters, so the brand page cannot disagree with any other report about a
    number — there is no second brand implementation to drift.
    """
    if len(filters.material_brand_names) != 1:
        return None
    return {
        "brand": filters.material_brand_names[0],
        "summary": run(ctx, "get_sales_summary", date_range, filters),
        "groups": run(ctx, "get_material_group_performance", date_range, filters,
                      group_by=GroupBy.MATERIAL_GROUP.value, limit=50),
        "materials": run(ctx, "get_material_performance", date_range, filters,
                         group_by=GroupBy.MATERIAL.value, limit=100),
        "monthly_trend": run(ctx, "get_sales_trend", date_range, filters,
                             granularity="month", limit=60),
        "territories": run(ctx, "get_territory_performance", date_range, filters,
                           group_by=GroupBy.TERRITORY.value, limit=50),
        "customers": run(ctx, "get_customer_performance", date_range, filters,
                         group_by=GroupBy.CUSTOMER.value, limit=50),
        "volume": run(ctx, "get_sales_volume", date_range, filters,
                      group_by=GroupBy.MATERIAL_BRAND.value, limit=50),
    }


@router.get("/customers")
def customers_page(
    request: Request,
    date_range=Depends(date_range_params),
    filters: ScopeFilters = Depends(scope_filters),
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.CUSTOMERS)),
) -> dict[str, Any]:
    """Customer analytics from the metrics the warehouse actually holds.

    Sales, quantity, invoice count and last transaction date are all measured.
    Nothing else is offered: inventing a metric the warehouse cannot support
    would be worse than omitting it.

    Receivables used to sit beside those figures — outstanding, overdue, and a
    collected amount per customer. All three left with the Collection and
    Outstanding modules in revision 0020, and no substitute is put in their
    place: a customer's exposure is not something this platform can state any
    more, so it says nothing rather than something adjacent.
    """
    def build(ctx, date_range, filters, limit=limit):
        sales = run(ctx, "get_customer_performance", date_range, filters,
                    group_by=GroupBy.CUSTOMER.value, limit=200)
        last_seen = _last_transaction_by_customer(ctx, date_range, filters)
        sales_by = {r.get("code"): r for r in (sales.get("rows") or [])}

        rows = []
        for code, sale in sales_by.items():
            if code is None:
                continue
            rows.append({
                "code": code,
                "label": sale.get("label") or code,
                "net_sales": sale.get("net_sales") or 0.0,
                "quantity": sale.get("quantity") or 0.0,
                "invoice_count": sale.get("invoice_count") or 0,
                "last_transaction": last_seen.get(code),
            })
        rows.sort(key=lambda r: r["net_sales"], reverse=True)
        # A customer in scope who bought nothing in the period. Previously this
        # also required an unpaid balance, which is no longer knowable.
        inactive = [r for r in rows if r["net_sales"] == 0]
        return {
            "customers": {"rows": rows[:limit], "row_count": len(rows),
                          "truncated": len(rows) > limit},
            "top_customers": rows[:10],
            "inactive_customers": inactive[:10],
            "note": (
                "Customer master data is not loaded yet, so customers are "
                "identified by code."
            ),
        }
    return _page(build, page="customers")(request, date_range, filters, session, user)


def _last_transaction_by_customer(ctx, date_range, filters) -> dict[str, Any]:
    """Latest invoice date per customer inside the window and scope."""
    scoped = ctx.scoped(filters)
    table = q.view(ctx.session, q.SALES_VIEW)
    statement = select(
        table.c["customer_code"], func.max(table.c["full_date"])
    ).select_from(table)
    conditions = q.filter_conditions(table, scoped, date_range.date_from,
                                     date_range.date_to)
    if conditions:
        statement = statement.where(and_(*conditions))
    rows = ctx.session.execute(statement.group_by(table.c["customer_code"])).all()
    return {row[0]: (row[1].isoformat() if hasattr(row[1], "isoformat") else row[1])
            for row in rows if row[0]}


def _shift(date_range):
    """The comparison period as a range object, for a like-for-like second call."""
    from ..ai.schemas import ResolvedDateRange

    start = date_range.compare_from or date_range.date_from
    end = date_range.compare_to or date_range.date_to
    return ResolvedDateRange(type=date_range.type, date_from=start, date_to=end,
                             label="comparison period")


def _growth(current: Any, previous: Any) -> float | None:
    from ..etl.transforms import growth_percent

    value = growth_percent(current, previous)
    return None if value is None else float(value)


# ---------------------------------------------------------------------------
# Transaction table (server-side pagination)
# ---------------------------------------------------------------------------

#: Defined in ``app.reporting.columns`` and re-exported here, so the names this
#: module has always used keep working while the definitions live somewhere a
#: service can import without reaching into the HTTP layer.
TRANSACTION_COLUMNS = report_columns.TRANSACTION_COLUMNS
_VIEW_BY_TYPE = report_columns.VIEW_BY_TYPE
_SECTION_BY_TYPE = report_columns.SECTION_BY_TYPE
_SEARCH_COLUMNS = report_columns.SEARCH_COLUMNS


@router.get("/transactions/{data_type}")
def transactions(
    data_type: str,
    request: Request,
    date_range=Depends(date_range_params),
    filters: ScopeFilters = Depends(scope_filters),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    search: str | None = Query(None, max_length=100),
    sort_by: str | None = Query(None),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """Row-level transactions, paginated on the server.

    The browser never receives more than one page, and ``sort_by`` is checked
    against the column whitelist rather than passed through to SQL.

    The section is chosen by ``data_type`` rather than declared as a dependency,
    because one route serves five sections: a user denied Stock must be refused
    ``/transactions/stock`` while keeping ``/transactions/sales``.
    """
    if data_type not in TRANSACTION_COLUMNS:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"Unknown data type '{data_type}'.")
    if not has_section(session, user, _SECTION_BY_TYPE[data_type]):
        raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN_MESSAGE)
    columns = TRANSACTION_COLUMNS[data_type]
    if sort_by and sort_by not in columns:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"Cannot sort by '{sort_by}'.")

    try:
        ctx = tool_context(session, user)
        scoped = ctx.scoped(filters)
        table = q.view(session, _VIEW_BY_TYPE[data_type])

        conditions = q.filter_conditions(table, scoped, date_range.date_from,
                                         date_range.date_to)
        if search:
            needle = f"%{search.strip()}%"
            searchable = [
                table.c[name].ilike(needle)
                for name in _SEARCH_COLUMNS[data_type] if name in table.c
            ]
            if searchable:
                conditions.append(or_(*searchable))

        selected = [table.c[name] for name in columns if name in table.c]
        base = select(*selected).select_from(table)
        if conditions:
            base = base.where(and_(*conditions))

        total = session.execute(
            select(func.count()).select_from(
                base.with_only_columns(*selected).subquery()
            )
        ).scalar_one()

        order_column = table.c[sort_by] if sort_by else table.c[columns[0]]
        base = base.order_by(desc(order_column) if sort_dir == "desc"
                             else asc(order_column))
        rows = q.normalize_rows([
            dict(row._mapping)
            for row in session.execute(base.limit(page_size)
                                       .offset((page - 1) * page_size))
        ])
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, f"{data_type} transactions") from exc

    return {
        "data_type": data_type,
        "period": date_range.model_dump(mode="json"),
        "columns": [c for c in columns if c in table.c],
        "rows": rows,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }
