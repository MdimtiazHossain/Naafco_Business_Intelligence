"""Normalisation rules: code preservation, Unicode, phones, blanks."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.utils.text import (
    cell_to_str,
    is_blank,
    normalize_code,
    normalize_phone,
    normalize_text,
    parse_datetime,
    parse_decimal,
    parse_int,
    snake_case,
)


@pytest.mark.parametrize("code", ["001", "01", "A001", "R001", "0", "00A", "SKU-0007"])
def test_codes_keep_their_exact_form(code: str) -> None:
    assert normalize_code(code) == code


def test_codes_are_only_trimmed_never_rewritten() -> None:
    assert normalize_code("  R001  ") == "R001"
    assert normalize_code("r001") == "r001"  # case is preserved, not upper-cased
    assert normalize_code("REG 001") == "REG 001"


def test_blank_values_become_none() -> None:
    for value in (None, "", "   ", "\t\n", float("nan")):
        assert is_blank(value)
        assert normalize_text(value) is None


def test_bangla_text_is_preserved() -> None:
    bangla = "প্রিমিয়াম চা ৫০০ গ্রাম"
    assert normalize_text(f"  {bangla}  ") == bangla
    assert normalize_text(bangla) == bangla


def test_phone_numbers_stay_strings() -> None:
    assert normalize_phone("+880 1700 000000") == "+8801700000000"
    assert normalize_phone("01700000000") == "01700000000"
    # A phone that Excel stored as a number still renders without a decimal point.
    assert normalize_phone(1700000000) == "1700000000"


def test_cell_to_str_does_not_add_decimal_noise() -> None:
    assert cell_to_str(1) == "1"
    assert cell_to_str(1.0) == "1"
    assert cell_to_str(120.5) == "120.5"
    assert cell_to_str(Decimal("120.5000")) == "120.5"
    assert cell_to_str(dt.datetime(2024, 1, 1)) == "2024-01-01T00:00:00"
    assert cell_to_str(None) is None


def test_zero_width_characters_are_removed() -> None:
    assert normalize_text("REG001​") == "REG001"
    assert normalize_text("﻿Dhaka") == "Dhaka"


def test_parse_int() -> None:
    assert parse_int("12") == (12, None)
    assert parse_int(12.0) == (12, None)
    assert parse_int(None) == (None, None)
    value, error = parse_int("abc")
    assert value is None and error is not None
    value, error = parse_int(12.5)
    assert value is None and error is not None


def test_parse_decimal() -> None:
    assert parse_decimal("1,234.50") == (Decimal("1234.50"), None)
    assert parse_decimal(120.5)[0] == Decimal("120.5")
    value, error = parse_decimal("n/a")
    assert value is None and error is not None


def test_parse_datetime_accepts_common_formats() -> None:
    assert parse_datetime("2024-01-01")[0] == dt.datetime(2024, 1, 1)
    assert parse_datetime("01/02/2024")[0] == dt.datetime(2024, 2, 1)
    assert parse_datetime(dt.datetime(2024, 1, 1, 9, 30))[0] == dt.datetime(2024, 1, 1, 9, 30)
    value, error = parse_datetime("not a date")
    assert value is None and error is not None


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Company Code", "company_code"),
        ("BU Code", "bu_code"),
        ("Sub Territory Head_Phone Number", "sub_territory_head_phone_number"),
        ("SKU Name Bn", "sku_name_bn"),
        ("Unit Conversation Ratio", "unit_conversation_ratio"),
        ("  Region  HQ ", "region_hq"),
    ],
)
def test_snake_case(label: str, expected: str) -> None:
    assert snake_case(label) == expected
