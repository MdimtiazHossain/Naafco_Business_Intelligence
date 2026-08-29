"""Unit tests for date parsing, numeric parsing, the calendar and transforms."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.etl import errors
from app.etl.calendar import FinancialYearConfig, build_date_row, to_date_id
from app.etl.transforms import (
    achievement_percent,
    build_material_stock_measures,
    build_sales_measures,
    gross_margin_percent,
    growth_percent,
)
from app.etl.validation import parse_date, parse_number, safe_divide

# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------


def test_iso_dates_are_unambiguous() -> None:
    assert parse_date("2026-08-15").value == dt.date(2026, 8, 15)


def test_day_greater_than_twelve_is_unambiguous() -> None:
    assert parse_date("15/08/2026").value == dt.date(2026, 8, 15)
    assert parse_date("08/15/2026").value == dt.date(2026, 8, 15)


def test_ambiguous_date_is_rejected_not_guessed() -> None:
    result = parse_date("05/08/2026")
    assert not result.ok
    assert result.error is errors.AMBIGUOUS_DATE
    assert "Configure the source's date format" in result.message


def test_configured_format_resolves_ambiguity() -> None:
    assert parse_date("05/08/2026", "DD/MM/YYYY").value == dt.date(2026, 8, 5)
    assert parse_date("05/08/2026", "MM/DD/YYYY").value == dt.date(2026, 5, 8)


def test_impossible_date_is_rejected() -> None:
    result = parse_date("31/02/2026")
    assert not result.ok
    assert result.error is errors.IMPOSSIBLE_DATE


def test_value_not_matching_the_configured_format_is_rejected() -> None:
    result = parse_date("2026-08-15", "DD/MM/YYYY")
    assert not result.ok
    assert result.error is errors.INVALID_DATE


def test_garbage_date_is_rejected() -> None:
    assert not parse_date("not a date").ok


def test_blank_date_is_none_not_an_error() -> None:
    result = parse_date(None)
    assert result.ok and result.value is None


def test_real_datetime_passes_through() -> None:
    assert parse_date(dt.datetime(2026, 8, 15, 9, 30)).value == dt.date(2026, 8, 15)


# --------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------


def test_thousands_separators_are_removed() -> None:
    assert parse_number("1,250,000").value == Decimal("1250000")


def test_accounting_parentheses_mean_negative() -> None:
    assert parse_number("(1,250)").value == Decimal("-1250")


def test_non_numeric_is_rejected_not_zeroed() -> None:
    result = parse_number("n/a")
    assert not result.ok
    assert result.error is errors.INVALID_NUMERIC
    assert result.value is None


def test_negative_allowed_where_business_rules_permit() -> None:
    assert parse_number("-5", allow_negative=True).value == Decimal("-5")


def test_negative_rejected_where_forbidden_and_never_flipped() -> None:
    result = parse_number("-5", allow_negative=False, field_name="discount")
    assert not result.ok
    assert result.error is errors.NEGATIVE_NOT_ALLOWED
    assert result.value is None


def test_percentages_are_rejected() -> None:
    assert not parse_number("12%").ok


def test_safe_divide_never_raises() -> None:
    assert safe_divide(10, 0) is None
    assert safe_divide(10, 2) == Decimal(5)
    assert safe_divide(1, 4, percent=True) == Decimal(25)


# --------------------------------------------------------------------------
# Financial calendar
# --------------------------------------------------------------------------


def test_july_financial_year_labels() -> None:
    config = FinancialYearConfig(start_month=7)
    assert config.label(dt.date(2026, 8, 15)) == "FY 2026-27"
    assert config.label(dt.date(2027, 6, 30)) == "FY 2026-27"
    assert config.label(dt.date(2027, 7, 1)) == "FY 2027-28"
    assert config.label(dt.date(2026, 6, 30)) == "FY 2025-26"


def test_calendar_financial_year_is_supported() -> None:
    config = FinancialYearConfig(start_month=1)
    assert config.label(dt.date(2026, 8, 15)) == "FY 2026"
    assert config.month_number(dt.date(2026, 8, 15)) == 8


def test_financial_month_and_quarter() -> None:
    config = FinancialYearConfig(start_month=7)
    assert config.month_number(dt.date(2026, 7, 1)) == 1
    assert config.month_number(dt.date(2027, 6, 1)) == 12
    assert config.quarter(dt.date(2026, 7, 1)) == 1
    assert config.quarter(dt.date(2027, 6, 1)) == 4


def test_financial_year_boundaries() -> None:
    config = FinancialYearConfig(start_month=7)
    assert config.year_start(dt.date(2026, 8, 15)) == dt.date(2026, 7, 1)
    assert config.year_end(dt.date(2026, 8, 15)) == dt.date(2027, 6, 30)
    assert config.is_year_end(dt.date(2027, 6, 30))


def test_invalid_start_month_is_rejected() -> None:
    with pytest.raises(ValueError):
        FinancialYearConfig(start_month=13)


def test_date_row_is_fully_populated() -> None:
    row = build_date_row(dt.date(2026, 3, 31), FinancialYearConfig(start_month=7))
    assert row["date_id"] == 20260331
    assert row["month_name"] == "March"
    assert row["quarter_name"] == "Q1"
    assert row["is_month_end"] is True
    assert row["is_quarter_end"] is True
    assert row["is_year_end"] is False
    assert row["financial_year"] == "FY 2025-26"


def test_to_date_id() -> None:
    assert to_date_id(dt.date(2026, 8, 15)) == 20260815


# --------------------------------------------------------------------------
# Business calculations
# --------------------------------------------------------------------------


def test_gross_sales_is_derived_when_absent() -> None:
    """The direction that matters now: net is required, gross is reconstructed."""
    measures = build_sales_measures(
        {"quantity": 10, "net_sales": Decimal(1000), "discount": Decimal(200)}
    )
    assert measures["gross_sales"] == Decimal(1200)
    assert measures["net_sales"] == Decimal(1000)


def test_gross_sales_equals_net_when_there_is_no_discount() -> None:
    measures = build_sales_measures({"net_sales": Decimal(500)})
    assert measures["gross_sales"] == Decimal(500)
    assert measures["discount"] == Decimal(0)


def test_a_supplied_gross_is_not_overwritten() -> None:
    """A source that sends both is believed, even if the two disagree.

    Reconciling them is a data-quality question, not something to paper over by
    silently recomputing one from the other.
    """
    measures = build_sales_measures({
        "gross_sales": Decimal(1300), "discount": Decimal(200),
        "net_sales": Decimal(1000),
    })
    assert measures["gross_sales"] == Decimal(1300)
    assert measures["net_sales"] == Decimal(1000)


def test_net_sales_is_still_derived_when_absent() -> None:
    """Unreachable through the pipeline, which rejects the row first — but a
    direct caller must not get a silent zero."""
    measures = build_sales_measures(
        {"quantity": 10, "gross_sales": Decimal(1200), "discount": Decimal(200)}
    )
    assert measures["net_sales"] == Decimal(1000)
    assert measures["gross_profit"] is None  # no cost supplied


def test_net_sales_from_source_is_respected() -> None:
    measures = build_sales_measures({
        "gross_sales": Decimal(1200), "discount": Decimal(200),
        "net_sales": Decimal(950), "cost": Decimal(700),
    })
    assert measures["net_sales"] == Decimal(950)
    assert measures["gross_profit"] == Decimal(250)


def test_gross_margin_handles_zero_net_sales() -> None:
    assert gross_margin_percent(0, 100) is None
    assert gross_margin_percent(1000, 250) == Decimal(25)


def test_receivables_arithmetic_does_not_live_in_transforms() -> None:
    """Aging and overdue arithmetic is back, and deliberately not back *here*.

    Revision 0020 removed these functions with the Outstanding module. Revision
    0031 reinstated receivables against a source that exists, so the arithmetic
    returned — but to :mod:`app.etl.credit`, which owns it alone so that the
    loader, the reporting views and the endpoints cannot come to three different
    answers about how late an invoice is. A copy reappearing in this module would
    be exactly the drift that module exists to prevent.

    ``build_outstanding_measures`` and ``build_collection_measures`` stay absent
    for the original reason: ``fact_outstanding`` and ``fact_collection`` are
    still gone, and no source produces either.
    """
    from app.etl import credit, transforms

    for name in ("aging_bucket", "days_overdue_between", "AGING_BUCKETS",
                 "build_outstanding_measures", "build_collection_measures"):
        assert not hasattr(transforms, name), name
    # ...and every one of the first three does exist, once, over here.
    for name in ("aging_bucket", "days_overdue", "AGING_BUCKETS"):
        assert hasattr(credit, name), name
    assert set(transforms.MEASURE_BUILDERS) == {
        "sales", "material_stock", "target", "credit_invoice"}


def test_material_stock_measures_are_taken_verbatim() -> None:
    """The four categories are stated by the file and never derived."""
    measures = build_material_stock_measures({
        "unrestricted_stock": 100, "quality_inspection_stock": 20,
        "blocked_stock": 5, "stock_in_transit": 11,
        "production_date": dt.date(2026, 1, 1),
        "shelf_life_expiration_date": dt.date(2027, 1, 1),
    })
    assert measures["unrestricted_stock"] == Decimal(100)
    assert measures["quality_inspection_stock"] == Decimal(20)
    assert measures["blocked_stock"] == Decimal(5)
    assert measures["stock_in_transit"] == Decimal(11)
    assert measures["production_date"] == dt.date(2026, 1, 1)
    assert measures["shelf_life_expiration_date"] == dt.date(2027, 1, 1)
    # No total: the view sums the four, so there is one definition of it.
    assert "total_stock" not in measures


def test_material_stock_absent_categories_are_zero_not_missing() -> None:
    """A category the file leaves blank holds no stock — that is a real zero.

    The dates are the exception: a missing expiry date means the shelf life is
    unknown, which is why they stay ``None`` and get their own expiry bucket.
    """
    measures = build_material_stock_measures({"unrestricted_stock": 40})
    assert measures["quality_inspection_stock"] == Decimal(0)
    assert measures["blocked_stock"] == Decimal(0)
    assert measures["stock_in_transit"] == Decimal(0)
    assert measures["shelf_life_expiration_date"] is None


def test_achievement_and_growth_never_divide_by_zero() -> None:
    assert achievement_percent(500, 0) is None
    assert achievement_percent(500, 1000) == Decimal(50)
    assert growth_percent(150, 0) is None
    assert growth_percent(150, 100) == Decimal(50)
