"""Executive dashboard, alerts and notifications.

Every figure here comes from the Phase 3 tools — the same code the AI agent
calls — so the dashboard and the chat assistant can never disagree. No business
calculation is reimplemented for the web UI.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from sqlalchemy import desc, func, select, update
from sqlalchemy.orm import Session

from ..ai import queries as q
from ..ai.date_resolver import DateResolver
from ..ai.permission_filter import PermissionFilter, UserContext
from ..ai.exceptions import DateResolutionError
from ..ai.schemas import DateRangeType, GroupBy, ScopeFilters
from ..ai.tools import ToolContext, execute_tool
from ..auth import audit
from ..config import get_settings
from ..database.models_ai import AuditAction, Notification
from ..etl.mapping import MasterDataIndex
from ..auth.permissions import require_section
from ..security.sections import SectionKey
from .deps import get_current_user, get_session, internal_error

logger = logging.getLogger("app.api.dashboard")

router = APIRouter(prefix="/api", tags=["dashboard"])

#: How many brands the dashboard's headline ranking shows.
TOP_BRANDS = 15


# ---------------------------------------------------------------------------
# Shared filter plumbing
# ---------------------------------------------------------------------------


def scope_filters(
    company_code: list[str] | None = Query(None),
    bu_code: list[str] | None = Query(None),
    sales_line_code: list[str] | None = Query(None),
    zone_code: list[str] | None = Query(None),
    region_code: list[str] | None = Query(None),
    area_code: list[str] | None = Query(None),
    unit_code: list[str] | None = Query(None),
    territory_code: list[str] | None = Query(None),
    sub_territory_code: list[str] | None = Query(None),
    customer_code: list[str] | None = Query(None),
    sales_force_code: list[str] | None = Query(None),
    batch_code: list[str] | None = Query(None),
    material_code: list[str] | None = Query(
        None, description="Material codes, as they appear in the Material "
                          "Master. Narrows sales, target and stock alike."),
    material_group_code: list[str] | None = Query(
        None, description="Material groups — the Material Master's top "
                          "classification level."),
    material_brand: list[str] | None = Query(
        None, description="Material brands, by name as the Material Master "
                          "spells them — the brand code identifies nothing. "
                          "This is the brand every report groups by."),
    plant_code: list[str] | None = Query(
        None, description="Plant codes. Stock only — a sale states no plant."),
    storage_location_key: list[str] | None = Query(
        None, description="Storage locations, as 'plant|location'. The bare "
                          "location code is unique only within its plant, so "
                          "the pair is what identifies one. Stock only."),
    expiry_status: list[str] | None = Query(
        None, description="Shelf-life buckets: EXPIRED, EXPIRING_SOON, VALID "
                          "or NO_EXPIRY. Stock only."),
    source_system: str | None = Query(None),
) -> ScopeFilters:
    """The global filter bar, as tool filters.

    **Every level is a repeated parameter**, because the bar is multi-select:
    ``?region_code=R1&region_code=R2`` is two regions and means "either", which
    is what ``filter_conditions`` turns into one ``IN``. A single value is the
    one-element case of the same thing, so a link written before the bar could
    multi-select still resolves to exactly the report it always did.

    Only these named fields exist — there is no free-text filter and no way to
    pass a predicate, so a crafted query string can narrow a report but never
    widen one. There is no volume-unit filter: a transaction line records one
    Total Volume and no unit, so there is no unit to narrow to.
    """
    def values(supplied: list[str] | None) -> list[str]:
        # De-duplicated, order preserved, blanks dropped: a repeated parameter
        # is easy to send twice and ``IN ('R1', 'R1')`` is noise in the plan.
        return list(dict.fromkeys(v for v in (supplied or []) if v))

    return ScopeFilters(
        company_codes=values(company_code),
        business_unit_codes=values(bu_code),
        sales_line_codes=values(sales_line_code),
        zone_codes=values(zone_code),
        region_codes=values(region_code),
        area_codes=values(area_code),
        unit_codes=values(unit_code),
        territory_codes=values(territory_code),
        sub_territory_codes=values(sub_territory_code),
        customer_codes=values(customer_code),
        sales_force_codes=values(sales_force_code),
        batch_codes=values(batch_code),
        material_codes=values(material_code),
        material_group_codes=values(material_group_code),
        material_brand_names=values(material_brand),
        plant_codes=values(plant_code),
        storage_location_keys=values(storage_location_key),
        expiry_statuses=values(expiry_status),
        source_system=source_system,
    )


def resolve_range(period: str | None, date_from: dt.date | None,
                  date_to: dt.date | None, today: dt.date | None = None):
    """Turn a named period (or explicit dates) into a concrete range.

    Named periods are resolved by the Phase 3 resolver, so "YTD" follows the
    company's configured financial year — the frontend never computes a date.
    """
    resolver = DateResolver(today=today)
    if date_from and date_to:
        if date_to < date_from:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "The end date precedes the start date.")
        return resolver.custom_range(date_from, date_to)
    if period:
        try:
            return resolver.of_type(DateRangeType(period.upper()))
        except (ValueError, KeyError, DateResolutionError):
            # `DateResolutionError` covers a range type that exists but cannot be
            # resolved from a name alone — MONTH needs to be told *which* month,
            # so it is never offered as a preset and is refused here like any
            # other unusable value rather than reaching the client as a 500.
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                f"Unknown period '{period}'.") from None
    return resolver.of_type(DateRangeType.THIS_MONTH)


def date_range_params(
    period: str | None = Query(None, description="TODAY, THIS_MONTH, YTD, ..."),
    date_from: dt.date | None = Query(None),
    date_to: dt.date | None = Query(None),
):
    return resolve_range(period, date_from, date_to)


def _narrower_than_the_country(user: UserContext,
                               filters: ScopeFilters) -> str | None:
    """Why the country card is not showing the country, when it is not.

    Every tool is scoped, so a regional manager's "Monthly Performance"
    is their region's months — correct for what they may see, and a figure that
    is neither the whole nor labelled as partial is exactly what this platform
    does not put on a screen. The card is **narrowed and labelled**, never
    hidden: withholding it would deny them the one view of their own year.

    The same sentence covers an active filter, because a reader who narrowed to
    one region is looking at the identical partial number. Filters are read from
    the model rather than listed here, so a level added to ``ScopeFilters``
    is named by this note on the day it is added.
    """
    narrowings: list[str] = []
    if not user.is_unrestricted and user.data_scope:
        narrowings.append(f"your data scope ({user.describe_scope()})")
    active = sorted(
        key.removesuffix("_codes").removesuffix("_keys").removesuffix("_names")
           .replace("_", " ")
        for key, value in filters.model_dump(mode="json").items() if value
    )
    if active:
        narrowings.append(f"the filters you have set ({', '.join(active)})")
    if not narrowings:
        return None
    return ("These figures cover " + " and ".join(narrowings)
            + ", not the whole country.")


def tool_context(session: Session, user: UserContext) -> ToolContext:
    index = MasterDataIndex(session)
    return ToolContext(session, PermissionFilter(session, user, index), user)


def run(ctx: ToolContext, tool: str, date_range, filters: ScopeFilters,
        **extra: Any) -> dict[str, Any]:
    """Execute a Phase 3 tool and return its result as plain data."""
    arguments = {
        "date_from": date_range.date_from.isoformat(),
        "date_to": date_range.date_to.isoformat(),
        "filters": filters.model_dump(mode="json"),
        **extra,
    }
    invocation = execute_tool(ctx, tool, arguments)
    if invocation.result is None:
        return {"tool": tool, "rows": [], "values": {}, "value": None,
                "error": invocation.error_message}
    return invocation.result.model_dump(mode="json")


@router.get("/period-options")
def period_options() -> dict[str, Any]:
    """Named periods the date filter offers, with the financial-year setting."""
    settings = get_settings()
    resolver = DateResolver()
    options = []
    for range_type in (DateRangeType.TODAY, DateRangeType.YESTERDAY,
                       DateRangeType.THIS_WEEK, DateRangeType.LAST_WEEK,
                       DateRangeType.THIS_MONTH, DateRangeType.LAST_MONTH,
                       DateRangeType.THIS_QUARTER, DateRangeType.YTD,
                       DateRangeType.THIS_YEAR, DateRangeType.LAST_YEAR):
        resolved = resolver.of_type(range_type)
        options.append({
            "value": range_type.value,
            "label": resolved.label,
            "date_from": resolved.date_from.isoformat(),
            "date_to": resolved.date_to.isoformat(),
        })
    return {
        "options": options,
        "financial_year_start_month": settings.financial_year_start_month,
        "current_financial_year": resolver.fy.label(resolver.today),
    }


# No ``/units`` endpoint. It served the volume-unit filter in the global filter
# bar, and both are gone: a transaction line states one Total Volume and no
# unit, so there is nothing for a reader to filter by. Since revision 0022 a
# target volume has no unit either.


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The dashboard's cards
#
# Each is its own request, fetched in parallel by the browser. They were one
# response until the page took nine seconds to paint: eight independent tool
# calls, none of them dominant, run one after another because they shared a
# session. That is the same problem the business map met — "five layers in one
# call waited 7.5 s on the PostgreSQL deployment before the first could paint" —
# and this is the same answer, so the two surfaces solve it once.
#
# Splitting rather than parallelising inside the endpoint is deliberate.
# ``DB_POOL_SIZE`` is 5 with 5 of overflow, so one request running eight
# aggregates at once would hold eight connections and two readers would exhaust
# the pool. A request per card holds one, and a card paints when its own answer
# arrives instead of every card waiting for the slowest.
#
# Every builder below passes ``include_invoice_count=False``, and it is the
# largest single saving on this page rather than a tidy-up. A distinct count
# forces the database to sort every row by the group columns and then the
# invoice where the plain sums would hash-aggregate; measured on the deployment,
# three financial-year windows grouped by region cost 46.72s with it and 6.09s
# without. No index removes it — the sort key starts with a column of
# ``dim_region``, not of ``fact_sales`` — so declining it is the whole lever.
# It is stated per card rather than defaulted in ``run`` because ``run`` also
# serves the KPI strip and ``routes_pages``, and because the honest question at
# each call site is "does this card draw an invoice count?". None of them does.
# ---------------------------------------------------------------------------


def _sales_trend(ctx: ToolContext, date_range, filters: ScopeFilters,
                 user: UserContext) -> dict[str, Any]:
    """The period's own shape: by month where it is long enough, by day where not.

    Monthly windows carry two earlier years and the target; a daily one carries
    neither, because both are monthly by construction — a target is a month of a
    financial year, and aligning days across years compares different weekdays.
    """
    monthly = (date_range.date_to - date_range.date_from).days > 92
    return run(ctx, "get_sales_trend", date_range, filters,
               granularity=("month" if monthly else "day"), limit=200,
               include_invoice_count=False,
               **({"compare_years": 2, "include_target": True} if monthly else {}))


def _monthly_performance(ctx: ToolContext, date_range, filters: ScopeFilters,
                         user: UserContext) -> dict[str, Any]:
    """A year of months, whatever period is chosen.

    It used to draw ``sales_trend`` — one query, two cards — which held while
    the period was long enough to be charted by month and broke the moment it
    was not: on the default "This month" the trend is charted by day, so the
    card drew a bar per *date*, with no target and no earlier year to set it
    against, under a title promising months.

    Widening the shared query was the alternative and would have changed the
    Sales Trend card, which is not this card's business — a short period charted
    by day is exactly what that line chart is for. So the two cover different
    windows deliberately, and this one says which financial year it is showing.

    On This Year and Last Year the two windows coincide and the same query runs
    for both cards. That was deduplicated while they shared a request and cannot
    be now; it costs a second aggregate rather than a second wait, because the
    two requests are in flight together.
    """
    fy = DateResolver()
    year_of_card = fy.financial_year(fy.fy.start_year_of(date_range.date_to))
    section = run(ctx, "get_sales_trend", year_of_card, filters,
                  granularity="month", limit=200, compare_years=2,
                  include_target=True, include_invoice_count=False)
    if isinstance(section.get("notes"), list):
        section["notes"].insert(0, (
            f"Twelve months of {year_of_card.label}, the financial year of the "
            "selected period. This card is a year of months whatever period is "
            "chosen; your other filters still narrow it."
        ))
        narrowed = _narrower_than_the_country(user, filters)
        if narrowed:
            section["notes"].append(narrowed)
    return section


def _region_overview(ctx: ToolContext, date_range, filters: ScopeFilters,
                     user: UserContext) -> dict[str, Any]:
    """One call where there were two, because there was only ever one question.

    ``get_region_performance`` was drawing a region's net sales beside a card
    that already carried it: ``target_vs_actual``'s ``actual_sales`` **is** that
    figure, read a second time from the same view over the same window.

    The comparison window is the one the Total Sales KPI grows against, so a
    region's growth here and the headline growth cannot tell different stories
    about the same period. The pair is passed only when the range carries one,
    never defaulted to this window's own dates: comparing a period against
    itself would put a previous-period bar equal to the actual on every region
    and a growth line flat at 0%, which is a statement rather than a gap.
    """
    comparable = (date_range.compare_from is not None
                  and date_range.compare_to is not None)
    return run(
        ctx, "get_target_achievement", date_range, filters,
        group_by=GroupBy.REGION.value, limit=10, compare_years=2,
        include_invoice_count=False,
        **({"compare_from": date_range.compare_from.isoformat(),
            "compare_to": date_range.compare_to.isoformat()}
           if comparable else {}),
    )


def _territory_sales(ctx: ToolContext, date_range, filters: ScopeFilters,
                     user: UserContext) -> dict[str, Any]:
    """Ranked by what was sold rather than by how close it came to target.

    A card headed "Sales" that ranked by achievement would list whoever had the
    smallest target, and its top twenty would be a different twenty.
    """
    return run(ctx, "get_target_achievement", date_range, filters,
               group_by=GroupBy.TERRITORY.value, limit=20, compare_years=1,
               rank_by="actual", include_invoice_count=False)


def _brand_sales(ctx: ToolContext, date_range, filters: ScopeFilters,
                 user: UserContext) -> dict[str, Any]:
    """Ranked by volume, because it is *drawn* in volume.

    Ordered by taka and drawn in volume its bars came out in no order at all —
    the brand with the smallest volume of the top four sat at the head of the
    chart.
    """
    return run(ctx, "get_target_achievement", date_range, filters,
               group_by=GroupBy.MATERIAL_BRAND.value, limit=20, compare_years=1,
               rank_by="volume", include_invoice_count=False)


def _top_brands(ctx: ToolContext, date_range, filters: ScopeFilters,
                user: UserContext) -> dict[str, Any]:
    """Brands, not individual materials: fifteen pack sizes of one brand is not
    a picture of the business.

    The target-bearing variant, because the executive table sets each brand's
    plan against what it sold. ``get_material_brand_performance`` is untouched
    and still what the AI agent answers brand questions with.
    """
    return run(ctx, "get_material_brand_target_performance", date_range, filters,
               group_by=GroupBy.MATERIAL_BRAND.value, limit=TOP_BRANDS,
               include_invoice_count=False)


#: Every card below the KPI strip, by the name the browser asks for.
#:
#: The registry is the list — the frame endpoint publishes ``sections`` from it
#: and the section endpoint refuses anything not in it, so a card cannot be
#: added in one place and forgotten in the other. That is the rule at the top of
#: CLAUDE.md applied to a route: a name here must never outlive what it names.
DASHBOARD_SECTIONS: dict[str, Callable[..., dict[str, Any]]] = {
    "sales_trend": _sales_trend,
    "monthly_performance": _monthly_performance,
    "region_overview": _region_overview,
    "territory_sales": _territory_sales,
    "brand_sales": _brand_sales,
    "top_brands": _top_brands,
}


@router.get("/dashboard")
def dashboard(
    request: Request,
    date_range=Depends(date_range_params),
    filters: ScopeFilters = Depends(scope_filters),
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.DASHBOARD)),
) -> dict[str, Any]:
    """The KPI strip, the period, and the names of the cards that hang on it.

    Each KPI carries the current value, the comparable previous period and the
    growth between them, so the UI never has to compute a delta. The cards
    themselves are separate requests — see ``DASHBOARD_SECTIONS`` for why — and
    this is the only one that audits, because opening the dashboard is one act
    however many requests draw it.
    """
    try:
        ctx = tool_context(session, user)
        summary = run(ctx, "get_business_summary", date_range, filters)
        growth = run(ctx, "get_sales_growth", date_range, filters,
                     compare_from=(date_range.compare_from or
                                   date_range.date_from).isoformat(),
                     compare_to=(date_range.compare_to or
                                 date_range.date_to).isoformat())
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "dashboard") from exc

    values = summary.get("values") or {}
    sales_growth = (growth.get("values") or {}).get("growth_percent")
    previous_sales = (growth.get("values") or {}).get("previous")

    # No Total Quantity card: the executive view reports value and volume, and a
    # unit count across every SKU in the business answered no question anyone was
    # asking. ``values["quantity"]`` is deliberately still produced by
    # ``get_business_summary`` — the AI agent, the map metrics, the sales reports
    # and every grouped sales table read it, and only this card is gone.
    kpis = [
        _kpi("total_sales", "Total Sales", values.get("sales"), previous_sales,
             sales_growth, "currency"),
        _kpi("target", "Target", values.get("target"), None, None, "currency"),
        _kpi("achievement", "Achievement", values.get("achievement_percent"), None,
             None, "percent"),
        # No Collection, Outstanding or Overdue cards. All three left with the
        # modules that produced them in revision 0020; nothing replaces them,
        # because the receivables position is not something this platform can
        # state any more.
        #
        # Stock on the executive view is what is available and what is at risk.
        # "Low Stock SKUs" and "Out of Stock SKUs" are gone with the module that
        # produced them: both counted SKUs below a days-of-cover threshold, and
        # material stock has no material code on which stock and sales could
        # meet. Expired stock is the replacement worth an executive's attention —
        # it is money already lost rather than a forecast.
        #
        # ``stock``, not ``quantity``: these are the same measure the Material
        # Stock page reports, so they must not read as a bare count here. The
        # unit is in the label rather than on the value — the card shows
        # "Unrestricted Stock (KG/LTR)" over a plain figure — which is also why
        # the label is spelled out here and not just in the browser's
        # translations: an API caller reading the KPI must get the unit too.
        _kpi("unrestricted_stock", f"Unrestricted Stock ({q.STOCK_UNIT})",
             values.get("unrestricted_stock"), None, None, "stock"),
        _kpi("expiring_soon_stock", f"Expiring Soon ({q.STOCK_UNIT})",
             values.get("expiring_soon_stock"), None, None, "stock"),
        _kpi("expired_stock", f"Expired Stock ({q.STOCK_UNIT})",
             values.get("expired_stock"), None, None, "stock"),
        # No Sales Volume card. The executive view leads with value, target and
        # achievement, and the volume behind them is read where it can be broken
        # down — the Sales page's own volume section, the Top 15 Brands table's
        # Sales Vol column, and the agent's volume answers. ``get_sales_volume``
        # is untouched and still serves all three; only this card is gone, and
        # with it the region-grouped volume query the dashboard ran for it.
    ]

    audit.record(session, action=AuditAction.VIEW_REPORT, user_id=user.user_id,
                 username=user.username, resource="dashboard",
                 ip_address=audit.client_ip(request),
                 detail={"period": date_range.label})
    session.commit()

    return {
        "period": date_range.model_dump(mode="json"),
        "filters": {k: v for k, v in filters.model_dump(mode="json").items() if v},
        "kpis": kpis,
        # Named rather than assumed, so the browser asks for what this build
        # actually serves instead of a list it carries separately.
        "sections": list(DASHBOARD_SECTIONS),
    }


@router.get("/dashboard/section/{name}")
def dashboard_section(
    name: str,
    date_range=Depends(date_range_params),
    filters: ScopeFilters = Depends(scope_filters),
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.DASHBOARD)),
) -> dict[str, Any]:
    """One card of the dashboard, over the same period and filters as the rest.

    Not audited: the frame endpoint records the view, and auditing each card
    would file one opening of the dashboard as seven.
    """
    builder = DASHBOARD_SECTIONS.get(name)
    if builder is None:
        # Named in the refusal, because a caller that asked for a card this
        # build does not serve should be told which ones it does.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Unknown dashboard section '{name}'. "
            f"This dashboard serves: {', '.join(DASHBOARD_SECTIONS)}.")
    try:
        section = builder(tool_context(session, user), date_range, filters, user)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "dashboard") from exc

    return {
        "period": date_range.model_dump(mode="json"),
        "filters": {k: v for k, v in filters.model_dump(mode="json").items() if v},
        "name": name,
        "section": section,
    }


def _kpi(key: str, label: str, value: Any, previous: Any, growth: Any,
         fmt: str, unit: str | None = None) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "value": value,
        "previous_value": previous,
        "growth_percent": growth,
        "format": fmt,
        "unit": unit,
    }


# No ``_volume_kpi`` helper. It built the one Sales Volume card and nothing
# else, so it left with it. ``format="volume"`` is still a KPI format the pages
# use — ``routes_pages`` reports volume on the Sales page — and the frontend
# still renders it.


# ---------------------------------------------------------------------------
# Alerts and notifications
# ---------------------------------------------------------------------------


@router.get("/alerts")
def alerts(
    date_range=Depends(date_range_params),
    filters: ScopeFilters = Depends(scope_filters),
    severity: str | None = Query(None, pattern="^(CRITICAL|HIGH|MEDIUM|LOW)$"),
    category: str | None = Query(None),
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.ALERTS)),
) -> dict[str, Any]:
    """Live business alerts, computed from the warehouse within the caller's scope."""
    try:
        ctx = tool_context(session, user)
        result = run(ctx, "get_business_alerts", date_range, filters)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "alerts") from exc

    rows = result.get("rows") or []
    for row in rows:
        row["category"] = _alert_category(row.get("alert_type"))
        row["link"] = _alert_link(row.get("alert_type"))
    if severity:
        rows = [r for r in rows if r.get("severity") == severity]
    if category:
        rows = [r for r in rows if r.get("category") == category]

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["severity"]] = counts.get(row["severity"], 0) + 1

    return {
        "period": date_range.model_dump(mode="json"),
        "alerts": rows,
        "total": len(rows),
        "counts_by_severity": counts,
        "thresholds": (result.get("values") or {}).get("thresholds", {}),
    }


#: Alert type -> the page that explains it.
_ALERT_ROUTES = {
    "LOW_ACHIEVEMENT": "/target",
    "EXPIRED_STOCK": "/stock",
    "EXPIRING_STOCK": "/stock",
    "SALES_DECLINE": "/sales",
}
_ALERT_CATEGORIES = {
    "LOW_ACHIEVEMENT": "TARGET",
    "EXPIRED_STOCK": "STOCK",
    "EXPIRING_STOCK": "STOCK",
    "SALES_DECLINE": "SALES",
}


def _alert_category(alert_type: str | None) -> str:
    return _ALERT_CATEGORIES.get(alert_type or "", "SYSTEM")


def _alert_link(alert_type: str | None) -> str:
    return _ALERT_ROUTES.get(alert_type or "", "/alerts")


@router.get("/notifications")
def notifications(
    unread_only: bool = Query(False),
    limit: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """The caller's notifications, newest first."""
    conditions = [Notification.user_id == user.user_id]
    if unread_only:
        conditions.append(Notification.is_read.is_(False))

    rows = session.execute(
        select(Notification).where(*conditions)
        .order_by(desc(Notification.notification_id)).limit(limit)
    ).scalars().all()
    unread = session.execute(
        select(func.count()).select_from(Notification)
        .where(Notification.user_id == user.user_id, Notification.is_read.is_(False))
    ).scalar_one()

    return {
        "unread_count": unread,
        "notifications": [
            {
                "notification_id": row.notification_id,
                "category": row.category,
                "severity": row.severity,
                "title": row.title,
                "body": row.body,
                "link": row.link,
                "is_read": row.is_read,
                "created_at": row.created_at,
            }
            for row in rows
        ],
    }


@router.post("/notifications/{notification_id}/read")
def mark_read(notification_id: int, session: Session = Depends(get_session),
              user: UserContext = Depends(get_current_user)) -> dict[str, str]:
    """Mark one notification read. Only the owner may do so."""
    row = session.get(Notification, notification_id)
    if row is None or row.user_id != user.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found.")
    row.is_read = True
    row.read_at = dt.datetime.now(dt.timezone.utc)
    session.commit()
    return {"status": "read"}


@router.post("/notifications/read-all")
def mark_all_read(session: Session = Depends(get_session),
                  user: UserContext = Depends(get_current_user)) -> dict[str, int]:
    """Mark every unread notification read."""
    result = session.execute(
        update(Notification)
        .where(Notification.user_id == user.user_id, Notification.is_read.is_(False))
        .values(is_read=True, read_at=dt.datetime.now(dt.timezone.utc))
    )
    session.commit()
    return {"updated": result.rowcount or 0}
