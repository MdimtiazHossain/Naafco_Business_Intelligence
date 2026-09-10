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

from ..etl.calendar import FinancialYearConfig, shift_years
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

#: Ordinal words that name a quarter, in either language.
#:
#: Written out rather than derived: there is no table of Bangla ordinals to
#: derive them from, and four of them is a shorter list than the code that
#: would build it.
QUARTER_ORDINAL_WORDS: dict[str, int] = {
    "first": 1, "প্রথম": 1, "prothom": 1,
    "second": 2, "দ্বিতীয়": 2, "dwitiyo": 2, "ditiyo": 2,
    "third": 3, "তৃতীয়": 3, "tritiyo": 3,
    "fourth": 4, "চতুর্থ": 4, "choturtho": 4, "chaturtha": 4,
}

#: A quarter the reader named, in the shapes people write it.
#:
#: Every alternative demands either the letter, the word, or an ordinal
#: *suffix*, so "last 3 quarters" — three quarters ending now, not the third
#: quarter — does not match. A bare number beside the word is the one form
#: deliberately left unread: "3 quarter" is as likely to be a count as a name,
#: and this resolver reports what it cannot read rather than choosing.
QUARTER_RE = re.compile(
    r"\bq\s*([1-4])\b"
    r"|\bquarter\s*-?\s*([1-4])\b"
    r"|\b([1-4])\s*(?:st|nd|rd|th)\s*(?:quarter|ত্রৈমাসিক)"
    r"|ত্রৈমাসিক\s*([1-4])"
    r"|\b(" + "|".join(QUARTER_ORDINAL_WORDS) + r")\s*(?:quarter|ত্রৈমাসিক)",
    re.IGNORECASE,
)

#: The year written beside a named quarter — "Q3 2025", "2025 Q3".
#:
#: Read as the year the *financial* year began, the reading "FY 24-25" already
#: gets, because every quarter in this platform is a financial quarter. The
#: label says which year it landed in, so the reading is visible rather than
#: assumed.
QUARTER_YEAR_RE = re.compile(
    r"(?:q\s*[1-4]|quarter\s*-?\s*[1-4]|[1-4]\s*(?:st|nd|rd|th)\s*quarter)"
    r"[\s,]*((?:19|20)\d{2})"
    r"|((?:19|20)\d{2})[\s,]*(?:q\s*[1-4]|quarter\s*-?\s*[1-4])",
    re.IGNORECASE,
)

#: The word on its own, for the check that reports unmatched master-data terms.
#:
#: Without it every quarter question reported "no master record matches
#: 'quarter'" — noise that teaches a reader to ignore the one line that tells
#: them a filter did not apply.
QUARTER_WORD_RE = re.compile(
    r"\bquarters?\b|ত্রৈমাসিক|\bqtr\b|\b(?:" + "|".join(QUARTER_ORDINAL_WORDS) + r")\b",
    re.IGNORECASE,
)

#: "last 30 days", "গত ৩০ দিন", "past 7 days".
LAST_N_DAYS_RE = re.compile(
    r"(?:last|past|previous|গত|বিগত)\s*(\d{1,4})\s*(?:days?|দিন)", re.IGNORECASE
)
#: Calendar months by name, in both languages.
#:
#: The English half is derived from ``calendar`` rather than typed out, the same
#: way ``etl.period`` derives its own table — a hand-written list of twelve names
#: is a list that can lose one. The Bangla half has to be written down: these are
#: the Gregorian months as Bangladeshi business writes them, not the Bengali
#: calendar's own months, and Python knows nothing about either.
MONTH_NAMES: dict[str, int] = {
    **{calendar.month_name[m].lower(): m for m in range(1, 13)},
    **{calendar.month_abbr[m].lower(): m for m in range(1, 13)},
    "জানুয়ারি": 1, "জানুয়ারী": 1,
    "ফেব্রুয়ারি": 2, "ফেব্রুয়ারী": 2,
    "মার্চ": 3,
    "এপ্রিল": 4,
    "মে": 5,
    "জুন": 6,
    "জুলাই": 7,
    "আগস্ট": 8, "আগষ্ট": 8, "অগাস্ট": 8,
    "সেপ্টেম্বর": 9, "সেপ্টেম্বার": 9,
    "অক্টোবর": 10,
    "নভেম্বর": 11,
    "ডিসেম্বর": 12,
}

#: Month names that are ordinary words first and months second.
#:
#: English "may" is a modal verb, "mar" is a verb, and Bangla "মে" is a
#: postposition, so each can appear in a question that names no month at all.
#: They resolve when the sentence puts a year or the word "month" *beside* them
#: — "May 2025", "মে মাসের" — and are ignored when they stand alone. Reading a
#: stray "may" as a period would be the same class of mistake this whole change
#: exists to remove.
#:
#: "March" and "August" are not here: they are ordinary words too, but in a
#: question about a business they are overwhelmingly months, and requiring a
#: qualifier would refuse "March sales". The rule below — a month word loses to
#: a period the reader actually stated — is what covers "march ahead this
#: month" without costing that.
AMBIGUOUS_MONTH_WORDS = frozenset({"may", "mar", "মে"})

#: Every spelling of a month, longest first so "september" wins over "sep".
_MONTH_ALTERNATION = "|".join(
    sorted((re.escape(name) for name in MONTH_NAMES), key=len, reverse=True)
)

MONTH_RE = re.compile(
    r"(?<![\wঀ-৿])(" + _MONTH_ALTERNATION + r")(?![\wঀ-৿])",
    re.IGNORECASE,
)

#: A month with the year it belongs to, written in either order.
#:
#: Adjacency is the whole point. "August stock above 2000 units" names a
#: quantity, and reading that 2000 as the month's year dated the question
#: twenty-six years into the past while looking exactly like a right answer.
MONTH_YEAR_RE = re.compile(
    r"(?:(?:" + _MONTH_ALTERNATION + r")[\s,]*(?P<year_after>(?:19|20)\d{2})"
    r"|(?P<year_before>(?:19|20)\d{2})[\s,]*(?:" + _MONTH_ALTERNATION + r"))",
    re.IGNORECASE,
)

#: "month" as an operator romanises it — "mash", "mashe", "masher", "maser".
#:
#: The Bangla "মাস" was already read and its romanisation was not, although a
#: mixed-script question is the ordinary case here rather than the exception:
#: "dec masher sales" is how the same request arrives typed on an English
#: keyboard. Two things followed from the gap. "masher" was reported as a master
#: record nobody has, which is noise on the one line that exists to say a filter
#: did not apply; and the ambiguous month words had nothing to qualify them, so
#: "may masher sales" read "may" as an ordinary English word and answered for
#: the current month.
_ROMAN_MONTH = r"\bmash?(?:er|e)?\b"

#: Words that show a period is being written about at all.
MONTH_CUE_RE = re.compile(rf"\bmonth\b|মাস|{_ROMAN_MONTH}", re.IGNORECASE)

#: The word "month" written *beside* a month name — "May month", "মে মাসের".
#:
#: Adjacency for the same reason the year needs it: "the sales team should march
#: ahead this month" contains both a month name and the word "month", and they
#: have nothing to do with each other. "ত্রৈমাসিক" (quarterly) contains "মাস"
#: too, which is why the negative lookbehind is here and not in the wide cue
#: above — that one only asks whether a word is part of how a period is written.
_MONTH_WORD = rf"\bmonths?\b|(?<!ত্রৈ)মাস\S*|{_ROMAN_MONTH}"

MONTH_PROMOTION_RE = re.compile(
    r"(?:" + _MONTH_ALTERNATION + r")[\s,-]*(?:" + _MONTH_WORD + r")"
    r"|(?:" + _MONTH_WORD + r")[\s,-]*(?:" + _MONTH_ALTERNATION + r")",
    re.IGNORECASE,
)

#: "FY 2026-27", "FY2026", "অর্থবছর 2026-27", "FY 26-27", "24-25 অর্থবছরের".
#:
#: Two orders, because the two languages put the marker on opposite sides:
#: English writes "FY 2024-25" and Bangla writes "২৪-২৫ অর্থবছরের". The start
#: year may be two digits — which is how a planner actually writes it — so
#: ``_start_year`` widens 24 to 2024 rather than the regex pretending 24 is a
#: year in its own right.
#: The words that mark a financial year, in both languages. Declared once so
#: the pattern that reads a year and the test for "is this word a period at
#: all" cannot disagree about what a marker is.
FY_MARKERS = r"fy|f\.y\.|financial\s+year|fiscal\s+year|অর্থবছর\S*"

#: Markers that may only *follow* the pair, never introduce it.
#:
#: "year" is the ordinary word for a period — "this year", "last year", "year to
#: date" — so it can never open a financial year the way "FY" does, and adding
#: it to :data:`FY_MARKERS` would make "last year 24" read as FY 2024. After a
#: hyphenated pair it is unambiguous: nothing but a financial year is written
#: "24-25 year", and that is how a planner says it out loud.
#:
#: This is why "24-25 year এর December" used to answer for December of whichever
#: year had one most recently — the pair was read as two loose numbers, the
#: month fell back to its own most recent occurrence, and the answer covered a
#: period twelve months from the one that was asked for.
#: "বছর" is here in both scripts for the reason the month cue above is: a
#: question typed on an English keyboard says "24-25 bochorer sales", and it is
#: the same sentence. The spellings are the ones an operator actually varies
#: between — ch/chh/s/sh — with the genitive "-er" optional.
FY_TRAILING_MARKERS = rf"{FY_MARKERS}|years?|বছরে?র?|bo(?:chh|ch|sh|s)or(?:er)?"

FINANCIAL_YEAR_RE = re.compile(
    rf"(?:{FY_MARKERS})\s*:?\s*"
    r"(\d{4}|\d{2})(?:\s*[-/]\s*(\d{4}|\d{2}))?"
    r"|(\d{4}|\d{2})\s*[-/]\s*(\d{4}|\d{2})\s*"
    rf"(?:{FY_TRAILING_MARKERS})",
    re.IGNORECASE,
)

#: The marker on its own — "অর্থবছরের" with no digits beside it is still a
#: period word, and must not be reported as a master record nobody has.
#:
#: Built from the *trailing* set, which is the wider of the two. Whether a word
#: may open a financial year is a question about the pattern that reads one;
#: this asks only "is this word part of how a period is written", and "bochorer"
#: is, wherever it sits.
FY_MARKER_RE = re.compile(rf"(?:{FY_TRAILING_MARKERS})", re.IGNORECASE)

#: A year pair with no marker word at all — "2024-25 sales".
#:
#: The opening year must be written in full. Two digits are not enough evidence
#: on their own: "top 10-11 products", "rank 15-16 territories" and "pack of
#: 24-25 pieces" are all consecutive pairs, and reading them as financial years
#: silently replaced the period the reader had actually written in the same
#: sentence. Written "24-25", a financial year still resolves — it just has to
#: say so, which every question that means one already does ("FY 24-25",
#: "24-25 অর্থবছরের").
#:
#: Consecutive is still required on top of that, so "2026-08" stays August and
#: "2025-01-31" stays a day rather than the front of a year.
BARE_YEAR_PAIR_RE = re.compile(r"(?<!\d)(\d{4})\s*[-/]\s*(\d{2})(?!\d)")

#: The two-digit form, rescued by the month standing next to it.
#:
#: "January 24-25" is how the question in the specification is actually written,
#: and the month is what tells the pair apart from "top 10-11 products": a rank
#: range does not sit beside a month name. Adjacency again, for the third time
#: in this module and for the same reason each time.
MONTH_YEAR_PAIR_RE = re.compile(
    r"(?:" + _MONTH_ALTERNATION + r")[\s,]*(?<!\d)(\d{2})\s*[-/]\s*(\d{2})(?!\d)"
    r"|(?<!\d)(\d{2})\s*[-/]\s*(\d{2})(?!\d)[\s,]*(?:" + _MONTH_ALTERNATION + r")",
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
        """The first day of the *financial* quarter holding ``value``.

        Read from the configured calendar rather than from the calendar year, so
        "this quarter" and "Q3" cannot name two different three-month spans. The
        two agree exactly while the financial year starts on a quarter boundary,
        which is the only reason the calendar version survived this long.
        """
        return self._add_months(self.fy.year_start(value),
                                3 * (self.fy.quarter(value) - 1))

    # -- month and financial year -------------------------------------------

    @staticmethod
    def _start_year(digits: str) -> int:
        """Widen a two-digit financial year to a four-digit one.

        "24-25" is how the year is written on a target sheet and said out loud.
        There is no ambiguity to resolve — this warehouse holds no nineteen-
        hundreds — so two digits are read as this century rather than refused.
        """
        value = int(digits)
        return value if value >= 1000 else 2000 + value

    @staticmethod
    def _in_range(year: int | None) -> int | None:
        """Reject a year no business calendar here could mean.

        "9999-00" satisfies the consecutive-year test by wrapping at a hundred,
        and `dt.date(9999, 7, 1)` then fails deep inside `financial_year` with a
        bare ValueError the agent has no handler for. One bound at the point
        every year in this module is widened covers all of them.
        """
        return year if year is not None and 1900 <= year <= 2999 else None

    def _financial_year_start(self, lowered: str) -> int | None:
        """The starting year of a financial year named in the text, if any."""
        match = FINANCIAL_YEAR_RE.search(lowered)
        if match:
            return self._in_range(self._start_year(match.group(1) or match.group(3)))

        beside_month = MONTH_YEAR_PAIR_RE.search(lowered)
        pair = BARE_YEAR_PAIR_RE.search(lowered)
        if beside_month:
            groups = [g for g in beside_month.groups() if g]
            opening, following = groups[0], groups[1]
        elif pair:
            opening, following = pair.group(1), pair.group(2)
        else:
            return None

        first = self._start_year(opening)
        # Consecutive years, or it is not a financial year: "2026-08" is a
        # month and "2025-01" is the front of a date. Comparing the last two
        # digits keeps "2024-25" and "January 24-25" reading the same way.
        if int(following) == (first + 1) % 100:
            return self._in_range(first)
        return None

    def _detect_month(self, lowered: str) -> int | None:
        """The calendar month named in the text, if one unambiguously is."""
        for match in MONTH_RE.finditer(lowered):
            word = match.group(1).lower()
            if word in AMBIGUOUS_MONTH_WORDS and not self._month_is_qualified(lowered):
                # "may" with nothing to qualify it is a modal verb, and a stray
                # "মে" is a postposition. Neither is a period.
                continue
            return MONTH_NAMES[word]
        return None

    def _month_is_qualified(self, lowered: str) -> bool:
        """Whether the sentence gives an ambiguous month word a period's job.

        The word "month" in either language does it, and so does any year the
        month could belong to — "May 2025" and "May FY 2024-25" are periods
        however ordinary the word "may" is on its own.
        """
        return bool(
            MONTH_PROMOTION_RE.search(lowered)
            or self._calendar_year(lowered) is not None
            or self._financial_year_start(lowered) is not None
        )

    @staticmethod
    def _calendar_year(lowered: str) -> int | None:
        """The year written beside a month — "January 2025", "2025 January".

        Beside, not anywhere: "August stock above 2000 units" names a quantity,
        and reading it as the year 2000 dated a question twenty-six years into
        the past while looking exactly like a correct answer. A year on its own
        elsewhere in the sentence is already reported as something this resolver
        could not read.
        """
        match = MONTH_YEAR_RE.search(lowered)
        if not match:
            return None
        return int(match.group("year_after") or match.group("year_before"))

    def month_of(self, year: int, month: int) -> ResolvedDateRange:
        """One whole calendar month, from its first day to its last."""
        start = dt.date(year, month, 1)
        return self._build(DateRangeType.MONTH, start, self._month_end(start),
                           start.strftime("%B %Y"))

    def month_in_financial_year(self, month: int, start_year: int) -> ResolvedDateRange:
        """The instance of ``month`` that falls inside a financial year.

        This is the whole reason the two are resolved together. Under a July
        start, January of FY 2024-25 is January **2025** — a reader who takes it
        for January 2024 is a year out, and so is every figure they quote.
        """
        year = start_year if month >= self.fy.start_month else start_year + 1
        return self.month_of(year, month)

    def most_recent_month(self, month: int) -> ResolvedDateRange:
        """The latest occurrence of ``month`` that has already begun.

        A bare "January sales" means the January that has happened, not the one
        eleven months away. The label carries the year, so the assumption is on
        screen rather than hidden in the range.
        """
        year = self.today.year if month <= self.today.month else self.today.year - 1
        return self.month_of(year, month)

    def _detect_quarter(self, lowered: str) -> int | None:
        """The quarter number the text names, if it names one."""
        match = QUARTER_RE.search(lowered)
        if not match:
            return None
        for group in match.groups():
            if not group:
                continue
            if group.isdigit():
                return int(group)
            return QUARTER_ORDINAL_WORDS[group.lower()]
        return None

    @staticmethod
    def _quarter_year(lowered: str) -> int | None:
        """The four-digit year written beside a quarter, read as an FY start."""
        match = QUARTER_YEAR_RE.search(lowered)
        if not match:
            return None
        return int(next(group for group in match.groups() if group))

    def quarter_in_financial_year(self, quarter: int,
                                  start_year: int) -> ResolvedDateRange:
        """Quarter ``1``-``4`` of the financial year beginning in ``start_year``.

        Financial, never calendar. FY 2024-25 Q1 is July to September under a
        July start, which is what the target sheets say, what ``dim_date``
        stores and what the people who write both mean by "Q1". A calendar Q1
        here would be a second definition of one word.
        """
        start = self.fy.year_start(dt.date(start_year, self.fy.start_month, 1))
        first_month = self._add_months(start, 3 * (quarter - 1))
        end = self._add_months(first_month, 3) - dt.timedelta(days=1)
        return self._build(DateRangeType.QUARTER, first_month, end,
                           f"{self.fy.label(first_month)} Q{quarter}")

    def most_recent_quarter(self, quarter: int) -> ResolvedDateRange:
        """The latest occurrence of that quarter which has already begun.

        A bare "Q1 sales" means the Q1 that has happened, in the same way a bare
        month does. The label carries the financial year, so the reading is on
        screen rather than hidden inside the range.
        """
        start_year = self.fy.start_year_of(self.today)
        candidate = self.quarter_in_financial_year(quarter, start_year)
        if candidate.date_from > self.today:
            return self.quarter_in_financial_year(quarter, start_year - 1)
        return candidate

    @staticmethod
    def _add_months(day: dt.date, months: int) -> dt.date:
        """The first of the month ``months`` after ``day``'s month."""
        index = (day.year * 12 + day.month - 1) + months
        return dt.date(index // 12, index % 12 + 1, 1)

    def name_range(self, start: dt.date, end: dt.date) -> str:
        """What to call a range that was computed rather than asked for.

        A comparison period is derived from the one the reader named, so it
        arrives as two dates and used to be printed as two dates: "vs 2025-01-01
        to 2025-01-31", directly beneath a period line reading "January 2025".
        The answer named one period in words and the other in ISO, and the one
        the reader had to interpret was the one they had not written.

        Only shapes this resolver could itself have produced get a name — a
        whole month, a financial quarter, a financial year, a single day.
        Anything else keeps its dates, because inventing a name for an arbitrary
        span would be less precise than the span.
        """
        if start == end:
            return start.strftime("%d %b %Y")

        first_of_month = start.day == 1
        month_end = end == self._month_end(end) and end.month == start.month and \
            end.year == start.year
        if first_of_month and month_end:
            return start.strftime("%B %Y")

        if first_of_month:
            year = self.fy.start_year_of(start)
            quarter = self.fy.quarter(start)
            if (start, end) == self._quarter_bounds(year, quarter):
                return f"{self.fy.label(start)} Q{quarter}"
            if start == self.fy.year_start(start) and end == self.fy.year_end(start):
                return self.fy.label(start)

        return f"{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')}"

    def _quarter_bounds(self, start_year: int, quarter: int) -> tuple[dt.date, dt.date]:
        """First and last day of a financial quarter, without building a range."""
        first = self._add_months(
            self.fy.year_start(dt.date(start_year, self.fy.start_month, 1)),
            3 * (quarter - 1),
        )
        return first, self._add_months(first, 3) - dt.timedelta(days=1)

    @staticmethod
    def _month_end(day: dt.date) -> dt.date:
        return day.replace(day=calendar.monthrange(day.year, day.month)[1])

    def financial_year_of(self, period: ResolvedDateRange) -> int | None:
        """The financial year a resolved range sits inside, or None.

        None means the range straddles a year end, and a range that belongs to
        two financial years cannot anchor a month to one of them.
        """
        start = self.fy.start_year_of(period.date_from)
        if start != self.fy.start_year_of(period.date_to):
            return None
        return start

    def anchor_month(self, text: str, previous: ResolvedDateRange
                     ) -> ResolvedDateRange | None:
        """A bare month in a follow-up, placed in the year already under discussion.

        "February দেখাও" after a question about January of FY 2024-25 means
        February of that same financial year. Read on its own the phrase means
        the most recent February, which in August 2026 is thirteen months away
        from the figure the reader was just given — and the two answers are
        indistinguishable on screen, which is what makes the silent version of
        this the worst kind of wrong.

        Returns None when there is nothing to anchor: the question dates its own
        month, no month was named, or the previous range spans two financial
        years and so names none. The caller discloses the anchoring, because a
        period that moved without being mentioned is the defect this fixes.
        """
        if self.detect(text) is not DateRangeType.MONTH:
            return None
        lowered = normalize_digits(text).lower()
        month = self._detect_month(lowered)
        if month is None:
            return None
        # A month the reader dated themselves is not bare, and is theirs.
        if (self._calendar_year(lowered) is not None
                or self._financial_year_start(lowered) is not None):
            return None
        start_year = self.financial_year_of(previous)
        if start_year is None:
            return None
        return self.month_in_financial_year(month, start_year)

    #: Period-shaped text this resolver still cannot read.
    #:
    #: A bare year and the day-first date formats nobody here parses. They are
    #: listed so a question can be told what was not understood rather than
    #: quietly answered for some other period — "I could not read '17.03.25'" is
    #: a sentence a reader can act on, and "this covers the current month" alone
    #: is not.
    #:
    #: Named quarters left this list when ``QUARTER_RE`` learned to read them. A
    #: pattern in both places would report a period as unreadable in the same
    #: breath as answering for it.
    UNREAD_PERIOD_RE = re.compile(
        r"(?<![\d-])(?:19|20)\d{2}(?![\d-])"
        r"|\b\d{1,2}[-.]\d{1,2}[-.]\d{2,4}\b",
        re.IGNORECASE,
    )

    def is_period_word(self, token: str) -> bool:
        """Whether a single word is part of how a period is written.

        Read by the caller that reports words it could not match against the
        master data: "অর্থবছরের" and "January" are periods, not missing
        territories, and naming them as missing records would be noise the
        reader has to learn to ignore.
        """
        lowered = normalize_digits(token).lower()
        if lowered in MONTH_NAMES or FY_MARKER_RE.search(lowered):
            return True
        if MONTH_CUE_RE.search(lowered) or self.UNREAD_PERIOD_RE.search(lowered):
            return True
        if QUARTER_WORD_RE.search(lowered) or QUARTER_RE.search(lowered):
            return True
        return any(
            re.search(expression, lowered, re.IGNORECASE)
            for pattern in PERIOD_PATTERNS for expression in pattern.patterns
        )

    def unread_period_terms(self, text: str) -> list[str]:
        """Period-shaped words in the text that resolved to nothing.

        Called only when nothing else resolved, so a match here means the reader
        named a period this resolver does not understand. Reporting it is the
        difference between an answer that is wrong and an answer that says which
        part of the question it could not read.
        """
        lowered = normalize_digits(text).lower()
        if self.detect(text) is not None:
            return []
        return list(dict.fromkeys(m.group(0).strip()
                                  for m in self.UNREAD_PERIOD_RE.finditer(lowered)))

    # -- resolution ---------------------------------------------------------

    def detect(self, text: str) -> DateRangeType | None:
        """The period type mentioned in the text, if any."""
        lowered = normalize_digits(text).lower()
        if EXPLICIT_RANGE_RE.search(lowered) or SINGLE_DATE_RE.search(lowered):
            return DateRangeType.CUSTOM

        relative = self._relative_period(lowered)
        month = self._detect_month(lowered)
        if month is not None:
            # A named month is the narrower statement, so it wins over the
            # financial year it sits inside and over the bare word "মাসের",
            # which is the ordinary Bangla genitive of "month".
            #
            # It does not win over a period the reader actually stated. "The
            # sales team should march ahead this month" holds both, and the one
            # that was meant as a period is the one written as one — a month
            # name with no year and no "month" beside it is a word in a sentence
            # before it is a date.
            if relative is None or self._month_is_qualified(lowered):
                return DateRangeType.MONTH

        # A named quarter beats the financial year it sits inside, for the
        # reason a month does: "Q3 FY 24-25" asked for one quarter, and
        # answering with all four was a period silently multiplied by four with
        # nothing on screen saying so.
        if self._detect_quarter(lowered) is not None:
            return DateRangeType.QUARTER

        if self._financial_year_start(lowered) is not None:
            return DateRangeType.FINANCIAL_YEAR
        if LAST_N_DAYS_RE.search(lowered):
            return DateRangeType.LAST_N_DAYS
        return relative

    @staticmethod
    def _relative_period(lowered: str) -> DateRangeType | None:
        """The phrase-table period named in the text, if any."""
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

        # `detect` owns the decision about which of the two a month-and-period
        # sentence means, so it is asked rather than re-implemented here. The
        # two disagreeing would be a question answered for one period and
        # labelled with another.
        detected_type = self.detect(text)
        financial_start = self._financial_year_start(lowered)

        if detected_type is DateRangeType.MONTH:
            month = self._detect_month(lowered)
            if month is not None:
                if financial_start is not None:
                    return self.month_in_financial_year(month, financial_start)
                year = self._calendar_year(lowered)
                if year is not None:
                    return self.month_of(year, month)
                return self.most_recent_month(month)

        if detected_type is DateRangeType.QUARTER:
            quarter = self._detect_quarter(lowered)
            if quarter is not None:
                start_year = (financial_start if financial_start is not None
                              else self._quarter_year(lowered))
                if start_year is not None:
                    return self.quarter_in_financial_year(quarter, start_year)
                return self.most_recent_quarter(quarter)

        if detected_type is DateRangeType.FINANCIAL_YEAR and financial_start is not None:
            return self.financial_year(financial_start)

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

        if range_type is DateRangeType.MONTH:
            # A month has no meaning without a year, and `of_type` is handed
            # neither. It is reached only by a caller naming MONTH as a preset,
            # which the date filter never offers.
            raise DateResolutionError(
                "A month has to be named with its year — say 'January 2025' or "
                "'January FY 2024-25'.")

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
        growth_from, growth_to = self._growth_window(start, end)
        return ResolvedDateRange(
            type=range_type,
            date_from=start,
            date_to=end,
            label=label,
            financial_year=self.fy.label(end),
            compare_from=compare_from,
            compare_to=compare_to,
            compare_label=compare_label,
            growth_from=growth_from,
            growth_to=growth_to,
        )

    def _growth_window(self, start: dt.date, end: dt.date
                       ) -> tuple[dt.date, dt.date]:
        """The same dates a year earlier — see ``ResolvedDateRange``.

        Computed here, once, rather than by each surface that draws a growth
        figure. It lived in the dashboard's HTTP layer for exactly one change,
        which put it out of reach of the map and the pages that needed the same
        rule, and that is how three surfaces came to grow against the preceding
        period instead.

        The end is cut to today before the shift for the reason ``_elapsed``
        gives above: "This Year" runs to next June, and shifting its nominal end
        back would set two months of trading against a complete previous year.
        """
        return shift_years(start, -1), shift_years(self._elapsed(end), -1)

    def _comparison(self, range_type: DateRangeType, start: dt.date, end: dt.date
                    ) -> tuple[dt.date | None, dt.date | None, str | None]:
        """The naturally comparable preceding period.

        Month-shaped ranges compare against the same span of the previous month
        (so an MTD figure is compared like for like, not against a full month);
        everything else shifts back by its own length.

        **A window that has not finished is compared by the part of it that
        has**, which is what ``_elapsed`` below is for. "Like for like" was
        already this method's promise and it held for every range that ends
        today — but ``THIS_YEAR`` deliberately runs to the end of the financial
        year, so its *nominal* length is twelve months while its lived length,
        on 10 Sep, is two. Shifting back by the nominal length set two months of
        trading against a complete previous year and reported every region of a
        healthy business as collapsing: Chattogram read −87% where the same
        elapsed period a year earlier gives −41%, and the national headline read
        −87.6% against a true −21.1%. The report window is untouched — a target
        is set for the whole year and "This Year" still covers the year it
        names; only the thing it is *measured against* is cut to match.
        """
        if range_type in (DateRangeType.THIS_MONTH, DateRangeType.MTD):
            previous_end = self._month_start(start) - dt.timedelta(days=1)
            previous_start = self._month_start(previous_end)
            day_span = min(end.day, calendar.monthrange(previous_start.year,
                                                        previous_start.month)[1])
            return previous_start, previous_start.replace(day=day_span), "Previous month"

        if range_type in (DateRangeType.LAST_MONTH, DateRangeType.MONTH):
            previous_end = start - dt.timedelta(days=1)
            return self._month_start(previous_end), previous_end, "Month before"

        if range_type in (DateRangeType.THIS_YEAR, DateRangeType.YTD,
                          DateRangeType.FINANCIAL_YEAR):
            previous_end = start - dt.timedelta(days=1)
            previous_start = self.fy.year_start(previous_end)
            span = (self._elapsed(end) - start).days
            return previous_start, min(previous_start + dt.timedelta(days=span),
                                       previous_end), "Previous financial year"

        span = (self._elapsed(end) - start).days + 1
        previous_end = start - dt.timedelta(days=1)
        return previous_end - dt.timedelta(days=span - 1), previous_end, "Previous period"

    def _elapsed(self, end: dt.date) -> dt.date:
        """The last day of a window that can have happened yet.

        Applied to both branches above rather than to the year alone, because
        the fault is the class and not the instance: a named *current* quarter
        and the current ``FINANCIAL_YEAR`` end in the future for the same reason
        ``THIS_YEAR`` does, and would have compared the same way. It is a no-op
        for every window ending today or earlier, which is all the others.
        """
        return min(end, self.today)

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
    "MONTH_NAMES",
    "BANGLA_DIGITS",
]
