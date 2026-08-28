"""End-to-end ETL tests: import, validation, rejection, duplicates, incremental load."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models_warehouse import (
    BATCH_COMPLETED,
    BATCH_COMPLETED_WITH_ERRORS,
    BATCH_FAILED,
    STAGING_REJECTED,
    STAGING_VALID,
    EtlImportBatch,
    EtlRejectedRecord,
    FactMaterialStock,
    FactSales,
    FactTarget,
    StgSales,
)
from app.database.models import DimStorageLocation
from app.config import get_settings
from app.etl.pipeline import LOAD_MODE_INITIAL, run_import
from app.etl.readers import RecordsSourceReader
from conftest_phase2 import (
    material_stock_row,
    sales_row,
    target_row,
)


def do_import(engine, data_type, records, **kwargs):
    reader = RecordsSourceReader(records, source_name="test.csv", source_type="CSV")
    return run_import(engine, data_type, reader, source_system="TEST", **kwargs)


def count(engine, model) -> int:
    with Session(engine) as session:
        return session.execute(select(func.count()).select_from(model)).scalar_one()


def rejections(engine, batch_id: int) -> list[EtlRejectedRecord]:
    with Session(engine) as session:
        return list(session.execute(
            select(EtlRejectedRecord).where(EtlRejectedRecord.batch_id == batch_id)
        ).scalars())


# --------------------------------------------------------------------------
# Happy path for all five data types
# --------------------------------------------------------------------------


def test_sales_import(seeded_engine) -> None:
    result = do_import(seeded_engine, "sales", [sales_row()])

    assert result.status == BATCH_COMPLETED, result.error_counts
    assert (result.total_rows, result.valid_rows, result.rejected_rows) == (1, 1, 0)
    assert result.inserted_rows == 1
    assert count(seeded_engine, FactSales) == 1

    with Session(seeded_engine) as session:
        fact = session.execute(select(FactSales)).scalar_one()
    assert fact.net_sales == Decimal("1000.0000")       # 1200 - 200
    assert fact.gross_profit == Decimal("300.0000")     # 1000 - 700
    assert fact.date_id == 20260815
    # The whole hierarchy was derived from the territory code alone.
    assert fact.territory_id is not None
    assert fact.company_id is not None
    assert fact.region_id is not None
    # Codes for the PENDING_SOURCE_DATA dimensions are preserved, keys are NULL.
    assert fact.customer_code == "CUST-001"
    assert fact.customer_id is None


def test_the_retired_data_types_are_not_importable(seeded_engine) -> None:
    """Collection and Outstanding cannot be imported, because they do not exist.

    The ETL is registry-driven, so this is the whole removal in one assertion:
    no dataset spec means no staging table, no fact table, no upload template
    and no route. A file naming one of them is refused by name rather than
    loaded into something that happens to accept it.
    """
    from app.etl.datasets import DATA_TYPES, get_dataset

    assert set(DATA_TYPES) == {"sales", "material_stock", "target"}
    for data_type in ("collection", "outstanding"):
        with pytest.raises(ValueError):
            get_dataset(data_type)


def test_material_stock_import_stores_the_four_categories(seeded_engine) -> None:
    result = do_import(seeded_engine, "material_stock", [material_stock_row()])
    assert result.status == BATCH_COMPLETED, result.error_counts
    with Session(seeded_engine) as session:
        fact = session.execute(select(FactMaterialStock)).scalar_one()
    assert fact.unrestricted_stock == Decimal("100.0000")
    assert fact.quality_inspection_stock == Decimal("20.0000")
    assert fact.blocked_stock == Decimal("5.0000")
    assert fact.stock_in_transit == Decimal("10.0000")
    assert fact.shelf_life_expiration_date == dt.date(2027, 1, 15)
    # The row resolved to all three masters, and the raw codes stay on the fact
    # so a position can still be read without the dimension joins.
    assert fact.plant_id is not None
    assert fact.storage_location_id is not None
    assert fact.material_id is not None
    assert fact.plant_code == "PL01"
    # The material is the finest grain the position has, so it is on the fact
    # rather than only reachable through the master.
    assert fact.material_code == "MAT-001"


def test_material_stock_rejects_a_storage_location_the_master_does_not_have(
        seeded_engine) -> None:
    """An unknown storage location is a rejection, never a new dimension row.

    Stock may only sit where the Storage Location Master says it can; inventing
    the location would invent something in the business that does not exist. The
    error names that master rather than "the Material Master", because that is
    the file the operator has to correct.
    """
    result = do_import(seeded_engine, "material_stock",
                       [material_stock_row(**{"Storage Location": "NOPE"})])
    assert result.rejected_rows == 1
    assert result.valid_rows == 0
    with Session(seeded_engine) as session:
        assert session.execute(select(FactMaterialStock)).first() is None
        assert session.execute(
            select(DimStorageLocation)
            .where(DimStorageLocation.storage_location_code == "NOPE")
        ).first() is None
        rejected = session.execute(select(EtlRejectedRecord)).scalar_one()
    assert rejected.error_code == "INVALID_STORAGE_LOCATION_CODE"


def test_material_stock_rejects_a_plant_the_master_does_not_have(
        seeded_engine) -> None:
    """A plant the Plant Master has never heard of names that master, not another.

    Each of the three masters loads separately, so each failure has to say which
    one is missing the record — otherwise an operator re-uploads the wrong file.
    """
    result = do_import(seeded_engine, "material_stock",
                       [material_stock_row(**{"Plant": "PL99"})])
    assert result.rejected_rows == 1
    with Session(seeded_engine) as session:
        codes = {r.error_code for r in
                 session.execute(select(EtlRejectedRecord)).scalars().all()}
    # The storage location is keyed on plant + code, so an unknown plant takes
    # that with it: both are reported at once rather than over two attempts.
    assert "INVALID_PLANT_CODE" in codes


def test_material_stock_requires_a_material_code(seeded_engine) -> None:
    """A position naming no material identifies nothing and is rejected."""
    result = do_import(seeded_engine, "material_stock",
                       [material_stock_row(**{"Material": None})])
    assert result.rejected_rows == 1
    assert result.valid_rows == 0


def test_material_stock_rejects_an_unknown_material_code(seeded_engine) -> None:
    """A material the master has never heard of is named as such.

    The message matters: "no such combination" would leave the operator to work
    out which of the codes is wrong by re-reading three masters.
    """
    result = do_import(seeded_engine, "material_stock",
                       [material_stock_row(**{"Material": "MAT-999"})])
    assert result.rejected_rows == 1
    with Session(seeded_engine) as session:
        rejected = session.execute(select(EtlRejectedRecord)).scalar_one()
    assert rejected.error_code == "INVALID_MATERIAL_CODE"
    assert "MAT-999" in rejected.error_message


def test_material_stock_rejects_a_contradicted_material_group(seeded_engine) -> None:
    """The group the file states must be the group the master records.

    A material group classifies the material, so the two cannot disagree. Which
    of them is wrong this system cannot know, so it reports the disagreement and
    refuses the row instead of choosing one.
    """
    result = do_import(seeded_engine, "material_stock",
                       [material_stock_row(**{"Material Group": "MG02"})])
    assert result.rejected_rows == 1
    with Session(seeded_engine) as session:
        rejected = session.execute(select(EtlRejectedRecord)).scalar_one()
    assert rejected.error_code == "MATERIAL_GROUP_MISMATCH"
    # Both groups are named, because the correction could be to either.
    assert "MG01" in rejected.error_message and "MG02" in rejected.error_message


def test_material_stock_rejects_a_contradicted_material_brand(seeded_engine) -> None:
    """The brand is checked the same way the group is, and for the same reason.

    A material brand is an attribute of the material, recorded once in the
    Material Master. A position claiming a different one is a disagreement
    between two sources, and neither may silently win.
    """
    result = do_import(seeded_engine, "material_stock",
                       [material_stock_row(**{"Material Brand": "MB02"})])
    assert result.rejected_rows == 1
    with Session(seeded_engine) as session:
        rejected = session.execute(select(EtlRejectedRecord)).scalar_one()
    assert rejected.error_code == "MATERIAL_BRAND_MISMATCH"
    assert "MB01" in rejected.error_message and "MB02" in rejected.error_message


def test_material_stock_separates_two_materials_in_one_group(seeded_engine) -> None:
    """Two materials in the same location and group are two positions.

    This is the case the original four-code key could not express: it would have
    treated the second material as a re-upload of the first and overwritten it.
    Both materials share a group and a brand here, so nothing but the material
    code distinguishes them — which is exactly what the key has to do.
    """
    do_import(seeded_engine, "material_stock", [
        material_stock_row(),
        material_stock_row(**{"Material": "MAT-003", "Unrestricted": 700}),
    ])
    with Session(seeded_engine) as session:
        rows = session.execute(select(FactMaterialStock)).scalars().all()
        by_material = {row.material_code: row.unrestricted_stock for row in rows}
    assert by_material == {"MAT-001": Decimal("100.0000"),
                           "MAT-003": Decimal("700.0000")}


def test_material_stock_reimport_replaces_the_position(seeded_engine) -> None:
    """A position is current: re-importing the same placement updates it.

    The business key is company, plant, storage location and material plus the
    two goods dates, so the same physical position uploaded twice is one row
    with the newer figures — a stock snapshot must not accumulate.
    """
    do_import(seeded_engine, "material_stock", [material_stock_row()])
    do_import(seeded_engine, "material_stock",
              [material_stock_row(**{"Unrestricted": 250})])
    with Session(seeded_engine) as session:
        fact = session.execute(select(FactMaterialStock)).scalar_one()
    assert fact.unrestricted_stock == Decimal("250.0000")


def test_target_import(seeded_engine) -> None:
    result = do_import(seeded_engine, "target", [target_row()])
    assert result.status == BATCH_COMPLETED, result.error_counts
    with Session(seeded_engine) as session:
        fact = session.execute(select(FactTarget)).scalar_one()
    assert fact.target_amount == Decimal("5000000.0000")
    assert fact.target_month == "2026-08"
    assert fact.financial_year == "FY 2026-27"
    # The territory is what the file stated; everything above it was derived.
    assert fact.territory_id is not None
    assert fact.region_id is not None


# --------------------------------------------------------------------------
# Master data validation
# --------------------------------------------------------------------------


def test_invalid_material_is_rejected(seeded_engine) -> None:
    result = do_import(seeded_engine, "sales", [sales_row(**{"SKU Code": "SKU-NOPE"})])

    assert result.status == BATCH_COMPLETED_WITH_ERRORS
    assert result.rejected_rows == 1
    assert count(seeded_engine, FactSales) == 0
    rejected = rejections(seeded_engine, result.batch_id)[0]
    assert rejected.error_code == "INVALID_MATERIAL_CODE"
    assert rejected.error_category == "INVALID_MASTER"
    assert rejected.field_value == "SKU-NOPE"
    assert rejected.raw_data["SKU Code"] == "SKU-NOPE"


def test_invalid_region_is_rejected_with_a_clear_reason(seeded_engine) -> None:
    result = do_import(
        seeded_engine, "sales",
        [sales_row(**{"Territory Code": None, "Region Code": "R999"})],
    )
    rejected = rejections(seeded_engine, result.batch_id)[0]
    assert rejected.error_code == "INVALID_REGION_CODE"
    assert "R999" in rejected.error_message
    assert "dim_region" in rejected.error_message
    assert count(seeded_engine, FactSales) == 0


def test_hierarchy_mismatch_is_rejected_even_though_both_codes_exist(seeded_engine) -> None:
    """REG002 exists and AR001 exists, but AR001 belongs to REG001."""
    result = do_import(
        seeded_engine, "sales",
        [sales_row(**{"Territory Code": None, "Area Code": "AR001",
                      "Region Code": "REG002"})],
    )
    rejected = rejections(seeded_engine, result.batch_id)[0]
    assert rejected.error_code == "AREA_REGION_MISMATCH"
    assert rejected.error_category == "INVALID_HIERARCHY"
    assert "REG001" in rejected.error_message and "REG002" in rejected.error_message
    assert count(seeded_engine, FactSales) == 0


def test_region_zone_mismatch(seeded_engine) -> None:
    """A row naming a region under the wrong zone is rejected."""
    with Session(seeded_engine) as session:
        from app.database.models import DimSalesLine, DimZone

        session.add(DimSalesLine(sales_line_code="SL002", sales_line_name="Modern Trade",
                                 bu_code="BU001"))
        session.add(DimZone(zone_code="Z002", zone_name="Khulna Zone",
                            sales_line_code="SL002"))
        session.commit()

    result = do_import(
        seeded_engine, "sales",
        [sales_row(**{"Territory Code": None, "Region Code": "REG001",
                      "Zone Code": "Z002"})],
    )
    rejected = rejections(seeded_engine, result.batch_id)[0]
    assert rejected.error_code == "REGION_ZONE_MISMATCH"
    assert count(seeded_engine, FactSales) == 0


def test_consistent_multi_level_row_is_accepted(seeded_engine) -> None:
    result = do_import(
        seeded_engine, "sales",
        [sales_row(**{"Region Code": "REG001", "Area Code": "AR001",
                      "Zone Code": "Z001", "Territory Code": "TR001"})],
    )
    assert result.status == BATCH_COMPLETED, result.error_counts
    assert count(seeded_engine, FactSales) == 1


def test_row_with_no_organisational_code_is_rejected(seeded_engine) -> None:
    result = do_import(seeded_engine, "sales",
                       [sales_row(**{"Territory Code": None,
                                     "Customer Code": None})])
    rejected = rejections(seeded_engine, result.batch_id)[0]
    assert rejected.error_code == "NO_ORGANISATIONAL_CODE"
    assert count(seeded_engine, FactSales) == 0


def test_a_customer_the_master_cannot_place_is_named_in_the_rejection(
        seeded_engine) -> None:
    """The row has a customer, so the reason says what is actually missing:
    the Customer Master has no sub-territory to derive the hierarchy from."""
    result = do_import(seeded_engine, "sales", [sales_row(**{"Territory Code": None})])
    rejected = rejections(seeded_engine, result.batch_id)[0]
    assert rejected.error_code == "CUSTOMER_HIERARCHY_MISSING"
    assert "Customer hierarchy mapping missing" in rejected.error_message
    assert count(seeded_engine, FactSales) == 0


# --------------------------------------------------------------------------
# Value validation
# --------------------------------------------------------------------------


def test_invalid_date_is_rejected(seeded_engine) -> None:
    result = do_import(seeded_engine, "sales", [sales_row(**{"Date": "31/02/2026"})])
    rejected = rejections(seeded_engine, result.batch_id)[0]
    assert rejected.error_code == "IMPOSSIBLE_DATE"
    assert rejected.error_category == "INVALID_DATE"


def test_ambiguous_date_is_rejected_but_accepted_with_a_configured_format(
    seeded_engine,
) -> None:
    ambiguous = [sales_row(**{"Date": "05/08/2026"})]
    result = do_import(seeded_engine, "sales", ambiguous)
    assert rejections(seeded_engine, result.batch_id)[0].error_code == "AMBIGUOUS_DATE"

    result = do_import(seeded_engine, "sales", ambiguous, date_format="DD/MM/YYYY")
    assert result.status == BATCH_COMPLETED
    with Session(seeded_engine) as session:
        assert session.execute(select(FactSales.date_id)).scalar_one() == 20260805


def test_invalid_numeric_is_rejected(seeded_engine) -> None:
    result = do_import(seeded_engine, "sales", [sales_row(**{"Quantity": "ten"})])
    rejected = rejections(seeded_engine, result.batch_id)[0]
    assert rejected.error_code == "INVALID_NUMERIC"
    assert count(seeded_engine, FactSales) == 0


def test_negative_quantity_is_allowed_as_a_return(seeded_engine) -> None:
    result = do_import(
        seeded_engine, "sales",
        [sales_row(**{"Quantity": -5, "Gross Sales": -600, "Discount": 0, "Cost": -350})],
    )
    assert result.status == BATCH_COMPLETED, result.error_counts
    with Session(seeded_engine) as session:
        fact = session.execute(select(FactSales)).scalar_one()
    assert fact.quantity == Decimal("-5.0000")
    assert fact.net_sales == Decimal("-600.0000")


def test_negative_discount_is_rejected_not_corrected(seeded_engine) -> None:
    result = do_import(seeded_engine, "sales", [sales_row(**{"Discount": -50})])
    rejected = rejections(seeded_engine, result.batch_id)[0]
    assert rejected.error_code == "NEGATIVE_NOT_ALLOWED"
    assert count(seeded_engine, FactSales) == 0


def test_missing_required_value_is_rejected(seeded_engine) -> None:
    result = do_import(seeded_engine, "sales", [sales_row(**{"Invoice No": None})])
    rejected = rejections(seeded_engine, result.batch_id)[0]
    assert rejected.error_code == "MISSING_REQUIRED_FIELD"
    assert rejected.error_category == "MISSING_REQUIRED_FIELD"


def test_missing_required_column_fails_the_batch_without_loading(seeded_engine) -> None:
    row = sales_row()
    row.pop("Net Sales")
    result = do_import(seeded_engine, "sales", [row])

    assert result.status == BATCH_FAILED
    assert "net_sales" in result.missing_columns
    assert count(seeded_engine, FactSales) == 0
    assert count(seeded_engine, StgSales) == 0


def test_a_file_without_gross_sales_loads(seeded_engine) -> None:
    """Gross is the breakdown behind net, and plenty of sources omit it.

    Net is the figure every report is built on; gross is reconstructed from it
    and the discount, so a file carrying only the settled value is complete
    enough to load.
    """
    row = sales_row(**{"Net Sales": 1000, "Discount": 200})
    row.pop("Gross Sales")
    result = do_import(seeded_engine, "sales", [row])

    assert result.status != BATCH_FAILED, result.error_counts
    assert result.rejected_rows == 0
    assert count(seeded_engine, FactSales) == 1

    with Session(seeded_engine) as session:
        fact = session.execute(select(FactSales)).scalars().one()
    assert fact.net_sales == 1000
    assert fact.gross_sales == 1200      # net + discount


def test_unmapped_columns_are_reported_not_dropped(seeded_engine) -> None:
    result = do_import(
        seeded_engine, "sales", [sales_row(**{"Some Extra Column": "keep me"})]
    )
    assert "Some Extra Column" in result.unmapped_columns
    with Session(seeded_engine) as session:
        staged = session.execute(select(StgSales)).scalar_one()
    assert staged.raw_data["Some Extra Column"] == "keep me"


# --------------------------------------------------------------------------
# Duplicates and incremental load
# --------------------------------------------------------------------------


def test_duplicate_within_one_file_is_rejected(seeded_engine) -> None:
    result = do_import(seeded_engine, "sales", [sales_row(), sales_row()])

    assert result.valid_rows == 1
    assert result.rejected_rows == 1
    assert rejections(seeded_engine, result.batch_id)[0].error_code == "DUPLICATE_IN_FILE"
    assert count(seeded_engine, FactSales) == 1


def test_a_repeated_line_loads_beside_its_twin_when_the_check_is_off(
    seeded_engine,
) -> None:
    """With `ETL_REJECT_DUPLICATE_IN_FILE` off, the repeat is a second line.

    It cannot be written under the key its twin already holds: `business_key`
    is UNIQUE, and the upsert applies a repeated key as two parameter sets that
    resolve to whichever was written last — one line's figures would silently
    replace the other's, which is neither loading the repeat nor rejecting it.
    Numbering the repeat is what makes "both lines count" true.
    """
    settings = get_settings()
    object.__setattr__(settings, "etl_reject_duplicate_in_file", False)
    try:
        result = do_import(seeded_engine, "sales", [sales_row(), sales_row()])
    finally:
        object.__setattr__(settings, "etl_reject_duplicate_in_file", True)

    assert result.valid_rows == 2
    assert result.rejected_rows == 0
    assert not rejections(seeded_engine, result.batch_id)
    assert count(seeded_engine, FactSales) == 2

    with Session(seeded_engine) as session:
        keys = session.execute(select(FactSales.business_key)).scalars().all()
    assert len(set(keys)) == 2, keys
    assert sum(key.endswith("#2") for key in keys) == 1, keys


def test_reimporting_the_same_file_does_not_duplicate(seeded_engine) -> None:
    first = do_import(seeded_engine, "sales", [sales_row()])
    second = do_import(seeded_engine, "sales", [sales_row()])

    assert first.inserted_rows == 1
    assert second.inserted_rows == 0
    assert second.updated_rows == 1
    assert second.duplicate_rows == 1
    assert count(seeded_engine, FactSales) == 1


def test_incremental_load_updates_changed_values(seeded_engine) -> None:
    do_import(seeded_engine, "sales", [sales_row()])
    do_import(seeded_engine, "sales", [sales_row(**{"Quantity": 25, "Gross Sales": 3000})])

    with Session(seeded_engine) as session:
        fact = session.execute(select(FactSales)).scalar_one()
    assert fact.quantity == Decimal("25.0000")
    assert fact.net_sales == Decimal("2800.0000")
    assert count(seeded_engine, FactSales) == 1


def test_initial_load_mode_rejects_rows_already_in_the_warehouse(seeded_engine) -> None:
    do_import(seeded_engine, "sales", [sales_row()])
    result = do_import(seeded_engine, "sales", [sales_row()], load_mode=LOAD_MODE_INITIAL)

    assert result.valid_rows == 0
    assert rejections(seeded_engine, result.batch_id)[0].error_code == (
        "DUPLICATE_IN_WAREHOUSE"
    )
    assert count(seeded_engine, FactSales) == 1


def test_different_source_systems_do_not_collide(seeded_engine) -> None:
    reader = RecordsSourceReader([sales_row()], source_name="a.csv")
    run_import(seeded_engine, "sales", reader, source_system="DEMO")
    reader = RecordsSourceReader([sales_row()], source_name="b.csv")
    run_import(seeded_engine, "sales", reader, source_system="SAP")

    assert count(seeded_engine, FactSales) == 2


def test_incremental_load_adds_only_new_rows(seeded_engine) -> None:
    do_import(seeded_engine, "sales", [sales_row()])
    result = do_import(
        seeded_engine, "sales",
        [sales_row(), sales_row(**{"Invoice No": "INV-0002"})],
    )
    assert result.inserted_rows == 1
    assert result.updated_rows == 1
    assert count(seeded_engine, FactSales) == 2


# --------------------------------------------------------------------------
# Batch, staging and auditability
# --------------------------------------------------------------------------


def test_batch_records_the_full_audit_trail(seeded_engine) -> None:
    result = do_import(seeded_engine, "sales", [sales_row()])
    with Session(seeded_engine) as session:
        batch = session.get(EtlImportBatch, result.batch_id)
        fact = session.execute(select(FactSales)).scalar_one()

    assert batch.status == BATCH_COMPLETED
    assert batch.data_type == "sales"
    assert batch.source_system == "TEST"
    assert batch.source_type == "CSV"
    assert batch.total_rows == 1 and batch.successful_rows == 1
    assert batch.completed_at is not None
    # Each fact row traces back to system, file, row and batch.
    assert fact.source_system == "TEST"
    assert fact.source_file == "test.csv"
    assert fact.source_row_number == 1
    assert fact.import_batch_id == batch.batch_id


def test_staging_keeps_every_row_with_its_verdict(seeded_engine) -> None:
    do_import(seeded_engine, "sales",
              [sales_row(), sales_row(**{"Invoice No": "INV-9", "SKU Code": "BAD"})])
    with Session(seeded_engine) as session:
        staged = session.execute(select(StgSales).order_by(StgSales.staging_id)).scalars().all()

    assert len(staged) == 2
    assert staged[0].validation_status == STAGING_VALID
    assert staged[1].validation_status == STAGING_REJECTED
    assert staged[1].validation_error == "INVALID_MATERIAL_CODE"
    # Staging preserves the raw text so a bad value stays inspectable.
    assert staged[1].material_code == "BAD"


def test_a_file_larger_than_the_parameter_ceiling_still_writes_its_verdicts(
    seeded_engine, monkeypatch,
) -> None:
    """Staging verdicts are written in chunks, not one enormous ``IN``.

    Every row number in a verdict group becomes a bind parameter, so a single
    statement covering a large file exceeds SQLite's 32,766-parameter limit and
    the import dies with "too many SQL variables" after all the work is done.

    The ceiling is patched far below SQLite's real one so the chunking runs in a
    second rather than needing 40,000 rows. That means the statement count, not
    the absence of an exception, is what proves the fix: a single statement would
    pass here and still fail in production.
    """
    from sqlalchemy import event

    import app.etl.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "parameter_limit", lambda _session: 12)

    staging_updates: list[str] = []

    @event.listens_for(seeded_engine, "before_cursor_execute")
    def _record(_conn, _cursor, statement, _params, _context, _executemany):
        if statement.lstrip().upper().startswith("UPDATE STG_SALES"):
            staging_updates.append(statement)

    try:
        rows = [sales_row(**{"Invoice No": f"BULK-{n:04d}"}) for n in range(40)]
        result = do_import(seeded_engine, "sales", rows)
    finally:
        event.remove(seeded_engine, "before_cursor_execute", _record)

    assert result.status == BATCH_COMPLETED, result.error_counts
    assert result.valid_rows == 40
    # 40 rows of one verdict at 4 per statement: chunked, not one statement.
    assert len(staging_updates) > 1

    # And every staged row still carries its verdict, so no chunk was skipped and
    # none was left at the PENDING the loader wrote.
    with Session(seeded_engine) as session:
        statuses = session.execute(select(StgSales.validation_status)).scalars().all()
    assert len(statuses) == 40
    assert set(statuses) == {STAGING_VALID}


def test_dry_run_writes_nothing(seeded_engine) -> None:
    result = do_import(seeded_engine, "sales", [sales_row()], dry_run=True)
    assert result.valid_rows == 1
    assert count(seeded_engine, FactSales) == 0
    assert count(seeded_engine, EtlImportBatch) == 0


def test_dim_date_is_extended_automatically(seeded_engine) -> None:
    """A transaction outside the pre-built date range still loads."""
    from app.database.models_warehouse import DimDate

    result = do_import(seeded_engine, "sales", [sales_row(**{"Date": "2031-03-09"})])
    assert result.status == BATCH_COMPLETED, result.error_counts
    with Session(seeded_engine) as session:
        row = session.get(DimDate, 20310309)
    assert row is not None
    assert row.financial_year == "FY 2030-31"


def test_rows_at_different_hierarchy_levels_load_in_one_batch(seeded_engine) -> None:
    """A file mixing grains must not break the bulk insert.

    A territory-level row resolves territory_id and sub_territory_id; an
    area-level row does not, so the two fact rows carry different keys. The
    loader has to align them into one uniform batch.
    """
    result = do_import(seeded_engine, "sales", [
        sales_row(**{"Invoice No": "INV-T", "Territory Code": "TR001"}),
        sales_row(**{"Invoice No": "INV-A", "Territory Code": None,
                     "Area Code": "AR001"}),
    ])
    assert result.status == BATCH_COMPLETED, result.error_counts
    assert count(seeded_engine, FactSales) == 2

    with Session(seeded_engine) as session:
        facts = {f.invoice_no: f for f in
                 session.execute(select(FactSales)).scalars()}
    assert facts["INV-T"].territory_id is not None
    assert facts["INV-A"].territory_id is None
    assert facts["INV-A"].area_id is not None       # the level it did carry


def test_a_wide_fact_loads_more_rows_than_the_parameter_limit(seeded_engine) -> None:
    """A large batch must be chunked by bind parameters, not just by rows.

    fact_sales is the widest fact here, so a 5,000-row chunk needs well beyond
    SQLite's 32,766 placeholder limit and beyond PostgreSQL's 65,535. Chunking
    by rows alone fails on both.
    """
    # A distinct invoice per row keeps every business key unique, so the test
    # measures chunking rather than duplicate detection.
    rows = [
        sales_row(**{
            "Invoice No": f"INV-{index:05d}",
            "Invoice Date": f"2026-08-{(index % 28) + 1:02d}",
            "SKU Code": "SKU001" if index % 2 else "SKU002",
        })
        for index in range(3000)
    ]
    result = do_import(seeded_engine, "sales", rows)

    assert result.status == BATCH_COMPLETED, result.error_counts
    assert result.valid_rows == len(rows)
    assert count(seeded_engine, FactSales) == len(rows)


def test_chunk_size_respects_the_dialect_parameter_budget(seeded_engine) -> None:
    from sqlalchemy.orm import Session as SASession

    from app.etl.bulk import effective_chunk_size, parameter_limit

    with SASession(seeded_engine) as session:
        wide = [{f"c{i}": i for i in range(25)}] * 10
        narrow = [{"a": 1, "b": 2}] * 10

        limit = parameter_limit(session)
        assert effective_chunk_size(session, wide, 5000) <= limit // 25
        assert effective_chunk_size(session, narrow, 5000) == 5000
        # Never zero, even for an absurdly wide row.
        assert effective_chunk_size(session, [{str(i): i for i in range(100_000)}],
                                    5000) >= 1


def test_master_data_is_never_modified_by_an_import(seeded_engine) -> None:
    from app.database.models import DimMaterial, DimRegion

    before = (count(seeded_engine, DimMaterial), count(seeded_engine, DimRegion))
    do_import(seeded_engine, "sales",
              [sales_row(), sales_row(**{"SKU Code": "GHOST", "Invoice No": "INV-X"})])
    after = (count(seeded_engine, DimMaterial), count(seeded_engine, DimRegion))
    assert before == after
