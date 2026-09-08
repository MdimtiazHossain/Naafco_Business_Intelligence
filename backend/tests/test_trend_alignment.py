"""The multi-year trend reader: how the years line up, and where the gaps are.

Three things here regress silently, which is why each has a test rather than a
comment. A chart aligned on the wrong axis looks entirely reasonable — July
against January is a smooth line, and nothing about it says the two months are
unrelated. A month with no rows read as zero draws a plunge to the floor that
reads as a collapse in trade. And a year with no history read as zeros draws a
flat line along the bottom, which claims the business was trading and sold
nothing.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from app.ai import queries as q
from app.ai.permission_filter import PermissionFilter, UserContext
from app.ai.schemas import ScopeFilters, TrendToolInput
from app.ai.tools import ToolContext, get_sales_trend
from app.database.models_ai import Role
from app.etl.calendar import months_between, shift_years


# ==========================================================================
# The calendar primitives
# ==========================================================================


def test_a_window_shifts_by_whole_years_keeping_month_and_day():
    """What makes the comparison the reader's own window rather than ours."""
    assert shift_years(dt.date(2026, 3, 15), -1) == dt.date(2025, 3, 15)
    assert shift_years(dt.date(2026, 9, 30), -2) == dt.date(2024, 9, 30)


def test_the_29th_of_february_becomes_the_28th():
    """Not the 1st of March.

    Rolling forward would move the day into the next month, and on a monthly
    series that drops February's first day into March's bucket — a silent
    off-by-one-month at exactly the boundary nobody checks.
    """
    assert shift_years(dt.date(2024, 2, 29), -1) == dt.date(2023, 2, 28)
    assert shift_years(dt.date(2024, 2, 29), -4) == dt.date(2020, 2, 29)


def test_the_positions_come_from_the_window_not_from_the_data():
    """Which is what lets a month with no trade keep its place."""
    assert months_between(dt.date(2026, 3, 15), dt.date(2026, 9, 30)) == [
        (2026, 3), (2026, 4), (2026, 5), (2026, 6), (2026, 7), (2026, 8), (2026, 9),
    ]
    # A window inside one month is one position, not none.
    assert months_between(dt.date(2026, 3, 2), dt.date(2026, 3, 28)) == [(2026, 3)]
    # An inverted window is no window at all rather than an exception.
    assert months_between(dt.date(2026, 9, 1), dt.date(2026, 3, 1)) == []


def test_alignment_is_by_offset_so_a_july_window_compares_with_july():
    """The trap the financial year sets, stated as an equality.

    The financial year starts in July here, so a window opening in July is the
    ordinary case. Aligned on calendar month, position 0 of the shifted window
    would be January; aligned on offset it is July, which is the same month a
    year earlier.
    """
    current = months_between(dt.date(2026, 7, 1), dt.date(2027, 6, 30))
    earlier = months_between(shift_years(dt.date(2026, 7, 1), -1),
                             shift_years(dt.date(2027, 6, 30), -1))
    assert len(current) == len(earlier) == 12
    for (year, month), (earlier_year, earlier_month) in zip(current, earlier):
        assert month == earlier_month, "position must hold the same month"
        assert earlier_year == year - 1


# ==========================================================================
# The reader, against real rows
# ==========================================================================


@pytest.fixture
def ctx(agent_engine, users):
    session = Session(agent_engine)
    user = UserContext(user_id=1, username="root", role=Role.SUPER_ADMIN,
                       data_scope={})
    try:
        yield ToolContext(session=session,
                          permissions=PermissionFilter(session, user), user=user)
    finally:
        session.close()


def test_a_month_with_no_rows_is_null_and_not_zero(ctx):
    """The difference between "we sold nothing" and "we were not trading".

    On a line chart this is the difference between a gap and a plunge, and
    `connectNulls={false}` in the browser only means anything if the reader
    sends a null for the renderer to break on.
    """
    series = q.aligned_series(
        ctx.session, q.SALES_MEASURES, ScopeFilters(),
        # A window years before any seeded sale.
        dt.date(2019, 1, 1), dt.date(2019, 4, 30),
        measure="net_sales", key="net_sales", label="2019",
    )
    assert len(series.values) == 4
    assert all(value is None for value in series.values)
    assert series.is_empty, "a window with nothing in it is absent, not zero"


def test_a_year_with_no_history_is_dropped_rather_than_drawn_flat(ctx):
    """A flat line along the bottom is a claim, and it would be a false one."""
    arguments = TrendToolInput(
        date_from=dt.date(2026, 8, 1), date_to=dt.date(2026, 8, 31),
        granularity="month", compare_years=3,
    )
    result = get_sales_trend(ctx, arguments)
    keys = {line["key"] for line in (result.chart.series if result.chart else [])}
    # The seeded warehouse has 2026 sales and nothing three years earlier, so
    # the older series must not appear at all.
    assert "net_sales" in keys
    assert "net_sales_minus_3" not in keys
    assert result.notes, "dropping a year has to be said, not left to be noticed"


def test_the_series_carry_their_own_keys_and_the_platform_period_names(ctx):
    arguments = TrendToolInput(
        date_from=dt.date(2026, 8, 1), date_to=dt.date(2026, 8, 31),
        granularity="month", compare_years=1,
    )
    result = get_sales_trend(ctx, arguments)
    assert result.chart is not None
    keys = [line["key"] for line in result.chart.series]
    assert keys[0] == "net_sales"
    # Every key a series declares is a column of every row, so the browser names
    # a dataKey rather than an index into a list.
    for row in result.rows:
        for key in keys:
            assert key in row


def test_a_single_series_request_is_unchanged(ctx):
    """The defaults reproduce the old answer exactly — no chart gained a legend."""
    arguments = TrendToolInput(
        date_from=dt.date(2026, 8, 1), date_to=dt.date(2026, 8, 31),
        granularity="month",
    )
    result = get_sales_trend(ctx, arguments)
    assert result.chart is not None
    assert result.chart.series == [], "one line needs no series list"
    assert result.chart.y_axis == "net_sales"


# ==========================================================================
# The two derived percentages
#
# They exist so the dashboard's combo card can draw a plan line and a
# year-on-year line without the browser calculating either — no business
# calculation lives in the browser. Each is nullable for its own reason, and
# both nulls are the point rather than a shortcoming.
# ==========================================================================


def test_the_percentages_are_derived_only_where_their_inputs_exist(ctx):
    """Absent stays absent on both, and for two different reasons.

    A month with no target has no achievement — 0% would say the business
    missed a target nobody set. A month the prior year did not record has no
    growth — -100% would say trade collapsed where in fact there is no history
    to compare against.
    """
    from conftest_phase2 import sales_row
    from conftest_phase3 import _load

    # One sale in July 2025 and none after it, so the prior-year series exists
    # (an empty one would be dropped entirely) with a hole at exactly the
    # position this test needs one.
    _load(ctx.session.get_bind(), "sales", [
        sales_row(**{"Invoice No": "INV-PY1", "Date": "2025-07-15",
                     "Quantity": 100, "Gross Sales": 1_200_000,
                     "Discount": 200_000, "Cost": 700_000,
                     "Source Transaction Id": "SRC-PY1"}),
    ])
    ctx.session.rollback()   # end the read transaction so the new rows are seen

    result = get_sales_trend(ctx, TrendToolInput(
        date_from=dt.date(2026, 7, 1), date_to=dt.date(2026, 10, 31),
        granularity="month", compare_years=1, include_target=True,
    ))
    months = {row["label"]: row for row in result.rows}
    assert list(months) == ["Jul 2026", "Aug 2026", "Sep 2026", "Oct 2026"]

    july = months["Jul 2026"]
    assert july["net_sales"] == pytest.approx(2_400_000)
    assert july["net_sales_minus_1"] == pytest.approx(1_000_000)
    assert july["growth_percent"] == pytest.approx(140.0)
    # The seeded targets are all August, so July has none.
    assert july["target_amount"] is None
    assert july["achievement_percent"] is None

    august = months["Aug 2026"]
    assert august["achievement_percent"] == pytest.approx(60.0)  # 1.8M of 3M
    assert august["net_sales_minus_1"] is None
    assert august["growth_percent"] is None

    # And a month with nothing on either side has neither, rather than 0 and
    # -100 stacked on top of each other.
    for label in ("Sep 2026", "Oct 2026"):
        assert months[label]["achievement_percent"] is None
        assert months[label]["growth_percent"] is None


def test_neither_percentage_reaches_the_chart_series(ctx):
    """The list that becomes lines on a *taka* axis.

    `trendSeriesFrom` turns `chart.series` into lines, and both the dashboard's
    Sales Trend card and /api/pages/sales read it. An achievement of 93 plotted
    there would sit on the floor beside figures in the crores — and neither of
    those cards asked for it. The combo card names its own lines instead.
    """
    result = get_sales_trend(ctx, TrendToolInput(
        date_from=dt.date(2026, 7, 1), date_to=dt.date(2026, 10, 31),
        granularity="month", compare_years=1, include_target=True,
    ))
    keys = {line["key"] for line in result.chart.series}
    assert "achievement_percent" not in keys
    assert "growth_percent" not in keys
    assert keys <= {"net_sales", "net_sales_minus_1", "net_sales_minus_2",
                    "target_amount"}
    # But they are on the rows, which is where the combo card reads them.
    assert "achievement_percent" in result.rows[0]


def test_a_plain_trend_carries_neither_key(ctx):
    """Not present-and-null: absent.

    Every trend table the assistant renders is built from these rows, so two
    columns of dashes on all of them would be two columns a reader learns to
    ignore.
    """
    result = get_sales_trend(ctx, TrendToolInput(
        date_from=dt.date(2026, 7, 1), date_to=dt.date(2026, 10, 31),
        granularity="month",
    ))
    for row in result.rows:
        assert "achievement_percent" not in row
        assert "growth_percent" not in row


def test_a_dropped_year_takes_its_growth_column_with_it(ctx):
    """The gate is the series drawn, not the argument passed.

    Nothing was sold in 2025 in the seeded warehouse, so `net_sales_minus_1` is
    dropped — and a growth column against a series that is not there would be
    nulls all the way down, which is the empty column the gate exists to
    prevent. Achievement stays, because August does have a target.
    """
    result = get_sales_trend(ctx, TrendToolInput(
        date_from=dt.date(2026, 7, 1), date_to=dt.date(2026, 10, 31),
        granularity="month", compare_years=1, include_target=True,
    ))
    keys = {line["key"] for line in result.chart.series}
    assert "net_sales_minus_1" not in keys
    for row in result.rows:
        assert "growth_percent" not in row
        assert "achievement_percent" in row


# ==========================================================================
# What the input refuses
# ==========================================================================


@pytest.mark.parametrize("extra", [{"compare_years": 2}, {"include_target": True}])
def test_the_extras_are_refused_at_daily_granularity(extra):
    """Refused, never quietly switched to monthly.

    Aligning days across years compares a Tuesday with a Thursday, and a target
    is a month of a financial year with no daily figure at all. Coercing the
    caller to monthly would answer a question they did not ask.
    """
    with pytest.raises(ValueError):
        TrendToolInput(date_from=dt.date(2026, 8, 1), date_to=dt.date(2026, 8, 31),
                       granularity="day", **extra)


def test_the_comparison_depth_is_bounded():
    arguments = TrendToolInput(
        date_from=dt.date(2026, 8, 1), date_to=dt.date(2026, 8, 31),
        granularity="month", compare_years=99,
    )
    assert arguments.compare_years == 4
