"""Historical sales analysis: the basis a target is generated from.

Before any volume is allocated, the engine has to know what actually happened.
This module reads two financial years of real sales per material and derives the
four numbers a planner uses to judge a target: last year, the year before,
the growth between them, and each material's share of the later year.

**It reads the same view, through the same scope layer, as every other report.**
``vw_sales_detail`` filtered by ``queries.filter_conditions`` with the caller's
scope merged in by ``PermissionFilter.enforce`` — so a regional manager's
allocation basis is their region's history, and the number here and the number
on the Sales page cannot disagree.

**The measure is volume, because volume is what gets allocated.** A country
target states a Target Volume, the engine distributes volume, and a basis
measured in taka would be a different quantity from the thing it is a basis
for. ``fact_sales.volume`` is the figure the source file stated, unit-free, and
is summed exactly as the Sales page sums it.

**A sum over a column that is sometimes NULL is a trap, and this module refuses
to fall into it.** ``SUM(volume)`` silently skips a row whose volume the source
did not state, so a material with a hundred sales lines and ten blank volumes
reports the other ninety as if they were all of it. Every year therefore carries
``rows`` and ``rows_without_volume`` beside its total, and a material with any
blank volume is marked incomplete — its basis is still shown, because it is the
best that exists, but it is never presented as a complete history.

**No history is not a history of zero.** A material with no sales in a year has
``volume=None``, not ``0.0``: the first means the business did not sell it (or
did not record it), the second is a measurement. Growth, average and
contribution are all ``None`` wherever they cannot be computed, and the basis
says ``NO_HISTORY`` rather than proposing a distribution built on nothing.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from ..ai import queries
from ..ai.permission_filter import PermissionFilter, UserContext
from ..ai.schemas import ScopeFilters
from ..database.models import DimMaterial
from ..database.models_target import TargetCountryLine, TargetPlan
from ..etl.calendar import FinancialYearConfig

#: How many financial years of actuals a basis is built from.
#:
#: Two, and that is a business decision rather than a technical one: one year
#: cannot show a trend, and three would weight a year the market has moved past.
#: A plan may state its own years in ``basis_financial_years``; this is what it
#: falls back to.
BASIS_YEARS = 2

#: Growth above which a material is judged to be moving fast enough that its
#: trend, rather than its two-year average, should drive the allocation.
#:
#: A planning heuristic, not a fact about the data — which is why it produces a
#: *suggestion* a planner can override, and why the suggestion is named in the
#: response rather than silently applied. The allocation engine reads it too, so
#: it is declared once here.
GROWTH_GUIDANCE_PERCENT = 15.0


class Basis:
    """Which rule the allocation engine should follow for one material."""

    #: Sold in both years, growing inside guidance: the average is the best
    #: single estimate.
    TWO_YEAR_AVERAGE = "TWO_YEAR_AVERAGE"
    #: Growing faster than guidance: the average would understate it.
    GROWTH_WEIGHTED = "GROWTH_WEIGHTED"
    #: Sold in the later year only. There is no trend to weight, so the
    #: allocation seeds it from its brand's contribution instead.
    NEW_MATERIAL = "NEW_MATERIAL"
    #: No sales in either year. Nothing here can propose a distribution, and
    #: saying so is the honest answer — the target still exists, it simply has
    #: no history to be derived from.
    NO_HISTORY = "NO_HISTORY"

    ALL = (TWO_YEAR_AVERAGE, GROWTH_WEIGHTED, NEW_MATERIAL, NO_HISTORY)


@dataclass(frozen=True)
class YearVolume:
    """One material's volume in one financial year, and how sound that figure is.

    ``volume`` is ``None`` when the material has no sales rows at all in the
    year — distinct from ``0.0``, which would claim the business measured a
    volume of zero.
    """

    financial_year: str
    volume: float | None
    rows: int
    rows_without_volume: int

    @property
    def complete(self) -> bool:
        """False when any contributing sales row stated no volume."""
        return self.rows_without_volume == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "financial_year": self.financial_year,
            "volume": self.volume,
            "rows": self.rows,
            "rows_without_volume": self.rows_without_volume,
            "complete": self.complete,
        }


@dataclass(frozen=True)
class MaterialHistory:
    """One row of the Allocation Basis table."""

    material_code: str
    material_description: str | None
    material_brand: str | None
    material_group_name: str | None
    #: Oldest first, so ``years[-1]`` is always the most recent basis year.
    years: tuple[YearVolume, ...]
    growth_percent: float | None
    average_volume: float | None
    contribution_percent: float | None
    current_target_volume: float | None
    target_growth_percent: float | None
    basis: str

    @property
    def complete(self) -> bool:
        return all(year.complete for year in self.years)

    def to_dict(self) -> dict[str, Any]:
        return {
            "material_code": self.material_code,
            "material_description": self.material_description,
            "material_brand": self.material_brand,
            "material_group_name": self.material_group_name,
            "years": [year.to_dict() for year in self.years],
            # Flattened for the table, which has one column per basis year.
            "volumes": [year.volume for year in self.years],
            "growth_percent": self.growth_percent,
            "average_volume": self.average_volume,
            "contribution_percent": self.contribution_percent,
            "current_target_volume": self.current_target_volume,
            "target_growth_percent": self.target_growth_percent,
            "basis": self.basis,
            "complete": self.complete,
        }


# ---------------------------------------------------------------------------
# The basis years
# ---------------------------------------------------------------------------


def _start_year(financial_year: str) -> int:
    """``FY 2026-27`` -> 2026. The label is the only place the year is written.

    Parsed rather than stored: ``financial_year`` is produced by
    :meth:`FinancialYearConfig.label`, so its shape follows the configured
    prefix and start month, and a second column holding the integer would be a
    copy that could disagree with it.
    """
    match = re.search(r"(\d{4})", financial_year or "")
    if not match:
        raise ValueError(
            f"cannot read a starting year from financial year {financial_year!r}"
        )
    return int(match.group(1))


def basis_years(plan: TargetPlan, config: FinancialYearConfig | None = None,
                count: int = BASIS_YEARS) -> list[str]:
    """The financial years a plan's basis is read from, oldest first.

    The plan's own ``basis_financial_years`` wins where it is set — "two years
    back" is a planning decision, and a plan built on different years has to be
    able to say so. Otherwise the ``count`` years immediately before the plan's
    own are derived from the configured calendar, never from a hard-coded list:
    a deployment starting its year in January labels them differently.
    """
    stated = (plan.basis_financial_years or "").strip()
    if stated:
        return [part.strip() for part in stated.split(",") if part.strip()]

    config = config or FinancialYearConfig.from_settings()
    start = _start_year(plan.financial_year)
    return [
        config.label(dt.date(start - offset, config.start_month, 1))
        for offset in range(count, 0, -1)
    ]


def year_window(financial_year: str,
                config: FinancialYearConfig | None = None,
                ) -> tuple[dt.date, dt.date]:
    """The first and last day of a financial year, from the configured calendar."""
    config = config or FinancialYearConfig.from_settings()
    anchor = dt.date(_start_year(financial_year), config.start_month, 1)
    return config.year_start(anchor), config.year_end(anchor)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def plan_filters(plan: TargetPlan, user: UserContext,
                 session: Session) -> ScopeFilters:
    """The plan's own scope, narrowed by whatever the caller may see.

    Two narrowings, and they compose the way the permission chain says they
    must: the plan fixes a company, business unit and sales line, and
    ``PermissionFilter.enforce`` then merges the user's data scope on top. A
    lower layer may narrow, never widen — so a regional manager reading a
    company-wide plan gets their region's history, not the company's.
    """
    filters = ScopeFilters(
        company_codes=[plan.company_code],
        business_unit_codes=[plan.bu_code],
        sales_line_codes=[plan.sales_line_code],
    )
    return PermissionFilter(session, user).enforce(filters)


def _volume_by_material(session: Session, filters: ScopeFilters,
                        window: tuple[dt.date, dt.date]) -> dict[str, YearVolume]:
    """One financial year's volume per material, with its blank-volume count.

    Written here rather than through ``queries.aggregate_by`` for one column:
    the count of contributing rows that stated no volume. ``SUM`` skips those
    silently, and this module's whole discipline is that a total which quietly
    omits part of its input is not reported as a total. Everything else — the
    view, the scope predicates, the void filter the view itself applies — is the
    shared machinery, unchanged.
    """
    table = queries.view(session, queries.SALES_VIEW)
    date_from, date_to = window

    # ``SUM(CASE WHEN volume IS NULL THEN 1 ELSE 0 END)`` rather than
    # ``COUNT(*) FILTER (WHERE ...)``: the FILTER clause needs PostgreSQL or a
    # recent SQLite, and this schema is written to run unchanged on both.
    blank = func.sum(case((table.c.volume.is_(None), 1), else_=0))

    statement = (
        select(
            table.c.material_code.label("material_code"),
            func.sum(table.c.volume).label("volume"),
            func.count().label("rows"),
            blank.label("rows_without_volume"),
        )
        .select_from(table)
        .group_by(table.c.material_code)
    )
    conditions = queries.filter_conditions(table, filters, date_from, date_to)
    if conditions:
        statement = statement.where(and_(*conditions))

    result: dict[str, YearVolume] = {}
    for row in session.execute(statement):
        code = row.material_code
        if not code:
            # A sale whose material the file did not state cannot contribute to
            # any material's basis. Skipped rather than bucketed under a
            # placeholder, which would invent a material.
            continue
        result[code] = YearVolume(
            financial_year="",  # filled in by the caller, which knows the label
            volume=float(row.volume) if row.volume is not None else None,
            rows=int(row.rows or 0),
            rows_without_volume=int(row.rows_without_volume or 0),
        )
    return result


def analyse(session: Session, user: UserContext, *, plan: TargetPlan,
            version_id: int) -> dict[str, Any]:
    """The whole Historical Analysis screen: rows, totals and what they imply."""
    config = FinancialYearConfig.from_settings()
    years = basis_years(plan, config)
    filters = plan_filters(plan, user, session)

    by_year: list[dict[str, YearVolume]] = []
    for financial_year in years:
        window = year_window(financial_year, config)
        raw = _volume_by_material(session, filters, window)
        by_year.append({
            code: YearVolume(
                financial_year=financial_year,
                volume=entry.volume,
                rows=entry.rows,
                rows_without_volume=entry.rows_without_volume,
            )
            for code, entry in raw.items()
        })

    targets = {
        line.material_code: float(line.target_volume or 0)
        for line in session.execute(
            select(TargetCountryLine).where(
                TargetCountryLine.version_id == version_id)
        ).scalars()
    }

    # Every material with history, plus every material the target already names.
    # The second half matters: a material somebody has set a target for with no
    # history behind it is exactly what a planner opens this screen to find.
    codes = sorted({code for year in by_year for code in year} | set(targets))
    attributes = _material_attributes(session, codes)

    latest_total = sum(
        year.volume or 0
        for year in (by_year[-1] if by_year else {}).values()
    )

    rows = [
        _build_row(code, years, by_year, targets, attributes, latest_total)
        for code in codes
    ]
    rows.sort(key=lambda row: (row.years[-1].volume or 0), reverse=True)

    return {
        "basis_years": years,
        "rows": [row.to_dict() for row in rows],
        "totals": _totals(rows, years),
        "notes": notes(rows, years),
        "growth_guidance_percent": GROWTH_GUIDANCE_PERCENT,
    }


def _material_attributes(session: Session,
                         codes: list[str]) -> dict[str, DimMaterial]:
    if not codes:
        return {}
    return {
        material.material_code: material
        for material in session.execute(
            select(DimMaterial).where(DimMaterial.material_code.in_(codes))
        ).scalars()
    }


def _build_row(code: str, years: list[str],
               by_year: list[dict[str, YearVolume]],
               targets: dict[str, float],
               attributes: dict[str, DimMaterial],
               latest_total: float) -> MaterialHistory:
    volumes = tuple(
        by_year[index].get(code)
        or YearVolume(financial_year=label, volume=None, rows=0,
                      rows_without_volume=0)
        for index, label in enumerate(years)
    )
    earliest = volumes[0].volume if volumes else None
    latest = volumes[-1].volume if volumes else None

    growth = _percent_change(earliest, latest)
    measured = [year.volume for year in volumes if year.volume is not None]
    average = sum(measured) / len(measured) if measured else None
    contribution = (
        (latest / latest_total) * 100
        if latest is not None and latest_total else None
    )
    target = targets.get(code)
    target_growth = _percent_change(latest, target)

    material = attributes.get(code)
    return MaterialHistory(
        material_code=code,
        material_description=material.material_description if material else None,
        material_brand=material.material_brand if material else None,
        material_group_name=material.material_group_name if material else None,
        years=volumes,
        growth_percent=growth,
        average_volume=average,
        contribution_percent=contribution,
        current_target_volume=target,
        target_growth_percent=target_growth,
        basis=_suggest_basis(earliest, latest, growth),
    )


def _percent_change(before: float | None, after: float | None) -> float | None:
    """``(after - before) / before``, or ``None`` where it cannot be computed.

    ``None`` for a missing figure and ``None`` for a zero denominator alike:
    growth from nothing is not infinite, it is undefined, and the screen renders
    ``n/a`` rather than a number nobody can act on.
    """
    if before is None or after is None or before == 0:
        return None
    return ((after - before) / before) * 100


def _suggest_basis(earliest: float | None, latest: float | None,
                   growth: float | None) -> str:
    """Which rule to allocate this material by. A suggestion, never applied here."""
    if latest is None and earliest is None:
        return Basis.NO_HISTORY
    if earliest is None or earliest == 0:
        return Basis.NEW_MATERIAL
    if growth is not None and growth > GROWTH_GUIDANCE_PERCENT:
        return Basis.GROWTH_WEIGHTED
    return Basis.TWO_YEAR_AVERAGE


def _totals(rows: list[MaterialHistory], years: list[str]) -> dict[str, Any]:
    """Column totals, and how much of the basis is sound.

    A year's total is the sum of what was measured. Unlike the country target's
    totals it is *not* suppressed when part of it is missing — the difference is
    that a missing history is the normal state of a new material and suppressing
    the total would leave the screen blank for every real plan, whereas a
    missing conversion factor makes a *derived* figure unknowable. What the
    screen must not do is imply the total is complete, which is what
    ``incomplete_materials`` and the notes are for.
    """
    per_year = []
    for index, label in enumerate(years):
        measured = [row.years[index].volume for row in rows
                    if row.years[index].volume is not None]
        per_year.append({
            "financial_year": label,
            "volume": sum(measured) if measured else None,
            "materials_with_volume": len(measured),
        })
    return {
        "material_count": len(rows),
        "years": per_year,
        "target_volume": sum(row.current_target_volume or 0 for row in rows),
        "with_history": sum(1 for row in rows if row.basis != Basis.NO_HISTORY),
        "incomplete_materials": [row.material_code for row in rows
                                 if not row.complete],
        "without_history": [row.material_code for row in rows
                            if row.basis == Basis.NO_HISTORY],
    }


def notes(rows: list[MaterialHistory], years: list[str]) -> list[str]:
    """What a planner has to know before trusting this basis."""
    out: list[str] = []
    if not rows:
        out.append(
            "No sales history and no country target yet for this plan's scope. "
            "The allocation basis is read from actual sales, so it stays empty "
            "until a sales file is loaded — nothing here is estimated."
        )
        return out

    if not any(row.basis != Basis.NO_HISTORY for row in rows):
        out.append(
            f"No sales were recorded in {' or '.join(years)} for this plan's "
            f"scope, so no material has a basis. Load the sales history for "
            f"those years before generating an allocation."
        )

    incomplete = [row.material_code for row in rows if not row.complete]
    if incomplete:
        out.append(
            f"{len(incomplete)} material(s) have sales rows that state no "
            f"volume, so their history is understated: "
            f"{_sample(incomplete)}. The figure shown is the sum of the rows "
            f"that did state one."
        )

    missing = [row.material_code for row in rows if row.basis == Basis.NO_HISTORY]
    if missing and len(missing) != len(rows):
        out.append(
            f"{len(missing)} material(s) on this target have no sales in either "
            f"basis year: {_sample(missing)}. An allocation cannot distribute "
            f"them by history, and will seed them from their brand instead."
        )
    return out


def _sample(codes: list[str], limit: int = 5) -> str:
    shown = ", ".join(codes[:limit])
    remaining = len(codes) - limit
    return f"{shown} and {remaining} more" if remaining > 0 else shown


__all__ = [
    "BASIS_YEARS",
    "GROWTH_GUIDANCE_PERCENT",
    "Basis",
    "YearVolume",
    "MaterialHistory",
    "basis_years",
    "year_window",
    "plan_filters",
    "analyse",
    "notes",
]
