"""Target period resolution: a month and a financial year become a date.

Every other transaction dataset carries a real date, so ``dim_date`` is reached
by parsing one column. A target does not: it is set for *a month of a financial
year*, and the Target file states exactly that. This module turns that pair into
the first day of the month, which is what anchors the target row in ``dim_date``
and therefore in every report that joins through it.

Two rules shape the whole module.

**Never guess.** A month is accepted in the forms an operator or an export can
be relied on to produce — ``2026-08``, ``August``, ``Aug``, ``8``, or a real
date the spreadsheet already typed as one — and anything else is rejected with a
message naming those forms. There is no fall-through that picks a plausible
month out of an unrecognised string.

**Never let the two disagree.** When the month names its own year, that year is
checked against the financial year on the same row; when it does not, the year
is derived from the financial year. A row saying ``2026-03`` under
``FY 2026-27`` is rejected rather than silently filed under either reading — the
financial year is configuration (``FINANCIAL_YEAR_START_MONTH``), so with a July
start March 2026 belongs to FY 2025-26 and the row is simply wrong.

The resolved values are also **canonical**, which is what makes the business key
work: ``August`` + ``2026-27`` and ``2026-08`` + ``FY 2026-27`` are the same
target, and both resolve to ``2026-08`` + ``FY 2026-27`` before the key is
built, so two files spelling the period differently update one row instead of
creating two.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..utils.text import is_blank, normalize_text
from . import errors
from .calendar import FinancialYearConfig
from .errors import ErrorSpec

#: ``2026-08``, ``2026/8``. The only numeric form that names its own year.
_YEAR_MONTH_RE = re.compile(r"^(\d{4})[-/](\d{1,2})$")
#: ``FY 2026-27``, ``FY2026-27``, ``2026-27``, ``2026-2027``.
_FY_RANGE_RE = re.compile(r"^(?:FY[\s-]*)?(\d{4})\s*[-/]\s*(\d{2}|\d{4})$", re.IGNORECASE)
#: ``FY 2026``, ``2026`` — only meaningful when the financial year is the
#: calendar year.
_FY_SINGLE_RE = re.compile(r"^(?:FY[\s-]*)?(\d{4})$", re.IGNORECASE)

#: English month names and their three-letter abbreviations. Deliberately not
#: locale-driven: the source files are English-headed exports, and a locale that
#: changed under the process would change what a stored key means.
_MONTH_NAMES: dict[str, int] = {
    **{calendar.month_name[m].lower(): m for m in range(1, 13)},
    **{calendar.month_abbr[m].lower(): m for m in range(1, 13)},
}

_MONTH_FORMS = ("YYYY-MM (2026-08)", "a month name (August, Aug)",
                "a month number 1-12", "a date the spreadsheet typed as one")
_FY_FORMS = ("FY 2026-27", "2026-27", "2026-2027")


@dataclass(frozen=True)
class PeriodResult:
    """A resolved target period, or the reason it could not be resolved."""

    #: Canonical values merged into the cleaned record when ``ok``.
    values: dict[str, Any] = field(default_factory=dict)
    error: ErrorSpec | None = None
    message: str | None = None
    field_name: str | None = None
    field_value: Any = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _fail(error: ErrorSpec, message: str, field_name: str,
          field_value: Any) -> PeriodResult:
    return PeriodResult(error=error, message=message, field_name=field_name,
                        field_value=field_value)


def parse_month(value: Any) -> tuple[int | None, int] | None:
    """``(year_or_None, month)`` from a target-month value, or ``None``.

    ``year`` is ``None`` for the forms that name only a month — the caller
    derives it from the financial year rather than assuming the current one.
    """
    if isinstance(value, dt.datetime):
        return value.year, value.month
    if isinstance(value, dt.date):
        return value.year, value.month
    # A whole number from a spreadsheet cell arrives as int or float. Only 1-12
    # is a month; 2026 is a year someone put in the wrong column.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if float(value).is_integer() and 1 <= int(value) <= 12:
            return None, int(value)
        return None

    text = normalize_text(value)
    if not text:
        return None

    match = _YEAR_MONTH_RE.match(text)
    if match:
        month = int(match.group(2))
        return (int(match.group(1)), month) if 1 <= month <= 12 else None

    named = _MONTH_NAMES.get(text.lower())
    if named is not None:
        return None, named

    if text.isdigit() and 1 <= int(text) <= 12:
        return None, int(text)
    return None


def parse_financial_year(value: Any, config: FinancialYearConfig) -> int | None:
    """The calendar year a stated financial year *starts* in, or ``None``.

    ``FY 2026-27`` starts in 2026. The two-digit end is checked against the
    start rather than trusted: ``2026-28`` is not a financial year, it is a
    typo, and accepting it would file the row a year out.
    """
    text = normalize_text(value)
    if not text:
        return None

    match = _FY_RANGE_RE.match(text)
    if match:
        start = int(match.group(1))
        end_text = match.group(2)
        end = int(end_text) if len(end_text) == 4 else (start // 100) * 100 + int(end_text)
        # A two-digit end rolls the century: FY 2099-00 ends in 2100.
        if len(end_text) == 2 and end < start:
            end += 100
        if end != start + 1 or config.start_month == 1:
            return None
        return start

    match = _FY_SINGLE_RE.match(text)
    if match and config.start_month == 1:
        return int(match.group(1))
    return None


def resolve_target_period(record: dict[str, Any],
                          config: FinancialYearConfig | None = None) -> PeriodResult:
    """Resolve ``target_month`` + ``financial_year`` into a ``dim_date`` anchor.

    On success the record gains three canonical values: ``target_date`` (the
    first of the month, which becomes ``date_id``), ``target_month`` as
    ``YYYY-MM`` and ``financial_year`` as the label ``dim_date`` stores. The
    anchor is the first of the month and never the last, so that a target and
    the sales of its own month land in the same financial period no matter how
    the calendar is configured.
    """
    config = config or FinancialYearConfig.from_settings()

    raw_month = record.get("target_month")
    raw_year = record.get("financial_year")

    if is_blank(raw_month):
        return _fail(errors.MISSING_REQUIRED_FIELD,
                     "Required field 'target_month' is empty.",
                     "target_month", raw_month)
    if is_blank(raw_year):
        return _fail(errors.MISSING_REQUIRED_FIELD,
                     "Required field 'financial_year' is empty.",
                     "financial_year", raw_year)

    start_year = parse_financial_year(raw_year, config)
    if start_year is None:
        return _fail(
            errors.INVALID_FINANCIAL_YEAR,
            f"Financial year '{_shown(raw_year)}' is not recognised. Accepted: "
            + ", ".join(_fy_forms(config)) + ".",
            "financial_year", raw_year,
        )

    parsed = parse_month(raw_month)
    if parsed is None:
        return _fail(
            errors.INVALID_TARGET_MONTH,
            f"Target month '{_shown(raw_month)}' is not recognised. Accepted: "
            + ", ".join(_MONTH_FORMS) + ".",
            "target_month", raw_month,
        )

    year, month = parsed
    if year is None:
        year = _year_within(month, start_year, config)

    anchor = dt.date(year, month, 1)
    stated = _label_for(start_year, config)
    actual = config.label(anchor)
    if actual != stated:
        return _fail(
            errors.TARGET_PERIOD_MISMATCH,
            f"Target month '{_shown(raw_month)}' resolves to {anchor:%B %Y}, "
            f"which belongs to {actual}, but the row states {stated}.",
            "target_month", raw_month,
        )

    return PeriodResult(values={
        "target_date": anchor,
        "target_month": f"{year:04d}-{month:02d}",
        "financial_year": actual,
    })


def _year_within(month: int, start_year: int, config: FinancialYearConfig) -> int:
    """The calendar year in which ``month`` falls, inside the given FY.

    With a July start, August is in the opening calendar year and February is in
    the next one; with a January start every month is in the same year.
    """
    return start_year if month >= config.start_month else start_year + 1


def _label_for(start_year: int, config: FinancialYearConfig) -> str:
    """The canonical label of the financial year beginning in ``start_year``."""
    return config.label(dt.date(start_year, config.start_month, 1))


def _fy_forms(config: FinancialYearConfig) -> tuple[str, ...]:
    """The accepted spellings, which depend on how the year is configured."""
    if config.start_month == 1:
        return (f"{config.label_prefix} 2026", "2026")
    return _FY_FORMS


def _shown(value: Any) -> str:
    """The value as the operator wrote it, for the rejection message."""
    return "" if value is None else str(value).strip()


#: Datasets whose period is derived rather than read from a date column.
#:
#: Registered by data type for the same reason ``MEASURE_BUILDERS`` is: the
#: pipeline stays one code path and a new dataset with its own period rule plugs
#: in here rather than adding a branch to the cleaner.
RECORD_PERIOD_RESOLVERS: dict[str, Callable[[dict[str, Any]], PeriodResult]] = {
    "target": resolve_target_period,
}


__all__ = [
    "PeriodResult",
    "parse_month",
    "parse_financial_year",
    "resolve_target_period",
    "RECORD_PERIOD_RESOLVERS",
]
