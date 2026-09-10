"""Pydantic schemas: intents, the structured query, tool I/O and chat contracts.

The structured query is the contract between natural language and the warehouse.
Whether it was produced by the LLM or by the deterministic planner, it is
validated here *before* any tool runs — an invalid query never reaches the
database.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_LIMIT = 500
DEFAULT_LIMIT = 100
#: How many earlier years a trend may bring back beside the requested window.
#:
#: Four lines is already the most a reader can tell apart on one axis, and each
#: one is another pass over the fact view — the ceiling is as much about the
#: chart being readable as about the query being bounded.
MAX_COMPARE_YEARS = 4
DEFAULT_TABLE_LIMIT = 20


class Intent(str, Enum):
    """Every business intent the agent recognises."""

    SALES_SUMMARY = "SALES_SUMMARY"
    SALES_DETAIL = "SALES_DETAIL"
    SALES_TREND = "SALES_TREND"
    SALES_GROWTH = "SALES_GROWTH"
    SALES_TARGET = "SALES_TARGET"
    SALES_ACHIEVEMENT = "SALES_ACHIEVEMENT"
    SALES_VOLUME = "SALES_VOLUME"

    # Credit Control. Receivables returned in revision 0031 against a source
    # that exists, so these three are answerable — which is the whole test an
    # intent has to pass here. An intent the agent recognises but no tool can
    # answer is worse than one it does not know, because the question gets a
    # confident-looking refusal instead of "I don't report that".
    #
    # There is still no COLLECTION intent. No extract states individual
    # payments, so a collections question remains one this platform cannot
    # answer, and the assistant says so rather than reaching for the aggregate
    # payment figure on an invoice and calling it a collection.
    CREDIT_SUMMARY = "CREDIT_SUMMARY"
    CREDIT_AGING = "CREDIT_AGING"
    CREDIT_OVERDUE = "CREDIT_OVERDUE"

    # Material stock. There is deliberately no LOW_STOCK, OUT_OF_STOCK or
    # STOCK_COVERAGE: all three divided stock by an average daily sales *rate*,
    # and a rate needs two readings and the time between them. The Material
    # Transaction Data states no posting date, so there is one reading and no
    # elapsed time — which is also why there is no STOCK_TREND. Since revision
    # 0022 stock and sales do name the same material, so the two figures can be
    # reported together; it is the rate that cannot be derived, not the join.
    STOCK_SUMMARY = "STOCK_SUMMARY"
    STOCK_BY_PLANT = "STOCK_BY_PLANT"
    STOCK_BY_STORAGE_LOCATION = "STOCK_BY_STORAGE_LOCATION"
    STOCK_BY_MATERIAL = "STOCK_BY_MATERIAL"
    STOCK_BY_MATERIAL_GROUP = "STOCK_BY_MATERIAL_GROUP"
    STOCK_BY_MATERIAL_BRAND = "STOCK_BY_MATERIAL_BRAND"
    STOCK_EXPIRY = "STOCK_EXPIRY"
    EXPIRING_STOCK = "EXPIRING_STOCK"

    TARGET_SUMMARY = "TARGET_SUMMARY"
    TARGET_ACHIEVEMENT = "TARGET_ACHIEVEMENT"
    TARGET_GAP = "TARGET_GAP"

    CUSTOMER_PERFORMANCE = "CUSTOMER_PERFORMANCE"
    #: The item-wise intents, named for the master they read. There is no
    #: ``PRODUCT_PERFORMANCE`` since revision 0022: the SKU master it ranked is
    #: gone, and "top products" is answered from the Material Master like every
    #: other item question.
    MATERIAL_PERFORMANCE = "MATERIAL_PERFORMANCE"
    MATERIAL_BRAND_PERFORMANCE = "MATERIAL_BRAND_PERFORMANCE"
    MATERIAL_GROUP_PERFORMANCE = "MATERIAL_GROUP_PERFORMANCE"
    REGION_PERFORMANCE = "REGION_PERFORMANCE"
    ZONE_PERFORMANCE = "ZONE_PERFORMANCE"
    AREA_PERFORMANCE = "AREA_PERFORMANCE"
    UNIT_PERFORMANCE = "UNIT_PERFORMANCE"
    TERRITORY_PERFORMANCE = "TERRITORY_PERFORMANCE"
    SUB_TERRITORY_PERFORMANCE = "SUB_TERRITORY_PERFORMANCE"
    SALES_FORCE_PERFORMANCE = "SALES_FORCE_PERFORMANCE"

    BUSINESS_SUMMARY = "BUSINESS_SUMMARY"
    BUSINESS_ALERT = "BUSINESS_ALERT"
    ROOT_CAUSE_ANALYSIS = "ROOT_CAUSE_ANALYSIS"

    UNKNOWN = "UNKNOWN"


class DateRangeType(str, Enum):
    TODAY = "TODAY"
    YESTERDAY = "YESTERDAY"
    THIS_WEEK = "THIS_WEEK"
    LAST_WEEK = "LAST_WEEK"
    THIS_MONTH = "THIS_MONTH"
    LAST_MONTH = "LAST_MONTH"
    THIS_QUARTER = "THIS_QUARTER"
    LAST_QUARTER = "LAST_QUARTER"
    #: A quarter the reader named — "Q3", "3rd quarter", "ত্রৈমাসিক ৩".
    #:
    #: Always a *financial* quarter, because every other quarter in this
    #: platform is: ``dim_date.financial_quarter`` is what the warehouse stores
    #: and FY 2024-25 Q1 is July to September. A calendar Q1 would be a fifth
    #: definition of the same word, disagreeing with the target sheets, the
    #: reporting views and the people who write them.
    QUARTER = "QUARTER"
    THIS_YEAR = "THIS_YEAR"
    LAST_YEAR = "LAST_YEAR"
    MTD = "MTD"
    QTD = "QTD"
    YTD = "YTD"
    LAST_N_DAYS = "LAST_N_DAYS"
    #: One named calendar month — "January", "জানুয়ারি", "January FY 2024-25".
    #: Deliberately not in the date filter's option list: a preset cannot name
    #: a month, so this type is only ever produced by resolving text.
    MONTH = "MONTH"
    FINANCIAL_YEAR = "FINANCIAL_YEAR"
    CUSTOM = "CUSTOM"


class GroupBy(str, Enum):
    """Dimensions a report can be grouped by. Anything else is rejected."""

    DATE = "date"
    MONTH = "month"
    #: The financial quarter, labelled with its year: two consecutive Q1s are
    #: two different quarters, and a breakdown that merged them would report one
    #: bar holding two years of sales.
    QUARTER = "quarter"
    COMPANY = "company"
    BUSINESS_UNIT = "business_unit"
    SALES_LINE = "sales_line"
    ZONE = "zone"
    REGION = "region"
    AREA = "area"
    UNIT = "unit"
    TERRITORY = "territory"
    SUB_TERRITORY = "sub_territory"
    CUSTOMER = "customer"
    SALES_FORCE = "sales_force"
    #: No ``WAREHOUSE`` and no ``AGING_BUCKET`` (revision 0020). A sale states no
    #: warehouse and stock is located by Plant and Storage Location; an aging
    #: bucket classified a receivable, and receivables are gone.
    #:
    #: No ``PRODUCT``, ``BRAND`` or ``CATEGORY`` either (revision 0022). Those
    #: three grouped by attributes of the SKU master, and the three below replace
    #: them outright — an item question is answered from the Material Master
    #: wherever it is asked, so a sales report and a stock report group the same
    #: goods by the same column.
    #:
    #: ``PLANT`` and ``STORAGE_LOCATION`` remain stock-only: they say where a
    #: position is held and a sale states neither, so ``aggregate_by`` refuses
    #: them against any other view rather than returning an empty result.
    PLANT = "plant"
    STORAGE_LOCATION = "storage_location"
    MATERIAL = "material"
    MATERIAL_GROUP = "material_group"
    MATERIAL_BRAND = "material_brand"


class EntityType(str, Enum):
    COMPANY = "company"
    BUSINESS_UNIT = "business_unit"
    SALES_LINE = "sales_line"
    ZONE = "zone"
    REGION = "region"
    AREA = "area"
    UNIT = "unit"
    TERRITORY = "territory"
    SUB_TERRITORY = "sub_territory"
    CUSTOMER = "customer"
    SALES_FORCE = "sales_force"
    #: A material in the Material Master, resolved by its code, its description
    #: or its material brand. The one item entity since revision 0022 — the SKU
    #: entity beside it named the same goods by a second identity.
    MATERIAL = "material"


#: Organisational entity types, shallowest first — the permission hierarchy.
ORG_ENTITY_TYPES: tuple[EntityType, ...] = (
    EntityType.COMPANY,
    EntityType.BUSINESS_UNIT,
    EntityType.SALES_LINE,
    EntityType.ZONE,
    EntityType.REGION,
    EntityType.AREA,
    EntityType.UNIT,
    EntityType.TERRITORY,
    EntityType.SUB_TERRITORY,
)


class ResolvedEntity(BaseModel):
    """A user term successfully matched to one official master record."""

    model_config = ConfigDict(frozen=True)

    entity_type: EntityType
    code: str
    label: str
    term: str = Field(description="The text the user actually typed.")
    match: Literal["code", "exact_name", "partial_name"] = "exact_name"


class ResolvedDateRange(BaseModel):
    """A concrete, inclusive date range plus how it was derived."""

    type: DateRangeType
    date_from: dt.date
    date_to: dt.date
    label: str
    financial_year: str | None = None
    #: The comparable **preceding** period — the month before, the quarter
    #: before. This is what "compare August with July" means, and it is what
    #: the assistant answers a "vs last month" question from.
    compare_from: dt.date | None = None
    compare_to: dt.date | None = None
    compare_label: str | None = None
    #: The same dates **a year earlier**, which is what *growth* means here.
    #:
    #: A second pair rather than a reinterpretation of the first, because the
    #: two answer different questions and one field cannot hold both. There
    #: used to be only the preceding period, so every surface that wanted
    #: growth reached for it — it was the only pair there — and three of them
    #: got the wrong question. The Region Performance card reported +96% for a
    #: region whose sales had fallen 11% year on year, comparing August with
    #: July instead of with August, and contradicted the earlier-year bars
    #: drawn beside it.
    #:
    #: Bangladeshi FMCG is seasonal: the month before is not a baseline for the
    #: month after. Anything drawing a figure it calls "growth" reads this pair;
    #: anything genuinely asking about the period before reads the one above.
    #: The end is capped at today before the shift, so a window running into the
    #: future — "This Year" reaches next June — is compared by the part of it
    #: that has actually happened rather than against a complete previous year.
    growth_from: dt.date | None = None
    growth_to: dt.date | None = None

    @model_validator(mode="after")
    def _ordered(self) -> "ResolvedDateRange":
        if self.date_to < self.date_from:
            raise ValueError("date_to precedes date_from")
        return self

    def as_filters(self) -> dict[str, str]:
        return {"date_from": self.date_from.isoformat(), "date_to": self.date_to.isoformat()}


class SortSpec(BaseModel):
    field: str = "net_sales"
    direction: Literal["asc", "desc"] = "desc"


class StructuredQuery(BaseModel):
    """Natural language, converted into something executable and checkable."""

    model_config = ConfigDict(use_enum_values=False)

    intent: Intent = Intent.UNKNOWN
    date_range: ResolvedDateRange | None = None
    entities: list[ResolvedEntity] = Field(default_factory=list)
    group_by: list[GroupBy] = Field(default_factory=list)
    sort: SortSpec = Field(default_factory=SortSpec)
    limit: int = DEFAULT_TABLE_LIMIT
    compare: bool = False
    language: Literal["en", "bn", "mixed"] = "en"
    #: Anything the agent inferred rather than being told, surfaced to the user.
    assumptions: list[str] = Field(default_factory=list)

    @field_validator("limit")
    @classmethod
    def _bound_limit(cls, value: int) -> int:
        return max(1, min(value, MAX_LIMIT))

    def codes_for(self, entity_type: EntityType) -> list[str]:
        return [e.code for e in self.entities if e.entity_type == entity_type]

    def filter_summary(self) -> dict[str, str]:
        """Human-readable filters, for the "assumptions" block in the answer."""
        summary: dict[str, str] = {}
        for entity in self.entities:
            key = entity.entity_type.value
            summary[key] = (
                f"{summary[key]}, {entity.label}" if key in summary else entity.label
            )
        return summary


# ---------------------------------------------------------------------------
# Tool input schemas
# ---------------------------------------------------------------------------


class ScopeFilters(BaseModel):
    """Organisational, item and location filters accepted by every tool.

    These are the *only* filter fields a tool will honour. There is no free-text
    field and no SQL field anywhere in the tool surface.
    """

    model_config = ConfigDict(extra="forbid")

    company_codes: list[str] = Field(default_factory=list)
    business_unit_codes: list[str] = Field(default_factory=list)
    sales_line_codes: list[str] = Field(default_factory=list)
    zone_codes: list[str] = Field(default_factory=list)
    region_codes: list[str] = Field(default_factory=list)
    area_codes: list[str] = Field(default_factory=list)
    unit_codes: list[str] = Field(default_factory=list)
    territory_codes: list[str] = Field(default_factory=list)
    sub_territory_codes: list[str] = Field(default_factory=list)
    customer_codes: list[str] = Field(default_factory=list)
    sales_force_codes: list[str] = Field(default_factory=list)
    #: Narrows to particular batches. A transaction-line attribute rather than
    #: organisational scope, so no permission is attached to it: it narrows what
    #: a user may already see, it never widens it.
    #:
    #: There is deliberately no volume-unit filter. A line states one Total
    #: Volume and no unit, so there is nothing to narrow by.
    batch_codes: list[str] = Field(default_factory=list)
    #: The item filters — the Material Master's three levels, narrowing sales,
    #: target and stock alike since revision 0022. Before that a sales report was
    #: narrowed by SKU, brand and category from one master while a stock report
    #: was narrowed by material, brand and group from another, and the two could
    #: not be asked the same question.
    #:
    #: None of them carries a permission of its own: a material does not belong
    #: to a region, so an item filter can only narrow what the caller's
    #: organisational scope already allows, never widen it.
    material_codes: list[str] = Field(default_factory=list)
    material_group_codes: list[str] = Field(default_factory=list)
    #: By name, because ``material_brand_code`` is ``'0'`` on every material the
    #: Material Master holds and so identifies nothing.
    material_brand_names: list[str] = Field(default_factory=list)
    #: Where a stock position is held. Each names a column only
    #: ``vw_material_stock_detail`` carries, so every other view skips them — a
    #: sale states no plant and no storage location.
    #:
    #: No permission attaches to these either. A plant belongs to a company, not
    #: to a region, and the organisational scope a user is granted says nothing
    #: about which plants they may see.
    plant_codes: list[str] = Field(default_factory=list)
    #: The ``plant|storage_location`` pair, never the bare code: a storage
    #: location code is unique only within its plant, so a filter on the code
    #: alone could not say *which* ``FG01`` was meant.
    storage_location_keys: list[str] = Field(default_factory=list)
    #: Shelf-life buckets: ``EXPIRED``, ``EXPIRING_SOON``, ``VALID`` or
    #: ``NO_EXPIRY``. Unlike every other filter here this names no column — the
    #: bucket is derived from ``shelf_life_expiration_date`` against today and
    #: the configured horizon, so it is applied by the stock queries, which know
    #: both, rather than by the generic column matcher.
    expiry_statuses: list[str] = Field(default_factory=list)
    source_system: str | None = None

    def is_empty(self) -> bool:
        return not any(
            getattr(self, name) for name in self.model_fields if name != "source_system"
        )


class BaseToolInput(BaseModel):
    """Common tool arguments. Dates are required so nothing is unbounded."""

    model_config = ConfigDict(extra="forbid")

    date_from: dt.date
    date_to: dt.date
    filters: ScopeFilters = Field(default_factory=ScopeFilters)

    @model_validator(mode="after")
    def _ordered(self) -> "BaseToolInput":
        if self.date_to < self.date_from:
            raise ValueError("date_to must not precede date_from")
        return self


class SalesMeasureToolInput(BaseToolInput):
    """A tool that aggregates the sales measures, and may decline the dear one.

    ``COUNT(DISTINCT invoice_no)`` rides on every sales aggregate through
    ``queries.SALES_MEASURES.counts``, and it is not a column like the others:
    a distinct count forces a ``GroupAggregate`` — every row sorted by the group
    columns and then the invoice — where the plain sums hash-aggregate and never
    sort. Measured on the deployment, three financial-year windows grouped by
    region cost 46.72s with it and 6.09s without. No index can repair that (the
    sort key starts with a column of ``dim_region``, not of ``fact_sales``), so
    the only lever is not asking for it.

    This sits between ``BaseToolInput`` and the three grouped-sales inputs
    rather than on ``BaseToolInput`` itself: a stock or credit tool has no
    invoice count to skip, and a flag that quietly does nothing is a setting
    somebody will trust.
    """

    #: Whether the distinct invoice count comes back with the figures.
    #:
    #: **This defaults on, unlike every other ``include_`` flag here**, and the
    #: asymmetry is deliberate rather than an oversight. Those flags add a
    #: column that was never there; this one has been returned since the first
    #: sales report, so defaulting it off would silently drop the Customers
    #: page's Invoices column and a field the assistant already formats. A
    #: caller that draws no invoice count turns it off and pays for nothing;
    #: everyone else is unchanged by doing nothing.
    include_invoice_count: bool = True


class GroupedToolInput(SalesMeasureToolInput):
    """A tool that returns rows grouped by a dimension."""

    group_by: GroupBy = GroupBy.REGION
    limit: int = DEFAULT_TABLE_LIMIT
    sort_direction: Literal["asc", "desc"] = "desc"
    #: Report each group's share of the period's total beside its figures.
    #:
    #: Off by default because it costs a second aggregate: the denominator is
    #: the total for the whole window and filters, never the sum of the rows
    #: returned. Summing the rows would make the top five's shares add to 100%
    #: however small a part of the business they are — a wrong number that
    #: looks exactly like a right one.
    include_share: bool = False

    @field_validator("limit")
    @classmethod
    def _bound_limit(cls, value: int) -> int:
        return max(1, min(value, MAX_LIMIT))


class VolumeToolInput(BaseToolInput):
    """A volume question, optionally grouped.

    There is no unit argument. A transaction line states one Total Volume and
    carries no unit of measure, so "how much volume" has exactly one answer for
    any scope and there is nothing for the caller to select between.
    """

    group_by: GroupBy | None = None
    limit: int = DEFAULT_TABLE_LIMIT

    @field_validator("limit")
    @classmethod
    def _bound_limit(cls, value: int) -> int:
        return max(1, min(value, MAX_LIMIT))


class TrendToolInput(SalesMeasureToolInput):
    """A time series, optionally against earlier years and the period's target.

    The comparison is **the caller's own window shifted back by whole years** —
    March to September against March to September — rather than a window this
    schema picks. That honours the date filter instead of overriding it, needs
    no rule about which financial year anchors a window that straddles two, and
    is the comparison ``get_sales_growth`` already makes, so the platform has
    one idea of what "the same period last year" means rather than two.
    """

    granularity: Literal["day", "month"] = "day"
    limit: int = DEFAULT_LIMIT
    #: How many earlier years to bring back beside the requested window.
    compare_years: int = 0
    #: Whether the period's target is drawn beside the actuals.
    include_target: bool = False

    @field_validator("limit")
    @classmethod
    def _bound_limit(cls, value: int) -> int:
        return max(1, min(value, MAX_LIMIT))

    @field_validator("compare_years")
    @classmethod
    def _bound_compare_years(cls, value: int) -> int:
        return max(0, min(value, MAX_COMPARE_YEARS))

    @model_validator(mode="after")
    def _monthly_only(self) -> "TrendToolInput":
        """Both extras are monthly, and refusing is better than coercing.

        Aligning *days* across years compares a Tuesday with a Thursday and
        February with a month that has thirty-one of them, so a daily comparison
        would be a chart of the calendar rather than of the business. And a
        target is a month of a financial year — ``fact_target`` carries no date
        at all — so there is no daily target to draw. Silently switching the
        caller to monthly would answer a question they did not ask.
        """
        if self.granularity != "month":
            if self.compare_years:
                raise ValueError(
                    "compare_years needs granularity='month': aligning days "
                    "across years compares different weekdays and month lengths."
                )
            if self.include_target:
                raise ValueError(
                    "include_target needs granularity='month': a target is a "
                    "month of a financial year and has no daily figure."
                )
        return self


class GrowthToolInput(BaseToolInput):
    """Current period against a comparison period."""

    compare_from: dt.date
    compare_to: dt.date


class MapLayerToolInput(BaseToolInput):
    """One business level, every entity, every measure the map draws.

    ``compare_from`` / ``compare_to`` are optional as a pair: with them the
    rows carry growth against that window, without them growth is ``None`` —
    never a growth against a window this tool chose for itself.
    """

    group_by: GroupBy = GroupBy.REGION
    compare_from: dt.date | None = None
    compare_to: dt.date | None = None

    @model_validator(mode="after")
    def _comparison_is_a_pair(self) -> "MapLayerToolInput":
        if (self.compare_from is None) != (self.compare_to is None):
            raise ValueError("compare_from and compare_to must be given together")
        if (self.compare_from is not None and self.compare_to is not None
                and self.compare_to < self.compare_from):
            raise ValueError("compare_to must not precede compare_from")
        return self


class CreditToolInput(BaseToolInput):
    """Credit Control questions.

    The inherited date range filters on the **invoice date**: "invoices raised
    this quarter" is the period question a receivable answers to. How *late*
    those invoices are is a different question with a different date, which is
    what ``as_on_date`` is for.

    ``as_on_date`` is the day the overdue arithmetic is done against, and it
    defaults to today rather than to ``date_to``. Those two are not the same
    question and conflating them would be wrong in the common case: asking about
    last quarter's invoices does not mean asking how late they were on the last
    day of that quarter — it means how late they are *now*. A reader who wants
    the month-end position says so.
    """

    as_on_date: dt.date | None = None
    #: How near a due date counts as Due Soon, overriding the configured
    #: horizon for one question. Configuration rather than a constant for the
    #: same reason the stock expiry horizon is: "soon" is a collections policy.
    due_soon_days: int | None = None


class StockToolInput(BaseToolInput):
    """Material stock questions.

    The inherited date range is accepted but does not filter: stock is a current
    position and the source carries no posting date. Every stock answer says so
    in a note rather than quietly returning the same figure for any window.

    ``expiring_within_days`` overrides the configured "expiring soon" horizon for
    one question — "what expires in the next week?" — and falls back to
    ``STOCK_EXPIRING_SOON_DAYS`` when unset. There was no existing business
    threshold, so it is configuration rather than a constant.
    """

    expiring_within_days: int | None = None
    #: Which of the four categories ranks a grouped stock answer. They are four
    #: different questions — "most blocked stock" is not "most stock" — and the
    #: rows carry all four either way, so this decides the ordering and the
    #: headline sentence, never what is reported. A closed set: the caller can
    #: choose a measure, never name a column.
    sort_by: Literal["total_stock", "unrestricted_stock",
                     "quality_inspection_stock", "blocked_stock",
                     "stock_in_transit"] = "total_stock"
    limit: int = DEFAULT_TABLE_LIMIT

    @field_validator("expiring_within_days")
    @classmethod
    def _sane_horizon(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return max(0, min(value, 3650))

    @field_validator("limit")
    @classmethod
    def _bound_limit(cls, value: int) -> int:
        return max(1, min(value, MAX_LIMIT))


class AchievementToolInput(SalesMeasureToolInput):
    """Target against actual, by group.

    ``compare_from`` / ``compare_to`` are optional **as a pair**, the same shape
    and for the same reason as :class:`MapLayerToolInput`: with them each row
    carries its actual for that window and the growth against it, without them
    growth is absent — never a growth against a window this tool chose for
    itself. A caller who has not said what to compare with has not asked for a
    comparison, and answering one anyway is the invented figure this platform
    exists to refuse.
    """

    group_by: GroupBy = GroupBy.REGION
    below_percent: float | None = None
    limit: int = DEFAULT_TABLE_LIMIT
    compare_from: dt.date | None = None
    compare_to: dt.date | None = None
    #: Earlier years to carry beside this window, each the caller's **own**
    #: window shifted back by whole years — the same comparison
    #: :class:`TrendToolInput` makes and for the same reason: it honours the
    #: date filter rather than replacing it with a period this tool chose, and
    #: it removes the question of which financial year anchors a window that
    #: straddles two. Distinct from ``compare_from`` / ``compare_to``, which is
    #: one named window and answers "against the period before this one";
    #: a caller may ask for either, both or neither.
    compare_years: int = 0
    #: Which figure "top" means. ``achievement`` is the default because that is
    #: what this tool is for — who is meeting their target — but a card headed
    #: *Sales* ranked that way lists whoever is closest to a small target, not
    #: whoever sold the most, and its top twenty is a different twenty. It is
    #: ``volume`` is the same argument again for a card that *draws* volume: a
    #: ranked chart ordered by one measure and drawn in another comes out in no
    #: order at all, which is worse than either. It is
    #: honoured only where the caller has not already declared an ordering:
    #: ``below_percent`` and ``gap_only`` are questions about who fell short,
    #: and they keep the worst-first order that answers them.
    rank_by: Literal["achievement", "actual", "volume"] = "achievement"

    @field_validator("limit")
    @classmethod
    def _bound_limit(cls, value: int) -> int:
        return max(1, min(value, MAX_LIMIT))

    @field_validator("compare_years")
    @classmethod
    def _bound_compare_years(cls, value: int) -> int:
        return max(0, min(value, MAX_COMPARE_YEARS))

    @model_validator(mode="after")
    def _comparison_is_a_pair(self) -> "AchievementToolInput":
        if (self.compare_from is None) != (self.compare_to is None):
            raise ValueError("compare_from and compare_to must be given together")
        if (self.compare_from is not None and self.compare_to is not None
                and self.compare_to < self.compare_from):
            raise ValueError("compare_to must not precede compare_from")
        return self


class RootCauseToolInput(BaseToolInput):
    """Variance analysis of one period against another."""

    compare_from: dt.date
    compare_to: dt.date
    top_n: int = 5


class BusinessSummaryToolInput(BaseToolInput):
    pass


class AlertToolInput(BaseToolInput):
    achievement_below_percent: float = 80.0
    # No coverage threshold: stock alerts are now about shelf life, and the
    # horizon comes from STOCK_EXPIRING_SOON_DAYS rather than from the caller.
    sales_decline_percent: float = 10.0
    #: How much of the outstanding book may be past due before it is an alert.
    #:
    #: A *share*, not an amount: a crore overdue is alarming on a small book and
    #: routine on a large one, so a threshold in taka would need re-setting every
    #: time the business grew.
    overdue_share_percent: float = 25.0


# ---------------------------------------------------------------------------
# Tool output
# ---------------------------------------------------------------------------


class ChartSpec(BaseModel):
    """Chart-ready data. Only emitted when a chart genuinely adds something."""

    type: Literal["line", "bar", "pie", "area"]
    x_axis: str
    y_axis: str
    data: list[dict[str, Any]] = Field(default_factory=list)
    #: The lines to draw, when there is more than one.
    #:
    #: ``y_axis`` names the primary measure and stays what it always was, so a
    #: single-series chart is unchanged and every existing reader keeps working.
    #: A chart carrying this draws one line per entry, keyed by ``key`` — which
    #: is a column of every row in ``data``, absent at a position the series has
    #: no figure for.
    series: list[dict[str, str]] = Field(default_factory=list)


class ToolResult(BaseModel):
    """Every tool returns this shape, so the formatter never special-cases."""

    tool: str
    metric: str | None = None
    currency: str = "BDT"
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    value: float | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    chart: ChartSpec | None = None
    #: Statements the data supports directly.
    facts: list[str] = Field(default_factory=list)
    #: Statements that are the agent's reading of the data, kept separate.
    interpretations: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    #: Warehouse objects the numbers came from.
    sources: list[str] = Field(default_factory=list)
    truncated: bool = False


# ---------------------------------------------------------------------------
# Chat API
# ---------------------------------------------------------------------------


class ChatContext(BaseModel):
    """The global filter bar as it stood when the question was asked.

    The assistant used to be the one screen in the application with no idea what
    the reader had selected: a dashboard filtered to one company, and a question
    asked beside it answered for all of them. What arrives here is what every
    report page already sends, under the same names, so there is no second
    vocabulary for the same bar.

    Nothing here can widen an answer. Each value becomes an ordinary filter and
    passes the same permission gate a question's own entities do, so a crafted
    request narrows a report exactly as a crafted query string does — and never
    reaches a row the caller could not already read.
    """

    model_config = ConfigDict(extra="ignore")

    company_code: str | None = None
    bu_code: str | None = None
    sales_line_code: str | None = None
    zone_code: str | None = None
    region_code: str | None = None
    area_code: str | None = None
    unit_code: str | None = None
    territory_code: str | None = None
    sub_territory_code: str | None = None
    customer_code: str | None = None
    sales_force_code: str | None = None
    material_code: str | None = None
    #: The bar's period, used only when the question names none of its own.
    period: str | None = None
    date_from: dt.date | None = None
    date_to: dt.date | None = None


#: Bar parameter -> the entity type it names. Mirrors the names
#: ``routes_dashboard.scope_filters`` accepts, which is the contract the browser
#: already speaks; `bu_code` is why this cannot simply be derived from
#: ``EntityType``.
CHAT_CONTEXT_LEVELS: dict[str, EntityType] = {
    "company_code": EntityType.COMPANY,
    "bu_code": EntityType.BUSINESS_UNIT,
    "sales_line_code": EntityType.SALES_LINE,
    "zone_code": EntityType.ZONE,
    "region_code": EntityType.REGION,
    "area_code": EntityType.AREA,
    "unit_code": EntityType.UNIT,
    "territory_code": EntityType.TERRITORY,
    "sub_territory_code": EntityType.SUB_TERRITORY,
    "customer_code": EntityType.CUSTOMER,
    "sales_force_code": EntityType.SALES_FORCE,
    "material_code": EntityType.MATERIAL,
}


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None
    #: What the reader has selected on screen. Optional: a client that sends
    #: nothing behaves exactly as it did before this existed.
    context: ChatContext | None = None


class ChatResponse(BaseModel):
    conversation_id: str
    #: The stored assistant turn this answer became.
    #:
    #: ``agent._persist`` has always returned it; until feedback existed nothing
    #: asked for it, so it never reached the caller and a live answer could not
    #: be referred back to. It is the same identifier
    #: ``conversation_history`` already exposes for a replayed turn, so a client
    #: names a message the same way whether it just arrived or was loaded from
    #: history. Optional because an answer that was never persisted has none.
    message_id: int | None = None
    intent: Intent
    answer: str
    data: dict[str, Any] = Field(default_factory=dict)
    filters: dict[str, Any] = Field(default_factory=dict)
    date_range: dict[str, Any] = Field(default_factory=dict)
    sources: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    chart: ChartSpec | None = None
    needs_clarification: bool = False
    error_code: str | None = None
    language: str = "en"
    tools_used: list[str] = Field(default_factory=list)
    elapsed_ms: int = 0


class FeedbackRequest(BaseModel):
    """A reader's verdict on one answer, and optionally what they expected.

    ``extra="forbid"`` like every other input schema here: a client that sends a
    field this does not declare is refused rather than quietly ignored.
    """

    model_config = ConfigDict(extra="forbid")

    message_id: int
    rating: Literal["UP", "DOWN"]
    #: Free text, and the only free text a reader can put into the learning
    #: tables. Bounded here so an unbounded body cannot reach the database, and
    #: sanitised in ``ai.feedback`` before it is stored.
    expected: str | None = Field(default=None, max_length=2000)


class FeedbackResponse(BaseModel):
    """What was recorded, so the client can render the current state."""

    message_id: int
    rating: str
    #: True when this replaced an earlier verdict from the same reader.
    updated: bool = False
    #: Set when the ``expected`` text carried injection-style instructions and
    #: was stripped. Surfaced so the client can say the text was edited rather
    #: than silently storing something different from what was typed.
    sanitized: bool = False


class ExportRequest(BaseModel):
    """Export of an already-validated report — never a fresh unrestricted query."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["xlsx", "csv", "pdf"]
    title: str = "Report"
    tool: str | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)
    conversation_id: str | None = None
    message_id: int | None = None
    #: Provenance shown at the top of the exported file.
    report_name: str | None = None
    date_range: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    #: ``[["Net Sales", "৳1.87 Cr"], ...]`` — already formatted by the backend.
    kpis: list[list[str]] = Field(default_factory=list)


__all__ = [
    "Intent",
    "DateRangeType",
    "GroupBy",
    "EntityType",
    "ORG_ENTITY_TYPES",
    "ResolvedEntity",
    "ResolvedDateRange",
    "SortSpec",
    "StructuredQuery",
    "ScopeFilters",
    "BaseToolInput",
    "SalesMeasureToolInput",
    "GroupedToolInput",
    "TrendToolInput",
    "GrowthToolInput",
    "StockToolInput",
    "AchievementToolInput",
    "RootCauseToolInput",
    "BusinessSummaryToolInput",
    "AlertToolInput",
    "ChartSpec",
    "ToolResult",
    "ChatRequest",
    "ChatResponse",
    "FeedbackRequest",
    "FeedbackResponse",
    "ExportRequest",
    "MAX_LIMIT",
    "DEFAULT_LIMIT",
    "DEFAULT_TABLE_LIMIT",
]
