"""Monthly seasonality: how a year's volume is spread across its months.

**A twelfth each is almost always wrong, and this module exists to say so.**
Agrochemical demand follows the spray window: October can be fourteen per cent
of the year and June four. Dividing a country target equally would set a
territory an October target it cannot fail and a June target it cannot meet, and
the sales force would be measured against both.

So the weights come from what the business actually sold, month by month, over
the same basis years the rest of the engine reads — and where they do not exist,
the fallback is **used and named**, never disguised as a seasonal profile.

The fallback chain, most specific first:

``MATERIAL``
    This material's own monthly pattern over the basis years. What a planner
    would draw by hand.
``BRAND``
    Its brand's pattern, where the material itself has too little history. A
    new pack of an established product sells when that product sells.
``SCOPE``
    The whole plan scope's pattern, where the brand has none either. Still a
    real seasonal shape, just a coarser one.
``EQUAL``
    A twelfth each. Reached only when the scope has no monthly history at all,
    and always reported as a fallback, because a flat profile is the one shape
    the business is certain not to have.

**A month with no sales gets a weight of zero, not a floor.** If nothing has
ever been sold in June, the honest projection is that June sells little — and
where *every* month is zero the chain has already moved to the next fallback, so
a zero here is a measured zero rather than an absence.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ..ai import queries
from ..ai.schemas import ScopeFilters
from ..etl.calendar import FinancialYearConfig
from . import history as history_module

#: Where a set of monthly weights came from.
MATERIAL = "MATERIAL"
BRAND = "BRAND"
SCOPE = "SCOPE"
EQUAL = "EQUAL"

#: How many months of the basis window must carry sales before a pattern is
#: trusted at that level.
#:
#: Three, so a single invoice cannot become "the seasonal profile". Below this
#: the chain falls through to the next, coarser source, which is more likely to
#: be right than a shape drawn from one month.
MIN_MONTHS_FOR_PATTERN = 3


@dataclass(frozen=True)
class Seasonality:
    """Monthly weights for one material, and where they came from.

    ``weights`` is keyed by the plan's month labels (``YYYY-MM``) and sums to 1
    within rounding — it is a shape, not a volume. The distributor is what turns
    it into volumes that sum exactly.
    """

    weights: dict[str, float]
    source: str
    #: Months in the basis window that carried any sale, at the level used.
    months_observed: int

    @property
    def is_fallback(self) -> bool:
        """True for anything but a real pattern at the requested grain."""
        return self.source != MATERIAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": self.weights,
            "source": self.source,
            "months_observed": self.months_observed,
            "is_fallback": self.is_fallback,
        }


def plan_months(financial_year: str, target_period: str,
                config: FinancialYearConfig | None = None) -> list[str]:
    """The ``YYYY-MM`` months a plan covers, in order.

    Derived from the configured financial year rather than listed: ``Q1`` is
    July-September under a July start and January-March under a January one, and
    a hard-coded list would be wrong for one of them.
    """
    config = config or FinancialYearConfig.from_settings()
    start_year = history_module._start_year(financial_year)
    first = dt.date(start_year, config.start_month, 1)

    offsets = range(12)
    if target_period != "FY":
        try:
            quarter = int(target_period[1:])
        except (ValueError, IndexError):
            quarter = 1
        offsets = range((quarter - 1) * 3, (quarter - 1) * 3 + 3)

    months = []
    for offset in offsets:
        month_index = (config.start_month - 1 + offset) % 12 + 1
        year = first.year + (config.start_month - 1 + offset) // 12
        months.append(f"{year:04d}-{month_index:02d}")
    return months


def _monthly_volume(session: Session, filters: ScopeFilters,
                    windows: Sequence[tuple[dt.date, dt.date]], *,
                    material_code: str | None = None,
                    material_brand: str | None = None,
                    ) -> dict[int, float]:
    """Volume per calendar month number (1-12) over the basis windows.

    Grouped by month *number* rather than by date, because a seasonal shape is
    about the time of year and the point of reading two years is to average two
    Octobers together.
    """
    table = queries.view(session, queries.SALES_VIEW)
    if "full_date" not in table.c:
        return {}

    # ``strftime('%m', ...)`` is SQLite-only and ``EXTRACT`` is not SQLite, so
    # the month is bucketed in Python from a grouped date rather than in SQL.
    # The grouping is by date, which both dialects do identically, and the
    # result set is at most a few hundred rows per year.
    statement = (
        select(table.c.full_date.label("day"),
               func.sum(table.c.volume).label("volume"))
        .select_from(table)
        .group_by(table.c.full_date)
    )

    conditions: list[Any] = []
    for date_from, date_to in windows:
        conditions.append(
            and_(table.c.full_date >= date_from, table.c.full_date <= date_to))
    window_clause = conditions[0]
    for extra in conditions[1:]:
        window_clause = window_clause | extra

    scope = queries.filter_conditions(table, filters)
    if material_code is not None and "material_code" in table.c:
        scope.append(table.c.material_code == material_code)
    if material_brand is not None and "material_brand" in table.c:
        scope.append(table.c.material_brand == material_brand)
    statement = statement.where(and_(window_clause, *scope))

    totals: dict[int, float] = {}
    for row in session.execute(statement):
        if row.day is None or row.volume is None:
            continue
        day = row.day if isinstance(row.day, dt.date) else dt.date.fromisoformat(
            str(row.day)[:10])
        totals[day.month] = totals.get(day.month, 0.0) + float(row.volume)
    return totals


def for_material(session: Session, filters: ScopeFilters, *,
                 material_code: str, material_brand: str | None,
                 months: Sequence[str],
                 basis_years: Sequence[str],
                 config: FinancialYearConfig | None = None) -> Seasonality:
    """Monthly weights for one material, walking the fallback chain."""
    config = config or FinancialYearConfig.from_settings()
    windows = [history_module.year_window(year, config) for year in basis_years]

    attempts: list[tuple[str, dict[int, float]]] = [
        (MATERIAL, _monthly_volume(session, filters, windows,
                                   material_code=material_code)),
    ]
    if material_brand:
        attempts.append(
            (BRAND, _monthly_volume(session, filters, windows,
                                    material_brand=material_brand)))
    attempts.append((SCOPE, _monthly_volume(session, filters, windows)))

    for source, totals in attempts:
        observed = sum(1 for value in totals.values() if value > 0)
        if observed >= MIN_MONTHS_FOR_PATTERN:
            return Seasonality(
                weights=_weights_for(months, totals),
                source=source, months_observed=observed,
            )

    return equal(months)


def equal(months: Sequence[str]) -> Seasonality:
    """A twelfth each, named as the fallback it is.

    Exposed rather than inlined so a caller that has switched seasonality *off*
    reaches the same object a caller with no history reaches, and both are
    reported the same way on the screen.
    """
    if not months:
        return Seasonality(weights={}, source=EQUAL, months_observed=0)
    share = 1.0 / len(months)
    return Seasonality(weights={month: share for month in months},
                       source=EQUAL, months_observed=0)


def _weights_for(months: Sequence[str],
                 by_month_number: dict[int, float]) -> dict[str, float]:
    """Map calendar-month totals onto the plan's months, normalised to 1.

    A plan month with no historical volume gets zero, deliberately: the fallback
    chain has already established that *some* month in this window sold
    something, so a zero here is a measured zero rather than missing data.
    """
    raw = {month: max(0.0, by_month_number.get(int(month[-2:]), 0.0))
           for month in months}
    total = sum(raw.values())
    if total <= 0:
        return {month: 1.0 / len(months) for month in months}
    return {month: value / total for month, value in raw.items()}


__all__ = [
    "MATERIAL",
    "BRAND",
    "SCOPE",
    "EQUAL",
    "MIN_MONTHS_FOR_PATTERN",
    "Seasonality",
    "plan_months",
    "for_material",
    "equal",
]
