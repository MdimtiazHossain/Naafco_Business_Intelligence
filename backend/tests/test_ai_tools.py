"""Tool execution, result validation, number formatting and export."""

from __future__ import annotations

import datetime as dt

import pytest

from app.ai.exceptions import ToolExecutionError
from app.ai.permission_filter import PermissionFilter
from app.ai.response_formatter import (
    ResponseFormatter,
    format_amount,
    format_percent,
    group_indian,
    render_table,
)
from app.ai.schemas import Intent, ResolvedDateRange, DateRangeType, ToolResult
from app.ai.tools import REGISTRY, ToolContext, execute_tool
from app.ai.validators import validate_tool_result
from conftest_phase3 import TODAY


@pytest.fixture
def ctx(session, users) -> ToolContext:
    permissions = PermissionFilter(session, users["ceo"])
    return ToolContext(session, permissions, users["ceo"], TODAY)


def run(ctx: ToolContext, tool: str, **arguments):
    payload = {"date_from": "2026-08-01", "date_to": "2026-08-31", "filters": {},
               **arguments}
    return execute_tool(ctx, tool, payload).result


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def test_every_expected_tool_is_registered() -> None:
    expected = {
        "get_sales_summary", "get_sales_detail", "get_sales_trend", "get_sales_growth",
        "get_sales_target", "get_sales_achievement",
        "get_stock_summary", "get_stock_by_plant", "get_stock_by_storage_location",
        "get_stock_by_material", "get_stock_by_material_group",
        "get_stock_by_material_brand",
        "get_stock_expiry", "get_expiring_stock",
        "get_target_summary", "get_target_achievement", "get_target_gap",
        "get_customer_performance", "get_zone_performance",
        "get_region_performance", "get_area_performance", "get_unit_performance",
        "get_territory_performance", "get_sub_territory_performance",
        "get_salesforce_performance",
        # The item tools, all three reading the Material Master.
        "get_material_performance", "get_material_brand_performance",
        "get_material_group_performance", "get_material_brand_target_performance",
        "get_business_summary", "get_business_alerts", "get_root_cause_analysis",
    }
    assert expected <= set(REGISTRY)


def test_tool_schemas_are_json_serialisable_for_openai() -> None:
    for name, spec in REGISTRY.items():
        schema = spec.openai_schema()
        assert schema["function"]["name"] == name
        assert schema["function"]["description"]
        assert "properties" in schema["function"]["parameters"]


def test_tools_reject_unknown_arguments() -> None:
    """No free-text or SQL field can be smuggled into a tool call."""
    from app.ai.schemas import BaseToolInput

    with pytest.raises(Exception):
        BaseToolInput.model_validate({
            "date_from": "2026-08-01", "date_to": "2026-08-31",
            "sql": "SELECT * FROM fact_sales",
        })


def test_unknown_tool_is_refused(ctx: ToolContext) -> None:
    with pytest.raises(ToolExecutionError):
        execute_tool(ctx, "get_everything", {})


def test_invalid_arguments_do_not_raise_but_are_reported(ctx: ToolContext) -> None:
    invocation = execute_tool(ctx, "get_sales_summary", {"date_from": "not-a-date"})
    assert not invocation.success
    assert invocation.error_code == "INVALID_ARGUMENTS"
    assert invocation.result is None


# --------------------------------------------------------------------------
# Sales
# --------------------------------------------------------------------------


def test_sales_summary_matches_the_warehouse(ctx: ToolContext) -> None:
    result = run(ctx, "get_sales_summary")
    # 1,000,000 + 500,000 (Dhaka) + 300,000 (Khulna)
    assert result.value == pytest.approx(1_800_000)
    assert result.values["quantity"] == pytest.approx(170)
    # Gross sales, gross profit and margin are no longer reported measures —
    # they stay on the fact table and off every report.
    for absent in ("gross_sales", "gross_profit", "gross_margin_percent"):
        assert absent not in result.values
    assert result.sources == ["vw_sales_detail"]
    assert result.facts


def test_sales_detail_groups_and_ranks(ctx: ToolContext) -> None:
    result = run(ctx, "get_sales_detail", group_by="region", limit=10)
    assert [row["code"] for row in result.rows] == ["REG001", "REG002"]
    assert result.rows[0]["net_sales"] == pytest.approx(1_500_000)
    assert result.chart.type == "bar"


def test_sales_detail_by_material(ctx: ToolContext) -> None:
    result = run(ctx, "get_sales_detail", group_by="material", limit=10)
    codes = {row["code"] for row in result.rows}
    assert codes == {"SKU001", "SKU002"}


def test_grouping_a_report_by_an_unsupported_dimension_is_refused(ctx: ToolContext):
    """Sales cannot be grouped by a stock dimension. Refused, not answered empty."""
    with pytest.raises(ToolExecutionError) as exc:
        run(ctx, "get_sales_detail", group_by="plant", limit=10)
    assert "cannot be broken down" in exc.value.user_message


def test_sales_growth_compares_two_periods(ctx: ToolContext) -> None:
    result = run(ctx, "get_sales_growth", compare_from="2026-07-01",
                 compare_to="2026-07-31")
    assert result.values["current"] == pytest.approx(1_800_000)
    assert result.values["previous"] == pytest.approx(2_400_000)
    assert result.values["growth_percent"] == pytest.approx(-25.0)


def test_growth_reports_no_percentage_when_the_base_is_zero(ctx: ToolContext) -> None:
    result = run(ctx, "get_sales_growth", compare_from="2020-01-01",
                 compare_to="2020-01-31")
    assert result.values["growth_percent"] is None
    assert any("cannot be calculated" in note for note in result.notes)


def test_sales_trend_returns_a_series(ctx: ToolContext) -> None:
    result = run(ctx, "get_sales_trend", granularity="day", limit=100)
    assert len(result.rows) == 3
    assert result.chart.type == "line"


# --------------------------------------------------------------------------
# Stock, target
# --------------------------------------------------------------------------


def test_the_receivables_tools_are_gone(ctx: ToolContext) -> None:
    """Eight tools left with the Collection and Outstanding modules (0020).

    Named individually so a reader who comes looking for one of them finds the
    reason rather than an absence, and so re-registering one by accident fails
    here instead of in production.
    """
    for name in ("get_collection_summary", "get_collection_detail",
                 "get_collection_trend", "get_collection_growth",
                 "get_outstanding_summary", "get_outstanding_detail",
                 "get_outstanding_aging", "get_top_outstanding_customers"):
        assert name not in REGISTRY, name
        with pytest.raises(ToolExecutionError) as exc:
            run(ctx, name)
        assert "unknown tool" in str(exc.value)


def test_stock_summary_keeps_the_four_categories_apart(ctx: ToolContext) -> None:
    """Only unrestricted stock is sellable, so the four never collapse into one."""
    result = run(ctx, "get_stock_summary")
    values = result.values
    assert values["unrestricted_stock"] == pytest.approx(815)   # 40+200+500+75
    assert values["quality_inspection_stock"] == pytest.approx(50)
    assert values["blocked_stock"] == pytest.approx(10)
    assert values["stock_in_transit"] == pytest.approx(25)
    # In transit is inside the total: it is stock the business owns.
    assert values["total_stock"] == pytest.approx(900)


def test_stock_by_plant_groups_on_the_material_master(ctx: ToolContext) -> None:
    result = run(ctx, "get_stock_by_plant", limit=20)
    assert [row["code"] for row in result.rows] == ["PL01"]
    assert result.rows[0]["label"] == "Dhaka Plant"
    assert result.rows[0]["total_stock"] == pytest.approx(900)


def test_stock_by_storage_location_splits_the_position(ctx: ToolContext) -> None:
    """Grouped on the plant/location pair, never on the bare location code.

    A storage location code is unique only within its plant — ``FG01`` names a
    different place at every plant that has one — so the code alone merged
    separate locations and split single ones. The key is the identity, and the
    label carries the plant so two rows called "Finished Goods" can be told
    apart.
    """
    result = run(ctx, "get_stock_by_storage_location", limit=20)
    by_code = {row["code"]: row for row in result.rows}
    assert set(by_code) == {"PL01|SL01", "PL01|SL02"}
    assert by_code["PL01|SL01"]["total_stock"] == pytest.approx(325)   # 50 + 275
    assert by_code["PL01|SL02"]["total_stock"] == pytest.approx(575)   # 500 + 75
    assert "Dhaka Plant" in by_code["PL01|SL01"]["label"]


def test_stock_by_material_reaches_below_the_material_group(ctx: ToolContext) -> None:
    """The grain a material group cannot express.

    MAT-001 and MAT-003 are both MG01 in SL01: a group breakdown reports one
    figure of 325 for them, and only a material breakdown says which of the two
    it belongs to.
    """
    result = run(ctx, "get_stock_by_material", limit=20)
    by_code = {row["code"]: row for row in result.rows}
    assert set(by_code) == {"MAT-001", "MAT-002", "MAT-003"}
    assert by_code["MAT-001"]["total_stock"] == pytest.approx(50)
    assert by_code["MAT-003"]["total_stock"] == pytest.approx(275)
    assert by_code["MAT-002"]["total_stock"] == pytest.approx(575)
    # The label is the description the Material Master states — read from the
    # master through the view, never composed here.
    assert by_code["MAT-001"]["label"] == "Premium Tea 500g Bulk"


def test_stock_by_material_brand_groups_on_the_plants_brand(ctx: ToolContext) -> None:
    """The material brand, which is not the sales brand of a SKU.

    MAT-001 and MAT-003 share a brand, so the breakdown adds them together —
    which is exactly what a brand-level question asks for, and what neither the
    material nor the storage-location breakdown answers.

    Grouped on the brand **name**, which is its identity here: the Material
    Master carries ``material_brand_code`` but sets it to the same value on
    every material, so grouping by the code would return one row for every
    brand in the business.
    """
    result = run(ctx, "get_stock_by_material_brand", limit=20)
    by_code = {row["code"]: row for row in result.rows}
    assert set(by_code) == {"Example Brand", "Dairy Brand"}
    assert by_code["Example Brand"]["total_stock"] == pytest.approx(325)  # 50 + 275
    assert by_code["Dairy Brand"]["total_stock"] == pytest.approx(575)
    assert by_code["Example Brand"]["label"] == "Example Brand"


def test_stock_by_material_ranks_by_the_category_asked_for(ctx: ToolContext) -> None:
    """"Most blocked stock" is not "most stock", and must not be answered as it."""
    by_total = run(ctx, "get_stock_by_material", limit=20)
    by_blocked = run(ctx, "get_stock_by_material", limit=20,
                     sort_by="blocked_stock")
    assert by_total.rows[0]["code"] == "MAT-002"        # 575, the largest total
    assert by_blocked.rows[0]["code"] == "MAT-001"      # the only blocked stock
    # The four categories are reported whatever the ranking.
    assert by_blocked.rows[0]["unrestricted_stock"] == pytest.approx(40)


def test_stock_filters_to_one_material(ctx: ToolContext) -> None:
    """"Stock for material X" narrows the position rather than ranking it."""
    result = run(ctx, "get_stock_summary",
                 filters={"material_codes": ["MAT-002"]})
    assert result.values["total_stock"] == pytest.approx(575)


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"plant_codes": ["PL01"]}, 900),
        ({"plant_codes": ["PL99"]}, 0),
        ({"storage_location_keys": ["PL01|SL01"]}, 325),
        ({"storage_location_keys": ["PL01|SL02"]}, 575),
        ({"material_group_codes": ["MG01"]}, 325),
        ({"material_brand_names": ["Dairy Brand"]}, 575),
        # Every filter is ANDed, so a combination narrows to the intersection.
        ({"material_group_codes": ["MG01"],
          "storage_location_keys": ["PL01|SL01"]}, 325),
        # ...and an impossible combination is empty rather than either half.
        ({"material_group_codes": ["MG02"],
          "storage_location_keys": ["PL01|SL01"]}, 0),
    ],
)
def test_each_stock_filter_narrows_the_position(
    ctx: ToolContext, filters: dict, expected: float,
) -> None:
    """Every filter on the stock page reaches a column the stock view carries.

    The bare storage-location code is deliberately absent from this list: it is
    not an identity, and the filter takes the ``plant|location`` key instead.
    """
    result = run(ctx, "get_stock_summary", filters=filters)
    assert result.values.get("total_stock", 0) == pytest.approx(expected)


def test_expiry_status_narrows_the_whole_page_not_just_the_cards(
    ctx: ToolContext,
) -> None:
    """Selecting a shelf-life bucket filters the totals and the breakdowns too.

    The status is derived rather than stored, so it is applied by the stock
    queries; this is what proves it reaches the same numbers the KPI cards and
    every grouped table are built from.
    """
    summary = run(ctx, "get_stock_summary", filters={"expiry_statuses": ["EXPIRED"]})
    assert summary.values["total_stock"] == pytest.approx(50)

    by_plant = run(ctx, "get_stock_by_plant", limit=20,
                   filters={"expiry_statuses": ["EXPIRED"]})
    assert by_plant.rows[0]["total_stock"] == pytest.approx(50)

    # The expiry cards narrow with everything else, so the page cannot show
    # VALID stock it has just excluded from its own totals.
    buckets = run(ctx, "get_stock_expiry", filters={"expiry_statuses": ["EXPIRED"]})
    by_bucket = {row["code"]: row["total_stock"] for row in buckets.rows}
    assert by_bucket["EXPIRED"] == pytest.approx(50)
    assert by_bucket["VALID"] == pytest.approx(0)


def test_stock_discloses_a_filter_it_cannot_honour(ctx: ToolContext) -> None:
    """A filter the stock view has no column for is reported, never silent.

    The stock page no longer offers one, but the tools are reachable from the
    agent and from a hand-written query string, and an ignored filter that says
    nothing looks exactly like a filter that worked.
    """
    result = run(ctx, "get_stock_summary", filters={"region_codes": ["R01"]})
    assert any("region" in note for note in result.notes)
    # ...and it is ignored rather than applied, so the figure is the total.
    assert result.values["total_stock"] == pytest.approx(900)


def test_stock_empty_state_does_not_blame_the_period(ctx: ToolContext) -> None:
    """Stock has no period, so an empty result must not point at one."""
    result = run(ctx, "get_stock_summary", filters={"plant_codes": ["PL99"]})
    assert any("No stock data found for the selected filters." in note
               for note in result.notes)
    assert not any("period" in note for note in result.notes)


def test_stock_expiry_returns_every_bucket_even_when_empty(ctx: ToolContext) -> None:
    """A missing bucket would read as "none expired" when it may mean "not asked"."""
    result = run(ctx, "get_stock_expiry")
    buckets = {row["code"]: row["total_stock"] for row in result.rows}
    assert set(buckets) == {"EXPIRED", "EXPIRING_SOON", "VALID", "NO_EXPIRY"}
    assert buckets["EXPIRED"] == pytest.approx(50)        # expired 2026-06-30
    assert buckets["EXPIRING_SOON"] == pytest.approx(275)  # expires 2026-09-30
    assert buckets["VALID"] == pytest.approx(500)
    assert buckets["NO_EXPIRY"] == pytest.approx(75)


def test_expiring_stock_lists_positions_at_risk(ctx: ToolContext) -> None:
    result = run(ctx, "get_expiring_stock", limit=20)
    # A position with no expiry date is not at risk and must not be listed.
    assert all(row["shelf_life_expiration_date"] for row in result.rows)
    assert len(result.rows) == 2                          # expired + expiring soon


def test_target_achievement(ctx: ToolContext) -> None:
    result = run(ctx, "get_target_achievement", group_by="region", limit=20)
    assert result.values["target"] == pytest.approx(3_000_000)
    assert result.values["actual"] == pytest.approx(1_800_000)
    assert result.values["achievement_percent"] == pytest.approx(60.0)
    assert result.values["gap"] == pytest.approx(1_200_000)


def test_target_achievement_totals_carry_volume_beside_quantity(
    ctx: ToolContext,
) -> None:
    """The headline has to state every measure the rows do.

    The Target page's Volume card reads ``values.target_volume``, and for a
    while nothing put that key in the totals: volume was summed per group and
    shown in the table, so the column was populated while the card above it
    rendered "—" over data that was there. Pinning the key is what stops a
    measure being added to the rows again without the headline following.
    """
    result = run(ctx, "get_target_achievement", group_by="region", limit=20)

    for key in ("target_quantity", "actual_quantity",
                "target_volume", "actual_volume"):
        assert key in result.values, f"totals are missing {key}"

    # The totals equal the sum of the rows they head — the card and the table
    # cannot disagree about the same scope. Written for both cases because the
    # seeded targets state no volume: an empty sum is 0, but the total of
    # nothing stated is None, and collapsing the two is the whole mistake.
    for total_key, row_key in (("target_volume", "target_volume"),
                               ("actual_volume", "actual_volume")):
        measured = [row[row_key] for row in result.rows
                    if row.get(row_key) is not None]
        expected = pytest.approx(sum(measured)) if measured else None
        assert result.values[total_key] == expected

    # This fixture's targets carry no volume, so the card reads "n/a" rather
    # than a zero somebody might act on.
    assert result.values["target_volume"] is None


def test_target_volume_is_absent_rather_than_zero_when_none_was_stated(
    ctx: ToolContext,
) -> None:
    """A target nobody expressed in volume is not a target of no volume.

    ``None`` renders "n/a"; a zero would read as a real figure somebody could
    act on, which is the distinction this platform draws everywhere else.
    """
    from app.ai import queries as q

    assert q._volume_sum({}) is None
    assert q._volume_sum({"REG001": None}) is None
    assert q._volume_sum({"REG001": 12.5, "REG002": 7.5}) == pytest.approx(20.0)
    # A group that stated none is skipped, not counted as zero.
    assert q._volume_sum({"REG001": 12.5, "REG002": None}) == pytest.approx(12.5)


def test_target_achievement_can_list_only_underperformers(ctx: ToolContext) -> None:
    result = run(ctx, "get_target_achievement", group_by="region", limit=20,
                 below_percent=50.0)
    assert [row["code"] for row in result.rows] == ["REG002"]   # 300k / 1000k = 30%


# The comparison window on the achievement tool. It exists because the dashboard
# draws target, actual and *last period's* actual as three bars of one group,
# and reading the third from a second call would be two reads of one view that
# could disagree. Everything below is about the pair being optional and about
# absent staying absent.
#
# The fixture's comparison window is deliberately narrow — 15 to 31 July catches
# INV-P2 (Khulna, 400,000) and nothing of Dhaka's, so one region has a history
# in it and the other has none.
COMPARISON = {"compare_from": "2026-07-15", "compare_to": "2026-07-31"}


def test_achievement_carries_the_named_comparison_window(ctx: ToolContext) -> None:
    result = run(ctx, "get_target_achievement", group_by="region", limit=20,
                 **COMPARISON)
    rows = {row["code"]: row for row in result.rows}

    # Khulna sold 400,000 in that window and 300,000 in this one.
    assert rows["REG002"]["previous_sales"] == pytest.approx(400_000)
    assert rows["REG002"]["growth_percent"] == pytest.approx(-25.0)

    # The headline grows the same actual against the same window, so the card
    # and the table cannot tell different stories about one period.
    assert result.values["previous"] == pytest.approx(400_000)
    assert result.values["growth_percent"] == pytest.approx(350.0)
    # And it says which window, because "growth" without two dates is a claim
    # the reader cannot check.
    assert result.values["compare_from"] == "2026-07-15"
    assert result.values["compare_to"] == "2026-07-31"


def test_a_group_absent_from_the_comparison_window_has_no_growth(
    ctx: ToolContext,
) -> None:
    """Absent is not zero, and growth from nothing is not -100%.

    Dhaka has no sale between 15 and 31 July. Filling ``previous_sales`` with
    0.0 would draw a last-period bar at the axis for a period nobody measured,
    and the -100% that follows from it would be the worst figure on the chart.
    """
    result = run(ctx, "get_target_achievement", group_by="region", limit=20,
                 **COMPARISON)
    dhaka = next(row for row in result.rows if row["code"] == "REG001")

    assert dhaka["actual_sales"] == pytest.approx(1_500_000)
    assert dhaka["previous_sales"] is None
    assert dhaka["growth_percent"] is None


def test_without_the_pair_neither_key_exists(ctx: ToolContext) -> None:
    """Not present-and-null: absent.

    Every achievement table the assistant renders is built from these keys, so
    two columns of dashes on every one of them would be two columns a reader
    learns to ignore — and a growth against a window the caller never named
    would be this layer inventing the comparison.
    """
    result = run(ctx, "get_target_achievement", group_by="region", limit=20)

    for row in result.rows:
        assert "previous_sales" not in row
        assert "growth_percent" not in row
    for key in ("previous", "growth_percent", "compare_from", "compare_to"):
        assert key not in result.values


def test_earlier_years_are_the_window_shifted_back_whole_years(
    ctx: ToolContext,
) -> None:
    """The same comparison the trend makes, so one card cannot mean two things.

    The dashboard's region card draws two earlier years beside the plan and the
    outcome. They are this window shifted back by whole years — never a period
    the tool chose — which is what honours the date filter and what lets the
    bar and the trend line above it agree about a year.
    """
    from conftest_phase2 import sales_row
    from conftest_phase3 import _load

    _load(ctx.session.get_bind(), "sales", [
        sales_row(**{"Invoice No": "INV-PY1", "Date": "2025-08-10",
                     "Quantity": 100, "Gross Sales": 1_200_000,
                     "Discount": 200_000, "Cost": 700_000,
                     "Source Transaction Id": "SRC-PY1"}),
    ])
    ctx.session.rollback()

    result = run(ctx, "get_target_achievement", group_by="region", limit=20,
                 compare_years=2)
    dhaka = next(row for row in result.rows if row["code"] == "REG001")
    # August 2025 is August 2026 shifted back one year.
    assert dhaka["net_sales_minus_1"] == pytest.approx(1_000_000)

    # Nothing two years back, so that year is dropped rather than drawn along
    # the floor — the rule ``_multi_year_trend`` states, applied here.
    assert all("net_sales_minus_2" not in row for row in result.rows)
    keys = {line["key"] for line in result.chart.series}
    assert "net_sales_minus_1" in keys and "net_sales_minus_2" not in keys
    # The plan and the outcome are named beside them, so the browser reads
    # every bar off one list.
    assert {"target_amount", "actual_sales"} <= keys


def test_a_region_absent_from_an_earlier_year_has_no_bar_there(
    ctx: ToolContext,
) -> None:
    """Absent, not zero — a bar at the axis would say it traded and sold none."""
    from conftest_phase2 import sales_row
    from conftest_phase3 import _load

    _load(ctx.session.get_bind(), "sales", [
        sales_row(**{"Invoice No": "INV-PY1", "Date": "2025-08-10",
                     "Quantity": 100, "Gross Sales": 1_200_000,
                     "Discount": 200_000, "Cost": 700_000,
                     "Source Transaction Id": "SRC-PY1"}),
    ])
    ctx.session.rollback()

    result = run(ctx, "get_target_achievement", group_by="region", limit=20,
                 compare_years=1)
    khulna = next(row for row in result.rows if row["code"] == "REG002")
    assert khulna["net_sales_minus_1"] is None


def test_without_compare_years_no_row_carries_a_year(ctx: ToolContext) -> None:
    """The Target page and the assistant call this tool too, and are unchanged."""
    result = run(ctx, "get_target_achievement", group_by="region", limit=20)
    for row in result.rows:
        assert not [key for key in row if key.startswith("net_sales_minus_")]
    assert result.chart.series == [], "no years asked for, so no series named"


@pytest.mark.parametrize("half", ["compare_from", "compare_to"])
def test_half_a_comparison_window_is_refused(ctx: ToolContext, half: str) -> None:
    """One date without the other has no window to measure, so it is rejected.

    Reading the missing half as "the start of the report window" would answer a
    question nobody asked and label it as the caller's own. The refusal is the
    ordinary argument refusal rather than an exception, because that is what a
    caller — the planner or a page — has to be able to act on.
    """
    invocation = execute_tool(ctx, "get_target_achievement", {
        "date_from": "2026-08-01", "date_to": "2026-08-31", "filters": {},
        "group_by": "region", "limit": 20, half: COMPARISON[half],
    })
    assert invocation.success is False
    assert invocation.error_code == "INVALID_ARGUMENTS"
    assert invocation.result is None


def test_target_gap_lists_shortfalls_largest_first(ctx: ToolContext) -> None:
    result = run(ctx, "get_target_gap", group_by="region", limit=20)
    gaps = [row["gap"] for row in result.rows]
    assert gaps == sorted(gaps, reverse=True)
    assert all(g > 0 for g in gaps)


# --------------------------------------------------------------------------
# Business summary, alerts, root cause
# --------------------------------------------------------------------------


def test_business_summary_combines_every_metric(ctx: ToolContext) -> None:
    result = run(ctx, "get_business_summary")
    values = result.values
    assert values["sales"] == pytest.approx(1_800_000)
    assert values["target"] == pytest.approx(3_000_000)
    # No receivables in the snapshot: the keys are gone, not zeroed.
    assert "collection" not in values
    assert "outstanding" not in values
    assert "overdue" not in values
    assert values["achievement_percent"] == pytest.approx(60.0)
    assert values["top_region"]["code"] == "REG001"
    assert values["bottom_region"]["code"] == "REG002"
    # Stock's contribution is availability and money at risk, not a count of
    # SKUs below a coverage threshold the material model cannot compute.
    assert values["unrestricted_stock"] == pytest.approx(815)
    assert values["expired_stock"] == pytest.approx(50)


def test_business_alerts_have_severity_and_thresholds(ctx: ToolContext) -> None:
    result = run(ctx, "get_business_alerts")
    assert result.rows
    for alert in result.rows:
        assert alert["severity"] in {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
        assert alert["alert_type"]
        assert alert["recommended_attention"]
    types = {a["alert_type"] for a in result.rows}
    assert "LOW_ACHIEVEMENT" in types
    # Shelf life replaced coverage as the stock alert: expired stock is money
    # already lost rather than a forecast built on a join that no longer exists.
    assert "EXPIRED_STOCK" in types
    # Most severe first.
    assert result.rows[0]["severity"] == "CRITICAL"


def test_root_cause_separates_fact_from_interpretation(ctx: ToolContext) -> None:
    result = run(ctx, "get_root_cause_analysis", compare_from="2026-07-01",
                 compare_to="2026-07-31", top_n=5)
    assert result.facts
    assert result.interpretations
    assert any("declined" in fact for fact in result.facts)
    # Interpretations must be hedged, never stated as proven causation.
    joined = " ".join(result.interpretations).lower()
    assert "appears to be" in joined or "may have" in joined or "no single" in joined
    assert "because of" not in joined

    contributors = result.values["contributors"]
    assert set(contributors) == {"region", "brand", "territory"}


def test_root_cause_identifies_the_declining_region(ctx: ToolContext) -> None:
    result = run(ctx, "get_root_cause_analysis", compare_from="2026-07-01",
                 compare_to="2026-07-31", top_n=5)
    regions = {row["code"]: row for row in result.values["contributors"]["region"]}
    assert regions["REG001"]["change"] == pytest.approx(-500_000)


# --------------------------------------------------------------------------
# Empty data
# --------------------------------------------------------------------------


def test_a_period_with_no_data_returns_a_clear_note_not_a_fake_number(ctx) -> None:
    result = run(ctx, "get_sales_summary", date_from="2020-01-01", date_to="2020-01-31")
    assert result.value is None
    assert result.rows == []
    assert any("No data found" in note for note in result.notes)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_validation_rejects_nan_and_infinity() -> None:
    result = ToolResult(tool="get_sales_summary", value=float("nan"))
    assert not validate_tool_result(result).ok
    result = ToolResult(tool="get_sales_summary", value=float("inf"))
    assert not validate_tool_result(result).ok


def test_validation_rejects_an_impossible_percentage() -> None:
    result = ToolResult(tool="get_sales_summary",
                        values={"gross_margin_percent": 10_000_000.0})
    assert not validate_tool_result(result).ok


def test_validation_rejects_a_backwards_date_range() -> None:
    result = ToolResult(tool="get_sales_summary", date_from=dt.date(2026, 8, 31),
                        date_to=dt.date(2026, 8, 1))
    assert not validate_tool_result(result).ok


def test_validation_rejects_a_window_that_does_not_match_the_request() -> None:
    expected = ResolvedDateRange(type=DateRangeType.THIS_MONTH,
                                 date_from=dt.date(2026, 8, 1),
                                 date_to=dt.date(2026, 8, 31), label="This month")
    result = ToolResult(tool="get_sales_summary", date_from=dt.date(2026, 7, 1),
                        date_to=dt.date(2026, 7, 31))
    assert not validate_tool_result(result, expected).ok


def test_stock_is_allowed_to_ignore_the_window() -> None:
    expected = ResolvedDateRange(type=DateRangeType.THIS_MONTH,
                                 date_from=dt.date(2026, 8, 1),
                                 date_to=dt.date(2026, 8, 31), label="This month")
    result = ToolResult(tool="get_stock_summary", date_from=dt.date(2026, 1, 1),
                        date_to=dt.date(2026, 1, 31))
    outcome = validate_tool_result(result, expected)
    assert outcome.ok
    assert outcome.warnings


def test_every_real_tool_result_passes_validation(ctx: ToolContext) -> None:
    for tool in ("get_sales_summary", "get_business_summary",
                 "get_target_achievement", "get_stock_summary", "get_expiring_stock",
                 "get_business_alerts"):
        result = run(ctx, tool)
        assert validate_tool_result(result).ok, tool


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------


def test_indian_digit_grouping() -> None:
    assert group_indian(1000) == "1,000"
    assert group_indian(100000) == "1,00,000"
    assert group_indian(18700000) == "1,87,00,000"
    assert group_indian(-52400) == "-52,400"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (18_700_000, "৳1.87 Cr"),
        (5_240_000, "৳52.40 L"),
        (52_400, "৳52.4 K"),
        (940, "৳940"),
        (None, "—"),
    ],
)
def test_amounts_use_lakh_and_crore(value, expected) -> None:
    assert format_amount(value) == expected


def test_raw_amount_is_available_uncompacted() -> None:
    assert format_amount(18_700_000, compact=False) == "৳1,87,00,000"


def test_percentages_and_missing_ratios() -> None:
    assert format_percent(89.44) == "89.4%"
    assert format_percent(8.4, signed=True) == "+8.4%"
    assert format_percent(-8.4, signed=True) == "-8.4%"
    assert format_percent(None) == "n/a"       # never rendered as 0%


def test_table_rendering_right_aligns_numbers() -> None:
    rows = [{"label": "Dhaka", "quantity": 170, "net_sales": 52_400_000}]
    table = render_table(rows, ("label", "quantity", "net_sales"))
    assert "| Name | Qty | Net Sales |" in table
    assert "---:" in table
    assert "৳5.24 Cr" in table


def test_formatter_labels_interpretation_separately(ctx: ToolContext) -> None:
    result = run(ctx, "get_root_cause_analysis", compare_from="2026-07-01",
                 compare_to="2026-07-31")
    text = ResponseFormatter().format(Intent.ROOT_CAUSE_ANALYSIS, [result])
    assert "**Facts**" in text
    assert "Interpretation" in text
    assert "analysis, not measured fact" in text


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


def test_csv_export_has_a_bom_for_excel() -> None:
    from app.ai.export import to_csv

    payload = to_csv([{"label": "Dhaka", "net_sales": 100}])
    assert payload.startswith("﻿".encode("utf-8"))
    assert b"Net Sales" in payload


def test_xlsx_export_is_a_real_workbook() -> None:
    import io

    from openpyxl import load_workbook

    from app.ai.export import to_xlsx

    payload = to_xlsx([{"label": "Dhaka", "net_sales": 100}], "Sales")
    workbook = load_workbook(io.BytesIO(payload))
    assert workbook.active.title == "Sales"
    assert workbook.active["A1"].value == "Name"


def test_pdf_export_is_a_real_pdf() -> None:
    from app.ai.export import to_pdf

    payload = to_pdf([{"label": "Dhaka", "net_sales": 100}], "Sales")
    assert payload.startswith(b"%PDF")


def test_export_of_no_rows_says_so_rather_than_failing() -> None:
    from app.ai.export import to_csv, to_pdf, to_xlsx

    assert to_csv([]) is not None
    assert to_xlsx([]) is not None
    assert to_pdf([]).startswith(b"%PDF")


# --------------------------------------------------------------------------
# Who missed target: the narrowing happens before the limit
# --------------------------------------------------------------------------


def _achievements(ctx: ToolContext, **arguments) -> list[tuple[str, float | None]]:
    result = run(ctx, "get_target_achievement", group_by="territory", **arguments)
    return [(row["label"], row["achievement_percent"]) for row in result.rows]


def test_an_unmeasurable_achievement_never_ranks_as_the_best(ctx: ToolContext) -> None:
    """A group with no target is neither the best performer nor the worst.

    Achievement is a ratio, and a group with no target has none — the sort used
    to put those rows first, so "top 1 territory achievement" answered with the
    one territory nobody had set a target for, presented as the leader.
    """
    ranked = _achievements(ctx, limit=1)
    assert ranked, "expected at least one territory"
    assert ranked[0][1] is not None, ranked

    everything = _achievements(ctx, limit=500)
    unmeasurable = [row for row in everything if row[1] is None]
    if unmeasurable:
        assert everything[-len(unmeasurable):] == unmeasurable, everything


def test_a_threshold_is_tested_against_every_group_not_the_top_few(
    ctx: ToolContext,
) -> None:
    """"Which territories are below 80%?" asked of all of them, not the best.

    The filter used to run over a list already cut to the top ``limit``
    performers, so the threshold was tested against exactly the groups it was
    not about. With a limit of 1 the answer was reliably empty while groups were
    short — an inverted answer that renders identically to a correct one.
    """
    everyone = run(ctx, "get_target_achievement", group_by="territory", limit=500,
                   below_percent=80.0)
    assert everyone.rows, "fixture must have a territory below 80%"

    capped = run(ctx, "get_target_achievement", group_by="territory", limit=1,
                 below_percent=80.0)
    assert len(capped.rows) == 1
    assert capped.truncated is (len(everyone.rows) > 1)

    # The worst comes first, because that is who the question is about.
    below = [row["achievement_percent"] for row in everyone.rows]
    assert below == sorted(below), below
    assert capped.rows[0]["achievement_percent"] == below[0]
    assert all(value < 80.0 for value in below)


def test_a_gap_question_shows_the_biggest_shortfall_first(ctx: ToolContext) -> None:
    """A capped gap list must not hide the largest gap.

    ``get_target_gap`` with a limit of one returned nothing at all while two
    groups were short, and with a limit of two returned the *least* short of
    them. Both are the opposite of what the tool's own description promises.
    """
    everyone = run(ctx, "get_target_gap", group_by="territory", limit=500)
    assert everyone.rows, "fixture must have a territory short of target"

    gaps = [row["gap"] for row in everyone.rows]
    assert gaps == sorted(gaps, reverse=True), gaps
    assert all(gap > 0 for gap in gaps)

    capped = run(ctx, "get_target_gap", group_by="territory", limit=1)
    assert len(capped.rows) == 1
    assert capped.rows[0]["gap"] == gaps[0]


def test_a_condition_that_matches_nothing_says_so(ctx: ToolContext) -> None:
    """An empty table under a live headline must be explained, not left blank.

    The totals are still on screen, so a table that is simply missing reads as a
    rendering fault. "No territory is below 1% of target" is the finding.
    """
    result = run(ctx, "get_target_achievement", group_by="territory", limit=20,
                 filters={"territory_codes": ["TR001"]}, below_percent=50.0)
    assert not result.rows, [row["achievement_percent"] for row in result.rows]
    assert any("below 50% of target" in note for note in result.notes), result.notes


def test_the_headline_covers_the_whole_scope_not_the_narrowed_list(
    ctx: ToolContext,
) -> None:
    """Filtering changes who is listed, never what the period achieved.

    The headline answers "how did we do" and the table answers "who". Recomputing
    the total over the under-performers would report the business as doing worse
    than it did, under the same label.
    """
    everyone = run(ctx, "get_target_achievement", group_by="territory", limit=500)
    narrowed = run(ctx, "get_target_achievement", group_by="territory", limit=500,
                   below_percent=80.0)
    assert narrowed.values["target"] == everyone.values["target"]
    assert narrowed.values["actual"] == everyone.values["actual"]


# --------------------------------------------------------------------------
# The tool boundary, checked over the whole registry
# --------------------------------------------------------------------------


def test_no_tool_accepts_an_argument_it_did_not_declare() -> None:
    """Every input model, not just the base one.

    ``BaseToolInput`` forbidding extras was already pinned, but a tool declares
    its own model and one written without ``extra="forbid"`` would accept
    anything the caller invented — a ``sql`` field, an ``order_by``, a raw
    ``where``. Derived from the registry so a tool added tomorrow is covered on
    the day it is registered rather than the day somebody remembers to add it
    here.
    """
    for name, spec in REGISTRY.items():
        assert spec.input_model.model_config.get("extra") == "forbid", name


def test_no_tool_can_be_asked_for_more_rows_than_the_ceiling() -> None:
    """A limit is clamped, never honoured, however it arrives.

    ``/api/reports/*`` refuses an oversized limit with a 422; the tool path
    takes its limit from a validated query and clamps it instead, because the
    caller here is the planner rather than a person and a refusal would turn a
    plausible question into an error. Either way no caller reaches an unbounded
    read.
    """
    from app.ai.schemas import MAX_LIMIT

    bounded = [name for name, spec in REGISTRY.items()
               if "limit" in spec.input_model.model_fields]
    assert bounded, "expected some tools to take a limit"

    for name in bounded:
        model = REGISTRY[name].input_model
        instance = model.model_validate({
            "date_from": "2026-08-01", "date_to": "2026-08-31",
            "limit": 1_000_000,
        })
        assert instance.limit <= MAX_LIMIT, name
        assert model.model_validate({
            "date_from": "2026-08-01", "date_to": "2026-08-31", "limit": 0,
        }).limit >= 1, name


def test_every_tool_demands_the_dates_it_reports_on() -> None:
    """A window is mandatory, so no tool can read the whole table by omission.

    The stock tools are the deliberate exception the schema itself encodes: a
    material stock position carries no posting date, so a date window would
    filter on a column that does not exist.
    """
    for name, spec in REGISTRY.items():
        fields = spec.input_model.model_fields
        if "date_from" not in fields:
            continue
        assert fields["date_from"].is_required(), name
        assert fields["date_to"].is_required(), name
