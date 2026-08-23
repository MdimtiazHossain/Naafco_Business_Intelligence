"""Value-level validation: dates and numbers.

Two principles:

* **Never guess.** ``15/08/2026`` is unambiguous, ``05/08/2026`` is not. Under
  the default ``auto`` format the ambiguous value is rejected with
  ``AMBIGUOUS_DATE`` and the source's real format must be configured.
* **Never quietly rewrite a number.** Thousands separators are removed and
  accounting parentheses are honoured, but anything else is a rejection, and a
  negative value is only dropped when the dataset says negatives are illegal —
  it is never silently flipped to positive.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from ..utils.text import is_blank, normalize_text
from . import errors
from .errors import ErrorSpec

#: Named formats a source may declare.
DATE_FORMAT_AUTO = "auto"
DATE_FORMATS: dict[str, str] = {
    "DD/MM/YYYY": "%d/%m/%Y",
    "MM/DD/YYYY": "%m/%d/%Y",
    "YYYY-MM-DD": "%Y-%m-%d",
    "DD-MM-YYYY": "%d-%m-%Y",
    "YYYY/MM/DD": "%Y/%m/%d",
}

_ISO_RE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$")
_DMY_RE = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$")
_NUMERIC_CLEAN_RE = re.compile(r"[,\s ]")
_CURRENCY_RE = re.compile(r"^[^\d\-+(.]*")


@dataclass(frozen=True)
class ParseResult:
    """Outcome of parsing one value."""

    value: object | None
    error: ErrorSpec | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _fail(error: ErrorSpec, message: str) -> ParseResult:
    return ParseResult(None, error, message)


def parse_date(value: object, date_format: str = DATE_FORMAT_AUTO) -> ParseResult:
    """Parse a source date.

    ``date_format`` is either ``"auto"``, one of :data:`DATE_FORMATS`, or a raw
    ``strptime`` pattern, allowing per-source configuration.
    """
    if is_blank(value):
        return ParseResult(None)
    if isinstance(value, dt.datetime):
        return ParseResult(value.date())
    if isinstance(value, dt.date):
        return ParseResult(value)

    text = normalize_text(value)
    if text is None:
        return ParseResult(None)

    if date_format and date_format != DATE_FORMAT_AUTO:
        pattern = DATE_FORMATS.get(date_format, date_format)
        try:
            return ParseResult(dt.datetime.strptime(text, pattern).date())
        except ValueError:
            return _fail(
                errors.INVALID_DATE,
                f"'{text}' does not match the configured date format '{date_format}'.",
            )

    # --- auto: accept only what cannot be misread ---------------------------
    iso = _ISO_RE.match(text)
    if iso:
        year, month, day = (int(g) for g in iso.groups())
        return _build_date(year, month, day, text)

    dmy = _DMY_RE.match(text)
    if dmy:
        first, second, year = (int(g) for g in dmy.groups())
        if first > 12 and second <= 12:
            return _build_date(year, second, first, text)      # unambiguously D/M/Y
        if second > 12 and first <= 12:
            return _build_date(year, first, second, text)      # unambiguously M/D/Y
        if first > 12 and second > 12:
            return _fail(errors.IMPOSSIBLE_DATE,
                         f"'{text}' has no valid month component.")
        return _fail(
            errors.AMBIGUOUS_DATE,
            f"'{text}' could be day/month or month/day. Configure the source's "
            "date format (DD/MM/YYYY or MM/DD/YYYY) instead of guessing.",
        )

    # Textual months are unambiguous, so they are safe to accept.
    for pattern in ("%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%d-%b-%Y", "%Y%m%d"):
        try:
            return ParseResult(dt.datetime.strptime(text, pattern).date())
        except ValueError:
            continue

    return _fail(errors.INVALID_DATE, f"'{text}' is not a recognisable date.")


def _build_date(year: int, month: int, day: int, text: str) -> ParseResult:
    try:
        return ParseResult(dt.date(year, month, day))
    except ValueError:
        return _fail(errors.IMPOSSIBLE_DATE, f"'{text}' is not a real calendar date.")


def parse_number(value: object, allow_negative: bool = True,
                 field_name: str = "value") -> ParseResult:
    """Parse a numeric measure.

    ``"1,250,000"`` becomes ``1250000``; ``"(1,250)"`` becomes ``-1250``
    (accounting notation). Anything else non-numeric is rejected rather than
    coerced to zero, so a broken column can never look like real business data.
    """
    if is_blank(value):
        return ParseResult(None)
    if isinstance(value, bool):
        return _fail(errors.INVALID_NUMERIC,
                     f"{field_name} is a boolean, which is not a valid measure.")
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
        return _check_sign(number, allow_negative, field_name)

    text = normalize_text(value)
    if text is None:
        return ParseResult(None)

    negative_parentheses = text.startswith("(") and text.endswith(")")
    if negative_parentheses:
        text = text[1:-1].strip()

    text = _CURRENCY_RE.sub("", text, count=1) if not text[:1].isdigit() else text
    text = _NUMERIC_CLEAN_RE.sub("", text)
    if text.endswith("%"):
        return _fail(errors.INVALID_NUMERIC,
                     f"{field_name} '{value}' is a percentage, not an absolute value.")

    if text in ("", "-", "+", "."):
        return _fail(errors.INVALID_NUMERIC, f"{field_name} '{value}' is not numeric.")

    try:
        number = Decimal(text)
    except InvalidOperation:
        return _fail(errors.INVALID_NUMERIC, f"{field_name} '{value}' is not numeric.")

    if negative_parentheses:
        number = -number
    return _check_sign(number, allow_negative, field_name)


def _check_sign(number: Decimal, allow_negative: bool, field_name: str) -> ParseResult:
    if number < 0 and not allow_negative:
        return _fail(
            errors.NEGATIVE_NOT_ALLOWED,
            f"{field_name} is {number}, but negative values are not permitted for this "
            "measure. The value is rejected, not silently corrected.",
        )
    return ParseResult(number)


def safe_divide(numerator, denominator, percent: bool = False):
    """Division that returns ``None`` instead of raising on a zero denominator.

    Used by every ratio in the reporting layer: margin %, achievement %,
    growth %, stock coverage days.
    """
    if numerator is None or denominator is None:
        return None
    numerator = Decimal(str(numerator))
    denominator = Decimal(str(denominator))
    if denominator == 0:
        return None
    result = numerator / denominator
    return result * 100 if percent else result


__all__ = [
    "DATE_FORMAT_AUTO",
    "DATE_FORMATS",
    "ParseResult",
    "parse_date",
    "parse_number",
    "safe_divide",
]
