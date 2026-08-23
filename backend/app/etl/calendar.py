"""Financial calendar and ``dim_date`` generation.

The financial year is driven entirely by
``Settings.financial_year_start_month``; no calendar-year assumption is baked
into the code. With a start month of 7 (July), 2026-08-15 falls in
``FY 2026-27``; with a start month of 1 the same date falls in ``FY 2026``.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterator

from ..config import get_settings


@dataclass(frozen=True)
class FinancialYearConfig:
    """Configurable financial-year definition."""

    start_month: int = 7
    label_prefix: str = "FY"

    def __post_init__(self) -> None:
        if not 1 <= self.start_month <= 12:
            raise ValueError(
                f"financial_year_start_month must be 1-12, got {self.start_month}"
            )

    @classmethod
    def from_settings(cls) -> "FinancialYearConfig":
        settings = get_settings()
        return cls(
            start_month=settings.financial_year_start_month,
            label_prefix=settings.financial_year_label_prefix,
        )

    def start_year_of(self, value: date) -> int:
        """Calendar year in which the financial year containing ``value`` began."""
        return value.year if value.month >= self.start_month else value.year - 1

    def label(self, value: date) -> str:
        """``FY 2026-27`` for a July start, ``FY 2026`` for a January start."""
        start_year = self.start_year_of(value)
        if self.start_month == 1:
            return f"{self.label_prefix} {start_year}"
        return f"{self.label_prefix} {start_year}-{(start_year + 1) % 100:02d}"

    def month_number(self, value: date) -> int:
        """1-12, where 1 is the first month of the financial year."""
        return (value.month - self.start_month) % 12 + 1

    def quarter(self, value: date) -> int:
        """1-4 within the financial year."""
        return (self.month_number(value) - 1) // 3 + 1

    def year_start(self, value: date) -> date:
        return date(self.start_year_of(value), self.start_month, 1)

    def year_end(self, value: date) -> date:
        start = self.year_start(value)
        end_year = start.year + 1 if self.start_month > 1 else start.year
        end_month = self.start_month - 1 if self.start_month > 1 else 12
        return date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])

    def is_year_end(self, value: date) -> bool:
        return value == self.year_end(value)


def to_date_id(value: date) -> int:
    """``date(2026, 8, 15)`` -> ``20260815``."""
    return value.year * 10000 + value.month * 100 + value.day


def from_date_id(date_id: int) -> date:
    """``20260815`` -> ``date(2026, 8, 15)``."""
    return date(date_id // 10000, (date_id // 100) % 100, date_id % 100)


def build_date_row(value: date, config: FinancialYearConfig) -> dict:
    """One fully-populated ``dim_date`` row."""
    last_day_of_month = calendar.monthrange(value.year, value.month)[1]
    quarter = (value.month - 1) // 3 + 1
    iso_year, iso_week, _ = value.isocalendar()
    return {
        "date_id": to_date_id(value),
        "full_date": value,
        "day": value.day,
        "month": value.month,
        "month_name": calendar.month_name[value.month],
        "month_number": value.month,
        "quarter": quarter,
        "quarter_name": f"Q{quarter}",
        "year": value.year,
        "week": iso_week,
        "week_name": f"{iso_year}-W{iso_week:02d}",
        "financial_year": config.label(value),
        "financial_month": config.month_number(value),
        "financial_quarter": config.quarter(value),
        "is_month_end": value.day == last_day_of_month,
        "is_quarter_end": value.month in (3, 6, 9, 12) and value.day == last_day_of_month,
        "is_year_end": value.month == 12 and value.day == 31,
        "is_financial_year_end": config.is_year_end(value),
    }


def iter_dates(start: date, end: date) -> Iterator[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def build_date_rows(start: date, end: date,
                    config: FinancialYearConfig | None = None) -> list[dict]:
    """Every ``dim_date`` row in ``[start, end]`` inclusive."""
    if end < start:
        raise ValueError(f"end date {end} precedes start date {start}")
    config = config or FinancialYearConfig.from_settings()
    return [build_date_row(d, config) for d in iter_dates(start, end)]


def populate_dim_date(session, start: date, end: date,
                      config: FinancialYearConfig | None = None) -> int:
    """Insert missing ``dim_date`` rows for the range. Returns rows inserted.

    Idempotent: dates already present are left untouched, so widening the range
    later is safe and never disturbs facts that reference existing ``date_id``s.
    """
    from sqlalchemy import select

    from ..database.models_warehouse import DimDate

    rows = build_date_rows(start, end, config)
    existing = set(
        session.execute(
            select(DimDate.date_id).where(
                DimDate.date_id.between(to_date_id(start), to_date_id(end))
            )
        ).scalars()
    )
    new_rows = [r for r in rows if r["date_id"] not in existing]
    if new_rows:
        session.execute(DimDate.__table__.insert(), new_rows)
    return len(new_rows)


def ensure_dates_exist(session, dates: set[date],
                       config: FinancialYearConfig | None = None) -> int:
    """Make sure every date in ``dates`` has a ``dim_date`` row.

    Called by the ETL so that a transaction never fails purely because the date
    dimension had not been extended far enough.
    """
    from sqlalchemy import select

    from ..database.models_warehouse import DimDate

    if not dates:
        return 0
    config = config or FinancialYearConfig.from_settings()
    wanted = {to_date_id(d): d for d in dates}
    existing = set(
        session.execute(
            select(DimDate.date_id).where(DimDate.date_id.in_(list(wanted)))
        ).scalars()
    )
    missing = [build_date_row(d, config) for i, d in wanted.items() if i not in existing]
    if missing:
        session.execute(DimDate.__table__.insert(), missing)
    return len(missing)


__all__ = [
    "FinancialYearConfig",
    "to_date_id",
    "from_date_id",
    "build_date_row",
    "build_date_rows",
    "populate_dim_date",
    "ensure_dates_exist",
]
