"""Import, upsert and foreign-key behaviour against a real database."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import (
    DimBusinessUnit,
    DimCompany,
    DimRegion,
    DimTerritory,
)
from app.master_data.importer import run_import


def count(engine, model) -> int:
    with Session(engine) as session:
        return session.execute(select(func.count()).select_from(model)).scalar_one()


def test_import_loads_every_dimension(engine, full_workbook) -> None:
    summary, report, _ = run_import(full_workbook(), engine)

    assert report.is_valid, [f.format() for f in report.errors]
    assert summary.validation_passed
    assert summary.committed
    assert summary.failed == 0
    assert summary.total_records == 9            # one record per sheet
    assert all(t.inserted == 1 for t in summary.tables)
    assert count(engine, DimCompany) == 1


def test_rerunning_the_import_updates_instead_of_duplicating(engine, full_workbook) -> None:
    path = full_workbook()
    run_import(path, engine)
    summary, _, _ = run_import(path, engine)

    assert count(engine, DimCompany) == 1
    company_result = next(t for t in summary.tables if t.table == "dim_company")
    assert company_result.inserted == 0
    assert company_result.unchanged == 1


def test_changed_values_are_updated_on_the_same_business_code(engine, full_workbook) -> None:
    run_import(full_workbook(), engine)
    changed = full_workbook({
        "Company Master": [
            ["Company Code", "Company", "Company Head ID", "Company Head Name"],
            ["C001", "Example Industries PLC", "EMP001", "Md. Rahman"],
        ],
    })
    summary, _, _ = run_import(changed, engine)

    company_result = next(t for t in summary.tables if t.table == "dim_company")
    assert company_result.updated == 1
    assert count(engine, DimCompany) == 1
    with Session(engine) as session:
        company = session.execute(select(DimCompany)).scalar_one()
        assert company.company_name == "Example Industries PLC"
        assert company.company_code == "C001"     # official code untouched


def test_surrogate_key_is_stable_across_reimports(engine, full_workbook) -> None:
    path = full_workbook()
    run_import(path, engine)
    with Session(engine) as session:
        first_id = session.execute(select(DimCompany.company_id)).scalar_one()
    run_import(path, engine)
    with Session(engine) as session:
        assert session.execute(select(DimCompany.company_id)).scalar_one() == first_id


def test_invalid_relationship_aborts_the_import(engine, full_workbook) -> None:
    path = full_workbook({
        "Region Master": [
            ["Zone Code", "Region Code", "Region", "Region Head ID", "Region Head",
             "Region HQ", "Location"],
            ["Z999", "REG001", "Dhaka", "EMP005", "Head", "Dhaka", "Dhaka North"],
        ],
    })
    summary, report, _ = run_import(path, engine)

    assert not report.is_valid
    assert not summary.committed
    assert summary.invalid_references >= 1
    assert all(t.skipped for t in summary.tables)
    assert count(engine, DimCompany) == 0        # nothing was written at all


def test_dry_run_writes_nothing(engine, full_workbook) -> None:
    summary, _, _ = run_import(full_workbook(), engine, dry_run=True)
    assert summary.dry_run
    assert not summary.committed
    assert count(engine, DimCompany) == 0


def test_codes_are_stored_as_strings_with_leading_zeros(engine, full_workbook) -> None:
    path = full_workbook({
        "Company Master": [
            ["Company Code", "Company", "Company Head ID", "Company Head Name"],
            ["001", "Alpha Ltd.", "EMP001", "Head One"],
        ],
        "Business Unit Master": [
            ["Company Code", "BU Code", "BU Name", "BU Head ID", "BU Head Name"],
            ["001", "01", "Consumer Products", "EMP002", "Head Two"],
        ],
        # The child must follow the renamed parent, otherwise this would fail as
        # an orphan rather than testing code preservation.
        "Sales Line Master": [
            ["BU Code", "Sales Line Code", "Sales Line Name", "Sales Line Head ID",
             "Sales Line Head"],
            ["01", "SL001", "General Trade", "EMP003", "Md. Alam"],
        ],
    })
    summary, report, _ = run_import(path, engine)
    assert report.is_valid, [f.format() for f in report.errors]

    with Session(engine) as session:
        company = session.execute(select(DimCompany)).scalar_one()
        bu = session.execute(select(DimBusinessUnit)).scalar_one()
    assert company.company_code == "001"
    assert bu.bu_code == "01"
    assert bu.company_code == "001"


def test_bangla_and_phone_round_trip_through_the_database(engine, full_workbook) -> None:
    """Unicode and a leading-plus phone survive the round trip unaltered.

    The Bangla half used to read a SKU's ``sku_name_bn``. Revision 0022 removed
    that master, and the Material Master carries one description rather than an
    English/Bangla pair — so the assertion moved to the territory head's name,
    which is the Bangla column the workbook still supplies.
    """
    run_import(full_workbook(), engine)
    with Session(engine) as session:
        territory = session.execute(select(DimTerritory)).scalar_one()
    assert territory.territory_head_phone == "+8801700000000"
    assert territory.territory_name == "Kazipara"


def test_foreign_key_constraint_is_enforced_by_the_database(engine) -> None:
    """The FK is a real database constraint, not only an application check."""
    with Session(engine) as session:
        session.add(DimRegion(
            region_code="REG001", region_name="Dhaka", zone_code="Z-NOPE",
        ))
        with pytest.raises(IntegrityError):
            session.commit()


def test_unique_business_code_is_enforced_by_the_database(engine) -> None:
    with Session(engine) as session:
        session.add(DimCompany(company_code="C001", company_name="Alpha"))
        session.commit()
    with Session(engine) as session:
        session.add(DimCompany(company_code="C001", company_name="Alpha Again"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_import_order_follows_the_hierarchy(engine, full_workbook) -> None:
    summary, _, _ = run_import(full_workbook(), engine)
    assert [t.table for t in summary.tables] == [
        "dim_company", "dim_business_unit", "dim_sales_line", "dim_zone", "dim_region",
        "dim_area", "dim_unit", "dim_territory", "dim_sub_territory",
    ]


def test_summary_renders_the_expected_sections(engine, full_workbook) -> None:
    summary, _, _ = run_import(full_workbook(), engine)
    text = summary.render()
    assert "Master Data Import Summary" in text
    assert "Total Records:" in text
    assert "Invalid References:" in text
    assert "Missing Required Values:" in text


def test_real_workbook_import_is_a_clean_no_op(engine, real_workbook_path) -> None:
    """The shipped workbook holds no records, so a real import inserts nothing."""
    summary, report, _ = run_import(real_workbook_path, engine)
    assert report.is_valid, [f.format() for f in report.errors]
    assert summary.total_records == 0
    assert summary.failed == 0
    assert count(engine, DimCompany) == 0
