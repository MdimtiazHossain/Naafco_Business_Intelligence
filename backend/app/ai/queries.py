"""The only code path between the agent and the database.

Everything here is built with SQLAlchemy Core against **reflected views**:

* filters are bound parameters, never string-interpolated;
* group-by dimensions come from the :class:`GroupBy` enum and are looked up in a
  whitelist — an unknown dimension is an error, not a column name;
* every statement is bounded by a date range and a row limit.

The LLM never reaches this module. It selects a tool; the tool builds a typed
input; this module turns that input into a parameterised query.

Aggregation is plain SUM/COUNT over measures ETL already computed, so no Phase 2
business calculation is reimplemented here. Ratios reuse
``etl.transforms`` / ``etl.validation.safe_divide``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Sequence

from sqlalchemy import MetaData, Table, and_, asc, case, desc, func, select
from sqlalchemy.orm import Session

from ..etl.transforms import achievement_percent, growth_percent
from ..etl.validation import safe_divide
from .schemas import GroupBy, ScopeFilters

MAX_ROWS = 500

SALES_VIEW = "vw_sales_detail"
#: Material stock: one view, and only one.
#:
#: The old module had three — a movement detail, a current position derived from
#: it, and a coverage view that divided stock by average daily sales per SKU.
#: None of them survives the new grain: material stock is already a current
#: position, so nothing needs deriving, and coverage in days needs a stock figure
#: and a sales figure measured over the same period, which a dateless position
#: cannot supply. Since revision 0022 stock and sales do at least name the *same*
#: material — one master, one code — so a stock figure and a sales figure can sit
#: side by side; what still cannot be derived is a rate of consumption from a
#: reading with no date on it.
MATERIAL_STOCK_VIEW = "vw_material_stock_detail"
#: Credit Control's row-level view. Its reporting date is ``invoice_date``; how
#: late a row is comes from ``due_date`` against the date the caller asks about,
#: which is why nothing here filters on a stored overdue column — there is none.
CREDIT_INVOICE_VIEW = "vw_credit_invoice_detail"
TARGET_VIEW = "vw_target_detail"
TARGET_VS_ACTUAL_VIEW = "vw_target_vs_actual"

#: The unit every material stock figure is reported in.
#:
#: ``KG/LTR``, on the business's stated convention. The Material Transaction
#: Data carries four quantities and **no unit of measure**; a plant's stock is
#: held in kilograms or litres depending on the material and the source never
#: says which, so the label names both rather than claiming one. It is a *label*
#: and nothing else: there is no factor in the source to convert with, so a
#: converted figure would be an invented one — the number stored is the number
#: reported, and this constant is how every surface says so with the same word.
#:
#: This is also why no stock column has a companion unit column. A per-row unit
#: would have to come from somewhere, and the only place left to get one is a
#: guess.
#:
#: Declared here, beside the view and the measure set, because this is the one
#: module the whole stock path already reads: the tools, the response formatter,
#: the reporting column catalogue and the map all import from it, so the unit
#: cannot be spelled two ways.
#:
#: Deliberately **not** a setting. A per-deployment unit would let two databases
#: report the same column in different units and still call both correct, which
#: is exactly the mixing the convention exists to prevent.
STOCK_UNIT = "KG/LTR"

#: ``ScopeFilters`` field -> the column it filters on.
#:
#: A value may name alternatives, first match wins — see
#: :data:`FILTER_COLUMN_ALTERNATES`, which currently has no entries: every
#: filter names one column that every view spells the same way.
FILTER_COLUMNS: dict[str, str] = {
    "company_codes": "company_code",
    "business_unit_codes": "bu_code",
    "sales_line_codes": "sales_line_code",
    "zone_codes": "zone_code",
    "region_codes": "region_code",
    "area_codes": "area_code",
    "unit_codes": "unit_code",
    "territory_codes": "territory_code",
    "sub_territory_codes": "sub_territory_code",
    "customer_codes": "customer_code",
    "sales_force_codes": "sales_force_code",
    "batch_codes": "batch_code",
    # The item filters. Since revision 0022 all three apply to sales, target
    # *and* stock: one Material Master means one set of item filters, and a
    # material brand named on the stock page narrows a sales report to the same
    # goods.
    "material_codes": "material_code",
    "material_group_codes": "material_group_code",
    "material_brand_names": "material_brand",
    # Stock only — a sale states no plant and no storage location. Every other
    # view is skipped for these by ``_filter_column``, which is the same rule
    # that already lets a territory filter pass over the stock view without
    # narrowing it to nothing.
    "plant_codes": "plant_code",
    # The pair, not the bare code — see ``ScopeFilters.storage_location_keys``.
    "storage_location_keys": "storage_location_key",
}

#: Filters that name no column and so cannot be matched by ``_filter_column``.
#: ``expiry_statuses`` is derived from a date against today and a configurable
#: horizon, so the stock queries apply it; it is listed here only so
#: :func:`unsupported_filters` can still report it against a view that carries
#: no shelf-life date at all.
DERIVED_FILTERS: dict[str, str] = {
    "expiry_statuses": "shelf_life_expiration_date",
}

#: Extra column names a filter accepts when its primary one is not on the view.
FILTER_COLUMN_ALTERNATES: dict[str, tuple[str, ...]] = {}


def _filter_column(table: Table, field_name: str) -> str | None:
    """The column this filter applies to on this view, or ``None``."""
    for candidate in (FILTER_COLUMNS[field_name],
                      *FILTER_COLUMN_ALTERNATES.get(field_name, ())):
        if candidate in table.c:
            return candidate
    return None

#: Grouping dimension -> (code column, label column). Whitelist by construction.
GROUP_COLUMNS: dict[GroupBy, tuple[str, str]] = {
    GroupBy.DATE: ("full_date", "full_date"),
    GroupBy.MONTH: ("month", "month_name"),
    GroupBy.COMPANY: ("company_code", "company_name"),
    GroupBy.BUSINESS_UNIT: ("bu_code", "bu_name"),
    GroupBy.SALES_LINE: ("sales_line_code", "sales_line_name"),
    GroupBy.ZONE: ("zone_code", "zone_name"),
    GroupBy.REGION: ("region_code", "region_name"),
    GroupBy.AREA: ("area_code", "area_name"),
    GroupBy.UNIT: ("unit_code", "unit_name"),
    GroupBy.TERRITORY: ("territory_code", "territory_name"),
    GroupBy.SUB_TERRITORY: ("sub_territory_code", "sub_territory_name"),
    # Keyed on the code, labelled with the name — the shape every other
    # dimension here has. The label was the code until revision 0024, because
    # ``dim_customer`` was PENDING_SOURCE_DATA and a join would have made every
    # customer NULL; the master has arrived, so the views carry the name and a
    # customer reads as itself. Grouping, filtering and scope are all still on
    # the code: nothing joins on a name.
    GroupBy.CUSTOMER: ("customer_code", "customer_name"),
    GroupBy.SALES_FORCE: ("sales_force_code", "sales_force_code"),
    # Material stock only. The label is the master's name, so a renamed plant
    # reads correctly on every stock report at once.
    GroupBy.PLANT: ("plant_code", "plant_name"),
    # Keyed on the plant/location pair, never on the bare code: a storage
    # location code is unique only within a plant, so grouping by the code
    # merged separate locations that happened to share a name and split ones
    # that did not. ``FG01`` alone names 40 different places. The label is
    # qualified by the plant for the same reason — 40 rows called "Finished
    # Goods" would be correct and unreadable.
    GroupBy.STORAGE_LOCATION: ("storage_location_key", "storage_location_label"),
    # The three item dimensions, and since revision 0022 the only ones. They
    # apply to sales and target as well as stock: "top brands" and "stock by
    # brand" now group the same goods by the same column of the same master.
    #
    # The Material Master carries a description per material, so a material
    # reads as itself rather than as a bare code. The label still comes from the
    # master through the view, so correcting a description there corrects every
    # report at once.
    GroupBy.MATERIAL: ("material_code", "material_description"),
    GroupBy.MATERIAL_GROUP: ("material_group_code", "material_group_name"),
    # The name is the identity here, not the code. ``material_brand_code`` is
    # ``'0'`` on every material the Material Master holds: it distinguishes
    # nothing, so grouping by it returned one row for all 235 brands and gave
    # every row of the brand breakdown the same key.
    GroupBy.MATERIAL_BRAND: ("material_brand", "material_brand"),
}

_VIEW_CACHE: dict[tuple[int, str], Table] = {}


def view(session: Session, name: str) -> Table:
    """Reflect a view once per engine."""
    bind = session.get_bind()
    key = (id(bind), name)
    if key not in _VIEW_CACHE:
        _VIEW_CACHE[key] = Table(name, MetaData(), autoload_with=bind)
    return _VIEW_CACHE[key]


def clear_view_cache() -> None:
    _VIEW_CACHE.clear()


# ---------------------------------------------------------------------------
# Predicate building
# ---------------------------------------------------------------------------


def filter_conditions(table: Table, filters: ScopeFilters,
                      date_from: dt.date | None = None, date_to: dt.date | None = None,
                      date_column: str = "full_date") -> list:
    """Bound predicates for a scope filter plus a date window.

    A filter naming a column the view does not carry is skipped rather than
    guessed at — for example ``territory_codes`` against the stock view, which is
    held at warehouse level.
    """
    conditions = []
    for field_name in FILTER_COLUMNS:
        values = getattr(filters, field_name, None)
        if not values:
            continue
        column_name = _filter_column(table, field_name)
        if column_name is not None:
            conditions.append(table.c[column_name].in_(list(values)))
    if filters.source_system and "source_system" in table.c:
        conditions.append(table.c["source_system"] == filters.source_system)
    if date_column in table.c:
        if date_from is not None:
            conditions.append(table.c[date_column] >= date_from)
        if date_to is not None:
            conditions.append(table.c[date_column] <= date_to)
    return conditions


def unsupported_filters(table: Table, filters: ScopeFilters) -> list[str]:
    """Filters the user asked for that this view cannot honour.

    Surfaced to the user as an assumption rather than silently ignored.
    """
    missing = []
    for field_name, column_name in FILTER_COLUMNS.items():
        if getattr(filters, field_name, None) and _filter_column(table, field_name) is None:
            missing.append(column_name)
    # A derived filter is unhonourable when the column it is derived *from* is
    # absent, which is the same test one column further back.
    for field_name, source_column in DERIVED_FILTERS.items():
        if getattr(filters, field_name, None) and source_column not in table.c:
            missing.append(source_column)
    return missing


def normalize_value(value: Any) -> Any:
    """Convert a database value into something JSON- and arithmetic-friendly.

    SQLAlchemy returns ``Decimal`` for NUMERIC columns. Mixing those with the
    Python floats used elsewhere raises ``TypeError`` on the first subtraction,
    and they are not JSON serialisable either, so every row leaving this module
    is normalised once, here.
    """
    if isinstance(value, Decimal):
        return float(value)
    return value


def normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: normalize_value(value) for key, value in row.items()} for row in rows]


def _rows(result) -> list[dict[str, Any]]:
    return normalize_rows([dict(row._mapping) for row in result])


def _f(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


# ---------------------------------------------------------------------------
# Measure definitions (plain aggregation over stored measures)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasureSet:
    """The aggregates a fact view exposes, and how to rank by them."""

    view_name: str
    sums: tuple[str, ...]
    default_sort: str
    counts: tuple[tuple[str, str], ...] = ()

    def expressions(self, table: Table) -> list:
        expressions = [
            func.sum(table.c[name]).label(name) for name in self.sums if name in table.c
        ]
        for label, column in self.counts:
            if column == "*":
                expressions.append(func.count().label(label))
            elif column in table.c:
                expressions.append(
                    func.count(func.distinct(table.c[column])).label(label)
                )
        return expressions


#: ``volume`` is summed here like any other measure.
#:
#: It used to be excluded, and had to be: a volume derived from a SKU master's
#: pack size carried that pack's unit, so summing the column would have added
#: kilograms to litres. A sales line now states its own Total Volume and carries
#: no unit at all, so the sum is simply the sum — the same arithmetic as net
#: sales, in one query rather than a second one joined back onto the rows.
#:
#: ``gross_sales`` and ``gross_profit`` are deliberately **not** aggregated.
#: Both remain stored on ``fact_sales`` and both are still derived by the ETL —
#: nothing was dropped from the warehouse — but the analysis surface reports the
#: three transactional measures the business ranks on: quantity, volume and net
#: sales. Removing them here removes them from every table, export and agent
#: answer at once, which is the point: one definition of what a sales report
#: says, rather than nine column lists that can drift apart.
SALES_MEASURES = MeasureSet(
    view_name=SALES_VIEW,
    sums=("quantity", "volume", "discount", "net_sales", "cost"),
    default_sort="net_sales",
    counts=(("transaction_count", "*"), ("invoice_count", "invoice_no")),
)

#: The four stock categories, summed independently, plus the total the view
#: computes. They stay four measures on every report: unrestricted stock is what
#: can be sold, and the other three are each held back for a different reason, so
#: a single number would answer a question nobody asked. ``total_stock`` is the
#: sum of all four — the definition the business confirmed, in-transit included —
#: and is computed once in ``vw_material_stock_detail`` rather than here, so the
#: dashboard, the table, the export and the agent cannot disagree about it.
MATERIAL_STOCK_MEASURES = MeasureSet(
    view_name=MATERIAL_STOCK_VIEW,
    sums=("unrestricted_stock", "quality_inspection_stock", "blocked_stock",
          "stock_in_transit", "total_stock"),
    default_sort="total_stock",
    counts=(("row_count", "*"),),
)

#: ``target_volume`` is summed here like the sales volume beside it.
#:
#: It used to be excluded, because a target named a SKU and inherited that SKU's
#: Pack Unit from the Product Master, so the column mixed units and could not be
#: added up. Revision 0022 removed that master: the Material Master states no
#: unit of measure, so a planned volume is the unit-free number the planner
#: typed, and the sum of those numbers is the sum.
TARGET_MEASURES = MeasureSet(
    view_name=TARGET_VIEW,
    sums=("target_amount", "target_quantity", "target_volume"),
    default_sort="target_amount",
    counts=(("target_count", "*"),),
)


# ---------------------------------------------------------------------------
# Generic aggregate queries
# ---------------------------------------------------------------------------


def aggregate_totals(session: Session, measures: MeasureSet, filters: ScopeFilters,
                     date_from: dt.date, date_to: dt.date,
                     date_column: str = "full_date",
                     extra_conditions: Sequence[Any] | None = None) -> dict[str, Any]:
    """One row of totals for the window and filters.

    ``extra_conditions`` carries predicates the generic column matcher cannot
    build — currently only the shelf-life bucket, which is derived from a date
    rather than read from a column. They are ``AND``ed with the rest, so an
    extra condition can only ever narrow the result.
    """
    table = view(session, measures.view_name)
    statement = select(*measures.expressions(table)).select_from(table)
    conditions = filter_conditions(table, filters, date_from, date_to, date_column)
    conditions.extend(extra_conditions or ())
    if conditions:
        statement = statement.where(and_(*conditions))
    row = session.execute(statement).one()
    totals = {key: _f(value) if not key.endswith("count") else (value or 0)
              for key, value in row._mapping.items()}
    return {k: (v if v is not None else 0.0) for k, v in totals.items()}


def aggregate_by(session: Session, measures: MeasureSet, filters: ScopeFilters,
                 date_from: dt.date, date_to: dt.date, group_by: GroupBy,
                 limit: int = 20, direction: str = "desc",
                 sort_field: str | None = None,
                 date_column: str = "full_date",
                 extra_conditions: Sequence[Any] | None = None,
                 ) -> tuple[list[dict[str, Any]], bool]:
    """Totals grouped by one whitelisted dimension.

    Returns ``(rows, truncated)``; ``truncated`` tells the formatter that more
    groups exist beyond the limit, so the answer can say so.

    ``extra_conditions`` is the same narrowing-only hook
    :func:`aggregate_totals` documents.
    """
    table = view(session, measures.view_name)
    if group_by not in GROUP_COLUMNS:
        raise ValueError(f"Unsupported grouping: {group_by}")
    code_column, label_column = GROUP_COLUMNS[group_by]
    if code_column not in table.c:
        raise ValueError(
            f"{measures.view_name} cannot be grouped by {group_by.value}"
        )

    code = table.c[code_column].label("code")
    label = table.c[label_column].label("label") if label_column in table.c else code
    statement = select(code, label, *measures.expressions(table)).select_from(table)

    conditions = filter_conditions(table, filters, date_from, date_to, date_column)
    conditions.extend(extra_conditions or ())
    if conditions:
        statement = statement.where(and_(*conditions))

    statement = statement.group_by(table.c[code_column])
    if label_column in table.c and label_column != code_column:
        statement = statement.group_by(table.c[label_column])

    order_field = sort_field or measures.default_sort
    order = desc(func.sum(table.c[order_field])) if direction == "desc" else asc(
        func.sum(table.c[order_field])
    )
    bounded = max(1, min(limit, MAX_ROWS))
    statement = statement.order_by(order).limit(bounded + 1)

    rows = _rows(session.execute(statement))
    truncated = len(rows) > bounded
    return [_label_unassigned(row) for row in rows[:bounded]], truncated


# ---------------------------------------------------------------------------
# Volume
#
# **One rule now, on both sides.** A volume is the number the source file stated,
# and it carries no unit anywhere in this system: a sales line states its own
# Total Volume, and since revision 0022 a target states its own planned volume in
# the same way. Both are therefore plain sums.
#
# Targets used to be different. A target named a SKU and no line, so its unit was
# that SKU's Pack Unit read through the Product Master, and this module carried
# unit-partitioned readers — ``volume_by_unit``, ``volume_totals``,
# ``single_volume_by_group`` — whose whole purpose was to refuse to add a
# kilogram to a litre. Removing the Product Master removed the unit they
# partitioned on: the Material Master states none, and inventing one to keep the
# machinery alive would be exactly the guess it existed to prevent. So the
# machinery is gone rather than left running on a column that would always be
# NULL, which would have rendered every target volume as "mixed" and shown the
# business an em dash where its own numbers are.
# ---------------------------------------------------------------------------


def volume_total(session: Session, view_name: str, filters: ScopeFilters,
                 date_from: dt.date | None = None,
                 date_to: dt.date | None = None,
                 date_column: str = "full_date",
                 volume_column: str = "volume") -> dict[str, Any]:
    """The total volume, as one number.

    For sales and for targets alike: both state their own figure and neither
    states a unit, so there is nothing to partition by and nothing to convert.
    This is a straight ``SUM`` — the same arithmetic as net sales. Point it at
    ``target_volume`` for the target side.

    Rows with no volume are excluded rather than counted as zero, and counted
    separately as ``unmeasured_lines``: a line whose file supplied no volume has
    an *unknown* volume, and folding that in as nothing would understate the
    total silently. ``value`` is ``None`` when no line in scope carried one at
    all, which reports as "n/a" rather than as a zero nobody can act on.
    """
    table = view(session, view_name)
    if volume_column not in table.c:
        return {"value": None, "measured_lines": 0, "unmeasured_lines": 0}

    conditions = filter_conditions(table, filters, date_from, date_to, date_column)
    measured = session.execute(
        select(func.sum(table.c[volume_column]), func.count())
        .select_from(table)
        .where(and_(*conditions, table.c[volume_column].isnot(None)))
    ).one()
    unmeasured = session.execute(
        select(func.count()).select_from(table)
        .where(and_(*conditions, table.c[volume_column].is_(None)))
    ).scalar_one()

    return {
        "value": None if measured[0] is None else float(measured[0]),
        "measured_lines": int(measured[1] or 0),
        "unmeasured_lines": int(unmeasured or 0),
    }


def volume_by_group(session: Session, view_name: str, filters: ScopeFilters,
                    date_from: dt.date | None = None,
                    date_to: dt.date | None = None,
                    group_by: GroupBy | None = None,
                    date_column: str = "full_date",
                    limit: int = MAX_ROWS,
                    volume_column: str = "volume") -> list[dict[str, Any]]:
    """Total volume per group — one row per group, one figure on it.

    The grouped counterpart of :func:`volume_total`, for the Volume column on a
    sales or target report. A group's figure is the sum of its rows' stated
    volume and nothing else, so it can never come back ``None`` for a group that
    has data.
    """
    table = view(session, view_name)
    if volume_column not in table.c:
        return []

    columns: list[Any] = []
    group_columns: list[Any] = []
    if group_by is not None:
        if group_by not in GROUP_COLUMNS:
            raise ValueError(f"Unsupported grouping: {group_by}")
        code_column, label_column = GROUP_COLUMNS[group_by]
        if code_column not in table.c:
            raise ValueError(f"{view_name} cannot be grouped by {group_by.value}")
        columns.append(table.c[code_column].label("code"))
        group_columns.append(table.c[code_column])
        if label_column in table.c and label_column != code_column:
            columns.append(table.c[label_column].label("label"))
            group_columns.append(table.c[label_column])

    columns.append(func.sum(table.c[volume_column]).label("volume"))
    columns.append(func.count().label("line_count"))

    conditions = filter_conditions(table, filters, date_from, date_to, date_column)
    conditions.append(table.c[volume_column].isnot(None))
    statement = select(*columns).select_from(table).where(and_(*conditions))
    if group_columns:
        statement = statement.group_by(*group_columns)
    statement = statement.order_by(desc(func.sum(table.c[volume_column])))

    return _rows(session.execute(statement.limit(max(1, min(limit, MAX_ROWS)))))


def _label_unassigned(row: dict[str, Any]) -> dict[str, Any]:
    """Name the NULL group honestly instead of showing "None".

    A fact row legitimately has no code at a level its source never carried —
    sales captured at area level have no territory. Those rows still belong in
    the total, so they are labelled rather than dropped.
    """
    if row.get("code") is None:
        row["code"] = "(unassigned)"
        row["label"] = "(not assigned at this level)"
    elif row.get("label") is None:
        row["label"] = row["code"]
    return row


def time_series(session: Session, measures: MeasureSet, filters: ScopeFilters,
                date_from: dt.date, date_to: dt.date, granularity: str = "day",
                limit: int = 100) -> list[dict[str, Any]]:
    """A chronological series for trend questions."""
    table = view(session, measures.view_name)
    if granularity == "month":
        keys = [table.c["year"].label("year"), table.c["month"].label("month"),
                table.c["month_name"].label("label")]
        group_columns = [table.c["year"], table.c["month"], table.c["month_name"]]
        order_columns = [table.c["year"], table.c["month"]]
    else:
        keys = [table.c["full_date"].label("date")]
        group_columns = [table.c["full_date"]]
        order_columns = [table.c["full_date"]]

    statement = select(*keys, *measures.expressions(table)).select_from(table)
    conditions = filter_conditions(table, filters, date_from, date_to)
    if conditions:
        statement = statement.where(and_(*conditions))
    statement = statement.group_by(*group_columns).order_by(*order_columns)
    return _rows(session.execute(statement.limit(max(1, min(limit, MAX_ROWS)))))


def detail_rows(session: Session, view_name: str, filters: ScopeFilters,
                date_from: dt.date, date_to: dt.date, columns: Sequence[str],
                order_by: str | None = None, direction: str = "desc",
                limit: int = 20, date_column: str = "full_date",
                extra_conditions: Iterable | None = None,
                ) -> tuple[list[dict[str, Any]], bool]:
    """Row-level detail, always bounded."""
    table = view(session, view_name)
    selected = [table.c[name] for name in columns if name in table.c]
    statement = select(*selected).select_from(table)

    conditions = filter_conditions(table, filters, date_from, date_to, date_column)
    if extra_conditions:
        conditions.extend(extra_conditions)
    if conditions:
        statement = statement.where(and_(*conditions))
    if order_by and order_by in table.c:
        statement = statement.order_by(
            desc(table.c[order_by]) if direction == "desc" else asc(table.c[order_by])
        )
    bounded = max(1, min(limit, MAX_ROWS))
    rows = _rows(session.execute(statement.limit(bounded + 1)))
    return rows[:bounded], len(rows) > bounded


# ---------------------------------------------------------------------------
# Derived metrics
# ---------------------------------------------------------------------------


def sales_kpis(totals: dict[str, Any]) -> dict[str, Any]:
    """Average selling price, computed once, divide-by-zero safe.

    Gross margin used to be computed here too. It is not reported any more —
    see :data:`SALES_MEASURES` — and computing a figure nothing displays would
    only invite it back into a table by accident.
    """
    net_sales = Decimal(str(totals.get("net_sales") or 0))
    quantity = Decimal(str(totals.get("quantity") or 0))
    return {
        "average_selling_price": _f(safe_divide(net_sales, quantity)),
    }


def compare_totals(current: dict[str, Any], previous: dict[str, Any],
                   measure: str) -> dict[str, Any]:
    """Current vs previous for one measure, with growth and absolute change."""
    current_value = current.get(measure) or 0.0
    previous_value = previous.get(measure) or 0.0
    return {
        "current": current_value,
        "previous": previous_value,
        "change": current_value - previous_value,
        "growth_percent": _f(growth_percent(current_value, previous_value)),
    }


def target_vs_actual(session: Session, filters: ScopeFilters, date_from: dt.date,
                     date_to: dt.date, group_by: GroupBy = GroupBy.REGION,
                     limit: int = 20) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Target and actual side by side, grouped by one dimension.

    Both sides are aggregated over the same window and joined in Python on the
    group code, so a group with a target but no sales (and the reverse) still
    appears — which is exactly the case a "who missed target" question is about.
    """
    code_column, _ = GROUP_COLUMNS[group_by]

    target_rows, _ = aggregate_by(
        session, TARGET_MEASURES, filters, date_from, date_to, group_by,
        limit=MAX_ROWS, sort_field="target_amount",
    )
    sales_rows, _ = aggregate_by(
        session, SALES_MEASURES, filters, date_from, date_to, group_by,
        limit=MAX_ROWS, sort_field="net_sales",
    )

    # Volume on both sides, one figure per group, read the same way on each:
    # both are the number the source file stated and neither carries a unit, so
    # both are plain sums over the same window.
    target_volume = {
        str(row["code"]): row.get("volume")
        for row in volume_by_group(session, TARGET_VIEW, filters, date_from,
                                   date_to, group_by,
                                   volume_column="target_volume")
    }
    actual_volume = {
        str(row["code"]): row.get("volume")
        for row in volume_by_group(session, SALES_VIEW, filters, date_from,
                                   date_to, group_by)
    }

    def _blank(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "code": row["code"], "label": row.get("label") or row["code"],
            "target_amount": 0.0, "actual_sales": 0.0,
            "target_quantity": 0.0, "actual_quantity": 0.0,
        }

    combined: dict[Any, dict[str, Any]] = {}
    for row in target_rows:
        entry = _blank(row)
        entry["target_amount"] = row.get("target_amount") or 0.0
        entry["target_quantity"] = row.get("target_quantity") or 0.0
        combined[row["code"]] = entry
    for row in sales_rows:
        entry = combined.setdefault(row["code"], _blank(row))
        entry["actual_sales"] = row.get("net_sales") or 0.0
        entry["actual_quantity"] = row.get("quantity") or 0.0

    for code, entry in combined.items():
        entry["achievement_percent"] = _f(
            achievement_percent(entry["actual_sales"], entry["target_amount"])
        )
        entry["gap"] = entry["target_amount"] - entry["actual_sales"]
        entry["quantity_achievement_percent"] = _f(
            achievement_percent(entry["actual_quantity"], entry["target_quantity"])
        )
        entry["quantity_gap"] = entry["target_quantity"] - entry["actual_quantity"]
        entry["target_volume"] = target_volume.get(str(code))
        entry["actual_volume"] = actual_volume.get(str(code))

    rows = sorted(
        combined.values(),
        key=lambda r: (r["achievement_percent"] is None, r["achievement_percent"] or 0),
        reverse=True,
    )
    totals_target = sum(r["target_amount"] for r in rows)
    totals_actual = sum(r["actual_sales"] for r in rows)
    target_quantity = sum(r["target_quantity"] for r in rows)
    actual_quantity = sum(r["actual_quantity"] for r in rows)
    totals = {
        "target": totals_target,
        "actual": totals_actual,
        "achievement_percent": _f(achievement_percent(totals_actual, totals_target)),
        "gap": totals_target - totals_actual,
        "target_quantity": target_quantity,
        "actual_quantity": actual_quantity,
        "quantity_achievement_percent": _f(
            achievement_percent(actual_quantity, target_quantity)
        ),
        "quantity_gap": target_quantity - actual_quantity,
        "group_by": group_by.value,
        "group_column": code_column,
    }
    return rows[: max(1, min(limit, MAX_ROWS))], totals


# ---------------------------------------------------------------------------
# Material stock
#
# **No date window applies.** The Material Transaction Data carries no posting
# date, so a stock figure is the position as it stands, not a total over a
# period. Every function below therefore passes ``None`` for the date bounds and
# ignores the caller's range — a stock answer would be the same for "today" and
# "last quarter", and silently filtering on a date the data does not have would
# return nothing at all.
# ---------------------------------------------------------------------------


#: The shelf-life buckets, in the order a report reads them.
EXPIRY_STATUSES: tuple[str, ...] = ("EXPIRED", "EXPIRING_SOON", "VALID", "NO_EXPIRY")


def expiry_bucket(table: Table, today: dt.date, soon_days: int):
    """The shelf-life bucket as one SQL expression.

    Defined **once** and used by both the bucket totals and the status filter,
    so "expired" cannot mean one thing on the expiry cards and another in the
    filter that narrows the page to them.

    A row with no shelf-life date is its own bucket. It is not "valid": nothing
    is known about it, and folding it in with genuinely in-date stock would
    overstate what is safe to ship.
    """
    expiry = table.c["shelf_life_expiration_date"]
    return case(
        (expiry.is_(None), "NO_EXPIRY"),
        (expiry < today, "EXPIRED"),
        (expiry <= today + dt.timedelta(days=soon_days), "EXPIRING_SOON"),
        else_="VALID",
    )


def _expiry_conditions(table: Table, filters: ScopeFilters,
                       today: dt.date | None, soon_days: int | None) -> list[Any]:
    """The status filter as a predicate list, empty when it does not apply.

    Needs ``today`` and the horizon to mean anything, so a caller that has
    neither simply does not filter — the same "skip rather than guess" rule
    ``filter_conditions`` follows for a column a view does not carry.
    """
    statuses = [s for s in filters.expiry_statuses if s in EXPIRY_STATUSES]
    if not statuses or today is None or soon_days is None:
        return []
    if "shelf_life_expiration_date" not in table.c:
        return []
    return [expiry_bucket(table, today, soon_days).in_(statuses)]


def material_stock_totals(session: Session, filters: ScopeFilters, *,
                          today: dt.date | None = None,
                          soon_days: int | None = None) -> dict[str, Any]:
    """The four categories and their total, across everything in scope."""
    table = view(session, MATERIAL_STOCK_VIEW)
    return aggregate_totals(
        session, MATERIAL_STOCK_MEASURES, filters, None, None,
        extra_conditions=_expiry_conditions(table, filters, today, soon_days),
    )


def material_stock_by(session: Session, filters: ScopeFilters, group_by: GroupBy,
                      limit: int = MAX_ROWS,
                      sort_field: str = "total_stock",
                      *, today: dt.date | None = None,
                      soon_days: int | None = None,
                      ) -> tuple[list[dict[str, Any]], bool]:
    """Stock grouped by plant, storage location, material or material group.

    ``sort_field`` picks which of the four categories ranks the result. It
    matters because they are four different questions: "which material has the
    most blocked stock" is not "which material has the most stock", and ranking
    by the total would answer the wrong one. The caller never chooses a column
    name freely — ``StockToolInput.sort_by`` is a closed set validated by
    Pydantic before it reaches here.
    """
    table = view(session, MATERIAL_STOCK_VIEW)
    return aggregate_by(
        session, MATERIAL_STOCK_MEASURES, filters, None, None,
        group_by, limit=limit, sort_field=sort_field,
        extra_conditions=_expiry_conditions(table, filters, today, soon_days),
    )


def expiry_buckets(session: Session, filters: ScopeFilters, *, today: dt.date,
                   soon_days: int) -> list[dict[str, Any]]:
    """Stock split into Expired / Expiring soon / Valid / No expiry date.

    Bucketed in SQL rather than row by row so a large position costs one query.
    ``soon_days`` is configuration, not a constant — the business had no existing
    threshold, so it is settable and passed in rather than assumed here.

    The bucket expression itself comes from :func:`expiry_bucket` so that these
    cards and the status filter cannot disagree about what "expired" means.
    """
    table = view(session, MATERIAL_STOCK_VIEW)
    bucket = expiry_bucket(table, today, soon_days).label("code")

    statement = select(
        bucket,
        func.sum(table.c["total_stock"]).label("total_stock"),
        func.sum(table.c["unrestricted_stock"]).label("unrestricted_stock"),
        func.count().label("row_count"),
    ).select_from(table)
    conditions = filter_conditions(table, filters, None, None)
    # A status filter narrows these cards too, so the page stays internally
    # consistent: with EXPIRED selected the other three buckets read zero
    # rather than continuing to report stock the rest of the page excludes.
    conditions.extend(_expiry_conditions(table, filters, today, soon_days))
    if conditions:
        statement = statement.where(and_(*conditions))
    rows = _rows(session.execute(statement.group_by(bucket)))

    # Every bucket is reported, including the empty ones: "no expired stock" is
    # an answer worth showing, and a missing row reads as missing data.
    found = {str(row["code"]): row for row in rows}
    return [
        found.get(code, {"code": code, "total_stock": 0.0,
                         "unrestricted_stock": 0.0, "row_count": 0})
        for code in EXPIRY_STATUSES
    ]


def expiring_rows(session: Session, filters: ScopeFilters, *, today: dt.date,
                  soon_days: int, include_expired: bool = True,
                  limit: int = 50) -> tuple[list[dict[str, Any]], bool]:
    """Individual positions at or past their shelf life, soonest first."""
    table = view(session, MATERIAL_STOCK_VIEW)
    expiry = table.c["shelf_life_expiration_date"]
    statement = select(table)
    conditions = filter_conditions(table, filters, None, None)
    # Selecting NO_EXPIRY empties this table, and correctly so: it lists
    # positions at or past a shelf life, and a position without one has neither.
    # The count is still visible on the expiry card beside it.
    conditions.extend(_expiry_conditions(table, filters, today, soon_days))
    conditions.append(expiry.isnot(None))
    conditions.append(expiry <= today + dt.timedelta(days=soon_days))
    if not include_expired:
        conditions.append(expiry >= today)
    statement = statement.where(and_(*conditions)).order_by(asc(expiry))
    bounded = max(1, min(limit, MAX_ROWS))
    rows = _rows(session.execute(statement.limit(bounded + 1)))
    return rows[:bounded], len(rows) > bounded


# ---------------------------------------------------------------------------
# Credit Control
# ---------------------------------------------------------------------------
#
# ``aging_rows`` is back. It was removed with the Outstanding module in revision
# 0020 because there was no receivable to age; revision 0031 reinstated
# receivables against a source that exists, so there is again.
#
# The status and bucket expressions are **imported, not rebuilt**. They are
# generated from ``etl.credit.OVERDUE_BUCKETS`` for a given reporting date, and
# one generator shared between the reporting layer and this one is the whole
# point: the agent and the Credit Control page must not be able to disagree
# about which bucket an invoice is in, which is exactly what a second generator
# here would eventually allow.


def _credit_expressions():
    """Imported late to keep the import graph acyclic.

    ``reporting.credit`` imports the shared report helpers, and importing it at
    module scope here would pull the reporting layer into every agent import.
    The functions themselves are pure — a Table and a date in, an expression out.
    """
    from ..reporting.credit import (
        aging_bucket_expression,
        credit_status_expression,
    )

    return credit_status_expression, aging_bucket_expression


def credit_totals(session: Session, filters: ScopeFilters,
                  date_from: dt.date, date_to: dt.date, *,
                  as_on: dt.date, due_soon_days: int) -> dict[str, Any]:
    """The headline receivables figures for one reporting date.

    Outstanding counts only *positive* balances, so a data-quality problem — an
    over-adjusted invoice reading as a negative balance — cannot net off against
    real debt and understate what is owed. That is the one direction a
    receivables figure must never be wrong in.
    """
    table = view(session, CREDIT_INVOICE_VIEW)
    conditions = filter_conditions(table, filters, date_from, date_to,
                                   date_column="invoice_date")
    open_invoice = table.c.balance_amount > 0
    overdue = and_(open_invoice, table.c.due_date < as_on)
    due_soon = and_(open_invoice, table.c.due_date >= as_on,
                    table.c.due_date <= as_on + dt.timedelta(days=due_soon_days))

    statement = select(
        func.count().label("invoice_count"),
        func.sum(table.c.invoice_value).label("invoice_value"),
        func.sum(table.c.net_invoice_amount).label("net_invoice_amount"),
        func.sum(table.c.payment_amount).label("payment_amount"),
        func.sum(case((open_invoice, table.c.balance_amount), else_=0))
            .label("outstanding_amount"),
        func.sum(case((open_invoice, 1), else_=0)).label("open_invoice_count"),
        func.sum(case((overdue, table.c.balance_amount), else_=0))
            .label("overdue_amount"),
        func.sum(case((overdue, 1), else_=0)).label("overdue_invoice_count"),
        func.sum(case((due_soon, table.c.balance_amount), else_=0))
            .label("due_soon_amount"),
        func.sum(case((due_soon, 1), else_=0)).label("due_soon_invoice_count"),
    ).select_from(table)
    if conditions:
        statement = statement.where(and_(*conditions))
    row = session.execute(statement).one()
    totals = dict(row._mapping)
    # Reported as a magnitude, exactly as ``reporting.credit`` does it. The
    # column is stored signed because that is what makes the balance a plain sum,
    # but an assistant that answered "-1,100,000 BDT paid" would be stating the
    # opposite of what happened. The two surfaces flip it the same way so a
    # question asked in chat and the same figure read off the page agree.
    if totals.get("payment_amount") is not None:
        totals["payment_amount"] = -totals["payment_amount"]
    return totals


def credit_aging_rows(session: Session, filters: ScopeFilters,
                      date_from: dt.date, date_to: dt.date, *,
                      as_on: dt.date) -> list[dict[str, Any]]:
    """Outstanding money per aging bucket, open invoices only.

    Every bucket the module declares is returned, in order, including the ones
    holding nothing: an empty bucket is a fact about the portfolio, and a gap in
    the list reads as a rendering fault instead.
    """
    from ..etl import credit as credit_rules

    _status, bucket_expression = _credit_expressions()
    table = view(session, CREDIT_INVOICE_VIEW)
    bucket = bucket_expression(table, as_on)
    conditions = filter_conditions(table, filters, date_from, date_to,
                                   date_column="invoice_date")
    conditions.append(table.c.balance_amount > 0)

    found = {
        row["aging_bucket"]: row
        for row in _rows(session.execute(
            select(
                bucket.label("aging_bucket"),
                func.count().label("invoice_count"),
                func.sum(table.c.balance_amount).label("outstanding_amount"),
            ).select_from(table).where(and_(*conditions)).group_by(bucket)
        ))
    }
    return [
        {
            "aging_bucket": code,
            "invoice_count": found[code]["invoice_count"] if code in found else 0,
            "outstanding_amount": (
                found[code]["outstanding_amount"] if code in found else 0
            ),
        }
        for code in credit_rules.AGING_BUCKETS
    ]


def overdue_customers(session: Session, filters: ScopeFilters,
                      date_from: dt.date, date_to: dt.date, *,
                      as_on: dt.date, limit: int = MAX_ROWS
                      ) -> tuple[list[dict[str, Any]], bool]:
    """Customers ranked by how much of their debt is past due."""
    table = view(session, CREDIT_INVOICE_VIEW)
    conditions = filter_conditions(table, filters, date_from, date_to,
                                   date_column="invoice_date")
    conditions.extend([table.c.balance_amount > 0, table.c.due_date < as_on])

    bounded = max(1, min(limit, MAX_ROWS))
    rows = _rows(session.execute(
        select(
            table.c.customer_code,
            table.c.customer_name,
            func.sum(table.c.balance_amount).label("overdue_amount"),
            func.count().label("invoice_count"),
            func.min(table.c.due_date).label("oldest_due_date"),
        ).select_from(table).where(and_(*conditions))
        .group_by(table.c.customer_code, table.c.customer_name)
        .order_by(func.sum(table.c.balance_amount).desc())
        .limit(bounded + 1)
    ))
    return rows[:bounded], len(rows) > bounded


# No ``latest_stock_date``. It read the old fact's date column to answer "as of
# when?", and the Material Transaction Data has no such column: the position is
# current by definition. What a reader actually wants — when the figures last
# changed — is the import batch's own timestamp, which the upload history already
# records against the file it came from.


__all__ = [
    "MeasureSet",
    "SALES_MEASURES",
    "MATERIAL_STOCK_MEASURES",
    "TARGET_MEASURES",
    "GROUP_COLUMNS",
    "FILTER_COLUMNS",
    "view",
    "clear_view_cache",
    "filter_conditions",
    "unsupported_filters",
    "aggregate_totals",
    "aggregate_by",
    "volume_total",
    "volume_by_group",
    "time_series",
    "detail_rows",
    "sales_kpis",
    "compare_totals",
    "target_vs_actual",
    "material_stock_totals",
    "material_stock_by",
    "expiry_bucket",
    "EXPIRY_STATUSES",
    "DERIVED_FILTERS",
    "expiry_buckets",
    "expiring_rows",
    "MAX_ROWS",
    "STOCK_UNIT",
    "SALES_VIEW",
    "MATERIAL_STOCK_VIEW",
    "TARGET_VIEW",
]
