"""Text / value normalisation helpers shared by the master-data pipeline.

Design rules enforced here (Phase 1 critical rules):

* Business codes are ALWAYS strings. ``001`` never becomes ``1``.
* Bangla / Unicode text is preserved byte-for-byte (only outer whitespace trimmed).
* Phone numbers are strings, never numerics.
* Empty strings and whitespace-only strings normalise to ``None`` (SQL NULL).
"""

from __future__ import annotations

import datetime as _dt
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

# Unicode whitespace that Excel exports frequently smuggle in.
_INVISIBLE = " ​‌‍﻿"
_WS_RE = re.compile(r"\s+")


def is_blank(value: Any) -> bool:
    """True when a value carries no information (``None``, NaN, empty/whitespace)."""
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    if isinstance(value, str):
        return strip_invisible(value).strip() == ""
    return False


def strip_invisible(value: str) -> str:
    """Remove zero-width / non-breaking characters without touching real text."""
    for ch in _INVISIBLE:
        value = value.replace(ch, " " if ch == " " else "")
    return value


def normalize_text(value: Any) -> str | None:
    """Trim whitespace, collapse internal runs, map blanks to ``None``.

    Unicode (including Bangla) is preserved: no case folding, no transliteration,
    no NFKC normalisation that would rewrite conjuncts.
    """
    if is_blank(value):
        return None
    if not isinstance(value, str):
        value = cell_to_str(value)
        if value is None:
            return None
    value = strip_invisible(value)
    value = _WS_RE.sub(" ", value).strip()
    return value or None


def cell_to_str(value: Any) -> str | None:
    """Convert a raw Excel cell value to its faithful string form.

    openpyxl hands back ``int`` / ``float`` / ``datetime`` for typed cells. A code
    cell typed as a number in Excel arrives as ``1`` (not ``"001"``); we render it
    without a spurious ``.0`` and let the inspector raise a data-quality warning
    about the possible loss of leading zeros at source.
    """
    if is_blank(value):
        return None
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    return str(value)


def normalize_code(value: Any) -> str | None:
    """Normalise an official business code.

    Whitespace is trimmed and internal runs collapsed; **nothing else changes**.
    Case is preserved, padding is preserved, punctuation is preserved.
    """
    return normalize_text(value)


def normalize_phone(value: Any) -> str | None:
    """Normalise a phone number, keeping it a string.

    Only whitespace is removed. Digits, ``+``, and separators stay exactly as the
    business recorded them so that ``+8801...`` and ``01...`` remain distinguishable.
    """
    text = normalize_text(value)
    if text is None:
        return None
    return text.replace(" ", "")


def parse_int(value: Any) -> tuple[int | None, str | None]:
    """Best-effort integer coercion. Returns ``(value, error)``."""
    if is_blank(value):
        return None, None
    if isinstance(value, bool):
        return int(value), None
    if isinstance(value, int):
        return value, None
    if isinstance(value, float):
        if value.is_integer():
            return int(value), None
        return None, f"expected an integer, got {value!r}"
    text = normalize_text(value)
    if text is None:
        return None, None
    try:
        return int(Decimal(text)), None
    except (InvalidOperation, ValueError):
        return None, f"expected an integer, got {text!r}"


def parse_decimal(value: Any) -> tuple[Decimal | None, str | None]:
    """Best-effort decimal coercion. Returns ``(value, error)``."""
    if is_blank(value):
        return None, None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Decimal(str(value)), None
    text = normalize_text(value)
    if text is None:
        return None, None
    text = text.replace(",", "")
    try:
        return Decimal(text), None
    except InvalidOperation:
        return None, f"expected a number, got {text!r}"


def parse_datetime(value: Any) -> tuple[_dt.datetime | None, str | None]:
    """Best-effort datetime coercion. Returns ``(value, error)``."""
    if is_blank(value):
        return None, None
    if isinstance(value, _dt.datetime):
        return value, None
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day), None
    text = normalize_text(value)
    if text is None:
        return None, None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%m/%d/%Y",
    ):
        try:
            return _dt.datetime.strptime(text, fmt), None
        except ValueError:
            continue
    return None, f"expected a date/time, got {text!r}"


def snake_case(label: str) -> str:
    """Convert a human sheet/field label into ``snake_case``.

    ``"Sub Territory Head_Phone Number"`` -> ``"sub_territory_head_phone_number"``
    ``"SKU Name Bn"``                     -> ``"sku_name_bn"``
    """
    text = unicodedata.normalize("NFKC", strip_invisible(str(label))).strip()
    text = re.sub(r"[^0-9A-Za-z]+", "_", text)
    # Split camelCase / PascalCase boundaries but keep acronym runs intact.
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text.lower()


def has_leading_or_trailing_space(value: Any) -> bool:
    """True when a raw string value carries outer whitespace (a quality defect)."""
    return isinstance(value, str) and value != strip_invisible(value).strip()


def is_empty_string(value: Any) -> bool:
    """True when the raw value is a string that is present but carries no text."""
    return isinstance(value, str) and value.strip() == ""
