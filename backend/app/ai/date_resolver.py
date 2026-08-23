"""Period resolution for English, Bangla and mixed text.

Every range is resolved against the **configured** financial year
(``FINANCIAL_YEAR_START_MONTH``), reusing ``etl.calendar.FinancialYearConfig`` —
there is no second implementation of the financial calendar and no calendar-year
assumption anywhere in this module.

Each resolved range also carries the comparable preceding period, which is what
makes "vs last month" and growth questions work without a second parse.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass

from ..etl.calendar import FinancialYearConfig
from .exceptions import DateResolutionError
from .schemas import DateRangeType, ResolvedDateRange

#: Bangla digits, so "১৫ দিন" parses as 15 days.
BANGLA_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


def normalize_digits(text: str) -> str:
    return text.translate(BANGLA_DIGITS)


@dataclass(frozen=True)
class PeriodPattern:
    """One phrase family mapped to a range type."""

    range_type: DateRangeType
    patterns: tuple[str, ...]


#: Order matters: the most specific phrase wins, so "গত মাস" (last month) is
#: matched before "মাস" (month) and "last week" before "week".
PERIOD_PATTERNS: tuple[PeriodPattern, ...] = (
    PeriodPattern(DateRangeType.YESTERDAY, (
        r"\byesterday\b", r"গতকাল", r"গত\s*কাল", r"কালকের",
    )),
    PeriodPattern(DateRangeType.TODAY, (
        r"\btoday\b", r"\btoday'?s\b", r"আজকের", r"আজ\b", r"\bcurrent day\b",
    )),
    PeriodPattern(DateRangeType.LAST_WEEK, (
        r"\blast week\b", r"\bprevious week\b", r"গত\s*সপ্তাহ", r"আগের\s*সপ্তাহ",
    )),
    PeriodPattern(DateRangeType.THIS_WEEK, (
        r"\bthis week\b", r"\bcurrent week\b", r"এই\s*সপ্তাহ", r"চলতি\s*সপ্তাহ",
    )),
    PeriodPattern(DateRangeType.LAST_MONTH, (
        r"\blast month\b", r"\bprevious month\b", r"\bprev month\b",
        r"গত\s*মাস", r"আগের\s*মাস", r"বিগত\s*মাস",
    )),
    PeriodPattern(DateRangeType.THIS_MONTH, (
        r"\bthis month\b", r"\bcurrent month\b", r"এই\s*মাস", r"চলতি\s*মাস",
        r"\bমাসের\b",
    )),
    PeriodPattern(DateRangeType.LAST_QUARTER, (
        r"\blast quarter\b", r"\bprevious quarter\b", r"গত\s*ত্রৈমাসিক",
    )),
    PeriodPattern(DateRangeType.THIS_QUARTER, (
        r"\bthis quarter\b", r"\bcurrent quarter\b", r"এই\s*ত্রৈমাসিক",
    )),
    PeriodPattern(DateRangeType.LAST_YEAR, (
        r"\blast year\b", r"\bprevious year\b", r"গত\s*বছর", r"আগের\s*বছর",
    )),
    PeriodPattern(DateRangeType.THIS_YEAR, (
        r"\bthis year\b", r"\bcurrent year\b", r"এই\s*বছর", r"চলতি\s*বছর",
    )),
    PeriodPattern(DateRangeType.MTD, (r"\bmtd\b", r"\bmonth to date\b")),
    PeriodPattern(DateRangeType.QTD, (r"\bqtd\b", r"\bquarter to date\b")),
    PeriodPattern(DateRangeType.YTD, (r"\bytd\b", r"\byear to date\b")),
)

#: "last 30 days", "গত ৩০ দিন", "past 7 days".
LAST_N_DAYS_RE = re.compile(
    r"(?:last|past|previous|গত|বিগত)\s*(\d{1,4})\s*(?:days?|দিন)", re.IGNORECASE
)
#: "FY 2026-27", "FY2026", "অর্থবছর 2026-27".
FINANCIAL_YEAR_RE = re.compile(
    r"(?:fy|f\.y\.|financial year|fiscal year|অর্থবছর)\s*:?\s*(\d{4})(?:\s*[-/]\s*(\d{2,4}))?",
    re.IGNORECASE,
)
#: "2026-08-01 to 2026-08-31", "01/08/2026 - 31/08/2026".
EXPLICIT_RANGE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})\s*(?:to|until|till|-|—|থেকে)\s*"
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE,
)
SINGLE_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

#: "compared to last month", "গত মাসের তুলনায়", "vs last year".
#:
#: When one of these introduces a period, that period is the *baseline*, not the
#: period being reported on: "গত মাসের তুলনায় sales কত বেড়েছে?" asks about this
#: month measured against last month.
COMPARISON_MARKERS = re.compile(
    r"তুলনায়|তুলনা|বনাম|\bcompared to\b|\bcompared with\b|\bvs\.?\b|\bversus\b|"
    r"\bagainst\b|\bover\b(?=\s+(?:last|previous|গত))",
    re.IGNORECASE,
)

#: Baseline period -> the current period it is naturally compared against.
CURRENT_FOR_BASELINE: dict[DateRangeType, DateRangeType] = {
    DateRangeType.LAST_MONTH: DateRangeType.THIS_MONTH,
    DateRangeType.LAST_WEEK: DateRangeType.THIS_WEEK,
    DateRangeType.LAST_QUARTER: DateRangeType.THIS_QUARTER,
    DateRangeType.LAST_YEAR: DateRangeType.THIS_YEAR,
    DateRangeType.YESTERDAY: DateRangeType.TODAY,
}


class DateResolver:
    """Turns period text into a concrete, comparable range."""

    def __init__(self, today: dt.date | None = None,
                 financial_year: FinancialYearConfig | None = None) -> None:
        self.today = today or dt.date.today()
        self.fy = financial_year or FinancialYearConfig.from_settings()

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _month_end(value: dt.date) -> dt.date:
        return value.replace(day=calendar.monthrange(value.year, value.month)[1])

    @staticmethod
    def _month_start(value: dt.date) -> dt.date:
        return value.replace(day=1)

    def _week_start(self, value: dt.date) -> dt.date:
        # ISO weeks start on Monday.
        return value - dt.timedelta(days=value.weekday())

    def _quarter_start(self, value: dt.date) -> dt.date:
        return dt.date(value.year, 3 * ((value.month - 1) // 3) + 1, 1)

    # -- resolution ---------------------------------------------------------

    def detect(self, text: str) -> DateRangeType | None:
        """The period type mentioned in the text, if any."""
        lowered = normalize_digits(text).lower()
        if EXPLICIT_RANGE_RE.search(lowered) or SINGLE_DATE_RE.search(lowered):
            return DateRangeType.CUSTOM
        if FINANCIAL_YEAR_RE.search(lowered):
            return DateRangeType.FINANCIAL_YEAR
        if LAST_N_DAYS_RE.search(lowered):
            return DateRangeType.LAST_N_DAYS
        for pattern in PERIOD_PATTERNS:
            for expression in pattern.patterns:
                if re.search(expression, lowered, re.IGNORECASE):
                    return pattern.range_type
        return None

    def resolve(self, text: str, default: DateRangeType = DateRangeType.THIS_MONTH,
                ) -> ResolvedDateRange:
        """Resolve the period in ``text``, falling back to ``default``.

        The fallback is always reported as an assumption by the orchestrator, so
        the user can see which period the answer covers.
        """
        lowered = normalize_digits(text).lower()

        explicit = EXPLICIT_RANGE_RE.search(lowered)
        if explicit:
            start = self._parse_date(explicit.group(1))
            end = self._parse_date(explicit.group(2))
            if start and end:
                if end < start:
                    start, end = end, start
                return self._build(DateRangeType.CUSTOM, start, end,
                                   f"{start.isoformat()} to {end.isoformat()}")

        financial = FINANCIAL_YEAR_RE.search(lowered)
        if financial:
            return self.financial_year(int(financial.group(1)))

        last_n = LAST_N_DAYS_RE.search(lowered)
        if last_n:
            days = max(1, min(int(last_n.group(1)), 1826))   # cap at ~5 years
            end = self.today
            start = end - dt.timedelta(days=days - 1)
            return self._build(DateRangeType.LAST_N_DAYS, start, end, f"Last {days} days")

        single = SINGLE_DATE_RE.search(lowered)
        detected = self.detect(text)
        if single and detected in (None, DateRangeType.CUSTOM):
            day = self._parse_date(single.group(1))
            if day:
                return self._build(DateRangeType.CUSTOM, day, day, day.strftime("%d %b %Y"))

        return self.of_type(detected or default)

    def of_type(self, range_type: DateRangeType) -> ResolvedDateRange:
        """Resolve a known range type against ``today``."""
        today = self.today

        if range_type is DateRangeType.TODAY:
            return self._build(range_type, today, today, today.strftime("%d %b %Y"))

        if range_type is DateRangeType.YESTERDAY:
            day = today - dt.timedelta(days=1)
            return self._build(range_type, day, day, day.strftime("%d %b %Y"))

        if range_type is DateRangeType.THIS_WEEK:
            start = self._week_start(today)
            return self._build(range_type, start, today, "This week")

        if range_type is DateRangeType.LAST_WEEK:
            start = self._week_start(today) - dt.timedelta(days=7)
            return self._build(range_type, start, start + dt.timedelta(days=6), "Last week")

        if range_type in (DateRangeType.THIS_MONTH, DateRangeType.MTD):
            start = self._month_start(today)
            label = "This month" if range_type is DateRangeType.THIS_MONTH else "Month to date"
            return self._build(range_type, start, today, label)

        if range_type is DateRangeType.LAST_MONTH:
            end = self._month_start(today) - dt.timedelta(days=1)
            return self._build(range_type, self._month_start(end), end,
                               end.strftime("%B %Y"))

        if range_type in (DateRangeType.THIS_QUARTER, DateRangeType.QTD):
            start = self._quarter_start(today)
            label = "This quarter" if range_type is DateRangeType.THIS_QUARTER else (
                "Quarter to date")
            return self._build(range_type, start, today, label)

        if range_type is DateRangeType.LAST_QUARTER:
            end = self._quarter_start(today) - dt.timedelta(days=1)
            return self._build(range_type, self._quarter_start(end), end, "Last quarter")

        if range_type is DateRangeType.YTD:
            # Year *to date*: the financial year so far, ending today.
            start = self.fy.year_start(today)
            return self._build(range_type, start, today,
                               f"{self.fy.label(today)} to date")

        if range_type is DateRangeType.THIS_YEAR:
            # The whole financial year, not the part of it that has happened.
            #
            # This shared a branch with YTD and so returned an identical range
            # and an identical "FY 2026-27 to date" label. Two presets in the
            # date filter answered with the same window, and "This Year" never
            # covered the year it named — on 18 Aug 2026 under a July financial
            # year it reported 01 Jul - 18 Aug rather than 01 Jul 2026 -
            # 30 Jun 2027. ``LAST_YEAR`` two lines below always returned a
            # complete year, so the two were inconsistent with each other too.
            #
            # A period that has not finished still has a defined end: a target
            # is set for the whole year, and a report headed "This Year" is
            # expected to sit against it. YTD is how the elapsed part is asked
            # for, and it is a separate option in the same dropdown.
            start = self.fy.year_start(today)
            return self._build(range_type, start, self.fy.year_end(today),
                               self.fy.label(today))

        if range_type is DateRangeType.LAST_YEAR:
            previous = self.fy.year_start(today) - dt.timedelta(days=1)
            return self.financial_year(self.fy.start_year_of(previous))

        if range_type is DateRangeType.FINANCIAL_YEAR:
            return self.financial_year(self.fy.start_year_of(today))

        if range_type is DateRangeType.LAST_N_DAYS:
            start = today - dt.timedelta(days=29)
            return self._build(range_type, start, today, "Last 30 days")

        raise DateResolutionError(f"Unsupported range type {range_type}")

    def resolve_with_comparison(self, text: str,
                                default: DateRangeType = DateRangeType.THIS_MONTH,
                                ) -> ResolvedDateRange:
        """Resolve a period, honouring "compared to <period>" phrasing.

        "গত মাসের তুলনায় sales কত বেড়েছে?" reports **this** month with last
        month as the baseline — resolving it to last month would answer a
        different question.
        """
        detected = self.detect(text)
        if (COMPARISON_MARKERS.search(normalize_digits(text))
                and detected in CURRENT_FOR_BASELINE):
            baseline = self.of_type(detected)
            current = self.of_type(CURRENT_FOR_BASELINE[detected])
            return current.model_copy(update={
                "compare_from": baseline.date_from,
                "compare_to": baseline.date_to,
                "compare_label": baseline.label,
            })
        return self.resolve(text, default=default)

    def custom_range(self, start: dt.date, end: dt.date,
                     label: str | None = None) -> ResolvedDateRange:
        """An explicit range, with its comparison period filled in.

        Used by the API when the caller picks "Custom Range" in the date filter.
        """
        if end < start:
            start, end = end, start
        return self._build(DateRangeType.CUSTOM, start, end,
                           label or f"{start.isoformat()} to {end.isoformat()}")

    def financial_year(self, start_year: int) -> ResolvedDateRange:
        """The full financial year that begins in ``start_year``."""
        start = dt.date(start_year, self.fy.start_month, 1)
        end = self.fy.year_end(start)
        return self._build(DateRangeType.FINANCIAL_YEAR, start, end, self.fy.label(start))

    # -- construction -------------------------------------------------------

    def _build(self, range_type: DateRangeType, start: dt.date, end: dt.date,
               label: str) -> ResolvedDateRange:
        compare_from, compare_to, compare_label = self._comparison(range_type, start, end)
        return ResolvedDateRange(
            type=range_type,
            date_from=start,
            date_to=end,
            label=label,
            financial_year=self.fy.label(end),
            compare_from=compare_from,
            compare_to=compare_to,
            compare_label=compare_label,
        )

    def _comparison(self, range_type: DateRangeType, start: dt.date, end: dt.date
                    ) -> tuple[dt.date | None, dt.date | None, str | None]:
        """The naturally comparable preceding period.

        Month-shaped ranges compare against the same span of the previous month
        (so an MTD figure is compared like for like, not against a full month);
        everything else shifts back by its own length.
        """
        if range_type in (DateRangeType.THIS_MONTH, DateRangeType.MTD):
            previous_end = self._month_start(start) - dt.timedelta(days=1)
            previous_start = self._month_start(previous_end)
            day_span = min(end.day, calendar.monthrange(previous_start.year,
                                                        previous_start.month)[1])
            return previous_start, previous_start.replace(day=day_span), "Previous month"

        if range_type is DateRangeType.LAST_MONTH:
            previous_end = start - dt.timedelta(days=1)
            return self._month_start(previous_end), previous_end, "Month before"

        if range_type in (DateRangeType.THIS_YEAR, DateRangeType.YTD,
                          DateRangeType.FINANCIAL_YEAR):
            previous_end = start - dt.timedelta(days=1)
            previous_start = self.fy.year_start(previous_end)
            span = (end - start).days
            return previous_start, min(previous_start + dt.timedelta(days=span),
                                       previous_end), "Previous financial year"

        span = (end - start).days + 1
        previous_end = start - dt.timedelta(days=1)
        return previous_end - dt.timedelta(days=span - 1), previous_end, "Previous period"

    @staticmethod
    def _parse_date(text: str) -> dt.date | None:
        for pattern in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                return dt.datetime.strptime(text, pattern).date()
            except ValueError:
                continue
        return None


def resolve_period(text: str, today: dt.date | None = None,
                   default: DateRangeType = DateRangeType.THIS_MONTH) -> ResolvedDateRange:
    """Convenience wrapper used by the planner and the tests."""
    return DateResolver(today=today).resolve(text, default=default)


__all__ = [
    "DateResolver",
    "resolve_period",
    "normalize_digits",
    "PERIOD_PATTERNS",
    "BANGLA_DIGITS",
]
