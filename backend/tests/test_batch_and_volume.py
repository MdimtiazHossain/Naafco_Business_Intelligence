"""Batch-aware duplicate detection, and Total Volume taken from the source.

The specification's headline rules, tested as rules rather than as code paths:

* same invoice + same material + **different batch** is two lines, not a
  duplicate;
* the Total Volume the file states *is* the volume — never quantity times a
  pack size, a unit of measure or a conversion factor;
* nothing is guessed — a line with no Total Volume stores NULL and a reason.

The pack-size rule is now structural rather than defended. Revision 0022 removed
the SKU master those columns lived on, so there is no pack size anywhere for a
volume to be derived from and no unit of measure on any volume in the system.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models_warehouse import FactSales
from app.etl import uom
from app.etl.datasets import get_dataset
from app.etl.pipeline import LOAD_MODE_INITIAL, run_import
from app.etl.readers import RecordsSourceReader
from app.etl.volume import from_source
from conftest_phase2 import sales_row

SALES = get_dataset("sales")


def line(**overrides):
    """A sales row with a batch, since batch is now part of the identity."""
    row = sales_row(**{"Company Code": "C001", "Batch Code": "BATCH-A"})
    row.update(overrides)
    return row


def do_import(engine, rows, *, mode=LOAD_MODE_INITIAL, source="TEST"):
    return run_import(engine, "sales",
                      RecordsSourceReader(rows, source_name="sales.csv"),
                      source_system=source, load_mode=mode)


def facts(engine) -> list[FactSales]:
    with Session(engine) as session:
        return list(session.execute(select(FactSales)).scalars())


# ==========================================================================
# The key itself (items 2, 3, 8)
# ==========================================================================


def test_the_fallback_key_includes_company_and_batch():
    assert SALES.business_key_fields == (
        "company_code", "invoice_no", "material_code", "batch_code")


def test_the_preferred_key_is_the_source_line_number():
    assert SALES.preferred_key_fields == (
        "company_code", "invoice_no", "invoice_line_no")


def test_the_line_number_is_used_when_the_source_supplies_it():
    with_line = {"company_code": "C01", "invoice_no": "INV001",
                 "invoice_line_no": "001", "material_code": "SKU001",
                 "batch_code": "B"}
    assert SALES.key_fields_for(with_line) == SALES.preferred_key_fields


def test_a_blank_line_number_falls_back_rather_than_keying_on_nothing():
    """Half a line number is worse than none: it would key some rows one way
    and some another, and two copies of the same line could differ."""
    for blank in (None, "", "   "):
        record = {"company_code": "C01", "invoice_no": "INV001",
                  "invoice_line_no": blank, "material_code": "SKU001",
                  "batch_code": "B"}
        assert SALES.key_fields_for(record) == SALES.business_key_fields


def test_the_key_names_its_own_fields_so_two_schemes_cannot_collide():
    """A line keyed on its ERP line number and one keyed on its batch must not
    produce the same string just because their invoice and SKU match."""
    keyed_by_line = SALES.build_business_key(
        {"company_code": "C01", "invoice_no": "INV001", "invoice_line_no": "SKU001"},
        "X")
    keyed_by_batch = SALES.build_business_key(
        {"company_code": "C01", "invoice_no": "INV001", "material_code": "SKU001",
         "batch_code": ""}, "X")
    assert keyed_by_line != keyed_by_batch


def test_batch_is_normalised_for_comparison_only():
    base = {"company_code": "C01", "invoice_no": "INV001", "material_code": "SKU001"}
    spaced = SALES.build_business_key({**base, "batch_code": "  batch-a  "}, "X")
    plain = SALES.build_business_key({**base, "batch_code": "BATCH-A"}, "X")
    assert spaced == plain


def test_a_different_source_system_never_collides():
    record = {"company_code": "C01", "invoice_no": "INV001",
              "material_code": "SKU001", "batch_code": "BATCH-A"}
    assert (SALES.build_business_key(record, "DEMO")
            != SALES.build_business_key(record, "SAP"))


# ==========================================================================
# The specification's duplicate acceptance tests (items 4, 5, 40)
# ==========================================================================


def test_1_and_2_same_invoice_same_sku_different_batch_are_both_accepted(
    seeded_engine
):
    """The bug this whole change exists for."""
    result = do_import(seeded_engine, [
        line(**{"Invoice No": "INV001", "SKU Code": "SKU001",
                "Batch Code": "BATCH-A"}),
        line(**{"Invoice No": "INV001", "SKU Code": "SKU001",
                "Batch Code": "BATCH-B"}),
    ])

    assert result.rejected_rows == 0, result.error_counts
    assert result.valid_rows == 2
    assert {f.batch_code for f in facts(seeded_engine)} == {"BATCH-A", "BATCH-B"}


def test_5_three_batches_on_one_invoice_line_are_all_accepted(seeded_engine):
    result = do_import(seeded_engine, [
        line(**{"Invoice No": "INV001", "Batch Code": batch})
        for batch in ("BATCH-A", "BATCH-B", "BATCH-C")
    ])
    assert result.rejected_rows == 0, result.error_counts
    assert result.valid_rows == 3


def test_3_an_identical_line_already_in_the_warehouse_is_rejected(seeded_engine):
    do_import(seeded_engine, [line(**{"Invoice No": "INV001"})])
    result = do_import(seeded_engine, [line(**{"Invoice No": "INV001"})])

    assert result.duplicate_rows == 1
    assert result.rejected_rows == 1
    assert "DUPLICATE_IN_WAREHOUSE" in result.error_counts
    reason = result.rejections[0].message
    assert "Duplicate transaction line" in reason
    assert "INV001" in reason and "BATCH-A" in reason


def test_7_a_different_batch_against_the_database_is_accepted(seeded_engine):
    do_import(seeded_engine, [line(**{"Invoice No": "INV001",
                                      "Batch Code": "BATCH-A"})])
    result = do_import(seeded_engine, [line(**{"Invoice No": "INV001",
                                               "Batch Code": "BATCH-B"})])

    assert result.rejected_rows == 0, result.error_counts
    assert len(facts(seeded_engine)) == 2


def test_4_a_different_sku_on_the_same_invoice_is_accepted(seeded_engine):
    result = do_import(seeded_engine, [
        line(**{"Invoice No": "INV001", "SKU Code": "SKU001"}),
        line(**{"Invoice No": "INV001", "SKU Code": "SKU002"}),
    ])
    assert result.rejected_rows == 0, result.error_counts
    assert result.valid_rows == 2


def test_a_different_invoice_is_accepted(seeded_engine):
    result = do_import(seeded_engine, [
        line(**{"Invoice No": "INV001"}),
        line(**{"Invoice No": "INV002"}),
    ])
    assert result.rejected_rows == 0, result.error_counts
    assert result.valid_rows == 2


# ==========================================================================
# Duplicates within one file (item 6)
# ==========================================================================


def test_6_a_repeated_line_inside_the_file_rejects_only_the_second(seeded_engine):
    result = do_import(seeded_engine, [
        line(**{"Invoice No": "INV001", "Batch Code": "BATCH-A"}),
        line(**{"Invoice No": "INV001", "Batch Code": "BATCH-A"}),
    ])

    assert result.valid_rows == 1
    assert result.rejected_rows == 1
    assert "DUPLICATE_IN_FILE" in result.error_counts
    assert "Duplicate line found within uploaded file" in result.rejections[0].message


def test_6_two_batches_inside_one_file_are_both_kept(seeded_engine):
    result = do_import(seeded_engine, [
        line(**{"Invoice No": "INV001", "Batch Code": "BATCH-A"}),
        line(**{"Invoice No": "INV001", "Batch Code": "BATCH-B"}),
    ])
    assert result.valid_rows == 2
    assert result.rejected_rows == 0


def test_9_one_bad_line_does_not_reject_the_whole_invoice(seeded_engine):
    """Only the duplicate line is refused; its siblings load."""
    result = do_import(seeded_engine, [
        line(**{"Invoice No": "INV001", "Batch Code": "BATCH-A"}),
        line(**{"Invoice No": "INV001", "Batch Code": "BATCH-A"}),
        line(**{"Invoice No": "INV001", "Batch Code": "BATCH-C"}),
    ])
    assert result.valid_rows == 2
    assert result.rejected_rows == 1
    assert {f.batch_code for f in facts(seeded_engine)} == {"BATCH-A", "BATCH-C"}


def test_the_case_of_a_batch_code_does_not_let_a_duplicate_through(seeded_engine):
    result = do_import(seeded_engine, [
        line(**{"Invoice No": "INV001", "Batch Code": "BATCH-A"}),
        line(**{"Invoice No": "INV001", "Batch Code": " batch-a "}),
    ])
    assert result.valid_rows == 1
    assert result.rejected_rows == 1


def test_the_stored_batch_code_keeps_its_original_form(seeded_engine):
    """Normalisation is for comparison. The data is not rewritten."""
    do_import(seeded_engine, [line(**{"Invoice No": "INV001",
                                      "Batch Code": " batch-a "})])
    assert facts(seeded_engine)[0].batch_code == "batch-a"


# ==========================================================================
# Invoice line number (items 3, 40 test 5)
# ==========================================================================


def test_the_source_line_number_identifies_the_line(seeded_engine):
    """Same invoice, same SKU, same batch, different ERP line — two lines."""
    result = do_import(seeded_engine, [
        line(**{"Invoice No": "INV001", "Invoice Line No": "001"}),
        line(**{"Invoice No": "INV001", "Invoice Line No": "002"}),
    ])
    assert result.rejected_rows == 0, result.error_counts
    assert result.valid_rows == 2
    assert {f.invoice_line_no for f in facts(seeded_engine)} == {"001", "002"}


def test_a_repeated_line_number_is_a_duplicate(seeded_engine):
    result = do_import(seeded_engine, [
        line(**{"Invoice No": "INV001", "Invoice Line No": "001",
                "Batch Code": "BATCH-A"}),
        line(**{"Invoice No": "INV001", "Invoice Line No": "001",
                "Batch Code": "BATCH-B"}),
    ])
    # The ERP said these are the same line; its judgement outranks the batch.
    assert result.valid_rows == 1
    assert result.rejected_rows == 1


def test_a_file_with_no_line_numbers_still_works(seeded_engine):
    result = do_import(seeded_engine, [line(**{"Invoice No": "INV001"})])
    assert result.rejected_rows == 0
    assert facts(seeded_engine)[0].invoice_line_no is None


def test_the_column_is_recognised_under_its_common_aliases():
    for alias in ("Invoice Line No", "Line No", "Invoice Item No",
                  "Invoice Detail ID", "POSNR"):
        assert SALES.resolve_header(alias) == "invoice_line_no", alias


def test_the_batch_column_is_recognised_under_its_common_aliases():
    for alias in ("Batch Code", "Batch", "Batch No", "Lot", "Lot No"):
        assert SALES.resolve_header(alias) == "batch_code", alias


# ==========================================================================
# Units of measure (items 16, 17)
# ==========================================================================


def test_within_a_dimension_conversion_works():
    assert uom.convert(1, "KG", "GM") == Decimal(1000)
    assert uom.convert(2500, "ML", "LTR") == Decimal("2.5")


def test_across_dimensions_conversion_is_refused():
    """The rule the module exists for."""
    with pytest.raises(uom.IncompatibleUnits) as caught:
        uom.convert(1, "KG", "LTR")
    assert "density" in str(caught.value)


def test_mass_and_volume_are_different_dimensions():
    assert uom.dimension_of("KG") == uom.MASS
    assert uom.dimension_of("LTR") == uom.VOLUME
    assert not uom.compatible("KG", "LTR")
    assert uom.compatible("KG", "GM")


def test_totalling_keeps_the_dimensions_apart():
    """500 KG + 200 GM + 250 LTR is 500.2 KG and 250 LTR — never 750."""
    totals = uom.group_by_dimension([(500, "KG"), (200, "GM"), (250, "LTR")])
    assert totals[uom.MASS] == (Decimal("500.2"), "KG")
    assert totals[uom.VOLUME] == (Decimal("250"), "LTR")
    assert len(totals) == 2


def test_spellings_are_normalised():
    for text in ("kg", " KGS ", "Kilogram", "kilos"):
        assert uom.canonical(text) == "KG", text
    for text in ("l", "ltr", "Litres", "liter"):
        assert uom.canonical(text) == "LTR", text


def test_an_unknown_unit_is_none_not_an_exception():
    """A bad unit on a product is a data-quality finding, not a crash."""
    assert uom.canonical("furlongs") is None
    assert uom.parse_unit("") is None


# ==========================================================================
# The Total Volume rule (items 13, 15, 19, 41)
# ==========================================================================


def test_the_uploaded_total_volume_is_the_volume():
    """The headline rule. 100 units and a stated 250 is 250 — not 100 x anything."""
    result = from_source(Decimal("250.50"), quantity=100)
    assert result.volume == Decimal("250.50")
    assert result.ok
    assert result.reason is None


def test_a_decimal_total_volume_survives_intact():
    """No rounding and no scaling: what the file says is what is stored."""
    assert from_source("1275.75", quantity=500).volume == Decimal("1275.75")


def test_the_quantity_cannot_influence_the_figure():
    """The same volume against wildly different quantities stores the same number."""
    for quantity in (1, 100, 10_000):
        assert from_source(123.45, quantity=quantity).volume == Decimal("123.45")


def test_a_missing_total_volume_gives_null_and_a_reason():
    result = from_source(None, quantity=100)
    assert result.volume is None
    assert result.reason == "MISSING_VOLUME"
    assert "no total volume" in result.message


def test_an_unparseable_total_volume_is_missing_not_a_crash():
    assert from_source("not a number", quantity=100).reason == "MISSING_VOLUME"


def test_zero_volume_is_a_real_answer_not_a_missing_one():
    result = from_source(0, quantity=100)
    assert result.ok
    assert result.volume == Decimal(0)


def test_a_negative_volume_needs_a_negative_quantity():
    """The one thing the quantity is read for: the sign check."""
    assert from_source(-500, quantity=100).reason == "NEGATIVE_VOLUME"
    assert from_source(-500, quantity=-100).volume == Decimal(-500)


# ==========================================================================
# Volume through the pipeline (items 13, 18, 42)
# ==========================================================================


@pytest.fixture
def packed(seeded_engine):
    """The seeded warehouse, under the name the volume tests below still use.

    It used to give SKU001 a 5 KG pack and SKU002 a 5 LTR one, so that every
    test using it could assert the pack size made *no difference* to a sales
    volume. Revision 0022 removed the master those columns lived on: there is no
    pack size to set, and the rule is now guaranteed by the schema rather than
    demonstrated against it.

    The fixture is kept rather than inlined because the tests it feeds are
    acceptance tests named in the specification, and renaming them would lose
    the thread back to it.
    """
    return seeded_engine


def test_4_the_uploaded_volume_is_stored_exactly_as_supplied(packed):
    """Acceptance test 4: the file's figures are what the warehouse holds."""
    do_import(packed, [line(**{"SKU Code": "SKU001", "Quantity": 100,
                               "Total Volume": "250.50", "Net Sales": 50000})])
    fact = facts(packed)[0]
    assert float(fact.quantity) == 100
    assert float(fact.volume) == 250.50
    assert float(fact.net_sales) == 50000


def test_the_pack_size_cannot_reach_the_stored_volume(packed):
    """The old rule would have made this 100 x pack size. The file says 250.50.

    There is no expression a pack size could enter any more, and since revision
    0022 no pack size either: the Material Master states a group, a brand, a
    code and a description, and nothing that could scale a quantity.
    """
    do_import(packed, [line(**{"SKU Code": "SKU001", "Quantity": 100,
                               "Total Volume": "250.50"})])
    assert float(facts(packed)[0].volume) == 250.50


def test_no_unit_is_stored_against_a_sales_line(packed):
    """There is no UOM in the sales path at all, so the column stays empty."""
    do_import(packed, [line(**{"SKU Code": "SKU001", "Quantity": 100,
                               "Total Volume": 250})])
    fact = facts(packed)[0]
    assert fact.volume_unit is None
    assert fact.volume_factor is None


def test_the_upload_no_longer_asks_for_a_volume_unit():
    """Requirement 4: the column is gone from the sales schema entirely."""
    assert "volume_unit" not in SALES.field_map
    assert "volume" in SALES.field_map


def test_total_volume_is_recognised_under_its_common_spellings():
    field = SALES.field_map["volume"]
    for alias in ("total volume", "vol", "volume", "sales volume"):
        assert alias in field.aliases or alias == field.name, alias


def test_a_material_with_no_pack_data_still_stores_the_uploaded_volume(seeded_engine):
    """No master data is consulted, so none of it can withhold a volume."""
    result = do_import(seeded_engine, [line(**{"Quantity": 100,
                                               "Total Volume": 500})])

    assert result.rejected_rows == 0
    assert result.valid_rows == 1
    assert float(facts(seeded_engine)[0].volume) == 500
    # Nothing to report: the figure is complete on its own.
    assert result.volume_gaps == {}


def test_8_changing_the_master_does_not_rewrite_history(packed):
    """Acceptance test 8: last July's invoice keeps saying what it said.

    Stronger than it used to be, twice over. The volume comes from no expression
    at all, so there is nothing for a master-data change to re-evaluate — and
    since revision 0022 there is no pack size on the master to change. Renaming
    the material is the sharpest edit still available, and the figures ignore it.
    """
    from app.database.models import DimMaterial

    do_import(packed, [line(**{"SKU Code": "SKU001", "Quantity": 100,
                               "Total Volume": 500, "Net Sales": 50000})])

    with Session(packed) as session:
        material = session.execute(
            select(DimMaterial).where(DimMaterial.material_code == "SKU001")
        ).scalar_one()
        material.material_description = "Renamed After The Fact"
        session.commit()

    fact = facts(packed)[0]
    assert float(fact.volume) == 500
    assert float(fact.net_sales) == 50000
    # The raw code on the fact still says what the file said.
    assert fact.material_code == "SKU001"


def test_material_stock_carries_no_volume_at_all():
    """Stock is counted, not measured by volume — the dataset offers no such field.

    The old stock fact carried a volume column beside its quantities, which only
    made sense while stock named a SKU with a pack size behind it. Material stock
    is held per plant, storage location and material; there is no pack to
    convert and nothing in the file states a volume, so offering the field would
    invite an operator to supply a figure with no meaning.
    """
    spec = get_dataset("material_stock")
    assert "volume" not in spec.field_map
    assert "volume_unit" not in spec.field_map


def test_7_a_line_with_no_volume_loads_and_is_flagged(packed):
    """Acceptance test 7: flagged, not guessed — and the sale is not lost."""
    result = do_import(packed, [line(**{"SKU Code": "SKU001", "Quantity": 100,
                                        "Total Volume": None,
                                        "Net Sales": 50000})])

    assert result.rejected_rows == 0
    assert result.valid_rows == 1
    fact = facts(packed)[0]
    assert fact.volume is None
    assert float(fact.net_sales) == 50000        # the sale is untouched
    assert result.volume_gaps.get("MISSING_VOLUME") == 1


def test_a_negative_volume_against_a_positive_quantity_is_rejected(packed):
    result = do_import(packed, [line(**{"SKU Code": "SKU001", "Quantity": 100,
                                        "Total Volume": -500})])
    assert result.valid_rows == 0
    assert result.rejected_rows == 1
    assert result.error_counts.get("VOLUME_NEGATIVE") == 1


def test_a_return_may_carry_a_negative_volume(packed):
    """A credit note sends quantity and volume both negative. That is a return."""
    result = do_import(packed, [line(**{"SKU Code": "SKU001", "Quantity": -100,
                                        "Total Volume": -500,
                                        "Net Sales": -50000})])
    assert result.rejected_rows == 0
    assert float(facts(packed)[0].volume) == -500


# ==========================================================================
# There is no pack size left to back-fill (item 14, retired)
# ==========================================================================


def test_the_material_master_states_no_pack_size_or_unit():
    """The columns the pack-size backfill filled do not exist any more.

    Two tests stood here, covering ``etl.volume.backfill_pack_columns``: it
    parsed a SKU master's free-text "Pack Size" into a number and a unit, and it
    refused the ambiguous cases. Revision 0022 removed that master. The Material
    Master states a group, a brand, a code and a description, and the function
    went with the columns it wrote to.

    Asserted against the model rather than deleted outright, because "a material
    has no pack size" is the invariant that keeps a volume unforgeable — if a
    pack column ever reappears here, the derivation this whole module argues
    against becomes possible again.
    """
    from app.database.models import DimMaterial
    from app.etl import volume as volume_module

    columns = set(DimMaterial.__table__.columns.keys())
    for forbidden in ("pack_size", "pack_size_value", "pack_unit",
                      "unit_conversation_ratio", "retailer_unit",
                      "consumer_unit"):
        assert forbidden not in columns

    assert not hasattr(volume_module, "backfill_pack_columns")


# ==========================================================================
# Reporting keeps units apart (items 24, 26, 30)
# ==========================================================================


def test_the_volume_total_is_the_sum_of_the_uploaded_figures(packed):
    """Requirement 9: Sales Vol = SUM(Total Volume). Nothing else."""
    from app.ai import queries as q
    from app.ai.schemas import ScopeFilters

    do_import(packed, [
        line(**{"Invoice No": "INV001", "SKU Code": "SKU001", "Quantity": 100,
                "Total Volume": "250.50"}),
        line(**{"Invoice No": "INV002", "SKU Code": "SKU002", "Quantity": 50,
                "Total Volume": "480.00"}),
    ])

    with Session(packed) as session:
        totals = q.volume_total(session, q.SALES_VIEW, ScopeFilters())

    assert totals["value"] == pytest.approx(730.50)
    assert totals["measured_lines"] == 2
    assert totals["unmeasured_lines"] == 0


def test_lines_without_a_volume_are_reported_not_counted_as_zero(packed):
    from app.ai import queries as q
    from app.ai.schemas import ScopeFilters

    do_import(packed, [
        line(**{"Invoice No": "INV001", "SKU Code": "SKU001", "Quantity": 100,
                "Total Volume": 500}),
        # The file supplied no volume for this line, so there is none to report.
        line(**{"Invoice No": "INV002", "SKU Code": "SKU002", "Quantity": 10}),
    ])

    with Session(packed) as session:
        totals = q.volume_total(session, q.SALES_VIEW, ScopeFilters())

    assert totals["value"] == 500.0
    assert totals["unmeasured_lines"] == 1


def test_a_scope_with_no_volume_at_all_reports_none_not_zero(packed):
    from app.ai import queries as q
    from app.ai.schemas import ScopeFilters

    do_import(packed, [line(**{"SKU Code": "SKU001", "Quantity": 100})])

    with Session(packed) as session:
        totals = q.volume_total(session, q.SALES_VIEW, ScopeFilters())

    assert totals["value"] is None
    assert totals["unmeasured_lines"] == 1


def test_volume_can_be_grouped_by_a_dimension(packed):
    from app.ai import queries as q
    from app.ai.schemas import GroupBy, ScopeFilters

    do_import(packed, [line(**{"SKU Code": "SKU001", "Quantity": 100,
                               "Total Volume": 500})])

    with Session(packed) as session:
        rows = q.volume_by_group(session, q.SALES_VIEW, ScopeFilters(),
                                 group_by=GroupBy.REGION)

    assert rows
    assert rows[0]["volume"] == 500.0
    assert "code" in rows[0]
    # No unit column anywhere in the shape — that is what makes it one figure.
    assert "volume_unit" not in rows[0]


def test_two_lines_in_one_group_are_added_together(packed):
    """The rule that used to be impossible: one group, one summed figure.

    Two SKUs of the same brand once produced ``None`` whenever their pack units
    differed. There is no unit now, so the group has one honest total.
    """
    from app.ai import queries as q
    from app.ai.schemas import GroupBy, ScopeFilters

    do_import(packed, [
        line(**{"Invoice No": "INV001", "SKU Code": "SKU001", "Quantity": 100,
                "Total Volume": 500}),
        line(**{"Invoice No": "INV002", "SKU Code": "SKU002", "Quantity": 50,
                "Total Volume": 250}),
    ])

    with Session(packed) as session:
        rows = q.volume_by_group(session, q.SALES_VIEW, ScopeFilters(),
                                 group_by=GroupBy.MATERIAL_BRAND)

    assert [row["volume"] for row in rows] == [750.0]


def test_the_sales_measure_set_sums_volume():
    """It is a measure like any other now: one query, one column."""
    from app.ai import queries as q

    assert "volume" in q.SALES_MEASURES.sums


# ==========================================================================
# The transaction table exposes the new columns (items 10, 20)
# ==========================================================================


def test_the_sales_table_shows_the_line_identity_and_volume():
    from app.reporting.columns import SEARCH_COLUMNS, TRANSACTION_COLUMNS

    columns = TRANSACTION_COLUMNS["sales"]
    for name in ("invoice_line_no", "batch_code", "volume"):
        assert name in columns, name
    assert "volume_unit" not in columns
    assert "batch_code" in SEARCH_COLUMNS["sales"]


def test_the_material_stock_table_shows_the_four_categories_and_no_volume():
    from app.reporting.columns import TRANSACTION_COLUMNS

    columns = TRANSACTION_COLUMNS["material_stock"]
    for name in ("unrestricted_stock", "quality_inspection_stock",
                 "blocked_stock", "stock_in_transit", "total_stock"):
        assert name in columns, name
    assert "volume" not in columns
    assert "volume_unit" not in columns


def test_the_upload_template_offers_total_volume_and_no_unit():
    from app.upload.registry import get_upload_type

    sales = get_upload_type("sales")
    targets = {c.target for c in sales.columns}
    assert {"batch_code", "invoice_line_no", "volume"} <= targets
    assert "volume_unit" not in targets

    # The header an operator reads says what the number is.
    volume = next(c for c in sales.columns if c.target == "volume")
    assert volume.name == "Total Volume"
    assert volume.kind == "numeric"

    # Volume is not something the uploader must supply.
    required = {c.target for c in sales.columns if c.required}
    assert "volume" not in required
    assert "batch_code" not in required


def test_the_material_stock_template_offers_no_volume_column():
    from app.upload.registry import get_upload_type

    targets = {c.target for c in get_upload_type("material_stock").columns}
    assert {"unrestricted_stock", "quality_inspection_stock", "blocked_stock",
            "stock_in_transit"} <= targets
    assert "volume" not in targets
    assert "volume_unit" not in targets


# ==========================================================================
# Per-line reporting: the preview and the duplicate report (items 10, 36, 37)
# ==========================================================================


def three_lines(engine):
    """One accepted line, one accepted different batch, one exact duplicate.

    The specification's worked example, verbatim: the first two lines differ
    only by batch and are both valid, the third repeats the first exactly.
    """
    return do_import(engine, [
        line(**{"Invoice No": "INV001", "SKU Code": "SKU001",
                "Batch Code": "BATCH-A", "Quantity": 100,
                "Total Volume": 500, "Net Sales": 50000}),
        line(**{"Invoice No": "INV001", "SKU Code": "SKU001",
                "Batch Code": "BATCH-B", "Quantity": 50,
                "Total Volume": 250, "Net Sales": 25000}),
        line(**{"Invoice No": "INV001", "SKU Code": "SKU001",
                "Batch Code": "BATCH-A", "Quantity": 100,
                "Total Volume": 500, "Net Sales": 50000}),
    ])


def test_the_result_carries_a_verdict_for_every_line(packed):
    result = three_lines(packed)

    assert [entry.status for entry in result.lines] == [
        "ACCEPTED", "ACCEPTED", "REJECTED"]
    assert [entry.fields["batch_code"] for entry in result.lines] == [
        "BATCH-A", "BATCH-B", "BATCH-A"]
    assert "Duplicate line found within uploaded file" in result.lines[2].reason


def test_an_accepted_line_reports_the_volume_that_was_uploaded(packed):
    result = three_lines(packed)

    first = result.lines[0].to_dict()
    assert float(first["volume"]) == 500
    # No unit beside it: the preview shows the figure the file stated.
    assert "volume_unit" not in first
    assert first["reason"] == "Valid"
    # A rejected line was never loaded, so it has no volume to report.
    assert result.lines[2].to_dict()["volume"] is None


def test_a_line_rejected_before_cleaning_is_still_named(seeded_engine):
    """A bad date must not cost the report the invoice number."""
    result = do_import(seeded_engine, [
        line(**{"Invoice No": "INV009", "Transaction Date": "not a date"}),
    ])

    entry = result.lines[0]
    assert entry.status == "REJECTED"
    assert entry.fields["invoice_no"] == "INV009"
    assert entry.error_code == "INVALID_DATE"


def test_the_duplicate_report_shows_accepted_and_rejected_together(packed):
    from app.etl.quality import line_report

    result = three_lines(packed)
    with Session(packed) as session:
        report = line_report(session, result.batch_id)

    statuses = [record["status"] for record in report["records"]]
    assert statuses == ["ACCEPTED", "ACCEPTED", "REJECTED"]
    rejected = report["records"][2]
    assert rejected["batch_code"] == "BATCH-A"
    assert rejected["error_code"] == "DUPLICATE_IN_FILE"
    assert float(report["records"][0]["volume"]) == 500
    for name in ("company_code", "invoice_no", "invoice_line_no", "material_code",
                 "batch_code", "status", "reason"):
        assert name in report["columns"], name


def test_the_duplicate_report_can_be_narrowed_to_rejections(packed):
    from app.etl.quality import line_report

    result = three_lines(packed)
    with Session(packed) as session:
        report = line_report(session, result.batch_id, status="REJECTED")

    assert report["total"] == 1
    assert report["records"][0]["status"] == "REJECTED"


def test_the_validation_report_counts_what_the_business_asks_for(packed):
    from app.etl.quality import transaction_quality

    result = three_lines(packed)
    with Session(packed) as session:
        report = transaction_quality(session, result.batch_id)

    counts = report["counts"]
    assert counts["total_lines"] == 3
    assert counts["valid_lines"] == 2
    assert counts["duplicate_lines"] == 1
    assert counts["invalid_lines"] == 0
    # No line numbers anywhere in this file — a property of the file, so all
    # three lines count, not only the two that loaded.
    assert counts["missing_invoice_line"] == 3
    assert counts["missing_batch"] == 0
    assert counts["missing_volume"] == 0
    # The unit counter is gone with the column it counted.
    assert "missing_volume_unit" not in counts


def test_the_validation_report_counts_a_missing_volume(seeded_engine):
    """A file that sent no Total Volume: the line loads, the gap is counted."""
    from app.etl.quality import transaction_quality

    result = do_import(seeded_engine, [line(**{"Quantity": 100})])
    with Session(seeded_engine) as session:
        report = transaction_quality(session, result.batch_id)

    assert report["counts"]["valid_lines"] == 1
    assert report["counts"]["missing_volume"] == 1
    assert report["volume_gaps"] == {"MISSING_VOLUME": 1}
    # There is no pack-size cross-check any more, so there is nothing to report
    # about one having failed.
    assert "volume_reference_gaps" not in report
    assert "volume_warnings" not in report


def test_a_volume_the_master_cannot_corroborate_is_simply_stored(packed):
    """No cross-check, no warning: the file is the only authority."""
    from app.etl.quality import transaction_quality

    # SKU001 is a 5 KG pack; the old rule would have expected 500 for 100 units.
    result = do_import(packed, [line(**{"SKU Code": "SKU001", "Quantity": 100,
                                        "Total Volume": 700})])
    with Session(packed) as session:
        report = transaction_quality(session, result.batch_id)

    assert float(facts(packed)[0].volume) == 700
    assert report["counts"]["valid_lines"] == 1
    assert report["counts"]["invalid_lines"] == 0
    assert report["counts"]["missing_volume"] == 0
    assert "volume_difference_warnings" not in report["counts"]


def test_the_validation_report_separates_invalid_from_duplicate(seeded_engine):
    from app.etl.quality import transaction_quality

    result = do_import(seeded_engine, [
        line(**{"Invoice No": "INV001", "SKU Code": "NOPE"}),
        line(**{"Invoice No": "INV002", "Batch Code": "B1"}),
        line(**{"Invoice No": "INV002", "Batch Code": "B1"}),
    ])
    with Session(seeded_engine) as session:
        report = transaction_quality(session, result.batch_id)

    counts = report["counts"]
    assert counts["missing_material"] == 1
    assert counts["invalid_lines"] == 1
    assert counts["duplicate_lines"] == 1


# ==========================================================================
# Filters and units through the API surface (items 27, 28)
# ==========================================================================


def test_the_scope_filter_carries_batch_but_no_unit():
    from app.ai.queries import FILTER_COLUMNS
    from app.ai.schemas import ScopeFilters

    filters = ScopeFilters(batch_codes=["BATCH-A"])
    assert not filters.is_empty()
    assert FILTER_COLUMNS["batch_codes"] == "batch_code"
    # There is no unit on a line, so there is nothing to filter by.
    assert "volume_units" not in FILTER_COLUMNS
    assert "volume_units" not in ScopeFilters.model_fields


def test_the_report_filter_carries_batch_but_no_unit():
    from app.reporting.service import ReportFilters

    columns = ReportFilters(batch_code="BATCH-A",
                            sub_territory_code="ST001").code_filters()
    assert columns["batch_code"] == "BATCH-A"
    assert columns["sub_territory_code"] == "ST001"
    assert "volume_unit" not in columns


# ==========================================================================
# Hierarchy snapshot derived from the Customer Master (items 21, 22)
# ==========================================================================


NO_ORG_HEADERS = {"Date", "Invoice No", "SKU Code", "Quantity", "Net Sales",
                  "Customer Code"}


def no_org_row(**overrides):
    """A row with a customer code and no organisational code at all."""
    row = {key: value for key, value in line().items() if key in NO_ORG_HEADERS}
    row.update(overrides)
    return row


@pytest.fixture
def mapped_customer(seeded_engine):
    """Give a customer an authoritative sub-territory in the master."""
    from app.database.models import DimSubTerritory
    from app.database.models_warehouse import DimCustomer

    with Session(seeded_engine) as session:
        sub = session.execute(select(DimSubTerritory)).scalars().first()
        assert sub is not None, "the fixture must seed a sub-territory"
        session.add(DimCustomer(customer_code="CUST-MAPPED",
                                customer_name="Mapped Customer",
                                sub_territory_code=sub.sub_territory_code))
        session.add(DimCustomer(customer_code="CUST-UNMAPPED",
                                customer_name="Unmapped Customer"))
        session.commit()
        return seeded_engine, sub.sub_territory_code


def test_the_hierarchy_is_derived_from_the_customer_master(mapped_customer):
    engine, _ = mapped_customer
    result = do_import(engine, [no_org_row(**{"Customer Code": "CUST-MAPPED"})])

    assert result.rejected_rows == 0
    assert result.valid_rows == 1
    assert result.hierarchy_from_customer == 1
    fact = facts(engine)[0]
    assert fact.sub_territory_id is not None
    assert fact.region_id is not None      # derived by walking up the chain


def test_a_customer_with_no_sub_territory_is_flagged_not_guessed(mapped_customer):
    engine, _ = mapped_customer
    result = do_import(engine, [no_org_row(**{"Customer Code": "CUST-UNMAPPED"})])

    assert result.valid_rows == 0
    assert result.error_counts.get("CUSTOMER_HIERARCHY_MISSING") == 1
    assert any("Customer hierarchy mapping missing" in r.message
               for r in result.rejections)


def test_a_code_on_the_row_still_wins_over_the_master(mapped_customer):
    """The file is the transaction's own record of where it happened."""
    engine, _ = mapped_customer
    result = do_import(engine, [line(**{"Customer Code": "CUST-MAPPED"})])

    assert result.valid_rows == 1
    assert result.hierarchy_from_customer == 0


# ==========================================================================
# The agent understands a volume question (items 29, 30, 44)
# ==========================================================================


@pytest.mark.parametrize("question", [
    "Dhaka Region-এর sales volume কত?",
    "2026 সালের KG sales কত?",
    "July মাসে কত LTR sales হয়েছে?",
    "Territory T001-এর KG sales দেখাও",
    "Product-wise volume report দাও",
    "Total volume কত?",
])
def test_a_volume_question_reaches_the_volume_intent(question):
    """Naming a unit still routes to volume — it just cannot narrow the answer.

    A line records one Total Volume and no unit, so "KG sales" is understood as
    a volume question and answered with the one figure that exists.
    """
    from app.ai.intent import detect_intent
    from app.ai.schemas import Intent, VolumeToolInput

    prediction = detect_intent(question)
    assert prediction.intent is Intent.SALES_VOLUME, question
    assert not hasattr(prediction, "volume_unit")
    assert "volume_unit" not in VolumeToolInput.model_fields


def test_a_stock_volume_question_is_answered_with_stock_not_sales_volume():
    """"Stock volume" must not be silently answered with the *sales* volume.

    Material stock has no volume — it is four counted quantities. The word
    "volume" in a stock question therefore cannot route to the volume tool; it
    falls through to the stock branch, which answers with the figures that
    actually exist rather than a measure from a different fact table.
    """
    from app.ai.intent import detect_intent
    from app.ai.orchestrator import TOOL_BY_INTENT
    from app.ai.schemas import Intent

    prediction = detect_intent("Plant-wise stock volume দেখাও")
    assert prediction.intent is not Intent.SALES_VOLUME
    assert prediction.intent is Intent.STOCK_BY_PLANT
    assert TOOL_BY_INTENT[Intent.STOCK_BY_PLANT] == "get_stock_by_plant"


def test_naming_two_units_is_still_one_volume_question():
    from app.ai.intent import detect_intent
    from app.ai.schemas import Intent

    assert detect_intent("KG and LTR sales volume").intent is Intent.SALES_VOLUME


def test_an_ordinary_sales_question_is_not_a_volume_question():
    """A unit hiding inside a word must not divert the whole question."""
    from app.ai.intent import detect_intent
    from app.ai.schemas import Intent

    for question in ("Dhaka region-এর sales কত?", "monthly sales trend"):
        assert detect_intent(question).intent is not Intent.SALES_VOLUME, question


def test_the_unit_catalogue_outlived_the_master_it_served():
    """``etl.uom`` outlived the volume-unit filter: it parses pack sizes.

    A pack size is Product Master data, and a *target* volume's unit is reached
    through it. No sales or stock figure passes through this module.
    """
    catalogue = {entry["code"]: entry for entry in uom.catalogue()}
    assert {"KG", "GM", "LTR", "ML"} <= set(catalogue)
    assert catalogue["KG"]["dimension"] != catalogue["LTR"]["dimension"]
