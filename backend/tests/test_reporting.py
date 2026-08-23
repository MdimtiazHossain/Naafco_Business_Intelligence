"""Reporting views and report services over loaded facts."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.etl.pipeline import run_import
from app.etl.readers import RecordsSourceReader
from app.reporting.service import (
    ReportFilters,
    sales_report,
    stock_report,
    target_report,
)
from conftest_phase2 import (
    sales_row,
    material_stock_row,
    target_row,
)


def load(engine, data_type, records, **kwargs):
    reader = RecordsSourceReader(records, source_name="t.csv", source_type="CSV")
    result = run_import(engine, data_type, reader, source_system="TEST", **kwargs)
    assert result.rejected_rows == 0, result.error_counts
    return result


@pytest.fixture
def loaded_engine(seeded_engine):
    """A warehouse with a small but complete set of facts across all three types."""
    load(seeded_engine, "sales", [
        sales_row(**{"Invoice No": "INV-1", "Date": "2026-08-10",
                     "Quantity": 10, "Gross Sales": 1200, "Discount": 200, "Cost": 700}),
        sales_row(**{"Invoice No": "INV-2", "Date": "2026-08-15",
                     "Quantity": 20, "Gross Sales": 2400, "Discount": 400, "Cost": 1400}),
        sales_row(**{"Invoice No": "INV-3", "Date": "2026-07-15",
                     "Quantity": 5, "Gross Sales": 600, "Discount": 0, "Cost": 350}),
    ])
    # The second position states the group and brand the Material Master
    # records for MAT-002; a row that disagreed would be rejected, and `load`
    # asserts nothing was.
    load(seeded_engine, "material_stock", [
        material_stock_row(),
        material_stock_row(**{"Storage Location": "SL02", "Material": "MAT-002",
                              "Material Group": "MG02", "Material Brand": "MB02",
                              "Unrestricted": 400, "Quality Inspection": 0,
                              "Blocked": 0, "In Transit": 0,
                              "Shelf Life Expiration Date": "2026-06-30"}),
    ])
    load(seeded_engine, "target", [target_row(**{"Target Amount": 10000})])
    return seeded_engine


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------


VIEW_NAMES = [
    "vw_daily_sales", "vw_monthly_sales", "vw_region_sales",
    "vw_sales_detail", "vw_material_stock_detail",
    "vw_target_detail", "vw_target_vs_actual", "vw_region_target_achievement",
]


def test_the_retired_views_are_gone(loaded_engine) -> None:
    """Revision 0020 dropped six views with the modules that fed them, and 0022 a
    seventh with the master that fed it.

    Named individually rather than left to the list above, so a view resurrected
    by a migration that copied an older body fails here instead of quietly
    reappearing in the reporting surface.
    """
    with loaded_engine.connect() as connection:
        names = {
            row[0] for row in connection.execute(text(
                "SELECT name FROM sqlite_master WHERE type = 'view'"
            ))
        }
    assert names.isdisjoint({
        "vw_collection_detail", "vw_daily_collection", "vw_monthly_collection",
        "vw_outstanding_detail", "vw_customer_outstanding", "vw_outstanding_aging",
        # Dropped without a material-wise replacement: nothing read it, and an
        # unread view is one more hand-written thing to keep in step.
        "vw_product_sales",
    })


@pytest.mark.parametrize("view", VIEW_NAMES)
def test_every_view_is_queryable(loaded_engine, view: str) -> None:
    with loaded_engine.connect() as connection:
        connection.execute(text(f"SELECT * FROM {view}")).fetchall()


def test_daily_sales_view_aggregates_and_computes_margin(loaded_engine) -> None:
    with loaded_engine.connect() as connection:
        row = connection.execute(text(
            "SELECT net_sales, gross_profit, gross_margin_percent, average_selling_price, "
            "quantity FROM vw_daily_sales WHERE full_date = '2026-08-15'"
        )).one()
    net_sales, gross_profit, margin, asp, quantity = row
    assert float(net_sales) == 2000.0            # 2400 - 400
    assert float(gross_profit) == 600.0          # 2000 - 1400
    assert float(margin) == pytest.approx(30.0)
    assert float(asp) == pytest.approx(100.0)    # 2000 / 20
    assert float(quantity) == 20.0


def test_monthly_sales_view_rolls_up_by_financial_month(loaded_engine) -> None:
    with loaded_engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT financial_year, month, net_sales FROM vw_monthly_sales "
            "ORDER BY month"
        )).fetchall()
    by_month = {row[1]: float(row[2]) for row in rows}
    assert by_month[7] == 600.0        # July: 600 - 0
    assert by_month[8] == 3000.0       # August: 1000 + 2000
    # July 2026 and August 2026 are both in FY 2026-27 (July start).
    assert {row[0] for row in rows} == {"FY 2026-27"}


def test_material_stock_view_names_the_location_and_totals_the_categories(
        loaded_engine) -> None:
    """``total_stock`` is defined once, in the view, as the sum of all four.

    In transit is included deliberately: it is stock the business owns and has
    paid for. Keeping it out of the total would make the headline figure
    disagree with the balance sheet.
    """
    with loaded_engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT plant_name, storage_location_name, material_group_name, "
            "unrestricted_stock, total_stock "
            "FROM vw_material_stock_detail ORDER BY storage_location_code"
        )).fetchall()
    assert len(rows) == 2
    # The dimension's names come through the join, not off the fact row.
    assert rows[0][0] == "Dhaka Plant"
    assert rows[0][1] == "Finished Goods"
    assert rows[0][2] == "Tea"
    assert float(rows[0][3]) == 100.0
    assert float(rows[0][4]) == 135.0       # 100 + 20 + 5 + 10
    assert float(rows[1][4]) == 400.0       # the other location, one category


def test_target_vs_actual_view_computes_achievement_and_gap(loaded_engine) -> None:
    with loaded_engine.connect() as connection:
        row = connection.execute(text(
            "SELECT target_amount, actual_sales, achievement_percent, gap "
            "FROM vw_target_vs_actual WHERE month = 8"
        )).one()
    target, actual, achievement, gap = row
    assert float(target) == 10000.0
    assert float(actual) == 3000.0
    assert float(achievement) == pytest.approx(30.0)
    assert float(gap) == 7000.0


def test_target_view_returns_null_achievement_when_target_is_zero(seeded_engine) -> None:
    load(seeded_engine, "sales", [sales_row()])
    load(seeded_engine, "target", [target_row(**{"Target Amount": 0})])
    with seeded_engine.connect() as connection:
        row = connection.execute(text(
            "SELECT achievement_percent FROM vw_target_vs_actual WHERE month = 8"
        )).one()
    assert row[0] is None      # divide-by-zero yields NULL, not an error


# --------------------------------------------------------------------------
# Report services
# --------------------------------------------------------------------------


def test_sales_report_metrics(loaded_engine) -> None:
    with Session(loaded_engine) as session:
        report = sales_report(session, ReportFilters(date_to=dt.date(2026, 8, 15)))

    metrics = report["metrics"]
    assert metrics["total"]["net_sales"] == 3600.0        # every date up to 15 Aug
    assert metrics["daily"] == 2000.0
    assert metrics["mtd"] == 3000.0
    assert metrics["previous_month"] == 600.0
    assert metrics["ytd"] == 3600.0                       # FY starts 1 July
    assert metrics["financial_year"] == "FY 2026-27"
    assert metrics["growth_percent"] == pytest.approx(400.0)
    assert metrics["achievement"]["achievement_percent"] == pytest.approx(30.0)
    assert metrics["achievement"]["gap"] == 7000.0
    assert report["rows"]


def test_sales_report_filters_by_region(loaded_engine) -> None:
    with Session(loaded_engine) as session:
        matching = sales_report(session, ReportFilters(region_code="REG001"))
        other = sales_report(session, ReportFilters(region_code="REG002"))
    assert matching["metrics"]["total"]["net_sales"] == 3600.0
    assert not other["rows"]


def test_stock_report_totals_each_category_separately(loaded_engine) -> None:
    """The four categories stay four numbers: only unrestricted stock is sellable."""
    with Session(loaded_engine) as session:
        report = stock_report(session, ReportFilters())
    metrics = report["metrics"]
    assert metrics["unrestricted_stock"] == 500.0       # 100 + 400
    assert metrics["quality_inspection_stock"] == 20.0
    assert metrics["blocked_stock"] == 5.0
    assert metrics["stock_in_transit"] == 10.0
    assert metrics["total_stock"] == 535.0


def test_target_report_spans_every_period_by_default(loaded_engine) -> None:
    """July has sales but no target, so the unfiltered report includes both months."""
    with Session(loaded_engine) as session:
        report = target_report(session, ReportFilters())
    assert report["metrics"]["target"] == 10000.0
    assert report["metrics"]["actual"] == 3600.0        # July 600 + August 3000
    assert report["metrics"]["achievement_percent"] == pytest.approx(36.0)


def test_target_report_can_be_restricted_to_a_period(loaded_engine) -> None:
    with Session(loaded_engine) as session:
        report = target_report(session, ReportFilters(
            date_from=dt.date(2026, 8, 1), date_to=dt.date(2026, 8, 31)
        ))
    assert report["metrics"]["target"] == 10000.0
    assert report["metrics"]["actual"] == 3000.0
    assert report["metrics"]["achievement_percent"] == pytest.approx(30.0)
    assert report["metrics"]["gap"] == 7000.0


def test_reports_are_empty_not_broken_without_data(seeded_engine) -> None:
    """A report over an empty warehouse returns zeros and NULLs, never an error."""
    with Session(seeded_engine) as session:
        report = sales_report(session, ReportFilters())
    assert report["rows"] == []
    assert report["metrics"]["average_selling_price"] is None
    assert report["metrics"]["achievement"]["achievement_percent"] is None


def test_demo_data_can_be_separated_by_source_system(seeded_engine) -> None:
    reader = RecordsSourceReader([sales_row()], source_name="demo.csv")
    run_import(seeded_engine, "sales", reader, source_system="DEMO")
    reader = RecordsSourceReader([sales_row(**{"Invoice No": "INV-P"})],
                                 source_name="prod.csv")
    run_import(seeded_engine, "sales", reader, source_system="SAP")

    with Session(seeded_engine) as session:
        demo = sales_report(session, ReportFilters(source_system="DEMO"))
        production = sales_report(session, ReportFilters(source_system="SAP"))
    assert demo["metrics"]["total"]["net_sales"] == 1000.0
    assert production["metrics"]["total"]["net_sales"] == 1000.0
    assert len(demo["rows"]) == 1 and len(production["rows"]) == 1
