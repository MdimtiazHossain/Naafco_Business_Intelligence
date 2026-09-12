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
from ..ai.schemas import MAX_LIMIT, DateRangeType, GroupBy, ScopeFilters
from ..ai.tools import ToolContext, execute_tool
from ..auth import audit
from ..config import get_settings
from ..database.models_ai import AuditAction, Notification
from ..database.models_warehouse import DimDate, FactCreditInvoice
from ..etl.mapping import MasterDataIndex
from ..auth.permissions import has_section, require_section
from ..security.sections import SectionKey
from .deps import get_current_user, get_session, internal_error

logger = logging.getLogger("app.api.dashboard")

router = APIRouter(prefix="/api", tags=["dashboard"])

#: How many customers the dashboard's headline ranking shows.
TOP_CUSTOMERS = 50


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
    about the same period. It is **the same dates a year earlier**, not the
    preceding period — see ``ResolvedDateRange.growth_from`` for the +96% that
    rule was written to stop. It is always passed now: a year-shifted window
    can never coincide
    with the window it is compared against, so the case the old conditional
    guarded (a previous-period bar equal to the actual, a growth line flat at
    0%) cannot arise.

    That also makes ``previous_sales`` and ``net_sales_minus_1`` the same
    figure, deliberately: the growth line and the first earlier-year bar are
    then arithmetically incapable of disagreeing, which is exactly what went
    wrong when they were two different measures under one legend.

    **Every region, and the limit states no number of its own.** It asked for
    ten, and the deployment has thirteen — so three were dropped from a card
    whose title promises the regions rather than the best of them. The omission
    was worse than a short list because this tool ranks by *achievement*: on the
    financial year it was dropping Cumilla at 2.37 Cr while drawing Sreemangal
    at 1.33 Cr, so the three that went missing were not the three smallest and
    no reader could work out the rule. Raising it to thirteen would be the same
    defect with a longer fuse — the fourteenth region would vanish on the day it
    was opened, which is the stale-list failure at the top of CLAUDE.md. The
    rows are bounded by the master data (one per region) rather than by the
    facts, so the platform ceiling never bites and asking for it is how this
    card says "all of them".
    """
    grew_from, grew_to = date_range.growth_from, date_range.growth_to
    return run(
        ctx, "get_target_achievement", date_range, filters,
        group_by=GroupBy.REGION.value, limit=MAX_LIMIT, compare_years=2,
        include_invoice_count=False,
        compare_from=grew_from.isoformat(), compare_to=grew_to.isoformat(),
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


def _top_customers(ctx: ToolContext, date_range, filters: ScopeFilters,
                   user: UserContext) -> dict[str, Any]:
    """The fifty biggest customers, three years of net sales each.

    It replaced Top 15 Brands, which the brand cards above already answer twice
    over — ``brand_sales`` ranks brands by volume and the Sales page breaks down
    by brand — while nothing on this dashboard named a customer. The tool it
    called, ``get_material_brand_target_performance``, is untouched and still on
    the assistant's allow-list; only the card is gone.

    **On a year period, a column headed FY 2024-25 holds that financial year,
    whole.** The window is passed as the reader chose it, so ``compare_years``
    shifts it back by whole years — which is what the heading claims and the
    only reading that makes a history table a history.

    On a *shorter* period the columns are that same span shifted back, so "Last
    Month" draws August 2026 against August 2025 and August 2024. The headings
    still name the financial year each window falls in, which is the shared
    tool's rule and right for the region card's legend — three bars that differ
    by years should say so rather than repeating the month. As a table heading
    it is looser than it looks, and correcting it would move that legend too;
    it is left alone deliberately rather than overlooked.

    This was briefly the other way. The window was cut to today first, so every
    column covered the same elapsed span and ``growth_percent`` equalled the
    change between the last two exactly. That bought internal consistency at the
    price of the columns meaning something other than what they said: "FY
    2025-26" held ten weeks of FY 2025-26. Between a figure that is what it
    claims and a table that is arithmetically self-contained, the label wins —
    a reader comes to this card to see what a customer bought last year.

    **The cost is real and is stated rather than left to be inferred.** On an
    unfinished year the last column is only the part that has happened, so it
    sits low beside two complete ones, and ``growth_percent`` — which compares
    the same dates a year earlier, as growth does everywhere here — will not be
    the change between the two columns beside it. A note says so whenever the
    window runs past today. Every other period is unaffected: This Month, This
    Quarter, YTD and the rest already end today, and the completed ones end in
    the past, so only "This Year" ever differed.

    The columns are **not** hard-coded to 24-25 / 25-26 / 26-27. They are
    whatever ``chart.series`` names, which is two years rather than three when
    the third recorded nothing — a year that recorded nothing is dropped rather
    than drawn along the floor, the same rule the region card follows.
    """
    section = run(ctx, "get_target_achievement", date_range, filters,
                  group_by=GroupBy.CUSTOMER.value, limit=TOP_CUSTOMERS,
                  compare_years=2, rank_by="actual",
                  include_invoice_count=False,
                  compare_from=date_range.growth_from.isoformat(),
                  compare_to=date_range.growth_to.isoformat())

    today = dt.date.today()
    if date_range.date_to > today and isinstance(section.get("notes"), list):
        # Only when the period has not finished, and phrased as what the figures
        # *are* rather than as a warning: the earlier columns being whole years
        # is the point of the card, not a defect to apologise for.
        section["notes"].insert(0, (
            f"{ctx.financial_year_name(date_range.date_from, date_range.date_to)}"
            f" covers {date_range.date_from} to {today} so far; the earlier "
            "years are complete. Growth compares the same dates a year "
            "earlier, so it is not the change between the last two columns."
        ))
    return section



def _overdue_receivables(ctx: ToolContext, date_range, filters: ScopeFilters,
                         user: UserContext) -> dict[str, Any]:
    """What is overdue, as a share of what is owed, narrowed to the reader.

    **This card was refused for three revisions, and the reason it is here now is
    not that anybody changed their mind.** The dashboard is the one screen a
    regional manager opens by default, and the credit view reached the customer's
    sub-territory and no further — so the only two options were to refuse them
    the whole dashboard over a receivables figure they were never going to see,
    or to put an unscoped national overdue total on it. CLAUDE.md recorded the
    condition rather than the verdict: the card goes back on the table once the
    credit view carries the sales hierarchy. Revision 0040 is that, so the figure
    on this card is now the reader's own region.

    **It reports a share, not an amount**, for the same reason ``HIGH_OVERDUE``
    does: a crore overdue is alarming on a small book and routine on a large one,
    so a threshold in taka would need re-setting every time the business grew.

    **The period is ignored, and the first version of this only said so.** Every
    other card answers "what happened between these dates"; a receivable is a
    *position*, and what is owed today is owed whichever month the reader has
    selected. The docstring claimed that while the code passed the dashboard's
    window straight through to a tool that filters on invoice date — so on the
    default period the card came back empty on the real book, because the newest
    extract's last posting is 31 August and the default period is September.
    A card that reads "no data" about a ৳90 Cr book is worse than no card.

    So the window is widened to the whole book: from the earliest invoice the
    warehouse holds to the reporting date. The bounds exist only because
    ``CreditToolInput`` requires them — every tool schema does, deliberately —
    and they are read from the data rather than set to an arbitrary early date,
    so the card cannot silently start excluding invoices older than a constant
    somebody once guessed.

    The reader's **filters** still apply. A period is not a narrowing of a
    position; a region is.
    """
    earliest = ctx.session.execute(
        select(func.min(DimDate.full_date))
        .select_from(FactCreditInvoice)
        .join(DimDate, DimDate.date_id == FactCreditInvoice.invoice_date_id)
        .where(FactCreditInvoice.is_void == False)  # noqa: E712
    ).scalar()
    as_on = dt.date.today()
    whole_book = date_range.model_copy(update={
        "date_from": earliest or as_on,
        "date_to": as_on,
    })
    return run(ctx, "get_credit_summary", whole_book, filters,
               as_on_date=as_on.isoformat())


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
    "top_customers": _top_customers,
    "overdue_receivables": _overdue_receivables,
}

#: Cards that need a section of their own beyond the dashboard's.
#:
#: Receivables are the only one, and they are not incidental: credit exposure is
#: the basis for stopping a customer's supply, so Credit Control is a permission
#: in its own right rather than something that rides along with the reporting
#: sections every role gets. A reader who does not hold it does not get the card
#: **named** — it is absent, not refused — which is the same rule Target
#: Management follows: a control whose only outcome is a refusal teaches people
#: to ignore controls.
SECTION_BY_DASHBOARD_CARD: dict[str, str] = {
    "overdue_receivables": SectionKey.CREDIT_CONTROL,
}


def _cards_for(session: Session, user: UserContext) -> list[str]:
    """The cards this reader may actually be served, in registry order.

    Filtered rather than fixed, so the browser asks for what it can have. It
    takes the list from the frame instead of carrying its own, so a card gated
    here is a card it never requests — and the section endpoint applies the same
    gate, because a list the browser is given is not a permission check.
    """
    return [
        name for name in DASHBOARD_SECTIONS
        if name not in SECTION_BY_DASHBOARD_CARD
        or has_section(session, user, SECTION_BY_DASHBOARD_CARD[name])
    ]


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
        # Year on year, like the card below it — see the growth pair on
        # ``ResolvedDateRange``. The comparison pair beside it describes the
        # *preceding* period, which is a different question and made these two
        # figures answer it differently from the bars drawn beside them.
        grew_from, grew_to = date_range.growth_from, date_range.growth_to
        growth = run(ctx, "get_sales_growth", date_range, filters,
                     compare_from=grew_from.isoformat(),
                     compare_to=grew_to.isoformat())
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
        # actually serves instead of a list it carries separately — and filtered
        # to what *this reader* may be served, so a card they do not hold the
        # section for is absent rather than drawn and then refused.
        "sections": _cards_for(session, user),
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
    required = SECTION_BY_DASHBOARD_CARD.get(name)
    if required is not None and not has_section(session, user, required):
        # A 404, the same answer as a card this build does not serve. The frame
        # never named it to this reader, so from where they stand it does not
        # exist — and a 403 here would disclose that a receivables card is on
        # the dashboard of people senior to them.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Unknown dashboard section '{name}'. "
            f"This dashboard serves: {', '.join(_cards_for(session, user))}.")

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
