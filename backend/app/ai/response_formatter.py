"""Response formatting: Bangladeshi currency, tables and concise answers.

Number rules for Bangladesh:

* ``৳`` prefix, lakh/crore units — ``18,700,000`` renders as ``৳1.87 Cr``
* Indian digit grouping for raw figures — ``1,87,00,000``
* the exact value always stays in ``data`` so nothing is lost to rounding

The formatter builds the whole answer deterministically from validated tool
output. When an LLM is configured it may rewrite the prose, but it is given the
already-formatted figures — it never computes or reformats a number.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .queries import STOCK_UNIT
from .schemas import Intent, ResolvedDateRange, ToolResult

TAKA = "৳"
CRORE = 10_000_000
LAKH = 100_000
THOUSAND = 1_000


def group_indian(value: float | int | Decimal) -> str:
    """Indian digit grouping: ``18700000`` -> ``1,87,00,000``."""
    negative = value < 0
    digits = f"{abs(int(round(float(value)))):d}"
    if len(digits) <= 3:
        grouped = digits
    else:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        grouped = ",".join(parts) + "," + tail
    return f"-{grouped}" if negative else grouped


def format_amount(value: Any, *, compact: bool = True, symbol: str = TAKA) -> str:
    """Money for humans. ``None`` becomes an em dash, never ``0``."""
    if value is None:
        return "—"
    number = float(value)
    if not compact:
        return f"{symbol}{group_indian(number)}"

    magnitude = abs(number)
    if magnitude >= CRORE:
        return f"{symbol}{number / CRORE:,.2f} Cr"
    if magnitude >= LAKH:
        return f"{symbol}{number / LAKH:,.2f} L"
    if magnitude >= THOUSAND:
        return f"{symbol}{number / THOUSAND:,.1f} K"
    return f"{symbol}{number:,.0f}"


def format_quantity(value: Any) -> str:
    if value is None:
        return "—"
    number = float(value)
    if number == int(number):
        return f"{int(number):,}"
    return f"{number:,.2f}"


def stock_label(name: str) -> str:
    """``Unrestricted Stock`` -> ``Unrestricted Stock (KG/LTR)``.

    The unit belongs to the name of a stock measure and not to its value, so
    every heading and every prose label the agent writes is built here. The
    figure beside it stays a plain number — see :func:`format_stock`.
    """
    return f"{name} ({STOCK_UNIT})"


def format_stock(value: Any) -> str:
    """A material stock figure: the uploaded value, and no unit.

    The unit is on the label (:func:`stock_label`), which is why this is not
    simply :func:`format_quantity` under another name — it marks the figures the
    agent must never convert or re-base. There is no factor in the source to
    convert with, so a converted figure would be an invented one.
    """
    if value is None:
        return "—"
    return format_quantity(value)


def format_percent(value: Any, *, signed: bool = False, decimals: int = 1) -> str:
    """``None`` renders as ``n/a`` — a missing ratio is never shown as 0%."""
    if value is None:
        return "n/a"
    number = float(value)
    return f"{number:+.{decimals}f}%" if signed else f"{number:.{decimals}f}%"


def _target_figure(values: Mapping[str, Any], key: str) -> str:
    """A target amount, or ``n/a`` where no target was ever set.

    A target is a sum, and the sum of nothing is 0.0 — so an answer for a period
    and scope with no target row read "**Target:** ৳0" beside an achievement of
    n/a and a note saying no target is loaded. The three disagreed, and the one
    that looked most like a measurement was the wrong one: nobody set a target
    of nothing, and a reader who takes it at face value believes the sales team
    was asked for zero and beat it.

    ``achievement_percent`` is the signal rather than the figure itself, because
    it is ``None`` exactly when the target denominator was zero — the same test
    the note beneath it already applies. The gap goes with it: target minus
    actual with no target is the whole of the sales, reported as a surplus.
    """
    if values.get("achievement_percent") is None:
        return "n/a"
    return format_amount(values.get(key))


def format_days(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.1f} days"


def format_date_range(date_range: ResolvedDateRange | None) -> str:
    if date_range is None:
        return "—"
    if date_range.date_from == date_range.date_to:
        return date_range.date_from.strftime("%d %b %Y")
    return (f"{date_range.date_from.strftime('%d %b %Y')} – "
            f"{date_range.date_to.strftime('%d %b %Y')}")


def _volume_lines(values: dict[str, Any]) -> list[str]:
    """``{"volume_KG": 500}`` -> ``["**Volume:** 500 KG"]``, one line per unit.

    There is deliberately no branch that sums these. A question answered with
    both 500 KG and 250 LTR gets two lines, because the total of a mass and a
    volume is not a quantity that exists.
    """
    lines = []
    for name, value in sorted(values.items()):
        if not name.startswith("volume_") or value is None:
            continue
        lines.append(f"**Volume:** {format_quantity(value)} {name[len('volume_'):]}")
    return lines


#: Column -> (heading, formatter). Anything not listed is rendered as-is.
COLUMN_FORMATS: dict[str, tuple[str, Any]] = {
    "label": ("Name", str),
    "code": ("Code", str),
    "net_sales": ("Net Sales", format_amount),
    # gross_sales, gross_profit and gross_margin_percent are deliberately absent:
    # a sales report states quantity, volume and net sales. They remain stored on
    # the fact table; they are simply not part of the analysis surface, and a
    # formatter here is all it would take for one to reappear in a table.
    "discount": ("Discount", format_amount),
    "cost": ("Cost", format_amount),
    "average_selling_price": ("ASP", format_amount),
    "quantity": ("Qty", format_quantity),
    "rank": ("Rank", lambda v: str(int(v)) if v is not None else "—"),
    # One Volume column, no unit column beside it: a transaction line states one
    # Total Volume and nothing qualifies it. A group whose lines all lack the
    # figure arrives as ``None`` and renders as an em dash rather than as a zero
    # nobody can act on.
    "volume": ("Volume", format_quantity),
    "invoice_count": ("Invoices", format_quantity),
    "transaction_count": ("Txns", format_quantity),
    "share_percent": ("Share", format_percent),
    "target_amount": ("Target", format_amount),
    "actual_sales": ("Actual", format_amount),
    "achievement_percent": ("Achievement", format_percent),
    "gap": ("Gap", format_amount),
    "target_quantity": ("Target Qty", format_quantity),
    "actual_quantity": ("Actual Qty", format_quantity),
    "quantity_achievement_percent": ("Qty Achievement", format_percent),
    "quantity_gap": ("Qty Gap", format_quantity),
    "target_volume": ("Target Volume", format_quantity),
    "actual_volume": ("Actual Volume", format_quantity),
    # Material stock. The heading carries the unit and the cells are plain
    # figures — one statement of KG/LTR per column instead of one per cell, and
    # a column of numbers that lines up. There is no UOM column beside them:
    # the source states no per-row unit, so there would be nothing true to fill
    # one with.
    "unrestricted_stock": (stock_label("Unrestricted Stock"), format_stock),
    "quality_inspection_stock": (stock_label("Stock in Quality Inspection"),
                                 format_stock),
    "blocked_stock": (stock_label("Blocked Stock"), format_stock),
    "stock_in_transit": (stock_label("Stock in Transit"), format_stock),
    "total_stock": (stock_label("Total Stock"), format_stock),
    "plant_code": ("Plant", str),
    "plant_name": ("Plant", str),
    "storage_location_code": ("Storage Loc.", str),
    "storage_location_name": ("Storage Location", str),
    "material_code": ("Material Code", str),
    "material_description": ("Material", str),
    "material_group_code": ("Material Group", str),
    "material_group_name": ("Material Group", str),
    "material_brand_code": ("Material Brand", str),
    "material_brand": ("Material Brand", str),
    "production_date": ("Produced", str),
    "shelf_life_expiration_date": ("Expires", str),
    "row_count": ("Positions", format_quantity),
    "bucket": ("Bucket", str),
    "customer_code": ("Customer", str),
    "severity": ("Severity", str),
    "alert_type": ("Alert", str),
    "entity": ("Entity", str),
    "metric": ("Metric", str),
    "current_value": ("Value", lambda v: f"{float(v):,.1f}" if v is not None else "—"),
    "threshold": ("Threshold", lambda v: f"{float(v):,.1f}" if v is not None else "—"),
    "recommended_attention": ("Attention", str),
    "change": ("Change", format_amount),
    "growth_percent": ("Growth", lambda v: format_percent(v, signed=True)),
    "contribution_percent": ("Contribution", lambda v: format_percent(v, signed=True)),
    "current": ("Current", format_amount),
    "previous": ("Previous", format_amount),
    "date": ("Date", str),
    "full_date": ("Date", str),
    "month_name": ("Month", str),
}

#: Columns worth showing per tool, in order. Keeps tables narrow and readable.
TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "get_sales_detail": ("label", "quantity", "volume", "net_sales"),
    # Stock tables lead with the label the grouping produced, then the four
    # categories and the total. The categories stay four columns: a single
    # figure would hide that only the unrestricted part can actually be sold.
    "get_stock_by_plant": ("label", "unrestricted_stock",
                           "quality_inspection_stock", "blocked_stock",
                           "stock_in_transit", "total_stock"),
    "get_stock_by_storage_location": ("label", "unrestricted_stock",
                                      "quality_inspection_stock", "blocked_stock",
                                      "stock_in_transit", "total_stock"),
    "get_stock_by_material": ("label", "unrestricted_stock",
                              "quality_inspection_stock", "blocked_stock",
                              "stock_in_transit", "total_stock"),
    "get_stock_by_material_group": ("label", "unrestricted_stock",
                                    "quality_inspection_stock", "blocked_stock",
                                    "stock_in_transit", "total_stock"),
    "get_stock_by_material_brand": ("label", "unrestricted_stock",
                                    "quality_inspection_stock", "blocked_stock",
                                    "stock_in_transit", "total_stock"),
    "get_stock_expiry": ("code", "total_stock", "unrestricted_stock", "row_count"),
    # Material code sits with the other identifying columns: what is expiring is
    # a material, and the location is where to go and find it. The description
    # follows the code because a code alone does not say what is about to expire.
    "get_expiring_stock": ("plant_name", "storage_location_name", "material_code",
                           "material_description", "material_group_name",
                           "shelf_life_expiration_date",
                           "unrestricted_stock", "total_stock"),
    # Quantity sits beside value: a region can hit its taka target on price and
    # miss it on volume shipped, and one column cannot show that.
    "get_target_achievement": ("label", "target_quantity", "actual_quantity",
                               "target_amount", "actual_sales",
                               "achievement_percent", "gap"),
    "get_sales_achievement": ("label", "target_quantity", "actual_quantity",
                              "target_amount", "actual_sales",
                              "achievement_percent", "gap"),
    "get_sales_target": ("label", "target_quantity", "actual_quantity",
                         "target_amount", "actual_sales", "achievement_percent"),
    "get_target_gap": ("label", "target_amount", "actual_sales", "gap"),
    "get_business_alerts": ("severity", "alert_type", "entity", "current_value",
                            "threshold", "recommended_attention"),
    "get_root_cause_analysis": ("label", "previous", "current", "change",
                                "growth_percent", "contribution_percent"),
    # Rank, brand, quantity, volume, net sales — and nothing else. In
    # particular no volume unit: nothing in this system's volumes has one.
    "get_material_brand_performance": ("rank", "label", "quantity", "volume",
                                       "net_sales"),
    # The executive variant: plan beside actual, volume then value, then the
    # two ratios and the two shortfalls. No quantity — this table answers "how
    # did each brand do against its plan", and a unit count is not part of that.
    "get_material_brand_target_performance": (
        "rank", "label", "target_volume", "volume",
        "target_amount", "net_sales",
        "volume_achievement_percent",
        "achievement_percent", "volume_shortfall",
        "amount_shortfall"),
    "get_sales_trend": ("date", "quantity", "net_sales"),
}

_PERFORMANCE_TOOLS = (
    "get_region_performance", "get_zone_performance", "get_area_performance",
    "get_unit_performance", "get_territory_performance",
    "get_sub_territory_performance", "get_material_performance",
    "get_material_group_performance",
    "get_customer_performance", "get_salesforce_performance",
)
for _tool in _PERFORMANCE_TOOLS:
    TABLE_COLUMNS[_tool] = TABLE_COLUMNS["get_sales_detail"]

#: Right-aligned columns: everything numeric.
#:
#: Stated as the *exclusions* because most of the catalogue is a measure. The
#: material and location identifiers listed here are text that happens to look
#: like a number — a material code right-aligned beside a net-sales figure reads
#: as a quantity.
_NUMERIC_COLUMNS = {
    name for name in COLUMN_FORMATS
    if name not in {"label", "code", "bucket", "customer_code",
                    "material_code", "material_description",
                    "material_group_code", "material_group_name",
                    "material_brand_code", "material_brand",
                    "plant_code", "plant_name",
                    "storage_location_code", "storage_location_key",
                    "storage_location_name", "storage_location_label",
                    "production_date", "shelf_life_expiration_date",
                    "severity", "alert_type", "entity", "metric",
                    "recommended_attention", "coverage_status", "date", "full_date",
                    "month_name"}
}


#: Per-tool heading overrides for the generic ``label`` column.
#:
#: ``label`` is "Name" almost everywhere, which is right for a region or a
#: customer and vague for a brand ranking — the column is what the table is
#: about, so it says so.
HEADING_OVERRIDES: dict[str, dict[str, str]] = {
    "get_material_brand_performance": {"label": "Material Brand"},
    "get_material_brand_target_performance": {"label": "Material Brand"},
    "get_material_performance": {"label": "Material"},
    "get_material_group_performance": {"label": "Material Group"},
}


def render_table(rows: Sequence[dict[str, Any]], columns: Sequence[str],
                 max_rows: int = 20, tool: str | None = None) -> str:
    """A markdown table with numeric columns right-aligned."""
    if not rows:
        return ""
    present = [c for c in columns if any(c in row for row in rows)]
    if not present:
        present = list(rows[0])[:6]

    overrides = HEADING_OVERRIDES.get(tool or "", {})
    headings = [overrides.get(c) or
                COLUMN_FORMATS.get(c, (c.replace("_", " ").title(), str))[0]
                for c in present]
    alignment = ["---:" if c in _NUMERIC_COLUMNS else "---" for c in present]

    lines = ["| " + " | ".join(headings) + " |",
             "|" + "|".join(alignment) + "|"]
    for row in rows[:max_rows]:
        cells = []
        for column in present:
            value = row.get(column)
            formatter = COLUMN_FORMATS.get(column, (None, None))[1]
            if value is None:
                cells.append("—")
            elif formatter is None:
                cells.append(str(value))
            else:
                try:
                    cells.append(formatter(value))
                except (TypeError, ValueError):
                    cells.append(str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Answer building
# ---------------------------------------------------------------------------

INTENT_TITLES: dict[Intent, str] = {
    Intent.SALES_SUMMARY: "📊 Sales",
    Intent.SALES_DETAIL: "📊 Sales Breakdown",
    Intent.SALES_TREND: "📈 Sales Trend",
    Intent.SALES_GROWTH: "📈 Sales Growth",
    Intent.SALES_TARGET: "🎯 Sales vs Target",
    Intent.SALES_ACHIEVEMENT: "🎯 Target Achievement",
    Intent.SALES_VOLUME: "⚖️ Sales Volume",
    Intent.STOCK_SUMMARY: "📦 Stock",
    Intent.STOCK_BY_PLANT: "📦 Stock by Plant",
    Intent.STOCK_BY_STORAGE_LOCATION: "📦 Stock by Storage Location",
    Intent.STOCK_BY_MATERIAL: "📦 Stock by Material",
    Intent.STOCK_BY_MATERIAL_GROUP: "📦 Stock by Material Group",
    Intent.STOCK_BY_MATERIAL_BRAND: "📦 Stock by Material Brand",
    Intent.STOCK_EXPIRY: "📦 Stock Expiry",
    Intent.EXPIRING_STOCK: "📦 Expiring Stock",
    Intent.TARGET_SUMMARY: "🎯 Target",
    Intent.TARGET_ACHIEVEMENT: "🎯 Target Achievement",
    Intent.TARGET_GAP: "🎯 Target Gap",
    Intent.BUSINESS_SUMMARY: "🗞️ Business Summary",
    Intent.BUSINESS_ALERT: "🚨 Business Alerts",
    Intent.ROOT_CAUSE_ANALYSIS: "🔎 Root Cause Analysis",
    Intent.REGION_PERFORMANCE: "📊 Region Performance",
    Intent.ZONE_PERFORMANCE: "📊 Zone Performance",
    Intent.AREA_PERFORMANCE: "📊 Area Performance",
    Intent.UNIT_PERFORMANCE: "📊 Unit Performance",
    Intent.TERRITORY_PERFORMANCE: "📊 Territory Performance",
    Intent.SUB_TERRITORY_PERFORMANCE: "📊 Sub-Territory Performance",
    Intent.MATERIAL_PERFORMANCE: "📊 Material Performance",
    Intent.MATERIAL_BRAND_PERFORMANCE: "📊 Material Brand Performance",
    Intent.MATERIAL_GROUP_PERFORMANCE: "📊 Material Group Performance",
    Intent.CUSTOMER_PERFORMANCE: "📊 Customer Performance",
    Intent.SALES_FORCE_PERFORMANCE: "📊 Sales Force Performance",
}


class ResponseFormatter:
    """Builds the user-facing answer from validated tool results."""

    def format(self, intent: Intent, results: Sequence[ToolResult],
               date_range: ResolvedDateRange | None = None,
               filters: dict[str, Any] | None = None,
               assumptions: Sequence[str] = (),
               entity_labels: Mapping[str, str] | None = None) -> str:
        if not results:
            return "No data found for the selected period and filters."

        primary = results[0]
        parts: list[str] = [INTENT_TITLES.get(intent, "📊 Report")]
        parts.append("")

        body = self._body(intent, primary, results)
        if body:
            parts.append(body)

        table = self._table(primary)
        if table:
            parts.append("")
            parts.append(table)
            if primary.truncated:
                parts.append(f"_Showing the top {len(primary.rows)}; more rows exist._")

        if primary.interpretations:
            parts.append("")
            parts.append("**Interpretation** (analysis, not measured fact)")
            for line in primary.interpretations:
                parts.append(f"- {line}")

        notes = [n for r in results for n in r.notes]
        if notes:
            parts.append("")
            for note in dict.fromkeys(notes):
                parts.append(f"_{note}_")

        footer = self._footer(date_range, filters, assumptions,
                              entity_labels)
        if footer:
            parts.append("")
            parts.append(footer)
        return "\n".join(parts).strip()

    # -- bodies -------------------------------------------------------------

    def _body(self, intent: Intent, primary: ToolResult,
              results: Sequence[ToolResult]) -> str:
        if primary.tool == "get_business_summary":
            return self._business_summary(primary)
        if primary.tool == "get_sales_growth":
            return self._growth(primary)
        if primary.tool == "get_root_cause_analysis":
            return self._root_cause(primary)
        if primary.tool == "get_business_alerts":
            return self._alerts(primary)
        if primary.tool == "get_sales_summary":
            return self._sales_summary(primary, results)
        if primary.tool == "get_target_summary":
            return self._simple_value(primary)
        if primary.tool in ("get_sales_achievement", "get_target_achievement",
                            "get_sales_target", "get_target_gap"):
            return self._achievement(primary)
        if primary.rows:
            return ""
        return self._simple_value(primary)

    def _sales_summary(self, result: ToolResult, results: Sequence[ToolResult]) -> str:
        values = result.values
        lines = [f"**Net Sales:** {format_amount(result.value)}"]
        if values.get("quantity"):
            lines.append(f"**Quantity:** {format_quantity(values['quantity'])}")
        if values.get("discount") is not None:
            lines.append(f"**Discount:** {format_amount(values.get('discount'))}")
        if values.get("average_selling_price") is not None:
            lines.append(f"**Avg Selling Price:** "
                         f"{format_amount(values['average_selling_price'])}")

        for extra in results[1:]:
            if extra.tool in ("get_sales_growth",):
                growth = extra.values.get("growth_percent")
                lines.append(
                    f"**vs {extra.values.get('previous_period', 'previous period')}:** "
                    f"{format_percent(growth, signed=True)}"
                )
            if extra.tool in ("get_sales_achievement", "get_sales_target"):
                lines.append(
                    f"**Target:** {_target_figure(extra.values, 'target')}   "
                    f"**Achievement:** "
                    f"{format_percent(extra.values.get('achievement_percent'))}"
                )
            if extra.tool == "get_sales_volume":
                # One line per unit, never a combined figure: the volume tool
                # returns ``{"volume_KG": …, "volume_LTR": …}`` precisely so
                # that mass and volume cannot be added here by accident.
                lines.extend(_volume_lines(extra.values))
        return "\n".join(lines)

    def _simple_value(self, result: ToolResult) -> str:
        metric = (result.metric or "value").replace("_", " ").title()
        return f"**{metric}:** {format_amount(result.value)}"

    def _achievement(self, result: ToolResult) -> str:
        values = result.values
        lines = [
            f"**Target:** {_target_figure(values, 'target')}",
            f"**Actual:** {format_amount(values.get('actual'))}",
            f"**Achievement:** {format_percent(values.get('achievement_percent'))}",
            f"**Gap:** {_target_figure(values, 'gap')}",
        ]
        if values.get("below_percent") is not None:
            lines.append(
                f"_Listing only groups below {format_percent(values['below_percent'])}._"
            )
        return "\n".join(lines)

    def _growth(self, result: ToolResult) -> str:
        values = result.values
        return "\n".join([
            f"**Current:** {format_amount(values.get('current'))} "
            f"({values.get('current_period')})",
            f"**Previous:** {format_amount(values.get('previous'))} "
            f"({values.get('previous_period')})",
            f"**Change:** {format_amount(values.get('change'))}   "
            f"**Growth:** {format_percent(values.get('growth_percent'), signed=True)}",
        ])

    def _business_summary(self, result: ToolResult) -> str:
        values = result.values
        lines = [
            f"**Sales:** {format_amount(values.get('sales'))}",
            f"**Target:** {format_amount(values.get('target'))}",
            f"**Achievement:** {format_percent(values.get('achievement_percent'))}",
        ]
        # Stock's line in the snapshot is availability and money at risk. The
        # old "N items below coverage" needed days of cover, and a dateless
        # position gives no rate of consumption to divide by.
        # The unit sits in the bolded label, as it does on the cards and in the
        # tables, so both figures on the line read as plain numbers.
        if values.get("total_stock"):
            lines.append(
                f"**{stock_label('Unrestricted Stock')}:** "
                f"{format_stock(values.get('unrestricted_stock'))} "
                f"of {format_stock(values.get('total_stock'))}"
            )
        alerts = []
        if values.get("expired_stock"):
            alerts.append(
                f"{stock_label('Expired Stock')}: "
                f"{format_stock(values['expired_stock'])}"
            )
        if values.get("expiring_soon_stock"):
            alerts.append(
                f"{stock_label('Stock Near Shelf Life')}: "
                f"{format_stock(values['expiring_soon_stock'])}"
            )
        if alerts:
            lines.append("")
            lines.append("**Critical Alerts**")
            lines.extend(f"- {a}" for a in alerts)

        top, bottom = values.get("top_region"), values.get("bottom_region")
        brand = values.get("top_brand")
        if top or bottom or brand:
            lines.append("")
        if top:
            lines.append(f"**Top Region:** {top.get('label') or top.get('code')} — "
                         f"{format_amount(top.get('net_sales'))}")
        if bottom and bottom.get("code") != (top or {}).get("code"):
            lines.append(f"**Bottom Region:** "
                         f"{bottom.get('label') or bottom.get('code')} — "
                         f"{format_amount(bottom.get('net_sales'))}")
        if brand:
            lines.append(f"**Top Brand:** "
                         f"{brand.get('label') or brand.get('code')} — "
                         f"{format_amount(brand.get('net_sales'))}")
        return "\n".join(lines)

    def _alerts(self, result: ToolResult) -> str:
        values = result.values
        if not result.rows:
            return "No alert thresholds were breached."
        return (f"**{result.row_count} alert(s)** — "
                f"{values.get('critical', 0)} critical, {values.get('high', 0)} high.")

    def _root_cause(self, result: ToolResult) -> str:
        lines = ["**Facts**"]
        lines.extend(f"- {fact}" for fact in result.facts)
        contributors = result.values.get("contributors") or {}
        for dimension, rows in contributors.items():
            movers = [r for r in rows if r.get("change")]
            if not movers:
                continue
            lines.append("")
            lines.append(f"**By {dimension}**")
            for row in movers[:3]:
                lines.append(
                    f"- {row['label']}: {format_amount(row['change'])} "
                    f"({format_percent(row.get('growth_percent'), signed=True)})"
                )
        return "\n".join(lines)

    # -- shared parts -------------------------------------------------------

    def _table(self, result: ToolResult) -> str:
        if not result.rows:
            return ""
        if result.tool == "get_business_summary":
            return ""
        columns = TABLE_COLUMNS.get(result.tool)
        if columns is None:
            columns = tuple(k for k in result.rows[0] if k in COLUMN_FORMATS)[:6]
        # A share is only ever present because the reader asked for one, and a
        # tool with a fixed column list would otherwise compute it and show
        # nothing. Appended rather than substituted: the contribution sits
        # beside the figure it is a proportion of, which is what makes it
        # readable as a share rather than as another measure.
        if "share_percent" in result.rows[0] and "share_percent" not in columns:
            columns = (*columns, "share_percent")
        return render_table(result.rows, columns, tool=result.tool)

    def _footer(self, date_range: ResolvedDateRange | None,
                filters: dict[str, Any] | None, assumptions: Sequence[str],
                entity_labels: Mapping[str, str] | None = None) -> str:
        """The period, what narrowed the answer, and what was assumed.

        A filter is named, not coded. The line used to read
        "territory: 1A1NBOGA00030" directly above an assumption calling the
        same place Adamdighi — the answer knew the name and showed the
        reader a code.

        Anything with no name behind it still shows its code rather than
        being dropped: a filter narrowed the figure and the reader has to
        see that it did, even where only the master data could say what it
        was. That is the case for a code injected by the reader's own data
        scope, which no entity in the question ever named.
        """
        labels = entity_labels or {}
        lines: list[str] = []
        if date_range is not None:
            lines.append(f"📅 **Period:** {date_range.label} "
                         f"({format_date_range(date_range)})")
        applied = {k: v for k, v in (filters or {}).items() if v}
        if applied:
            rendered = ", ".join(
                f"{_filter_heading(key)}: {_filter_values(value, labels)}"
                for key, value in applied.items()
            )
            lines.append(f"🔎 **Filters:** {rendered}")
        for assumption in assumptions:
            lines.append(f"ℹ️ {assumption}")
        return "\n".join(lines)


def _filter_heading(key: str) -> str:
    """``territory_codes`` -> ``Territory``."""
    return key.replace("_codes", "").replace("_", " ").title()


def _filter_values(value: Any, labels: Mapping[str, str]) -> str:
    """Each code under its master-data name, falling back to the code itself."""
    values = value if isinstance(value, list) else [value]
    return ", ".join(labels.get(str(item), str(item)) for item in values)


def format_error(message: str) -> str:
    return message


__all__ = [
    "ResponseFormatter",
    "format_amount",
    "format_percent",
    "format_quantity",
    "format_stock",
    "stock_label",
    "format_days",
    "format_date_range",
    "group_indian",
    "render_table",
    "COLUMN_FORMATS",
    "TABLE_COLUMNS",
    "INTENT_TITLES",
    "TAKA",
]
