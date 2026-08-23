"""The fixed Target structure: ten columns, and what each of them is checked against.

A target is a plan attached to master records. These tests hold two rules to the
wall. The period is stated as a month of a financial year and the two must agree
with the configured calendar — never be interpreted into agreement. And every
code on the row must reference a master record that actually exists *and* that
sits where the row says it sits, because a target filed against the wrong
territory is a wrong report for the whole of that month.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models_warehouse import (
    STATUS_AVAILABLE,
    DimCustomer,
    DimSalesForce,
    EtlRejectedRecord,
    FactTarget,
    MasterSourceStatus,
)
from app.etl.pipeline import run_import
from app.etl.readers import RecordsSourceReader
from conftest_phase2 import target_row


def load(engine, records: list[dict]):
    reader = RecordsSourceReader(records, source_name="target.csv", source_type="CSV")
    return run_import(engine, "target", reader, source_system="TEST")


def error_codes(engine, batch_id: int) -> list[str]:
    with Session(engine) as session:
        return list(session.execute(
            select(EtlRejectedRecord.error_code)
            .where(EtlRejectedRecord.batch_id == batch_id)
        ).scalars())


def only_target(engine) -> FactTarget:
    with Session(engine) as session:
        return session.execute(select(FactTarget)).scalar_one()


def target_count(engine) -> int:
    with Session(engine) as session:
        return session.execute(
            select(func.count()).select_from(FactTarget)
        ).scalar_one()


# ---------------------------------------------------------------------------
# The period: a month and a financial year, resolved together
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("month,financial_year", [
    ("2026-08", "FY 2026-27"),
    ("2026/8", "2026-27"),
    ("August", "FY 2026-27"),
    ("Aug", "2026-2027"),
    (8, "FY 2026-27"),
])
def test_the_accepted_month_and_year_forms_all_resolve_to_one_period(
        seeded_engine, month, financial_year) -> None:
    """Every accepted spelling produces the same canonical period."""
    result = load(seeded_engine, [
        target_row(**{"Target Month": month, "Financial Year": financial_year}),
    ])

    assert result.rejected_rows == 0, result.error_counts
    fact = only_target(seeded_engine)
    assert fact.target_month == "2026-08"
    assert fact.financial_year == "FY 2026-27"
    # First of the month, so a target and its own month's sales fall in the
    # same financial period however the calendar is configured.
    assert fact.date_id == 20260801


def test_a_february_target_belongs_to_the_opening_years_successor(seeded_engine) -> None:
    """With a July start, February 2027 is still FY 2026-27."""
    result = load(seeded_engine, [
        target_row(**{"Target Month": "February", "Financial Year": "FY 2026-27"}),
    ])

    assert result.rejected_rows == 0, result.error_counts
    fact = only_target(seeded_engine)
    assert fact.target_month == "2027-02"
    assert fact.date_id == 20270201


def test_an_unrecognised_month_is_rejected_not_interpreted(seeded_engine) -> None:
    result = load(seeded_engine, [target_row(**{"Target Month": "Q1"})])

    assert result.rejected_rows == 1
    assert error_codes(seeded_engine, result.batch_id) == ["INVALID_TARGET_MONTH"]
    assert target_count(seeded_engine) == 0


def test_an_unrecognised_financial_year_is_rejected(seeded_engine) -> None:
    result = load(seeded_engine, [target_row(**{"Financial Year": "2026-28"})])

    assert result.rejected_rows == 1
    assert error_codes(seeded_engine, result.batch_id) == ["INVALID_FINANCIAL_YEAR"]


def test_a_month_outside_the_stated_financial_year_is_rejected(seeded_engine) -> None:
    """March 2026 belongs to FY 2025-26, whatever the row claims."""
    result = load(seeded_engine, [target_row(**{"Target Month": "2026-03"})])

    assert result.rejected_rows == 1
    assert error_codes(seeded_engine, result.batch_id) == ["TARGET_PERIOD_MISMATCH"]
    message = _first_message(seeded_engine, result.batch_id)
    assert "FY 2025-26" in message and "FY 2026-27" in message


@pytest.mark.parametrize("field,value", [
    ("Target Month", ""),
    ("Financial Year", ""),
    ("Territory Code", ""),
    ("SKU Code", ""),
    ("Target Amount", ""),
])
def test_every_required_column_is_required(seeded_engine, field, value) -> None:
    result = load(seeded_engine, [target_row(**{field: value})])

    assert result.rejected_rows == 1, field
    assert target_count(seeded_engine) == 0


def test_two_spellings_of_one_period_update_a_single_target(seeded_engine) -> None:
    """The key is built from canonical values, so the second file is an update."""
    load(seeded_engine, [target_row(**{"Target Amount": 100})])
    load(seeded_engine, [
        target_row(**{"Target Month": "August", "Financial Year": "2026-27",
                      "Target Amount": 250}),
    ])

    assert target_count(seeded_engine) == 1
    assert only_target(seeded_engine).target_amount == Decimal("250.0000")


# ---------------------------------------------------------------------------
# Master codes, and where they sit
# ---------------------------------------------------------------------------


def test_an_unknown_territory_is_rejected(seeded_engine) -> None:
    result = load(seeded_engine, [target_row(**{"Territory Code": "TR-NOPE"})])

    assert error_codes(seeded_engine, result.batch_id) == ["INVALID_TERRITORY_CODE"]


def test_an_unknown_material_is_rejected(seeded_engine) -> None:
    result = load(seeded_engine, [target_row(**{"SKU Code": "SKU-NOPE"})])

    assert error_codes(seeded_engine, result.batch_id) == ["INVALID_MATERIAL_CODE"]


def test_a_sub_territory_from_another_territory_is_rejected(seeded_engine) -> None:
    """STR001 belongs to TR001, so naming it under another territory is wrong."""
    with Session(seeded_engine) as session:
        from app.database.models import DimTerritory

        session.add(DimTerritory(territory_code="TR009", territory_name="Elsewhere",
                                 unit_code="UN001"))
        session.commit()

    result = load(seeded_engine, [
        target_row(**{"Territory Code": "TR009", "Sub Territory Code": "STR001"}),
    ])

    assert error_codes(seeded_engine, result.batch_id) == [
        "SUB_TERRITORY_TERRITORY_MISMATCH"
    ]


def test_the_hierarchy_above_the_territory_is_derived_not_stated(seeded_engine) -> None:
    result = load(seeded_engine, [target_row()])

    assert result.rejected_rows == 0, result.error_counts
    fact = only_target(seeded_engine)
    assert fact.territory_id is not None
    assert fact.region_id is not None
    assert fact.company_id is not None


def test_a_region_column_is_reported_as_unmapped(seeded_engine) -> None:
    """A target file states a territory; repeating the region duplicates master data."""
    result = load(seeded_engine, [target_row(**{"Region Code": "REG001"})])

    assert "Region Code" in result.unmapped_columns
    assert result.rejected_rows == 0, result.error_counts


# ---------------------------------------------------------------------------
# Customer and sales-force assignment
# ---------------------------------------------------------------------------


def _customer_master(engine, **rows: str) -> None:
    """Populate dim_customer and mark it AVAILABLE, as a real master file would."""
    with Session(engine) as session:
        for code, sub_territory in rows.items():
            session.add(DimCustomer(customer_code=code, customer_name=code,
                                    sub_territory_code=sub_territory))
        session.execute(
            MasterSourceStatus.__table__.update()
            .where(MasterSourceStatus.__table__.c.table_name == "dim_customer")
            .values(status=STATUS_AVAILABLE)
        )
        session.commit()


def test_a_customer_in_another_sub_territory_is_rejected(seeded_engine) -> None:
    with Session(seeded_engine) as session:
        from app.database.models import DimSubTerritory

        session.add(DimSubTerritory(sub_territory_code="STR009",
                                    sub_territory_name="Kazipara South",
                                    territory_code="TR001"))
        session.commit()
    _customer_master(seeded_engine, CUST001="STR009")

    result = load(seeded_engine, [
        target_row(**{"Customer Code": "CUST001", "Sub Territory Code": "STR001"}),
    ])

    assert error_codes(seeded_engine, result.batch_id) == ["CUSTOMER_TERRITORY_MISMATCH"]
    assert "STR009" in _first_message(seeded_engine, result.batch_id)


def test_a_consistent_customer_loads(seeded_engine) -> None:
    _customer_master(seeded_engine, CUST001="STR001")

    result = load(seeded_engine, [
        target_row(**{"Customer Code": "CUST001", "Sub Territory Code": "STR001"}),
    ])

    assert result.rejected_rows == 0, result.error_counts
    fact = only_target(seeded_engine)
    assert fact.customer_code == "CUST001"
    assert fact.customer_id is not None


def test_a_customer_is_checked_one_level_up_when_no_sub_territory_is_stated(
        seeded_engine) -> None:
    """The row named only a territory, so that is where the comparison is made."""
    _customer_master(seeded_engine, CUST001="STR001")

    result = load(seeded_engine, [target_row(**{"Customer Code": "CUST001"})])

    assert result.rejected_rows == 0, result.error_counts
    assert only_target(seeded_engine).customer_code == "CUST001"


def test_an_unknown_customer_is_tolerated_until_the_master_has_a_source(
        seeded_engine) -> None:
    """dim_customer is PENDING_SOURCE_DATA: the code is kept, not rejected."""
    result = load(seeded_engine, [target_row(**{"Customer Code": "CUST-404"})])

    assert result.rejected_rows == 0, result.error_counts
    fact = only_target(seeded_engine)
    assert fact.customer_code == "CUST-404"
    assert fact.customer_id is None


def test_a_sales_force_assigned_elsewhere_is_rejected(seeded_engine) -> None:
    with Session(seeded_engine) as session:
        from app.database.models import DimTerritory

        session.add(DimTerritory(territory_code="TR009", territory_name="Elsewhere",
                                 unit_code="UN001"))
        session.add(DimSalesForce(sales_force_code="SF001",
                                  sales_force_name="Md. Anis",
                                  territory_code="TR009"))
        session.commit()

    result = load(seeded_engine, [target_row(**{"Sales Force Code": "SF001"})])

    assert error_codes(seeded_engine, result.batch_id) == [
        "SALES_FORCE_TERRITORY_MISMATCH"
    ]


def test_a_sales_force_with_no_recorded_territory_is_not_a_rejection(
        seeded_engine) -> None:
    """A blank assignment is a gap in the master, not a bad target."""
    with Session(seeded_engine) as session:
        session.add(DimSalesForce(sales_force_code="SF002",
                                  sales_force_name="Md. Rakib"))
        session.commit()

    result = load(seeded_engine, [target_row(**{"Sales Force Code": "SF002"})])

    assert result.rejected_rows == 0, result.error_counts
    assert only_target(seeded_engine).sales_force_code == "SF002"


# ---------------------------------------------------------------------------
# The grain
# ---------------------------------------------------------------------------


def test_two_materials_in_one_month_and_territory_are_two_targets(seeded_engine) -> None:
    result = load(seeded_engine, [
        target_row(**{"SKU Code": "SKU001", "Target Amount": 100}),
        target_row(**{"SKU Code": "SKU002", "Target Amount": 200}),
    ])

    assert result.rejected_rows == 0, result.error_counts
    assert target_count(seeded_engine) == 2


def test_the_same_combination_twice_in_one_file_is_a_duplicate(seeded_engine) -> None:
    result = load(seeded_engine, [
        target_row(**{"Target Amount": 100}),
        target_row(**{"Target Amount": 200}),
    ])

    assert error_codes(seeded_engine, result.batch_id) == ["DUPLICATE_IN_FILE"]
    assert target_count(seeded_engine) == 1


def test_the_volume_target_is_stored_unit_free(seeded_engine) -> None:
    """The figure the file stated, and no unit beside it.

    This test used to set a pack unit on the SKU master and assert the target
    view reported it as ``target_volume_unit``. Revision 0022 removed that
    master; the Material Master states no unit of measure, so asserting one
    would mean inventing the measurement the source never made.
    """
    result = load(seeded_engine, [target_row(**{"Target Volume": 42})])
    assert result.rejected_rows == 0, result.error_counts

    with Session(seeded_engine) as session:
        view = _detail_view(session)
        assert "target_volume_unit" not in view.c
        assert float(session.execute(select(view.c.target_volume)).scalar_one()) == 42


def _detail_view(session):
    from app.ai import queries as q

    return q.view(session, q.TARGET_VIEW)


def _first_message(engine, batch_id: int) -> str:
    with Session(engine) as session:
        return session.execute(
            select(EtlRejectedRecord.error_message)
            .where(EtlRejectedRecord.batch_id == batch_id)
        ).scalars().first() or ""
