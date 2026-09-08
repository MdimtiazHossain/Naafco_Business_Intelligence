"""Executive dashboard, alerts and notifications.

Every figure here comes from the Phase 3 tools — the same code the AI agent
calls — so the dashboard and the chat assistant can never disagree. No business
calculation is reimplemented for the web UI.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

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

    Every tool is scoped, so a regional manager's "Monthly Country Performance"
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


@router.get("/dashboard")
def dashboard(
    request: Request,
    date_range=Depends(date_range_params),
    filters: ScopeFilters = Depends(scope_filters),
    session: Session = Depends(get_session),
    user: UserContext = Depends(require_section(SectionKey.DASHBOARD)),
) -> dict[str, Any]:
    """Executive KPI cards plus the headline charts.

    Each KPI carries the current value, the comparable previous period and the
    growth between them, so the UI never has to compute a delta.
    """
    try:
        ctx = tool_context(session, user)
        summary = run(ctx, "get_business_summary", date_range, filters)
        growth = run(ctx, "get_sales_growth", date_range, filters,
                     compare_from=(date_range.compare_from or
                                   date_range.date_from).isoformat(),
                     compare_to=(date_range.compare_to or
                                 date_range.date_to).isoformat())
        # Monthly windows carry two earlier years and the target; a daily one
        # carries neither, because both are monthly by construction — a target
        # is a month of a financial year, and aligning days across years
        # compares different weekdays.
        #
        # **One section, two cards.** The Sales Trend line chart and the Monthly
        # Country Performance combo card both draw these rows: the first answers
        # "what shape is the year", the second "how did each month do against
        # its plan and against last year". Reading them off one result is what
        # stops the two disagreeing about a month, and it is why the key stays
        # ``sales_trend`` — same tool, same measure, same rows; only the drawing
        # was added. Renaming it would be churn rather than a stale name.
        monthly = (date_range.date_to - date_range.date_from).days > 92
        trend = run(ctx, "get_sales_trend", date_range, filters,
                    granularity=("month" if monthly else "day"),
                    limit=200,
                    **({"compare_years": 2, "include_target": True} if monthly else {}))
        if isinstance(trend.get("notes"), list):
            if not monthly:
                # The combo card is still drawn, with one actual series and no
                # percentage axis — but it has to say why, or a reader who saw
                # target bars last week reads their absence as a fault. An
                # absent card would be worse still: a section that vanishes
                # reads as a rendering failure rather than as a period too short
                # to answer the question.
                trend["notes"].append(
                    "This period is shorter than a quarter, so it is charted by "
                    "day: there is no year-on-year monthly comparison and no "
                    "monthly target to measure against."
                )
            narrowed = _narrower_than_the_country(user, filters)
            if narrowed:
                trend["notes"].append(narrowed)
        # One call where there were two, because there was only ever one
        # question. ``get_region_performance`` was drawing a region's net sales
        # beside a card that already carried it: ``target_vs_actual``'s
        # ``actual_sales`` **is** that figure, read a second time from the same
        # view over the same window. Two reads of one number is how two cards
        # come to disagree — and it cost a second pass over the fact view for a
        # column the other call already had.
        #
        # The comparison window is the one the Total Sales KPI grows against a
        # few lines above, deliberately: a region's growth on this card and the
        # headline growth beside it now answer to the same two dates, so they
        # cannot tell different stories about the same period.
        #
        # The pair is passed only when the range carries one, never defaulted to
        # this window's own dates: comparing a period against itself would put a
        # previous-period bar equal to the actual on every region and a growth
        # line flat at 0%, which is a statement rather than a gap. Absent stays
        # absent — the tool then returns neither key and the card draws neither.
        comparable = (date_range.compare_from is not None
                      and date_range.compare_to is not None)
        region_overview = run(
            ctx, "get_target_achievement", date_range, filters,
            group_by=GroupBy.REGION.value, limit=10,
            **({"compare_from": date_range.compare_from.isoformat(),
                "compare_to": date_range.compare_to.isoformat()}
               if comparable else {}),
        )
        # The dashboard ranks brands, not individual materials: fifteen pack
        # sizes of one brand is not a picture of the business. Material-level
        # ranking lives on the Material Analysis page, where it is asked for
        # explicitly.
        #
        # The target-bearing variant, because the executive table sets each
        # brand's plan against what it sold. Ranking is unchanged — it is the
        # same ``_grouped_sales`` ordering by net sales that the plain brand
        # tool uses. ``get_material_brand_performance`` is untouched and still
        # what the AI agent answers brand questions with.
        brands = run(ctx, "get_material_brand_target_performance", date_range,
                     filters, group_by=GroupBy.MATERIAL_BRAND.value,
                     limit=TOP_BRANDS)
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
        # Stock on the executive view is what is available and what is at risk.
        #
        # "Low Stock SKUs" and "Out of Stock SKUs" are gone with the module that
        # produced them: both counted SKUs below a days-of-cover threshold, and
        # material stock has no material code on which stock and sales could
        # meet. Expired stock is the replacement worth an executive's attention —
        # it is money already lost rather than a forecast.
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
        "summary": summary,
        "sales_trend": trend,
        # Not ``region_performance``: that name belongs to the sales-only tool,
        # which still runs on /api/pages/sales and is untouched there. A key
        # that outlived what it named would leave two different shapes under one
        # word.
        "region_overview": region_overview,
        "top_brands": brands,
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
