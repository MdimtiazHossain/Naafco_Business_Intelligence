"""Brand-wise performance: the general ranking that replaced product performance.

The rule this module holds to the wall is that "top products" is a *brand*
question everywhere except the Product Analysis page, and that a brand's Volume
is one figure — produced without ever adding a kilogram to a litre.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.ai import queries as q
from app.ai.intent import detect_intent
from app.ai.schemas import (
    BaseToolInput,
    GroupBy,
    GroupedToolInput,
    Intent,
    ScopeFilters,
)
from app.ai.tools import ToolContext, execute_tool
from app.ai.permission_filter import PermissionFilter, UserContext
from app.etl.mapping import MasterDataIndex
from app.etl.pipeline import run_import
from app.etl.readers import RecordsSourceReader
from conftest_phase2 import make_material, sales_row, target_row
from test_platform_api import auth, card, login, platform  # noqa: F401 - fixture reuse

WINDOW = (dt.date(2026, 8, 1), dt.date(2026, 8, 31))


def _load(engine: Engine, records: list[dict]) -> None:
    reader = RecordsSourceReader(records, source_name="t.csv", source_type="CSV")
    result = run_import(engine, "sales", reader, source_system="TEST")
    assert result.rejected_rows == 0, result.error_counts


@pytest.fixture
def brand_engine(seeded_engine: Engine) -> Engine:
    """Three brands across five materials, so a ranking has something to rank.

    "Example Brand" is the seeded pair (SKU001, SKU002). Two more brands are
    added here so a ranking has more than one shape to rank.

    No ``pack_unit`` anywhere. It used to be set on every row, because that was
    where a *target's* volume unit came from — a target named a SKU and the
    Product Master said what that SKU was measured in. Revision 0022 removed
    that master, the Material Master states no unit of measure, and both volumes
    are now the plain number the source file stated.
    """
    with Session(seeded_engine) as session:
        session.add(make_material("SKU003", "Spice Powder 200g",
                                  group="MG03", group_name="Grocery",
                                  brand_code="MB03", brand="Zesty"))
        session.add(make_material("SKU004", "Cooking Oil 1L",
                                  group="MG03", group_name="Grocery",
                                  brand_code="MB04", brand="Golden"))
        session.add(make_material("SKU005", "Ghee 500g",
                                  group="MG03", group_name="Grocery",
                                  brand_code="MB04", brand="Golden"))
        session.commit()

    _load(seeded_engine, [
        # Example Brand — the biggest by net sales. 500 + 200 = 700.
        sales_row(**{"Invoice No": "INV-B1", "Date": "2026-08-10", "SKU Code": "SKU001",
                     "Quantity": 100, "Gross Sales": 1_000_000, "Discount": 0,
                     "Cost": 600_000, "Total Volume": 500}),
        sales_row(**{"Invoice No": "INV-B2", "Date": "2026-08-11", "SKU Code": "SKU002",
                     "Quantity": 40, "Gross Sales": 400_000, "Discount": 0,
                     "Cost": 250_000, "Total Volume": 200}),
        # Zesty — two lines across two regions. 12,000 + 5,000 = 17,000.
        sales_row(**{"Invoice No": "INV-B3", "Date": "2026-08-12", "SKU Code": "SKU003",
                     "Quantity": 60, "Gross Sales": 600_000, "Discount": 0,
                     "Cost": 300_000, "Total Volume": 12_000}),
        # Golden — two SKUs whose pack units differ (LTR and KG), which used to
        # make its volume unreportable. 300 + 50 = 350.
        sales_row(**{"Invoice No": "INV-B4", "Date": "2026-08-13", "SKU Code": "SKU004",
                     "Quantity": 30, "Gross Sales": 200_000, "Discount": 0,
                     "Cost": 120_000, "Total Volume": 300}),
        sales_row(**{"Invoice No": "INV-B5", "Date": "2026-08-14", "SKU Code": "SKU005",
                     "Quantity": 10, "Gross Sales": 100_000, "Discount": 0,
                     "Cost": 60_000, "Total Volume": 50}),
        # Khulna, so a region filter has something to exclude.
        sales_row(**{"Invoice No": "INV-B6", "Date": "2026-08-15", "SKU Code": "SKU003",
                     "Quantity": 25, "Gross Sales": 250_000, "Discount": 0,
                     "Cost": 150_000, "Total Volume": 5_000,
                     "Territory Code": None, "Area Code": "AR002"}),
    ])
    return seeded_engine


def _context(session: Session) -> ToolContext:
    user = UserContext(user_id=1, username="ceo", display_name="MD",
                       role="MANAGEMENT", data_scope={})
    index = MasterDataIndex(session)
    return ToolContext(session, PermissionFilter(session, user, index), user,
                       dt.date(2026, 8, 15))


def _result(session: Session, **filters):
    ctx = _context(session)
    arguments = GroupedToolInput(date_from=WINDOW[0], date_to=WINDOW[1],
                                 filters=ScopeFilters(**filters), limit=15)
    return execute_tool(ctx, "get_material_brand_performance",
                        arguments.model_dump(mode="json")).result


def _brands(session: Session, **filters) -> list[dict]:
    return _result(session, **filters).rows


# ---------------------------------------------------------------------------
# Aggregation through the Product Master
# ---------------------------------------------------------------------------


def test_sales_are_grouped_by_brand_not_by_sku(brand_engine):
    with Session(brand_engine) as session:
        rows = _brands(session)

    # Four SKUs collapse into three brands.
    assert [row["label"] for row in rows] == ["Example Brand", "Zesty", "Golden"]
    assert len(rows) == 3


def test_the_ranking_is_net_sales_descending_and_carries_a_rank(brand_engine):
    with Session(brand_engine) as session:
        rows = _brands(session)

    assert [row["rank"] for row in rows] == [1, 2, 3]
    net_sales = [row["net_sales"] for row in rows]
    assert net_sales == sorted(net_sales, reverse=True)
    # Example Brand: 1,000,000 + 400,000.
    assert rows[0]["net_sales"] == pytest.approx(1_400_000)
    # Zesty: 600,000 in Dhaka plus 250,000 in Khulna.
    assert rows[1]["net_sales"] == pytest.approx(850_000)


def test_quantity_is_summed_per_brand(brand_engine):
    with Session(brand_engine) as session:
        rows = {row["label"]: row for row in _brands(session)}

    assert rows["Example Brand"]["quantity"] == pytest.approx(140)
    assert rows["Golden"]["quantity"] == pytest.approx(40)


# ---------------------------------------------------------------------------
# Volume: the sum of the Total Volume the source stated, one figure per brand
# ---------------------------------------------------------------------------


def test_volume_is_one_figure_per_brand(brand_engine):
    with Session(brand_engine) as session:
        rows = {row["label"]: row for row in _brands(session)}

    # 500 + 200.
    assert rows["Example Brand"]["volume"] == pytest.approx(700)
    assert isinstance(rows["Example Brand"]["volume"], float)


def test_a_brand_is_the_sum_of_its_lines(brand_engine):
    """Zesty's two lines, across two regions, add up. No conversion involved."""
    with Session(brand_engine) as session:
        rows = {row["label"]: row for row in _result(session).rows}

    assert rows["Zesty"]["volume"] == pytest.approx(17_000)


def test_differing_pack_units_no_longer_suppress_a_brand_volume(brand_engine):
    """Golden's two SKUs are packed in litres and kilograms.

    Under the derived-volume rule that made its figure unreportable — the two
    dimensions could not be added. The lines now state their own totals, which
    add up like any other measure.
    """
    with Session(brand_engine) as session:
        result = _result(session)
    rows = {row["label"]: row for row in result.rows}

    assert rows["Golden"]["volume"] == pytest.approx(350)
    assert not any("kilograms to litres" in note for note in result.notes)


def test_the_unit_never_travels_as_a_row_field(brand_engine):
    """Exports derive their columns from the row keys, so this is the guard."""
    with Session(brand_engine) as session:
        result = _result(session)

    for row in result.rows:
        assert "volume_basis" not in row
        assert "volume_unit" not in row
    # …and no basis map beside them either: there is no basis to report.
    assert "volume_basis" not in result.values


# ---------------------------------------------------------------------------
# The executive brand table: plan against actual
# ---------------------------------------------------------------------------


def _brand_targets(session: Session, **filters) -> dict[str, dict]:
    """``get_material_brand_target_performance`` rows, keyed by brand."""
    ctx = _context(session)
    arguments = GroupedToolInput(date_from=WINDOW[0], date_to=WINDOW[1],
                                 filters=ScopeFilters(**filters), limit=15)
    result = execute_tool(ctx, "get_material_brand_target_performance",
                          arguments.model_dump(mode="json")).result
    return {row["label"]: row for row in result.rows}


def test_the_brand_table_ranks_by_net_sales_exactly_as_the_plain_one_does(brand_engine):
    """§10: the ranking methodology is preserved, not reinvented.

    Both tools rank through the same ``_grouped_sales`` call, so this is the
    guard that adding targets did not quietly reorder the league table.
    """
    with Session(brand_engine) as session:
        plain = [row["label"] for row in _brands(session)]
        with_targets = list(_brand_targets(session))

    assert with_targets == plain
    with Session(brand_engine) as session:
        rows = _brand_targets(session)
    assert [rows[label]["rank"] for label in plain] == [1, 2, 3]


def test_target_amount_and_volume_are_summed_per_brand(brand_engine):
    """§5/§9: targets reach brand through the SKU the target names."""
    _load_targets(brand_engine, [
        # Two SKUs of one brand: their targets must add up to that brand's.
        target_row(**{"Target Amount": 1_000_000, "SKU Code": "SKU001",
                      "Target Volume": 400}),
        target_row(**{"Target Amount": 400_000, "SKU Code": "SKU002",
                      "Target Volume": 200}),
    ])
    with Session(brand_engine) as session:
        rows = _brand_targets(session)

    example = rows["Example Brand"]
    assert example["target_amount"] == pytest.approx(1_400_000)
    assert example["target_volume"] == pytest.approx(600)
    # Sales are unchanged by the target join — the guard against a fan-out
    # silently multiplying either side.
    assert example["net_sales"] == pytest.approx(1_400_000)
    assert example["volume"] == pytest.approx(700)


def test_a_second_target_row_does_not_multiply_the_sales_figure(brand_engine):
    """§9: adding target rows must not inflate what the brand sold."""
    with Session(brand_engine) as session:
        before = _brand_targets(session)["Example Brand"]

    _load_targets(brand_engine, [
        target_row(**{"Target Amount": 500_000, "SKU Code": "SKU001",
                      "Target Volume": 100}),
        target_row(**{"Target Amount": 500_000, "SKU Code": "SKU002",
                      "Target Volume": 100}),
    ])
    with Session(brand_engine) as session:
        after = _brand_targets(session)["Example Brand"]

    assert after["net_sales"] == pytest.approx(before["net_sales"])
    assert after["volume"] == pytest.approx(before["volume"])
    assert after["quantity"] == pytest.approx(before["quantity"])


def test_achievement_is_actual_over_target(brand_engine):
    """§7: Sales / Target x 100, for both volume and value."""
    _load_targets(brand_engine, [
        # Example Brand sells 1,400,000 against 2,000,000 -> 70%.
        # Its volume is 700 KG against a 1,000 KG plan -> 70%.
        target_row(**{"Target Amount": 1_600_000, "SKU Code": "SKU001",
                      "Target Volume": 800}),
        target_row(**{"Target Amount": 400_000, "SKU Code": "SKU002",
                      "Target Volume": 200}),
    ])
    with Session(brand_engine) as session:
        example = _brand_targets(session)["Example Brand"]

    assert example["achievement_percent"] == pytest.approx(70)
    assert example["volume_achievement_percent"] == pytest.approx(70)


def test_shortfall_is_actual_minus_target_so_missing_reads_negative(brand_engine):
    """§7: the subtraction is Sales - Target and is explicitly not reversed."""
    _load_targets(brand_engine, [
        target_row(**{"Target Amount": 2_000_000, "SKU Code": "SKU001",
                      "Target Volume": 1_000}),
    ])
    with Session(brand_engine) as session:
        example = _brand_targets(session)["Example Brand"]

    # Sold 1,400,000 against a 2,000,000 plan: 600,000 short, and negative.
    assert example["amount_shortfall"] == pytest.approx(-600_000)
    # 700 KG against 1,000 KG.
    assert example["volume_shortfall"] == pytest.approx(-300)


def test_beating_the_target_reads_positive(brand_engine):
    _load_targets(brand_engine, [
        target_row(**{"Target Amount": 1_000_000, "SKU Code": "SKU001",
                      "Target Volume": 500}),
    ])
    with Session(brand_engine) as session:
        example = _brand_targets(session)["Example Brand"]

    assert example["amount_shortfall"] == pytest.approx(400_000)
    assert example["volume_shortfall"] == pytest.approx(200)
    assert example["achievement_percent"] == pytest.approx(140)


def test_a_brand_with_no_target_reports_n_a_not_zero_or_infinity(brand_engine):
    """§8: a zero denominator suppresses the ratio, per the project convention."""
    with Session(brand_engine) as session:
        rows = _brand_targets(session)          # no targets loaded at all

    for row in rows.values():
        assert row["target_amount"] == 0.0
        assert row["achievement_percent"] is None
        assert row["volume_achievement_percent"] is None
        # The shortfall is still a real number: everything sold, nothing planned.
        assert row["amount_shortfall"] == pytest.approx(row["net_sales"])


def test_both_sides_of_a_brands_volume_are_plain_sums(brand_engine):
    """The two sides of the row are read the same way, and this proves it.

    This test used to assert the opposite. Golden's two materials were packed in
    litres and kilograms, a target's unit was the SKU's Pack Unit from the
    Product Master, and so its *target* volume had two physical dimensions and
    was suppressed while its *sales* volume — a stated total with no unit — was a
    number.

    Revision 0022 removed that master. The Material Master states no unit of
    measure, so a planned volume is the number the planner typed, exactly as a
    sales volume is the number the invoice stated. Both are summed, and 250 + 60
    is 310.
    """
    _load_targets(brand_engine, [
        target_row(**{"Target Amount": 300_000, "SKU Code": "SKU004",
                      "Target Volume": 250}),
        target_row(**{"Target Amount": 100_000, "SKU Code": "SKU005",
                      "Target Volume": 60}),
    ])
    with Session(brand_engine) as session:
        golden = _brand_targets(session)["Golden"]

    assert golden["volume"] == pytest.approx(350)
    assert golden["target_volume"] == pytest.approx(310)
    # Both sides are figures now, so the ratio is answerable.
    assert golden["volume_achievement_percent"] == pytest.approx(350 / 310 * 100)
    assert golden["volume_shortfall"] == pytest.approx(350 - 310)
    # The value side is unaffected and always was.
    assert golden["target_amount"] == pytest.approx(400_000)
    assert golden["amount_shortfall"] == pytest.approx(300_000 - 400_000)


def test_a_filter_narrows_target_and_sales_together(brand_engine):
    """§11: both sides must answer the same filter and the same window."""
    _load_targets(brand_engine, [
        target_row(**{"Target Amount": 1_000_000, "SKU Code": "SKU003",
                      "Target Volume": 10_000}),
    ])
    with Session(brand_engine) as session:
        unfiltered = _brand_targets(session)["Zesty"]
        # REG002 (Khulna) holds only the second Zesty line.
        khulna = _brand_targets(session, region_codes=["REG002"])["Zesty"]

    assert unfiltered["net_sales"] == pytest.approx(850_000)
    assert khulna["net_sales"] == pytest.approx(250_000)
    # The target was set for TR001, which is in REG001 — so under a Khulna
    # filter the brand has sales and no target, and says so rather than
    # carrying the unfiltered target across.
    assert unfiltered["target_amount"] == pytest.approx(1_000_000)
    assert khulna["target_amount"] == 0.0
    assert khulna["achievement_percent"] is None


def test_the_row_carries_no_unit_field(brand_engine):
    """The single-volume-column rule holds for the target column too."""
    _load_targets(brand_engine, [
        target_row(**{"Target Amount": 100_000, "SKU Code": "SKU001",
                      "Target Volume": 50}),
    ])
    with Session(brand_engine) as session:
        rows = _brand_targets(session)

    for row in rows.values():
        assert "volume_unit" not in row
        assert "target_volume_unit" not in row
        assert "volume_basis" not in row


def test_each_column_reads_its_own_source_table(brand_engine):
    """Target from ``fact_target``, sales from ``fact_sales``, never crossed.

    The four figures are given deliberately different values, so a column that
    read the wrong table would land on a number this test can name. If Target Vol
    were derived from sales it would be 700; if Sales Vol were derived from
    targets it would be 600.
    """
    _load_targets(brand_engine, [
        target_row(**{"Target Amount": 1_000_000, "SKU Code": "SKU001",
                      "Target Volume": 400}),
        target_row(**{"Target Amount": 400_000, "SKU Code": "SKU002",
                      "Target Volume": 200}),
    ])
    with Session(brand_engine) as session:
        example = _brand_targets(session)["Example Brand"]

    # fact_target.target_volume: 400 + 200. Not the 700 KG that was sold.
    assert example["target_volume"] == pytest.approx(600)
    # fact_sales.volume: 500 + 200 KG. Not the 600 that was planned.
    assert example["volume"] == pytest.approx(700)
    # fact_target.target_amount: 1,000,000 + 400,000.
    assert example["target_amount"] == pytest.approx(1_400_000)
    # fact_sales.net_sales — which happens to equal the target here, so the two
    # ratios below are what distinguish "read the right column" from "read any".
    assert example["net_sales"] == pytest.approx(1_400_000)

    # Volume achievement uses volume from both sides; value achievement uses
    # amounts from both sides. Crossing them would give 100% and 85.7%.
    assert example["volume_achievement_percent"] == pytest.approx(700 / 600 * 100)
    assert example["achievement_percent"] == pytest.approx(100)


def test_the_two_sides_are_aggregated_independently_then_joined_on_brand(brand_engine):
    """No raw target row is ever joined to a raw sales row.

    This is the multiplication the source clarification warns about. The brand
    below has an *unequal* number of rows on each side — three sales lines
    against two targets — so a cross join would produce six combinations and
    inflate both sums by a factor this test can detect. Each side is aggregated
    to one figure per brand first, and only those two summaries meet.
    """
    # A third sales line for the same brand, so the row counts differ (3 vs 2).
    _load(brand_engine, [
        sales_row(**{"Invoice No": "INV-B7", "Date": "2026-08-09",
                     "SKU Code": "SKU001", "Quantity": 20,
                     "Gross Sales": 250_000, "Discount": 0, "Cost": 150_000,
                     "Total Volume": 100}),
    ])
    _load_targets(brand_engine, [
        target_row(**{"Target Amount": 1_000_000, "SKU Code": "SKU001",
                      "Target Volume": 400}),
        target_row(**{"Target Amount": 400_000, "SKU Code": "SKU002",
                      "Target Volume": 200}),
    ])
    with Session(brand_engine) as session:
        example = _brand_targets(session)["Example Brand"]

    # Sales: 500 + 200 + 100, and 1,000,000 + 400,000 + 250,000 BDT.
    assert example["volume"] == pytest.approx(800)
    assert example["net_sales"] == pytest.approx(1_650_000)
    # Targets: 400 + 200, and 1,000,000 + 400,000. Summed once each — a 3x2
    # cross join would have given 1,800 and 4,200,000.
    assert example["target_volume"] == pytest.approx(600)
    assert example["target_amount"] == pytest.approx(1_400_000)

    # And the quantity on the sales side is likewise unmultiplied: 100 + 40 + 20.
    assert example["quantity"] == pytest.approx(160)


def test_the_two_views_are_row_preserving_through_the_product_master(brand_engine):
    """The SKU -> Brand join is many-to-one, so it cannot fan out a measure.

    Asserted at the view level because this is the property the brand
    aggregation rests on: one fact row in, one view row out. A ``dim_product``
    that ever gained a duplicate ``sku_code`` would break it silently, and this
    is what would catch that.
    """
    with Session(brand_engine) as session:
        for fact, view in (("fact_sales", "vw_sales_detail"),
                           ("fact_target", "vw_target_detail")):
            facts = session.execute(text(
                f"SELECT COUNT(*) FROM {fact} WHERE is_void = 0")).scalar()
            rows = session.execute(text(f"SELECT COUNT(*) FROM {view}")).scalar()
            assert rows == facts, f"{view} does not preserve {fact} row count"


def test_the_plain_brand_tool_is_untouched_by_any_of_this(brand_engine):
    """The AI agent's brand answer must not have grown target columns."""
    _load_targets(brand_engine, [
        target_row(**{"Target Amount": 100_000, "SKU Code": "SKU001",
                      "Target Volume": 50}),
    ])
    with Session(brand_engine) as session:
        rows = _brands(session)

    for row in rows:
        assert "target_amount" not in row
        assert "target_volume" not in row
        assert "amount_shortfall" not in row
    # …and it still reports quantity, which only the dashboard table dropped.
    assert all("quantity" in row for row in rows)


# ---------------------------------------------------------------------------
# Target quantity and volume, through the ETL
# ---------------------------------------------------------------------------


def _load_targets(engine: Engine, records: list[dict]) -> None:
    reader = RecordsSourceReader(records, source_name="t.csv", source_type="CSV")
    result = run_import(engine, "target", reader, source_system="TEST")
    assert result.rejected_rows == 0, result.error_counts


def test_a_target_file_can_carry_quantity_and_volume(brand_engine):
    _load_targets(brand_engine, [
        target_row(**{"Target Amount": 1_000_000,
                      "Target Qty": 250, "Target Volume": 500}),
    ])
    with Session(brand_engine) as session:
        row = session.execute(text(
            "SELECT target_amount, target_quantity, target_volume "
            "FROM vw_target_detail ORDER BY target_id DESC LIMIT 1")).one()

    assert float(row[0]) == 1_000_000
    assert float(row[1]) == 250
    assert float(row[2]) == 500


def test_a_target_without_quantity_or_volume_still_loads(brand_engine):
    """``target_amount`` is the one required measure; the other two are optional."""
    _load_targets(brand_engine, [target_row(**{"Target Amount": 900_000})])
    with Session(brand_engine) as session:
        row = session.execute(text(
            "SELECT target_amount, target_quantity, target_volume FROM fact_target "
            "ORDER BY target_id DESC LIMIT 1")).one()

    assert float(row[0]) == 900_000
    # NULL, not zero: "no quantity target" and "a target of zero" differ.
    assert row[1] is None
    assert row[2] is None


def test_the_target_view_states_no_unit_of_measure(brand_engine):
    """A target volume is unit-free, and the view no longer offers a column for one.

    It used to expose ``target_volume_unit``, read from the SKU's Pack Unit in
    the Product Master. Revision 0022 removed that master and the Material Master
    states no unit, so the column went with it rather than becoming a NULL that
    every report would have had to render as "mixed".
    """
    _load_targets(brand_engine, [
        target_row(**{"Target Amount": 500_000, "SKU Code": "SKU003",
                      "Target Volume": 40}),
    ])
    with Session(brand_engine) as session:
        columns = {row[1] for row in session.execute(
            text("PRAGMA table_info(vw_target_detail)"))}

    assert "target_volume" in columns
    assert "target_volume_unit" not in columns
    # No unit-of-measure column of any kind. ``unit_code``/``unit_name`` are
    # excluded by name: "Unit" is also an organisational level between Area and
    # Territory, and that one is still very much a column.
    measures = columns - {"unit_code", "unit_name"}
    assert not any("unit" in name for name in measures)


def test_a_negative_target_quantity_is_rejected(brand_engine):
    reader = RecordsSourceReader(
        [target_row(**{"Target Amount": 100, "Target Qty": -5})],
        source_name="t.csv", source_type="CSV")
    result = run_import(brand_engine, "target", reader, source_system="TEST")

    assert result.rejected_rows == 1


def test_target_versus_actual_compares_quantity_as_well_as_value(brand_engine):
    _load_targets(brand_engine, [
        target_row(**{"Target Amount": 2_000_000,
                      "Target Qty": 200, "Target Volume": 400}),
    ])
    with Session(brand_engine) as session:
        rows, totals, _ = q.target_vs_actual(
            session, ScopeFilters(), WINDOW[0], WINDOW[1], GroupBy.REGION)
    by_code = {row["code"]: row for row in rows}

    # The target is set for TR001, which the master puts in REG001.
    dhaka = by_code["REG001"]
    assert dhaka["target_quantity"] == pytest.approx(200)
    assert dhaka["actual_quantity"] == pytest.approx(240)
    assert dhaka["quantity_achievement_percent"] == pytest.approx(120)
    assert dhaka["quantity_gap"] == pytest.approx(-40)
    # The volume target is one figure, folded within its dimension.
    assert dhaka["target_volume"] == pytest.approx(400)
    assert totals["target_quantity"] == pytest.approx(200)


def test_a_target_summary_sums_the_volume_the_file_stated(brand_engine):
    # Different materials, because the target business key includes the item —
    # two rows for one month, territory and material are one target, not two.
    _load_targets(brand_engine, [
        target_row(**{"Target Amount": 1_000, "SKU Code": "SKU003",
                      "Target Volume": 12_000}),
        target_row(**{"Target Amount": 2_000, "SKU Code": "SKU001",
                      "Target Volume": 5}),
    ])
    with Session(brand_engine) as session:
        ctx = _context(session)
        arguments = BaseToolInput(date_from=WINDOW[0], date_to=WINDOW[1])
        result = execute_tool(ctx, "get_target_summary",
                              arguments.model_dump(mode="json")).result

    # 12,000 + 5. These used to be grams and kilograms, folded to 17 KG through
    # the SKU's Pack Unit; with no unit to fold by, the planner's own numbers add
    # up as they stand.
    assert result.values["target_volume"] == pytest.approx(12_005)


def test_a_target_with_no_volume_is_excluded_rather_than_counted_as_zero(brand_engine):
    """An unset volume target is unknown, not nil — and the answer says how many.

    This replaces a test that asserted two targets in different physical
    dimensions produced no single figure. There are no dimensions left to
    conflict: revision 0022 removed the unit a target volume used to inherit. The
    honesty this file was guarding is still worth a test, so it now guards the
    case that *does* still suppress information — a target the file left blank.
    """
    _load_targets(brand_engine, [
        target_row(**{"Target Amount": 1_000, "SKU Code": "SKU001",
                      "Target Volume": 50}),
        target_row(**{"Target Amount": 2_000, "SKU Code": "SKU004"}),
    ])
    with Session(brand_engine) as session:
        ctx = _context(session)
        arguments = BaseToolInput(date_from=WINDOW[0], date_to=WINDOW[1])
        result = execute_tool(ctx, "get_target_summary",
                              arguments.model_dump(mode="json")).result

    # The stated volume alone — the blank one is not folded in as a zero.
    assert result.values["target_volume"] == pytest.approx(50)
    assert any("set no volume" in note for note in result.notes)


def test_a_voided_target_leaves_every_target_view(brand_engine):
    """The rebuilt views must keep the void clause revision 0009 added."""
    _load_targets(brand_engine, [
        target_row(**{"Target Amount": 777_000, "Target Qty": 77}),
    ])
    with Session(brand_engine) as session:
        before = session.execute(text(
            "SELECT COUNT(*) FROM vw_target_detail "
            "WHERE target_amount = 777000")).scalar()
        session.execute(text(
            "UPDATE fact_target SET is_void = 1 WHERE target_amount = 777000"))
        session.commit()
        after = session.execute(text(
            "SELECT COUNT(*) FROM vw_target_detail "
            "WHERE target_amount = 777000")).scalar()

    assert before == 1
    assert after == 0


# ---------------------------------------------------------------------------
# Gross sales, gross profit and margin are off the analysis surface
# ---------------------------------------------------------------------------

#: Measures that must not reach a report, an export or an answer. They remain
#: stored on ``fact_sales`` and are still derived by the ETL — this is about
#: what a sales report *states*, which is quantity, volume and net sales.
RETIRED_MEASURES = ("gross_sales", "gross_profit", "gross_margin_percent")


@pytest.mark.parametrize("tool", [
    "get_material_brand_performance",
    "get_region_performance",
    "get_territory_performance",
    "get_customer_performance",
    "get_salesforce_performance",
    "get_material_performance",
    "get_sales_detail",
])
def test_no_report_row_carries_a_gross_measure(brand_engine, tool):
    """Exports derive their columns from row keys, so the row is the guard."""
    with Session(brand_engine) as session:
        ctx = _context(session)
        arguments = GroupedToolInput(date_from=WINDOW[0], date_to=WINDOW[1], limit=50)
        result = execute_tool(ctx, tool, arguments.model_dump(mode="json")).result

    assert result.rows
    for row in result.rows:
        for measure in RETIRED_MEASURES:
            assert measure not in row, f"{tool} still reports {measure}"


def test_the_formatter_cannot_render_a_gross_measure():
    from app.ai.response_formatter import COLUMN_FORMATS, TABLE_COLUMNS

    for measure in RETIRED_MEASURES:
        assert measure not in COLUMN_FORMATS
    for tool, columns in TABLE_COLUMNS.items():
        for measure in RETIRED_MEASURES:
            assert measure not in columns, f"{tool} still lists {measure}"


def test_the_sales_measure_set_reports_quantity_and_net_sales():
    assert "gross_sales" not in q.SALES_MEASURES.sums
    assert "gross_profit" not in q.SALES_MEASURES.sums
    assert "quantity" in q.SALES_MEASURES.sums
    assert "net_sales" in q.SALES_MEASURES.sums


def test_the_dashboard_measure_set_states_the_same_figures_as_the_full_one():
    """The cheap set may cost less; it may not *say* less.

    The dashboard and the assistant read the same warehouse through the same
    tools, and the one thing that must never differ is a figure. So the sums are
    asserted **identical by reference**, not merely equal: a second tuple with
    the same contents today is a second switch that can drift tomorrow, which is
    the failure the one-switch rule in ``SALES_MEASURES`` exists to prevent.
    """
    full, cheap = q.SALES_MEASURES, q.DASHBOARD_SALES_MEASURES

    assert cheap.sums is full.sums
    assert cheap.nullable_sums is full.nullable_sums
    assert cheap.view_name == full.view_name
    assert cheap.default_sort == full.default_sort

    # The only difference is the count, and it is the expensive one that goes.
    assert ("invoice_count", "invoice_no") in full.counts
    assert ("invoice_count", "invoice_no") not in cheap.counts
    # ``transaction_count`` is a plain COUNT(*) and rides along free, so a card
    # that declines the distinct count still knows how many rows it covered.
    assert ("transaction_count", "*") in cheap.counts


def test_the_dashboard_measure_set_emits_no_distinct_count(seeded_engine):
    """The saving is in the SQL, so that is where it is asserted.

    A set that merely *listed* one fewer count would pass a shape test and still
    emit the sort that costs the time.
    """
    with Session(seeded_engine) as session:
        table = q.view(session, q.SALES_VIEW)

    full = [str(e) for e in q.SALES_MEASURES.expressions(table)]
    cheap = [str(e) for e in q.DASHBOARD_SALES_MEASURES.expressions(table)]

    assert any("distinct" in e.lower() for e in full)
    assert not any("distinct" in e.lower() for e in cheap)
    # Every other expression survives, in the same order: the sums are what a
    # sales report says, and this set says all of them.
    assert cheap == full[:len(cheap)]


def test_a_tool_keeps_its_invoice_count_unless_the_caller_declines_it():
    """The saving is opt-in, and that direction is the whole safety of it.

    Defaulting it off would have been the tidier change and would have silently
    dropped a column from the assistant's answers and the Customers page. So
    every input that carries the flag must carry it *on*, and a tool with no
    invoice count to skip must not carry it at all — a flag that quietly does
    nothing is a setting somebody will trust.
    """
    from app.ai import schemas as sc

    window = {"date_from": dt.date(2026, 8, 1), "date_to": dt.date(2026, 8, 31)}
    for model in (sc.GroupedToolInput, sc.TrendToolInput, sc.AchievementToolInput):
        assert model(**window).include_invoice_count is True, model.__name__

    for model in (sc.StockToolInput, sc.CreditToolInput, sc.GrowthToolInput,
                  sc.BusinessSummaryToolInput, sc.VolumeToolInput):
        assert "include_invoice_count" not in model.model_fields, model.__name__


def test_the_transaction_table_does_not_expose_gross_columns():
    from app.reporting.columns import TRANSACTION_COLUMNS

    for measure in RETIRED_MEASURES:
        assert measure not in TRANSACTION_COLUMNS["sales"]
    # The three reported measures are all there.
    for measure in ("quantity", "volume", "net_sales"):
        assert measure in TRANSACTION_COLUMNS["sales"]


# ---------------------------------------------------------------------------
# Volume on every grouped sales report, not just the brand one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", [
    "get_region_performance",
    "get_territory_performance",
    "get_customer_performance",
    "get_salesforce_performance",
    "get_material_performance",
])
def test_every_performance_report_carries_volume(brand_engine, tool):
    """Quantity, volume and net sales travel together — all three or none."""
    with Session(brand_engine) as session:
        ctx = _context(session)
        arguments = GroupedToolInput(date_from=WINDOW[0], date_to=WINDOW[1], limit=50)
        result = execute_tool(ctx, tool, arguments.model_dump(mode="json")).result

    assert result.rows
    for row in result.rows:
        assert "quantity" in row
        assert "volume" in row
        assert "net_sales" in row
        # Still one figure with no unit beside it.
        assert "volume_unit" not in row
        assert "volume_basis" not in row


def test_region_volume_is_the_sum_of_the_lines_in_it(brand_engine):
    """Every region reports a figure now; none of them is suppressed."""
    with Session(brand_engine) as session:
        ctx = _context(session)
        arguments = GroupedToolInput(date_from=WINDOW[0], date_to=WINDOW[1], limit=50)
        result = execute_tool(ctx, "get_region_performance",
                              arguments.model_dump(mode="json")).result
    rows = {row["code"]: row for row in result.rows}

    # Khulna: the single 5,000 line.
    assert rows["REG002"]["volume"] == pytest.approx(5_000)
    # Dhaka: 500 + 200 + 12,000 + 300 + 50. Once unreportable, because those
    # SKUs' pack units spanned mass and volume.
    assert rows["REG001"]["volume"] == pytest.approx(13_050)
    assert not any("kilograms to litres" in note for note in result.notes)


def test_no_unit_column_is_produced_for_the_brand_table(brand_engine):
    from app.ai.response_formatter import TABLE_COLUMNS

    columns = TABLE_COLUMNS["get_material_brand_performance"]
    assert columns == ("rank", "label", "quantity", "volume", "net_sales")
    assert "volume_unit" not in columns


# ---------------------------------------------------------------------------
# Filter responsiveness (acceptance tests 4, 5, 6)
# ---------------------------------------------------------------------------


def test_a_region_filter_changes_the_brand_ranking(brand_engine):
    with Session(brand_engine) as session:
        unfiltered = {r["label"]: r["net_sales"] for r in _brands(session)}
        dhaka = {r["label"]: r["net_sales"] for r in
                 _brands(session, region_codes=["REG001"])}
        khulna = {r["label"]: r["net_sales"] for r in
                  _brands(session, region_codes=["REG002"])}

    assert unfiltered["Zesty"] == pytest.approx(850_000)
    assert dhaka["Zesty"] == pytest.approx(600_000)
    # Khulna carries only the one Zesty line.
    assert list(khulna) == ["Zesty"]
    assert khulna["Zesty"] == pytest.approx(250_000)


def test_a_territory_filter_changes_the_brand_ranking(brand_engine):
    with Session(brand_engine) as session:
        rows = _brands(session, territory_codes=["TR001"])

    # The Khulna line has no territory, so it drops out.
    assert {row["label"] for row in rows} == {"Example Brand", "Zesty", "Golden"}
    assert {r["label"]: r["net_sales"] for r in rows}["Zesty"] == pytest.approx(600_000)


def test_a_date_filter_changes_the_brand_ranking(brand_engine):
    with Session(brand_engine) as session:
        ctx = _context(session)
        arguments = GroupedToolInput(date_from=dt.date(2026, 8, 10),
                                     date_to=dt.date(2026, 8, 11), limit=15)
        rows = execute_tool(ctx, "get_material_brand_performance",
                            arguments.model_dump(mode="json")).result.rows

    assert [row["label"] for row in rows] == ["Example Brand"]


def test_a_material_group_filter_is_honoured(brand_engine):
    """By code, not by name — a group filter narrows on ``material_group_code``.

    The category filter this replaced was by *name*, because the SKU master's
    ``category`` was free text with no code beside it. A material group has
    both, so the code is the filter and the name is the label.
    """
    with Session(brand_engine) as session:
        rows = _brands(session, material_group_codes=["MG03"])

    assert {row["label"] for row in rows} == {"Zesty", "Golden"}


def test_a_brand_filter_narrows_to_that_brand(brand_engine):
    with Session(brand_engine) as session:
        rows = _brands(session, material_brand_names=["Zesty"])

    assert [row["label"] for row in rows] == ["Zesty"]


def test_the_ranking_is_capped_and_ordered_server_side(brand_engine):
    """The limit reaches SQL, so the browser never sees every transaction."""
    with Session(brand_engine) as session:
        ctx = _context(session)
        arguments = GroupedToolInput(date_from=WINDOW[0], date_to=WINDOW[1], limit=2)
        result = execute_tool(ctx, "get_material_brand_performance",
                              arguments.model_dump(mode="json")).result

    assert len(result.rows) == 2
    assert result.truncated is True


# ---------------------------------------------------------------------------
# Data quality: every material carries a brand, so a ranking has no gap
# ---------------------------------------------------------------------------


def test_a_material_cannot_be_registered_without_a_brand(brand_engine):
    """The unbranded case is now unreachable, and this is what closed it.

    A brand ranking used to need an "(not assigned at this level)" row, because
    ``dim_product.brand`` was nullable free text and a SKU could be loaded with
    none. ``dim_material.material_brand`` is ``NOT NULL``: the Material Master
    upload requires a brand on every material, so a sale can never reach the
    ranking without one.

    Pinned as a database constraint rather than as a report shape, because that
    is where the guarantee actually lives — the report has no special case left
    to test.
    """
    from sqlalchemy.exc import IntegrityError

    from app.database.models import DimMaterial

    with Session(brand_engine) as session:
        session.add(DimMaterial(
            material_code="SKU006", material_description="Unbranded Salt 1kg",
            material_group_code="MG03", material_group_name="Grocery",
            material_brand_code="MB05", material_brand=None,
        ))
        with pytest.raises(IntegrityError):
            session.commit()


def test_every_brand_in_a_ranking_is_a_real_brand(brand_engine):
    """No ranking row is the unassigned placeholder, and the total is complete."""
    with Session(brand_engine) as session:
        rows = _brands(session)

    labels = [row["label"] for row in rows]
    assert "(not assigned at this level)" not in labels
    assert all(label for label in labels)
    total = sum(row["net_sales"] for row in rows)
    assert total == pytest.approx(2_550_000)


# ---------------------------------------------------------------------------
# Intent routing (acceptance tests 9, 10, 11)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("question", [
    "Top products দেখাও",
    "top 10 products",
    "best performing products",
    "product-wise sales",
    "Brand-wise sales দেখাও",
    "top brands",
])
def test_a_general_product_question_ranks_brands(question):
    prediction = detect_intent(question)
    assert prediction.intent is Intent.MATERIAL_BRAND_PERFORMANCE
    assert prediction.group_by[0] is GroupBy.MATERIAL_BRAND


@pytest.mark.parametrize("question", [
    "Top SKU দেখাও",
    "SKU-wise sales",
    "top 10 skus",
    "sales by product code",
])
def test_an_explicit_sku_question_still_ranks_skus(question):
    prediction = detect_intent(question)
    assert prediction.intent is Intent.MATERIAL_PERFORMANCE
    assert prediction.group_by[0] is GroupBy.MATERIAL


def test_the_agent_answers_a_top_products_question_with_brands(brand_engine, make_agent):
    response = make_agent("ceo").chat("এই মাসে top products দেখাও")

    assert response.intent is Intent.MATERIAL_BRAND_PERFORMANCE
    assert response.tools_used == ["get_material_brand_performance"]
    assert "Brand Performance" in response.answer
    # No unit column leaks into the rendered table.
    assert "Volume Unit" not in response.answer


def test_the_agent_still_answers_a_top_sku_question_with_skus(brand_engine, make_agent):
    response = make_agent("ceo").chat("এই মাসে top SKU দেখাও")

    assert response.intent is Intent.MATERIAL_PERFORMANCE
    assert response.tools_used == ["get_material_performance"]


# ---------------------------------------------------------------------------
# The volume helpers, in isolation
# ---------------------------------------------------------------------------


def test_volume_by_group_sums_the_uploaded_totals(brand_engine):
    with Session(brand_engine) as session:
        rows = {
            row["code"]: row["volume"]
            for row in q.volume_by_group(session, q.SALES_VIEW, ScopeFilters(),
                                         WINDOW[0], WINDOW[1], GroupBy.MATERIAL_BRAND)
        }

    assert rows == {"Example Brand": 700.0, "Zesty": 17_000.0, "Golden": 350.0}


def test_the_target_side_uses_the_same_reader_as_the_sales_side(brand_engine):
    """One volume reader now, pointed at whichever column the view carries.

    ``single_volume_by_group`` used to sit beside ``volume_by_group`` to keep a
    target's units apart — it returned MIXED for a brand whose targets spanned
    kilograms and litres. Revision 0022 removed the unit, so it was removed too;
    this asserts both that it is gone and that the surviving reader answers the
    target side correctly.
    """
    assert not hasattr(q, "single_volume_by_group")
    assert not hasattr(q, "volume_by_unit")

    _load_targets(brand_engine, [
        target_row(**{"Target Amount": 300_000, "SKU Code": "SKU004",
                      "Target Volume": 250}),
        target_row(**{"Target Amount": 100_000, "SKU Code": "SKU005",
                      "Target Volume": 60}),
    ])
    with Session(brand_engine) as session:
        rows = {
            row["code"]: row["volume"]
            for row in q.volume_by_group(session, q.TARGET_VIEW, ScopeFilters(),
                                         WINDOW[0], WINDOW[1],
                                         GroupBy.MATERIAL_BRAND,
                                         volume_column="target_volume")
        }

    assert rows["Golden"] == pytest.approx(310)


# ---------------------------------------------------------------------------
# Over HTTP: the dashboard, the pages and the drill-down
# ---------------------------------------------------------------------------

HTTP_WINDOW = "date_from=2026-08-01&date_to=2026-08-31"


@pytest.fixture
def client(brand_engine, platform):
    """The platform client, with the brand fixtures loaded into its engine."""
    return platform


def test_the_dashboard_still_groups_brands_and_never_products(client):
    """Top 15 Brands is gone; the rule it enforced is not.

    That card was replaced by Top 50 Customers — the dashboard already answered
    the brand question twice (``brand_sales`` ranks brands, and the Sales page
    breaks down by brand) and named no customer at all. Both names are asserted
    against the frame's own section list rather than against the body: read off
    the body, an absent key passes on any response whatsoever, which is exactly
    the failure this guard exists to catch.
    """
    token = login(client, "ceo")
    body = client.get(f"/api/dashboard?{HTTP_WINDOW}", headers=auth(token)).json()

    assert "top_customers" in body["sections"]
    assert "top_brands" not in body["sections"]
    assert "top_products" not in body["sections"]

    # The surviving brand card still groups by brand, which is the rule the
    # removed card was one enforcement of.
    brands = card(client, token, "brand_sales", f"?{HTTP_WINDOW}")
    assert brands["values"]["group_by"] == "material_brand"


def test_the_brand_target_tool_reports_only_the_measures_a_sale_states(
    brand_engine,
):
    """Pinned on the tool, because the tool is what still exists.

    This rode on the dashboard's brand card and would have been deleted with
    it — but the guarantee belongs to ``get_material_brand_target_performance``,
    which is untouched and on the assistant's allow-list. A card is one caller;
    the shape of a row is the tool's.
    """
    with Session(brand_engine) as session:
        result = execute_tool(
            _context(session), "get_material_brand_target_performance",
            GroupedToolInput(date_from=WINDOW[0], date_to=WINDOW[1],
                             group_by=GroupBy.MATERIAL_BRAND,
                             limit=15).model_dump(mode="json"),
        ).result

    assert result.rows
    for field in ("rank", "label", "volume", "net_sales"):
        assert field in result.rows[0], field
    # The unit never travels as a column of its own.
    assert "volume_unit" not in result.rows[0]


def test_the_dashboard_brand_ranking_respects_a_region_filter(client):
    """Scope still narrows the brand card — now the one that survived."""
    token = login(client, "ceo")
    everywhere = card(client, token, "brand_sales", f"?{HTTP_WINDOW}")
    khulna = card(client, token, "brand_sales",
                  f"?{HTTP_WINDOW}&region_code=REG002")

    def by_brand(section):
        return {row["label"]: row["actual_sales"] for row in section["rows"]}

    # Khulna carries only part of the business, so both the membership and the
    # figures move with the filter.
    assert set(by_brand(khulna)) < set(by_brand(everywhere))
    assert by_brand(khulna)["Zesty"] == pytest.approx(250_000)
    assert by_brand(everywhere)["Zesty"] == pytest.approx(850_000)


def test_the_sales_page_breaks_down_by_brand(client):
    token = login(client, "ceo")
    body = client.get(f"/api/pages/sales?{HTTP_WINDOW}", headers=auth(token)).json()

    assert "brand_performance" in body
    assert "product_performance" not in body
    assert body["brand_performance"]["rows"][0]["rank"] == 1


@pytest.mark.parametrize("level",
                         ["material", "material_brand", "material_group"])
def test_material_analysis_supports_all_three_levels(client, level):
    token = login(client, "ceo")
    response = client.get(f"/api/pages/materials?level={level}&{HTTP_WINDOW}",
                          headers=auth(token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["level"] == level
    assert body["levels"] == ["material", "material_brand", "material_group"]
    assert body["materials"]["rows"]


def test_material_analysis_defaults_to_material_level(client):
    token = login(client, "ceo")
    body = client.get(f"/api/pages/materials?{HTTP_WINDOW}", headers=auth(token)).json()

    assert body["level"] == "material"
    # Material codes, not brand names — the page that ranks individual items.
    assert {row["code"] for row in body["materials"]["rows"]} >= {"SKU001", "SKU003"}


def test_material_analysis_cards_grow_and_rank_on_volume(client):
    """The two ranking cards measure volume; the table above them still does not.

    The Top 10 and Bottom 10 cards state a volume, so the growth beside it is
    that volume's growth and the order is by that volume — a growth column is
    read as the growth of the column it sits next to, and a "top ten" as the top
    ten of what the card shows. The main table keeps net sales and keeps growing
    on it, which is why the two growths travel as separate fields.
    """
    from app.etl.transforms import growth_percent

    token = login(client, "ceo")
    body = client.get(f"/api/pages/materials?{HTTP_WINDOW}", headers=auth(token)).json()

    rows = body["materials"]["rows"]
    assert rows
    for row in rows:
        by_amount = growth_percent(row["net_sales"], row["previous_net_sales"])
        assert row["growth_percent"] == (None if by_amount is None
                                         else pytest.approx(float(by_amount)))
        by_volume = growth_percent(row["volume"], row["previous_volume"])
        assert row["volume_growth_percent"] == (None if by_volume is None
                                                else pytest.approx(float(by_volume)))

    ranked = [row["volume"] or 0 for row in body["top"]]
    assert ranked == sorted(ranked, reverse=True)


def test_material_analysis_rejects_an_unknown_level(client):
    token = login(client, "ceo")
    response = client.get(f"/api/pages/materials?level=colour&{HTTP_WINDOW}",
                          headers=auth(token))

    assert response.status_code == 404


def test_clicking_a_brand_opens_its_breakdown(client):
    """The drill-down is a filter, so it is authorised like any other report."""
    token = login(client, "ceo")
    body = client.get(
        f"/api/pages/materials?level=material_brand&material_brand=Zesty&{HTTP_WINDOW}",
        headers=auth(token),
    ).json()

    detail = body["brand_detail"]
    assert detail["brand"] == "Zesty"
    assert detail["summary"]["value"] == pytest.approx(850_000)
    assert {r["label"] for r in detail["groups"]["rows"]} == {"Grocery"}
    assert {r["code"] for r in detail["materials"]["rows"]} == {"SKU003"}
    for panel in ("monthly_trend", "territories", "customers", "volume"):
        assert panel in detail


def test_there_is_no_brand_breakdown_until_one_brand_is_named(client):
    token = login(client, "ceo")
    body = client.get(f"/api/pages/materials?level=material_brand&{HTTP_WINDOW}",
                      headers=auth(token)).json()

    assert body["brand_detail"] is None


def test_a_brand_breakdown_is_still_scoped_to_the_caller(client):
    """A Khulna manager sees only Khulna's share of the brand."""
    token = login(client, "khulna_rm")
    body = client.get(
        f"/api/pages/materials?level=material_brand&material_brand=Zesty&{HTTP_WINDOW}",
        headers=auth(token),
    ).json()

    assert body["brand_detail"]["summary"]["value"] == pytest.approx(250_000)


def test_item_filter_options_come_from_the_material_master(client):
    """One master behind all three item filters, and no Product endpoints left.

    ``/options/brand`` and ``/options/category`` served the SKU master's two
    free-text attributes. Both are gone with it; the material chain answers the
    same three questions from ``dim_material``.
    """
    token = login(client, "ceo")
    brands = client.get("/api/master-data/options/material_brand",
                        headers=auth(token)).json()
    groups = client.get("/api/master-data/options/material_group_code",
                        headers=auth(token)).json()

    assert {option["code"] for option in brands["options"]} == {
        "Example Brand", "Zesty", "Golden", "Dairy Brand"}
    assert {"Tea", "Grocery", "Dairy"} <= {
        option["label"] for option in groups["options"]}

    for gone in ("brand", "category", "sku_code"):
        assert client.get(f"/api/master-data/options/{gone}",
                          headers=auth(token)).status_code == 404


# -- a period is identified by its year, not only by its number ---------------


@pytest.fixture
def two_year_engine(seeded_engine: Engine) -> Engine:
    """The same month in two financial years, which is the case that breaks.

    January 2025 belongs to FY 2024-25 and January 2026 to FY 2025-26. Both are
    "January", and a breakdown keyed on the month number alone cannot tell them
    apart.
    """
    _load(seeded_engine, [
        sales_row(**{"Invoice No": "INV-A", "Date": "2025-01-10", "Quantity": 100,
                     "Gross Sales": 1_000_000, "Discount": 0, "Cost": 600_000}),
        sales_row(**{"Invoice No": "INV-B", "Date": "2026-01-10", "Quantity": 50,
                     "Gross Sales": 400_000, "Discount": 0, "Cost": 250_000}),
    ])
    return seeded_engine


def test_a_monthly_breakdown_does_not_merge_two_financial_years(
    two_year_engine: Engine,
) -> None:
    """Two Januarys are two rows, never one row holding both.

    Keyed on the month number alone, a breakdown spanning a year end reported a
    single "January" worth both Januarys added together — one bar, two years,
    and nothing on screen saying so. The financial year is part of the key and
    part of the label, so the rows are distinct and a reader can tell which is
    which.
    """
    with Session(two_year_engine, future=True) as session:
        rows, _ = q.aggregate_by(session, q.SALES_MEASURES, ScopeFilters(),
                                 dt.date(2024, 12, 1), dt.date(2026, 3, 31),
                                 GroupBy.MONTH, limit=50)

    januaries = [row for row in rows if "January" in row["label"]]
    assert len(januaries) == 2, [row["label"] for row in rows]
    assert {row["net_sales"] for row in januaries} == {1_000_000.0, 400_000.0}
    assert len({row["code"] for row in januaries}) == 2
    assert all("FY" in row["label"] for row in januaries)


def test_a_quarterly_breakdown_is_keyed_and_labelled_by_its_financial_year(
    two_year_engine: Engine,
) -> None:
    """The same rule, for the grouping added beside the month.

    January is financial Q3 in both years, so a quarterly breakdown has exactly
    the flaw a monthly one had, and adding it without the year would have
    shipped the same defect twice.
    """
    with Session(two_year_engine, future=True) as session:
        rows, _ = q.aggregate_by(session, q.SALES_MEASURES, ScopeFilters(),
                                 dt.date(2024, 12, 1), dt.date(2026, 3, 31),
                                 GroupBy.QUARTER, limit=50)

    third = [row for row in rows if row["label"].endswith("Q3")]
    assert len(third) == 2, [row["label"] for row in rows]
    assert {row["label"] for row in third} == {"FY 2024-25 Q3", "FY 2025-26 Q3"}


def test_a_grouping_keyed_on_a_business_code_is_left_alone(
    two_year_engine: Engine,
) -> None:
    """Only the periods need a year: every other code is already unique."""
    with Session(two_year_engine, future=True) as session:
        rows, _ = q.aggregate_by(session, q.SALES_MEASURES, ScopeFilters(),
                                 dt.date(2024, 12, 1), dt.date(2026, 3, 31),
                                 GroupBy.REGION, limit=50)

    assert rows and all("|" not in str(row["code"]) for row in rows)
