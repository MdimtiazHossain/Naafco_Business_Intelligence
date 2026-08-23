"""The agent's tool surface — the only way it can reach business data.

Every tool has a Pydantic input schema (no free text, no SQL), runs the user's
permission scope through :meth:`PermissionFilter.enforce` **before** querying,
and returns a uniform :class:`ToolResult`.

The registry at the bottom is what gets advertised to the LLM as OpenAI tool
definitions. A tool the user's role cannot use is never advertised and, if it
were called anyway, would still be blocked by ``enforce``.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Iterable, Sequence

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..etl.transforms import achievement_percent, growth_percent
from ..etl.validation import safe_divide
from . import queries as q
from .exceptions import NoDataError, ToolExecutionError
from .permission_filter import PermissionFilter, UserContext
from .schemas import (
    AchievementToolInput,
    AlertToolInput,
    BaseToolInput,
    BusinessSummaryToolInput,
    ChartSpec,
    GroupBy,
    GroupedToolInput,
    GrowthToolInput,
    Intent,
    RootCauseToolInput,
    ScopeFilters,
    StockToolInput,
    ToolResult,
    TrendToolInput,
    VolumeToolInput,
)

logger = logging.getLogger("app.ai.tools")


@dataclass
class ToolContext:
    """Everything a tool needs, and nothing it doesn't."""

    session: Session
    permissions: PermissionFilter
    user: UserContext
    today: dt.date = field(default_factory=dt.date.today)

    def scoped(self, filters: ScopeFilters) -> ScopeFilters:
        """Apply the user's data scope. Called first by every tool."""
        return self.permissions.enforce(filters)


ToolHandler = Callable[[ToolContext, Any], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    intents: tuple[Intent, ...] = ()

    def openai_schema(self) -> dict[str, Any]:
        """The OpenAI tool-calling definition for this tool."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            },
        }


REGISTRY: dict[str, ToolSpec] = {}


def register(name: str, description: str, input_model: type[BaseModel],
             intents: Sequence[Intent] = ()) -> Callable[[ToolHandler], ToolHandler]:
    def decorator(handler: ToolHandler) -> ToolHandler:
        REGISTRY[name] = ToolSpec(name, description, input_model, handler, tuple(intents))
        return handler
    return decorator


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _base(tool: str, arguments: BaseToolInput, filters: ScopeFilters,
          metric: str | None = None) -> ToolResult:
    return ToolResult(
        tool=tool,
        metric=metric,
        date_from=arguments.date_from,
        date_to=arguments.date_to,
        filters=_describe(filters),
    )


def _describe(filters: ScopeFilters) -> dict[str, Any]:
    """Only the filters actually applied, for the answer's assumptions block."""
    described: dict[str, Any] = {}
    for name in filters.model_fields:
        value = getattr(filters, name)
        if value:
            described[name] = value
    return described


def _note_unsupported(result: ToolResult, session: Session, view_name: str,
                      filters: ScopeFilters) -> None:
    missing = q.unsupported_filters(q.view(session, view_name), filters)
    if missing:
        result.notes.append(
            "This report is not held at "
            + ", ".join(m.replace("_code", "").replace("_", " ") for m in missing)
            + " level, so that filter could not be applied."
        )


def _empty(result: ToolResult) -> ToolResult:
    """No rows is a legitimate answer, not an error — but it is flagged."""
    result.notes.append("No data found for the selected period and filters.")
    return result


def _empty_stock(result: ToolResult) -> ToolResult:
    """The same, without the period.

    A stock position has no posting date and the page says so; telling a reader
    their *period* returned nothing would point them at the one control that
    cannot have caused it.
    """
    result.notes.append("No stock data found for the selected filters.")
    return result


def _percent(value: Any) -> float | None:
    return None if value is None else float(value)


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------


def _volume_result(ctx: ToolContext, tool: str, view_name: str, noun: str,
                   arguments: "VolumeToolInput") -> ToolResult:
    """Volume for one dataset: the total its source file stated.

    One number, because there is one number to give. Each transaction line
    carries its own Total Volume and no unit of measure, so the answer is the
    sum of the lines in scope — the same arithmetic as net sales. Lines the
    file gave no volume for are excluded and counted, never read as zero.
    """
    filters = ctx.scoped(arguments.filters)
    result = _base(tool, arguments, filters, "volume")

    rows = q.volume_by_group(ctx.session, view_name, filters,
                             arguments.date_from, arguments.date_to,
                             group_by=arguments.group_by, limit=arguments.limit)
    totals = q.volume_total(ctx.session, view_name, filters,
                            arguments.date_from, arguments.date_to)

    if totals["value"] is None:
        return _empty(result)

    result.rows = rows
    result.row_count = len(rows)
    result.value = totals["value"]
    result.values = {"volume": totals["value"]}
    result.sources = [view_name]
    result.facts.append(
        f"{noun} volume for {arguments.date_from} to {arguments.date_to} "
        f"was {totals['value']:,.2f}."
    )

    if totals["unmeasured_lines"]:
        result.notes.append(
            f"{totals['unmeasured_lines']:,} line(s) carry no Total Volume in "
            "the source data and are not included in this figure. Nothing is "
            "estimated for them."
        )
    return result


@register("get_sales_volume",
          "Sales volume for a period — the total volume the sales data records "
          "for the lines in scope. Optionally grouped by region, territory, "
          "material, customer and so on.",
          VolumeToolInput, [Intent.SALES_VOLUME])
def get_sales_volume(ctx: ToolContext, arguments: VolumeToolInput) -> ToolResult:
    return _volume_result(ctx, "get_sales_volume", q.SALES_VIEW, "Sales",
                          arguments)


# No ``get_stock_volume``. The old stock fact carried a volume column beside its
# quantities; material stock is stated in four quantities and nothing else, so
# there is no separate volume to report. ``get_stock_summary`` is the stock
# figure now.


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------


@register("get_sales_summary",
          "Total sales for a period: quantity, gross, discount, net sales, cost, "
          "gross profit, average selling price and gross margin %.",
          BaseToolInput, [Intent.SALES_SUMMARY])
def get_sales_summary(ctx: ToolContext, arguments: BaseToolInput) -> ToolResult:
    filters = ctx.scoped(arguments.filters)
    totals = q.aggregate_totals(ctx.session, q.SALES_MEASURES, filters,
                                arguments.date_from, arguments.date_to)
    result = _base("get_sales_summary", arguments, filters, "net_sales")
    _note_unsupported(result, ctx.session, q.SALES_VIEW, filters)

    if not totals.get("transaction_count"):
        return _empty(result)

    result.value = totals.get("net_sales")
    result.values = {**totals, **q.sales_kpis(totals)}
    result.row_count = int(totals.get("transaction_count") or 0)
    result.sources = [q.SALES_VIEW]
    result.facts.append(
        f"Net sales for {arguments.date_from} to {arguments.date_to} were "
        f"{result.value:,.0f} BDT across {result.row_count:,} transactions."
    )
    return result


@register("get_sales_detail",
          "Sales broken down by a dimension such as region, territory, customer, "
          "material, material brand, material group or sales force.",
          GroupedToolInput, [Intent.SALES_DETAIL])
def get_sales_detail(ctx: ToolContext, arguments: GroupedToolInput) -> ToolResult:
    return _grouped_sales("get_sales_detail", ctx, arguments)


def _grouped_sales(tool: str, ctx: ToolContext, arguments: GroupedToolInput,
                   group_by: GroupBy | None = None) -> ToolResult:
    filters = ctx.scoped(arguments.filters)
    group = group_by or arguments.group_by
    result = _base(tool, arguments, filters, "net_sales")
    _note_unsupported(result, ctx.session, q.SALES_VIEW, filters)

    try:
        rows, truncated = q.aggregate_by(
            ctx.session, q.SALES_MEASURES, filters, arguments.date_from,
            arguments.date_to, group, limit=arguments.limit,
            direction=arguments.sort_direction,
        )
    except ValueError as exc:
        raise ToolExecutionError(str(exc), user_message=(
            f"Sales cannot be broken down by {group.value.replace('_', ' ')}."
        )) from exc

    if not rows:
        return _empty(result)

    for row in rows:
        row.update(q.sales_kpis(row))
    # Quantity, volume and net sales are the three transactional measures, so
    # every grouped sales report carries all three — a region table answering
    # "how much moved" in only one of them is half an answer. Volume needs no
    # second query any more: it is the Total Volume the file stated per line,
    # so ``aggregate_by`` sums it alongside the other two.

    result.rows = rows
    result.row_count = len(rows)
    result.truncated = truncated
    result.values = {"group_by": group.value}
    result.sources = [q.SALES_VIEW]
    result.chart = ChartSpec(type="bar", x_axis="label", y_axis="net_sales", data=rows)
    top = rows[0]
    result.facts.append(
        f"Highest {group.value.replace('_', ' ')} by net sales: "
        f"{top.get('label') or top.get('code')} at {top.get('net_sales', 0):,.0f} BDT."
    )
    return result


@register("get_sales_trend", "Daily or monthly sales over a period, for trend charts.",
          TrendToolInput, [Intent.SALES_TREND])
def get_sales_trend(ctx: ToolContext, arguments: TrendToolInput) -> ToolResult:
    filters = ctx.scoped(arguments.filters)
    rows = q.time_series(ctx.session, q.SALES_MEASURES, filters, arguments.date_from,
                         arguments.date_to, arguments.granularity, arguments.limit)
    result = _base("get_sales_trend", arguments, filters, "net_sales")
    if not rows:
        return _empty(result)
    result.rows = rows
    result.row_count = len(rows)
    result.sources = [q.SALES_VIEW]
    axis = "date" if arguments.granularity == "day" else "label"
    result.chart = ChartSpec(type="line", x_axis=axis, y_axis="net_sales", data=rows)

    first, last = rows[0].get("net_sales") or 0, rows[-1].get("net_sales") or 0
    result.facts.append(
        f"Net sales moved from {first:,.0f} to {last:,.0f} BDT across "
        f"{len(rows)} {arguments.granularity}s."
    )
    change = growth_percent(last, first)
    if change is not None:
        result.interpretations.append(
            f"That is a {float(change):+.1f}% change between the first and last "
            f"{arguments.granularity} of the period; it describes the endpoints, not "
            "the trend of every point in between."
        )
    return result


@register("get_sales_growth",
          "Sales for a period compared with a previous period: change and growth %.",
          GrowthToolInput, [Intent.SALES_GROWTH])
def get_sales_growth(ctx: ToolContext, arguments: GrowthToolInput) -> ToolResult:
    return _growth("get_sales_growth", ctx, arguments, q.SALES_MEASURES, "net_sales",
                   q.SALES_VIEW)


def _growth(tool: str, ctx: ToolContext, arguments: GrowthToolInput,
            measures: q.MeasureSet, measure: str, view_name: str) -> ToolResult:
    filters = ctx.scoped(arguments.filters)
    current = q.aggregate_totals(ctx.session, measures, filters, arguments.date_from,
                                 arguments.date_to)
    previous = q.aggregate_totals(ctx.session, measures, filters, arguments.compare_from,
                                  arguments.compare_to)
    comparison = q.compare_totals(current, previous, measure)

    result = _base(tool, arguments, filters, measure)
    result.values = {
        **comparison,
        "current_period": f"{arguments.date_from} to {arguments.date_to}",
        "previous_period": f"{arguments.compare_from} to {arguments.compare_to}",
        "current_totals": current,
        "previous_totals": previous,
    }
    result.value = comparison["current"]
    result.sources = [view_name]

    if not current.get(measure) and not previous.get(measure):
        return _empty(result)

    growth = comparison["growth_percent"]
    result.facts.append(
        f"{measure.replace('_', ' ').title()}: {comparison['current']:,.0f} BDT this "
        f"period vs {comparison['previous']:,.0f} BDT in the comparison period "
        f"(change {comparison['change']:+,.0f} BDT)."
    )
    if growth is None:
        result.notes.append(
            "Growth % cannot be calculated because the comparison period is zero."
        )
    else:
        result.facts.append(f"That is {growth:+.1f}%.")
    return result


@register("get_sales_target", "Sales target for a period, with actual sales alongside.",
          AchievementToolInput, [Intent.SALES_TARGET, Intent.TARGET_SUMMARY])
def get_sales_target(ctx: ToolContext, arguments: AchievementToolInput) -> ToolResult:
    return _target_view("get_sales_target", ctx, arguments)


@register("get_sales_achievement",
          "Achievement % against target, optionally grouped and filtered to those "
          "below a threshold.",
          AchievementToolInput,
          [Intent.SALES_ACHIEVEMENT, Intent.TARGET_ACHIEVEMENT])
def get_sales_achievement(ctx: ToolContext, arguments: AchievementToolInput) -> ToolResult:
    return _target_view("get_sales_achievement", ctx, arguments)


def _target_view(tool: str, ctx: ToolContext, arguments: AchievementToolInput,
                 gap_only: bool = False) -> ToolResult:
    filters = ctx.scoped(arguments.filters)
    rows, totals = q.target_vs_actual(ctx.session, filters, arguments.date_from,
                                      arguments.date_to, arguments.group_by,
                                      arguments.limit)
    result = _base(tool, arguments, filters, "achievement_percent")
    result.sources = [q.TARGET_VIEW, q.SALES_VIEW]
    result.values = totals

    if arguments.below_percent is not None:
        rows = [r for r in rows
                if r["achievement_percent"] is not None
                and r["achievement_percent"] < arguments.below_percent]
        result.values["below_percent"] = arguments.below_percent
    if gap_only:
        rows = [r for r in rows if r["gap"] > 0]

    if not rows and not totals["target"] and not totals["actual"]:
        return _empty(result)

    result.rows = rows
    result.row_count = len(rows)
    result.value = totals["achievement_percent"]
    result.chart = ChartSpec(type="bar", x_axis="label", y_axis="achievement_percent",
                             data=rows)
    result.facts.append(
        f"Target {totals['target']:,.0f} BDT, actual {totals['actual']:,.0f} BDT, "
        f"gap {totals['gap']:,.0f} BDT."
    )
    if totals["achievement_percent"] is None:
        result.notes.append(
            "Achievement % cannot be calculated because no target is loaded for this "
            "period and scope."
        )
    else:
        result.facts.append(f"Overall achievement is {totals['achievement_percent']:.1f}%.")
    return result


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------


#: The note every stock answer carries.
#:
#: Stock is a position, not a total over a period, and the caller always supplies
#: a date range because every tool input requires one. Saying so once, plainly,
#: is what stops "stock in July" being read as a July figure.
_SNAPSHOT_NOTE = (
    "Stock is the current position as last uploaded. The source carries no "
    "posting date, so the requested date range does not apply to it."
)


@register("get_stock_summary",
          "Current material stock: unrestricted, quality inspection, blocked and "
          "in-transit quantities, with the total across all four.",
          StockToolInput, [Intent.STOCK_SUMMARY])
def get_stock_summary(ctx: ToolContext, arguments: StockToolInput) -> ToolResult:
    filters = ctx.scoped(arguments.filters)
    totals = q.material_stock_totals(ctx.session, filters, today=ctx.today,
                                     soon_days=_stock_horizon(arguments))
    result = _base("get_stock_summary", arguments, filters, "total_stock")
    result.sources = [q.MATERIAL_STOCK_VIEW]
    _note_unsupported(result, ctx.session, q.MATERIAL_STOCK_VIEW, filters)
    if not totals or not totals.get("row_count"):
        return _empty_stock(result)

    result.value = totals.get("total_stock")
    result.values = totals
    result.notes.append(_SNAPSHOT_NOTE)
    # The unit opens the sentence, attached to the measure it qualifies, and no
    # figure in the list repeats it. The four categories stay four numbers here
    # as everywhere else — only unrestricted stock can be sold.
    result.facts.append(
        f"{_stock_measure('Material stock')} — "
        f"unrestricted {_stock(totals.get('unrestricted_stock'))}, "
        f"quality inspection {_stock(totals.get('quality_inspection_stock'))}, "
        f"blocked {_stock(totals.get('blocked_stock'))}, "
        f"in transit {_stock(totals.get('stock_in_transit'))}, "
        f"total {_stock(totals.get('total_stock'))}."
    )
    return result


def _stock(value: Any) -> str:
    """A stock figure in a sentence: ``1,200``, with no unit on it.

    Every fact a stock tool states goes through here. The unit is named once by
    the sentence's label — see :func:`_stock_measure`, which spells it out of
    :data:`app.ai.queries.STOCK_UNIT` — and never trails the number, so a figure
    reads the same in prose as it does on a card or in a column.

    The number itself is untouched. Material stock is reported in KG/LTR by
    convention, not by conversion, because the source carries no unit to convert
    from; and a decimal the upload stated is kept, since rounding here would
    report a figure the plant never held.
    """
    number = float(value or 0)
    return f"{number:,.0f}" if number == int(number) else f"{number:,.2f}"


def _stock_measure(name: str) -> str:
    """``unrestricted stock`` -> ``unrestricted stock (KG/LTR)``.

    The counterpart of :func:`_stock`: the unit goes on the name of the measure
    and nowhere else. Each stock sentence names it exactly once, at the point
    where it says what is being counted.
    """
    return f"{name} ({q.STOCK_UNIT})"


#: How each rankable measure reads in a sentence.
_STOCK_MEASURE_NOUN = {
    "total_stock": "total stock",
    "unrestricted_stock": "unrestricted stock",
    "quality_inspection_stock": "quality inspection stock",
    "blocked_stock": "blocked stock",
    "stock_in_transit": "stock in transit",
}


def _stock_horizon(arguments: "StockToolInput") -> int:
    """The "expiring soon" window: the caller's, else the configured default.

    Read in one place because it decides two things that must agree — which
    positions the expiry cards call EXPIRING_SOON, and which rows an
    ``EXPIRING_SOON`` status filter keeps.
    """
    return arguments.expiring_within_days or get_settings().stock_expiring_soon_days


def _stock_by(tool: str, ctx: ToolContext, arguments: "StockToolInput",
              group: GroupBy, noun: str) -> ToolResult:
    """Stock grouped by one of the four dimensions the master provides.

    Every row carries all four categories whatever the ranking, because they
    answer different questions and a stock report states all four. ``sort_by``
    decides only the order and the headline sentence.
    """
    filters = ctx.scoped(arguments.filters)
    measure = arguments.sort_by
    rows, truncated = q.material_stock_by(ctx.session, filters, group,
                                          limit=arguments.limit,
                                          sort_field=measure,
                                          today=ctx.today,
                                          soon_days=_stock_horizon(arguments))
    result = _base(tool, arguments, filters, measure)
    result.sources = [q.MATERIAL_STOCK_VIEW]
    _note_unsupported(result, ctx.session, q.MATERIAL_STOCK_VIEW, filters)
    if not rows:
        return _empty_stock(result)
    result.rows = rows
    result.row_count = len(rows)
    result.truncated = truncated
    result.notes.append(_SNAPSHOT_NOTE)
    result.chart = ChartSpec(type="bar", x_axis="label", y_axis=measure,
                             data=rows)
    top = rows[0]
    result.facts.append(
        f"Highest {noun} by {_stock_measure(_STOCK_MEASURE_NOUN[measure])}: "
        f"{top.get('label') or top.get('code')} "
        f"at {_stock(top.get(measure))}."
    )
    return result


@register("get_stock_by_plant", "Material stock totalled by plant.",
          StockToolInput, [Intent.STOCK_BY_PLANT])
def get_stock_by_plant(ctx: ToolContext, arguments: StockToolInput) -> ToolResult:
    return _stock_by("get_stock_by_plant", ctx, arguments, GroupBy.PLANT, "plant")


@register("get_stock_by_storage_location",
          "Material stock totalled by storage location.",
          StockToolInput, [Intent.STOCK_BY_STORAGE_LOCATION])
def get_stock_by_storage_location(ctx: ToolContext,
                                  arguments: StockToolInput) -> ToolResult:
    return _stock_by("get_stock_by_storage_location", ctx, arguments,
                     GroupBy.STORAGE_LOCATION, "storage location")


@register("get_stock_by_material",
          "Material stock totalled by material code — the finest grain a stock "
          "position has. Answers 'which material has the most unrestricted "
          "stock', 'stock for material X', 'blocked stock by material'.",
          StockToolInput, [Intent.STOCK_BY_MATERIAL])
def get_stock_by_material(ctx: ToolContext, arguments: StockToolInput) -> ToolResult:
    return _stock_by("get_stock_by_material", ctx, arguments, GroupBy.MATERIAL,
                     "material")


@register("get_stock_by_material_group",
          "Material stock totalled by material group.",
          StockToolInput, [Intent.STOCK_BY_MATERIAL_GROUP])
def get_stock_by_material_group(ctx: ToolContext,
                                arguments: StockToolInput) -> ToolResult:
    return _stock_by("get_stock_by_material_group", ctx, arguments,
                     GroupBy.MATERIAL_GROUP, "material group")


@register("get_stock_by_material_brand",
          "Material stock totalled by material brand — the same brand a sales "
          "report ranks by, read from the same Material Master.",
          StockToolInput, [Intent.STOCK_BY_MATERIAL_BRAND])
def get_stock_by_material_brand(ctx: ToolContext,
                                arguments: StockToolInput) -> ToolResult:
    return _stock_by("get_stock_by_material_brand", ctx, arguments,
                     GroupBy.MATERIAL_BRAND, "material brand")


@register("get_stock_expiry",
          "Stock by shelf life: expired, expiring soon, valid, or with no expiry "
          "date recorded.",
          StockToolInput, [Intent.STOCK_EXPIRY])
def get_stock_expiry(ctx: ToolContext, arguments: StockToolInput) -> ToolResult:
    filters = ctx.scoped(arguments.filters)
    soon_days = _stock_horizon(arguments)
    rows = q.expiry_buckets(ctx.session, filters, today=ctx.today, soon_days=soon_days)
    result = _base("get_stock_expiry", arguments, filters, "total_stock")
    result.sources = [q.MATERIAL_STOCK_VIEW]
    _note_unsupported(result, ctx.session, q.MATERIAL_STOCK_VIEW, filters)
    if not any(row.get("row_count") for row in rows):
        return _empty_stock(result)

    by_bucket = {str(row["code"]): row for row in rows}
    result.rows = rows
    result.row_count = len(rows)
    result.values = {
        "expiring_within_days": soon_days,
        **{code.lower(): float(row.get("total_stock") or 0)
           for code, row in by_bucket.items()},
    }
    result.notes.append(_SNAPSHOT_NOTE)
    result.notes.append(
        f"'Expiring soon' means within {soon_days} days of {ctx.today}. Stock with "
        "no shelf-life date is reported separately rather than counted as valid."
    )
    expired = float(by_bucket.get("EXPIRED", {}).get("total_stock") or 0)
    soon = float(by_bucket.get("EXPIRING_SOON", {}).get("total_stock") or 0)
    result.facts.append(
        f"{_stock_measure('Stock')} — {_stock(expired)} expired and "
        f"{_stock(soon)} expiring within {soon_days} days."
    )
    result.chart = ChartSpec(type="bar", x_axis="code", y_axis="total_stock",
                             data=rows)
    return result


@register("get_expiring_stock",
          "Individual stock positions at or past their shelf life, soonest first.",
          StockToolInput, [Intent.EXPIRING_STOCK])
def get_expiring_stock(ctx: ToolContext, arguments: StockToolInput) -> ToolResult:
    filters = ctx.scoped(arguments.filters)
    soon_days = _stock_horizon(arguments)
    rows, truncated = q.expiring_rows(ctx.session, filters, today=ctx.today,
                                      soon_days=soon_days, limit=arguments.limit)
    result = _base("get_expiring_stock", arguments, filters, "total_stock")
    result.sources = [q.MATERIAL_STOCK_VIEW]
    _note_unsupported(result, ctx.session, q.MATERIAL_STOCK_VIEW, filters)
    result.values = {"expiring_within_days": soon_days}
    if not rows:
        result.notes.append(
            f"Nothing expires within {soon_days} days for this scope."
        )
        return result
    result.rows = rows
    result.row_count = len(rows)
    result.truncated = truncated
    result.notes.append(_SNAPSHOT_NOTE)
    result.facts.append(
        f"{len(rows)} position(s) expire within {soon_days} days or already have."
    )
    return result


# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------


@register("get_target_summary", "Total target for a period.",
          BaseToolInput, [Intent.TARGET_SUMMARY])
def get_target_summary(ctx: ToolContext, arguments: BaseToolInput) -> ToolResult:
    filters = ctx.scoped(arguments.filters)
    totals = q.aggregate_totals(ctx.session, q.TARGET_MEASURES, filters,
                                arguments.date_from, arguments.date_to)
    result = _base("get_target_summary", arguments, filters, "target_amount")
    if not totals.get("target_count"):
        return _empty(result)
    result.value = totals.get("target_amount")
    result.values = totals
    # The volume target comes back from ``aggregate_totals`` with the rest since
    # revision 0022 — it is a unit-free number like every other measure, so there
    # is no second, unit-partitioned query to run for it.
    volume = q.volume_total(ctx.session, q.TARGET_VIEW, filters,
                            arguments.date_from, arguments.date_to,
                            volume_column="target_volume")
    if volume["unmeasured_lines"]:
        result.notes.append(
            f"{volume['unmeasured_lines']:,} target(s) in scope set no volume. "
            "They are excluded from the volume figure rather than counted as "
            "zero — an unset target is not a target of zero."
        )
    result.row_count = int(totals.get("target_count") or 0)
    result.sources = [q.TARGET_VIEW]
    result.facts.append(f"Target for the period is {result.value:,.0f} BDT.")
    return result


@register("get_target_achievement",
          "Target vs actual by dimension, with achievement % and gap. Use "
          "below_percent to list only under-performers.",
          AchievementToolInput, [Intent.TARGET_ACHIEVEMENT])
def get_target_achievement(ctx: ToolContext, arguments: AchievementToolInput) -> ToolResult:
    return _target_view("get_target_achievement", ctx, arguments)


@register("get_target_gap", "Groups that are short of target, with the gap amount.",
          AchievementToolInput, [Intent.TARGET_GAP])
def get_target_gap(ctx: ToolContext, arguments: AchievementToolInput) -> ToolResult:
    result = _target_view("get_target_gap", ctx, arguments, gap_only=True)
    result.rows.sort(key=lambda r: r["gap"], reverse=True)
    return result


# ---------------------------------------------------------------------------
# Performance (one implementation, many dimensions)
# ---------------------------------------------------------------------------


def _performance_tool(name: str, group: GroupBy, description: str,
                      intent: Intent) -> None:
    @register(name, description, GroupedToolInput, [intent])
    def _handler(ctx: ToolContext, arguments: GroupedToolInput,
                 _group: GroupBy = group, _name: str = name) -> ToolResult:
        return _grouped_sales(_name, ctx, arguments, group_by=_group)


_performance_tool("get_region_performance", GroupBy.REGION,
                  "Sales performance by region.", Intent.REGION_PERFORMANCE)
_performance_tool("get_zone_performance", GroupBy.ZONE,
                  "Sales performance by zone.", Intent.ZONE_PERFORMANCE)
_performance_tool("get_area_performance", GroupBy.AREA,
                  "Sales performance by area.", Intent.AREA_PERFORMANCE)
_performance_tool("get_unit_performance", GroupBy.UNIT,
                  "Sales performance by unit.", Intent.UNIT_PERFORMANCE)
_performance_tool("get_territory_performance", GroupBy.TERRITORY,
                  "Sales performance by territory.", Intent.TERRITORY_PERFORMANCE)
_performance_tool("get_sub_territory_performance", GroupBy.SUB_TERRITORY,
                  "Sales performance by sub-territory.", Intent.SUB_TERRITORY_PERFORMANCE)
_performance_tool("get_material_performance", GroupBy.MATERIAL,
                  "Sales performance by individual material. Use this only when "
                  "the question explicitly asks about material codes, item codes "
                  "or product codes — general 'top products' questions are "
                  "answered by get_material_brand_performance.",
                  Intent.MATERIAL_PERFORMANCE)
_performance_tool("get_material_group_performance", GroupBy.MATERIAL_GROUP,
                  "Sales performance by material group — the Material Master's "
                  "top classification level.",
                  Intent.MATERIAL_GROUP_PERFORMANCE)
_performance_tool("get_customer_performance", GroupBy.CUSTOMER,
                  "Sales performance by customer.", Intent.CUSTOMER_PERFORMANCE)
_performance_tool("get_salesforce_performance", GroupBy.SALES_FORCE,
                  "Sales performance by sales officer / sales force code.",
                  Intent.SALES_FORCE_PERFORMANCE)


#: What a brand ranking reports. Deliberately the three transactional measures
#: and nothing else — a brand league table answers "how much moved and what it
#: was worth", and margin or invoice counts only crowd that out.
BRAND_ROW_FIELDS: tuple[str, ...] = ("rank", "code", "label", "quantity", "volume",
                                     "net_sales")


@register("get_material_brand_performance",
          "Ranked brand-wise sales performance: quantity, volume and net sales "
          "per material brand, ranked by net sales. This is the tool for general "
          "'top products', 'best performing products', 'brand performance' and "
          "'product performance' questions — brand is the level this business "
          "ranks at. Use get_material_performance instead only when individual "
          "material or item codes are asked for by name.",
          GroupedToolInput, [Intent.MATERIAL_BRAND_PERFORMANCE])
def get_material_brand_performance(ctx: ToolContext,
                                   arguments: GroupedToolInput) -> ToolResult:
    """Brand-wise performance, derived through the Material Master.

    Brand is not a dimension table and there is no Brand Master: it is an
    attribute of ``dim_material`` that the sales detail view already carries via
    its material join, so every transaction reaches its brand by the one mapping
    the Material Master defines. Stock reaches its brand the same way through the
    same master, which is what makes "top brands" and "stock by brand" two
    questions about one set of goods.

    A line whose material carries no brand is grouped as "(not assigned at this
    level)" by the shared aggregation rather than dropped — the sale happened and
    its net sales figure belongs in the total.
    """
    # The grouped-sales path already aggregates and attaches volume. A brand
    # ranking is that report plus an explicit rank.
    result = _grouped_sales("get_material_brand_performance", ctx, arguments,
                            group_by=GroupBy.MATERIAL_BRAND)
    if not result.rows:
        return result

    for position, row in enumerate(result.rows, start=1):
        row["rank"] = position
    result.values["top_brand"] = result.rows[0]
    return result


#: The brand league table with its targets beside it, in reading order.
#: ``volume`` is the shipped figure and ``target_volume`` the planned one; the
#: two are deliberately never added or compared as a single "volume".
BRAND_TARGET_ROW_FIELDS: tuple[str, ...] = (
    "rank", "code", "label", "target_volume", "volume", "target_amount",
    "net_sales", "volume_achievement_percent", "achievement_percent",
    "volume_shortfall", "amount_shortfall",
)


@register("get_material_brand_target_performance",
          "Ranked brand-wise performance with targets beside actuals: target "
          "volume, sales volume, target amount, net sales, the two achievement "
          "percentages and the two shortfalls, per material brand, ranked by net "
          "sales.",
          GroupedToolInput)
def get_material_brand_target_performance(ctx: ToolContext,
                                          arguments: GroupedToolInput) -> ToolResult:
    """The brand ranking, with each brand's target set against what it sold.

    **Each column reads its own source table. They are never crossed.**

    ===============  ====================================================
    Target Vol       ``fact_target.target_volume``  (via vw_target_detail)
    Target BDT       ``fact_target.target_amount``  (via vw_target_detail)
    Sales Vol        ``fact_sales.volume``          (via vw_sales_detail)
    Net Sales        ``fact_sales.net_sales``       (via vw_sales_detail)
    ===============  ====================================================

    Both reach brand the same way and the only way there is: the fact row's
    ``material_id`` to ``dim_material``, whose ``material_brand`` is an attribute
    of the material. There is no Brand Master.

    **The two sides are aggregated independently, then joined on the brand
    code.** Four separate queries — target amount, target volume, sales measures,
    sales volume — each grouping its own view by brand before anything is
    combined. No raw target row ever meets a raw sales row, which is what a
    brand with two targets and three sales lines would otherwise turn into six
    inflated combinations.

    Nor can either side fan out on its own: both views reach brand through one
    join on the row's own material — many facts to one material — so each view
    returns exactly one row per fact row and each measure is summed once.

    Built by composition, not by a second implementation. The ranked sales side
    is :func:`_grouped_sales` — the same call ``get_material_brand_performance``
    makes, so the ranking and the sales figures are identical to the plain brand
    table. The target side reuses the two helpers
    :func:`queries.target_vs_actual` uses: ``aggregate_by`` over
    ``TARGET_MEASURES`` for the amount, and ``volume_by_group`` for the volume.

    Calling ``target_vs_actual`` outright would have been shorter and wrong: it
    re-aggregates sales and re-derives actual volume, which this has already
    done, so the dashboard would run four queries to recompute figures it holds.

    **A target is a month, not a day.** ``fact_target`` carries a target month
    that resolves to the first of that month, so a date window which excludes the
    1st sees the sales and none of the target. The achievement then reports n/a
    rather than a figure computed against a target the window never contained.
    """
    result = _grouped_sales("get_material_brand_target_performance", ctx, arguments,
                            group_by=GroupBy.MATERIAL_BRAND)
    if not result.rows:
        return result

    filters = ctx.scoped(arguments.filters)
    target_rows, _ = q.aggregate_by(
        ctx.session, q.TARGET_MEASURES, filters, arguments.date_from,
        arguments.date_to, GroupBy.MATERIAL_BRAND, limit=q.MAX_ROWS,
        sort_field="target_amount",
    )
    target_volumes = {
        str(row["code"]): row.get("volume")
        for row in q.volume_by_group(
            ctx.session, q.TARGET_VIEW, filters, arguments.date_from,
            arguments.date_to, GroupBy.MATERIAL_BRAND,
            volume_column="target_volume")
    }
    amount_by_code = {str(row["code"]): row.get("target_amount") or 0.0
                      for row in target_rows}

    for position, row in enumerate(result.rows, start=1):
        code = str(row.get("code"))
        row["rank"] = position

        target_amount = amount_by_code.get(code, 0.0)
        target_volume = target_volumes.get(code)
        row["target_amount"] = target_amount
        row["target_volume"] = target_volume

        # Achievement is actual over target, and a target of zero or none makes
        # it unanswerable rather than zero — ``achievement_percent`` returns
        # None, which the UI renders as n/a. That is this project's rule for
        # every ratio and it is not restated here.
        row["achievement_percent"] = _percent(
            achievement_percent(row.get("net_sales"), target_amount))
        row["volume_achievement_percent"] = _percent(
            achievement_percent(row.get("volume"), target_volume))

        # Shortfall is actual *minus* target, so falling short reads negative.
        # Deliberately not ``queries``' ``gap``, which is target minus actual:
        # the two answer the same question with opposite signs, and mixing them
        # would put a positive number against a brand that missed.
        row["amount_shortfall"] = (row.get("net_sales") or 0.0) - target_amount
        row["volume_shortfall"] = (
            None if row.get("volume") is None or target_volume is None
            else float(row["volume"]) - float(target_volume)
        )

    result.values["top_brand"] = result.rows[0]
    result.sources = [q.SALES_VIEW, q.TARGET_VIEW]
    return result


# ---------------------------------------------------------------------------
# Business summary, alerts, root cause
# ---------------------------------------------------------------------------


@register("get_business_summary",
          "One combined management snapshot: sales, target, achievement, stock "
          "alerts, best and worst region, top brand.",
          BusinessSummaryToolInput, [Intent.BUSINESS_SUMMARY])
def get_business_summary(ctx: ToolContext, arguments: BusinessSummaryToolInput) -> ToolResult:
    filters = ctx.scoped(arguments.filters)
    result = _base("get_business_summary", arguments, filters, "net_sales")
    result.sources = [q.SALES_VIEW, q.TARGET_VIEW, q.MATERIAL_STOCK_VIEW]

    sales = q.aggregate_totals(ctx.session, q.SALES_MEASURES, filters,
                               arguments.date_from, arguments.date_to)
    _, target_totals = q.target_vs_actual(ctx.session, filters, arguments.date_from,
                                          arguments.date_to, GroupBy.REGION, limit=200)
    regions, _ = q.aggregate_by(ctx.session, q.SALES_MEASURES, filters,
                                arguments.date_from, arguments.date_to, GroupBy.REGION,
                                limit=200)
    brands, _ = q.aggregate_by(ctx.session, q.SALES_MEASURES, filters,
                               arguments.date_from, arguments.date_to,
                               GroupBy.MATERIAL_BRAND, limit=5)
    # Stock's contribution to the management snapshot is what is actually
    # available and what is at risk of being written off — not a count of
    # materials below a coverage threshold, which a dateless position cannot
    # compute.
    stock = q.material_stock_totals(ctx.session, filters)
    expiry = {
        str(row["code"]): float(row.get("total_stock") or 0)
        for row in q.expiry_buckets(
            ctx.session, filters, today=ctx.today,
            soon_days=get_settings().stock_expiring_soon_days)
    }

    result.value = sales.get("net_sales")
    result.values = {
        "sales": sales.get("net_sales") or 0.0,
        "quantity": sales.get("quantity") or 0.0,
        "target": target_totals["target"],
        "achievement_percent": target_totals["achievement_percent"],
        "gap": target_totals["gap"],
        "unrestricted_stock": stock.get("unrestricted_stock") or 0.0,
        "total_stock": stock.get("total_stock") or 0.0,
        "expired_stock": expiry.get("EXPIRED", 0.0),
        "expiring_soon_stock": expiry.get("EXPIRING_SOON", 0.0),
        "top_region": regions[0] if regions else None,
        "bottom_region": regions[-1] if len(regions) > 1 else None,
        "top_brand": brands[0] if brands else None,
        "average_selling_price": q.sales_kpis(sales)["average_selling_price"],
    }

    # Sales is the only transactional count left in the snapshot, so it alone
    # decides whether there is anything to report.
    if not sales.get("transaction_count"):
        return _empty(result)

    result.facts.append(
        f"Sales {result.values['sales']:,.0f} BDT against a target of "
        f"{result.values['target']:,.0f} BDT."
    )
    result.facts.append(
        f"{_stock_measure('Unrestricted stock')} "
        f"{_stock(result.values['unrestricted_stock'])}, of which "
        f"{_stock(result.values['expired_stock'])} has expired."
    )
    return result


@register("get_business_alerts",
          "Threshold-based alerts: low achievement, expired and near-expiry "
          "stock, and sales decline. Returns severity per alert.",
          AlertToolInput, [Intent.BUSINESS_ALERT])
def get_business_alerts(ctx: ToolContext, arguments: AlertToolInput) -> ToolResult:
    filters = ctx.scoped(arguments.filters)
    result = _base("get_business_alerts", arguments, filters, "alerts")
    result.sources = [q.SALES_VIEW, q.TARGET_VIEW, q.MATERIAL_STOCK_VIEW]
    alerts: list[dict[str, Any]] = []

    rows, _ = q.target_vs_actual(ctx.session, filters, arguments.date_from,
                                 arguments.date_to, GroupBy.REGION, limit=200)
    for row in rows:
        achievement = row["achievement_percent"]
        if achievement is not None and achievement < arguments.achievement_below_percent:
            alerts.append(_alert(
                "LOW_ACHIEVEMENT",
                "CRITICAL" if achievement < arguments.achievement_below_percent * 0.75
                else "HIGH",
                entity=row.get("label") or row["code"], entity_type="region",
                metric="achievement_percent", value=achievement,
                threshold=arguments.achievement_below_percent,
                attention="Review coverage, stock availability and pending orders.",
            ))

    # No HIGH_OVERDUE alert. It measured the overdue share of a receivables
    # portfolio, and receivables left this platform with the Outstanding module
    # in revision 0020 — there is nothing to be overdue.

    # Stock alerts are about shelf life, not replenishment. LOW_STOCK_COVERAGE
    # and OUT_OF_STOCK both needed days of cover, which needs stock and sales to
    # meet on a material code the Material Master does not carry. What the new
    # data *can* raise is stock that has expired or is about to, which is money
    # already at risk rather than a forecast.
    soon_days = get_settings().stock_expiring_soon_days
    expiry = {
        str(row["code"]): row
        for row in q.expiry_buckets(ctx.session, filters, today=ctx.today,
                                    soon_days=soon_days)
    }
    expired = float(expiry.get("EXPIRED", {}).get("total_stock") or 0)
    if expired > 0:
        alerts.append(_alert(
            "EXPIRED_STOCK", "CRITICAL", entity="Material stock",
            entity_type="stock", metric="expired_stock", value=expired,
            threshold=0,
            attention="Quarantine and write off; it cannot be sold.",
        ))
    expiring = float(expiry.get("EXPIRING_SOON", {}).get("total_stock") or 0)
    if expiring > 0:
        alerts.append(_alert(
            "EXPIRING_STOCK", "HIGH", entity="Material stock",
            entity_type="stock", metric="expiring_soon_stock", value=expiring,
            threshold=soon_days,
            attention=f"Move or sell within {soon_days} days.",
        ))

    span = (arguments.date_to - arguments.date_from).days + 1
    previous_to = arguments.date_from - dt.timedelta(days=1)
    previous_from = previous_to - dt.timedelta(days=span - 1)
    current_sales = q.aggregate_totals(ctx.session, q.SALES_MEASURES, filters,
                                       arguments.date_from, arguments.date_to)
    previous_sales = q.aggregate_totals(ctx.session, q.SALES_MEASURES, filters,
                                        previous_from, previous_to)
    change = q.compare_totals(current_sales, previous_sales, "net_sales")
    if (change["growth_percent"] is not None
            and change["growth_percent"] < -arguments.sales_decline_percent):
        alerts.append(_alert(
            "SALES_DECLINE", "HIGH", entity="Total sales", entity_type="company",
            metric="growth_percent", value=change["growth_percent"],
            threshold=-arguments.sales_decline_percent,
            attention="Run a root-cause analysis to locate the contributors.",
        ))

    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    alerts.sort(key=lambda a: severity_order.get(a["severity"], 9))
    result.rows = alerts[:arguments.__dict__.get("limit", 50) or 50]
    result.row_count = len(alerts)
    result.values = {
        "critical": sum(1 for a in alerts if a["severity"] == "CRITICAL"),
        "high": sum(1 for a in alerts if a["severity"] == "HIGH"),
        "thresholds": {
            "achievement_below_percent": arguments.achievement_below_percent,
            "expiring_within_days": soon_days,
            "sales_decline_percent": arguments.sales_decline_percent,
        },
    }
    if not alerts:
        result.notes.append("No alert thresholds were breached for this scope and period.")
    else:
        result.facts.append(f"{len(alerts)} alert(s) breached their thresholds.")
    return result


def _alert(alert_type: str, severity: str, entity: Any, entity_type: str, metric: str,
           value: Any, threshold: Any, attention: str) -> dict[str, Any]:
    return {
        "alert_type": alert_type,
        "severity": severity,
        "entity": entity,
        "entity_type": entity_type,
        "metric": metric,
        "current_value": value,
        "threshold": threshold,
        "recommended_attention": attention,
    }


@register("get_root_cause_analysis",
          "Explains a change between two periods: overall variance plus the regions, "
          "brands and territories that contributed most, and stock constraints. "
          "Returns facts and interpretations separately.",
          RootCauseToolInput, [Intent.ROOT_CAUSE_ANALYSIS])
def get_root_cause_analysis(ctx: ToolContext, arguments: RootCauseToolInput) -> ToolResult:
    filters = ctx.scoped(arguments.filters)
    result = _base("get_root_cause_analysis", arguments, filters, "net_sales")
    result.sources = [q.SALES_VIEW]

    current = q.aggregate_totals(ctx.session, q.SALES_MEASURES, filters,
                                 arguments.date_from, arguments.date_to)
    previous = q.aggregate_totals(ctx.session, q.SALES_MEASURES, filters,
                                  arguments.compare_from, arguments.compare_to)
    overall = q.compare_totals(current, previous, "net_sales")

    if not current.get("transaction_count") and not previous.get("transaction_count"):
        return _empty(result)

    contributors: dict[str, list[dict[str, Any]]] = {}
    # Brand rather than individual material: "which of our lines lost ground" is
    # the question a root-cause answer is asked, and a list of pack sizes buries
    # it.
    for label, group in (("region", GroupBy.REGION),
                         ("brand", GroupBy.MATERIAL_BRAND),
                         ("territory", GroupBy.TERRITORY)):
        contributors[label] = _variance_by(
            ctx, filters, group, arguments, overall["previous"], arguments.top_n
        )

    # No stock contributor. "Sales fell because we were out of stock" needs a
    # stock figure measured over the same period as the sales it explains, and a
    # material stock position carries no posting date — the position is what it
    # is today, not what it was during the quarter that declined.
    result.value = overall["current"]
    result.values = {
        "overall": overall,
        "current_period": f"{arguments.date_from} to {arguments.date_to}",
        "previous_period": f"{arguments.compare_from} to {arguments.compare_to}",
        "contributors": contributors,
    }
    result.rows = contributors["region"]

    direction = "declined" if overall["change"] < 0 else "grew"
    if overall["growth_percent"] is not None:
        result.facts.append(
            f"Net sales {direction} {abs(overall['growth_percent']):.1f}% "
            f"({overall['change']:+,.0f} BDT) versus the comparison period."
        )
    else:
        result.facts.append(
            f"Net sales were {overall['current']:,.0f} BDT; the comparison period had "
            "no sales, so a percentage change cannot be calculated."
        )

    for label, rows in contributors.items():
        for row in rows[:3]:
            if row["change"] == 0:
                continue
            result.facts.append(
                f"{label.title()} {row['label']}: {row['change']:+,.0f} BDT "
                f"({row['contribution_percent']:+.1f}% of the period's base)."
            )

    movers = [r for rows in contributors.values() for r in rows if r["change"] < 0]
    if movers and overall["change"] < 0:
        worst = min(movers, key=lambda r: r["change"])
        result.interpretations.append(
            f"{worst['label']} shows the largest single decline and therefore appears to "
            "be a significant contributor. The data shows the association, not the cause."
        )
    if not result.interpretations:
        result.interpretations.append(
            "No single dimension dominates the variance in this data."
        )
    return result


def _variance_by(ctx: ToolContext, filters: ScopeFilters, group: GroupBy,
                 arguments: RootCauseToolInput, base: float,
                 top_n: int) -> list[dict[str, Any]]:
    """Per-group change between the two periods, largest movers first."""
    try:
        current_rows, _ = q.aggregate_by(ctx.session, q.SALES_MEASURES, filters,
                                         arguments.date_from, arguments.date_to, group,
                                         limit=q.MAX_ROWS)
        previous_rows, _ = q.aggregate_by(ctx.session, q.SALES_MEASURES, filters,
                                          arguments.compare_from, arguments.compare_to,
                                          group, limit=q.MAX_ROWS)
    except ValueError:
        return []

    current = {r["code"]: r for r in current_rows}
    previous = {r["code"]: r for r in previous_rows}
    movers: list[dict[str, Any]] = []
    for code in set(current) | set(previous):
        now = current.get(code, {})
        before = previous.get(code, {})
        current_value = now.get("net_sales") or 0.0
        previous_value = before.get("net_sales") or 0.0
        change = current_value - previous_value
        movers.append({
            "code": code,
            "label": now.get("label") or before.get("label") or code,
            "current": current_value,
            "previous": previous_value,
            "change": change,
            "growth_percent": _percent(growth_percent(current_value, previous_value)),
            "contribution_percent": _percent(
                safe_divide(Decimal(str(change)), Decimal(str(base)), percent=True)
            ) or 0.0,
        })
    movers.sort(key=lambda r: abs(r["change"]), reverse=True)
    return movers[:top_n]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class ToolInvocation:
    """One executed tool call, for observability and the chat response."""

    tool_name: str
    arguments: dict[str, Any]
    result: ToolResult | None = None
    success: bool = True
    error_code: str | None = None
    error_message: str | None = None
    execution_ms: int = 0


def get_tool(name: str) -> ToolSpec:
    spec = REGISTRY.get(name)
    if spec is None:
        raise ToolExecutionError(
            f"unknown tool {name!r}",
            user_message="I don't have a report that answers that question.",
        )
    return spec


def execute_tool(ctx: ToolContext, name: str, arguments: dict[str, Any]) -> ToolInvocation:
    """Validate arguments, run the tool, and time it.

    Business errors (permission, no data) propagate so the orchestrator can phrase
    them; unexpected errors are logged and converted into a generic message that
    exposes nothing about the internals.
    """
    spec = get_tool(name)
    started = time.perf_counter()
    invocation = ToolInvocation(tool_name=name, arguments=dict(arguments))

    try:
        parsed = spec.input_model.model_validate(arguments)
    except ValidationError as exc:
        invocation.success = False
        invocation.error_code = "INVALID_ARGUMENTS"
        invocation.error_message = "The report request was not valid."
        logger.warning("tool %s rejected arguments: %s", name, exc.error_count())
        invocation.execution_ms = int((time.perf_counter() - started) * 1000)
        return invocation

    try:
        invocation.result = spec.handler(ctx, parsed)
    except Exception as exc:  # noqa: BLE001 - classified below, never leaked raw
        invocation.success = False
        from .exceptions import AgentError

        if isinstance(exc, AgentError):
            invocation.error_code = exc.code
            invocation.error_message = exc.user_message
            raise
        logger.exception("tool %s failed", name)
        invocation.error_code = "TOOL_FAILED"
        invocation.error_message = ToolExecutionError.user_message
        raise ToolExecutionError(str(exc)) from exc
    finally:
        invocation.execution_ms = int((time.perf_counter() - started) * 1000)

    if invocation.result is not None:
        invocation.result.row_count = invocation.result.row_count or len(
            invocation.result.rows
        )
    return invocation


def openai_tool_definitions(names: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Tool definitions to advertise to the LLM."""
    selected = list(names) if names else list(REGISTRY)
    return [REGISTRY[name].openai_schema() for name in selected if name in REGISTRY]


def tools_for_intent(intent: Intent) -> list[str]:
    return [name for name, spec in REGISTRY.items() if intent in spec.intents]


__all__ = [
    "ToolContext",
    "ToolSpec",
    "ToolInvocation",
    "REGISTRY",
    "register",
    "get_tool",
    "execute_tool",
    "openai_tool_definitions",
    "tools_for_intent",
]
