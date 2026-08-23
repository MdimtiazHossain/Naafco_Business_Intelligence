"""Phase 2 test fixtures: a migrated database seeded with master data.

The master records here are **test doubles**, created through the ORM inside a
throwaway SQLite database. They never touch the real workbook, the real database
or the demo generator — which still refuses to invent master codes.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Iterator

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session

from app.database.models import (
    DimArea,
    DimBusinessUnit,
    DimCompany,
    DimMaterial,
    DimPlant,
    DimRegion,
    DimSalesLine,
    DimStorageLocation,
    DimSubTerritory,
    DimTerritory,
    DimUnit,
    DimZone,
    plant_key,
    storage_location_key,
)
from app.etl.calendar import populate_dim_date
from app.etl.mapping import ensure_master_source_status

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _alembic_config(url: str):
    from alembic.config import Config

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location",
                           str(BACKEND_DIR / "app" / "database" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def warehouse_engine(tmp_path: Path, monkeypatch) -> Iterator[Engine]:
    """A fully migrated database, including the reporting views."""
    from alembic import command

    url = f"sqlite:///{tmp_path / 'warehouse.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(_alembic_config(url), "head")

    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    try:
        yield engine
    finally:
        engine.dispose()


#: One complete, internally consistent organisational branch, plus a second
#: region under the same zone so hierarchy mismatches can be provoked.
MASTER_SEED: dict[str, Any] = {
    "company": ("C001", "Example Industries Ltd."),
    "bu": ("BU001", "Consumer Products"),
    "sales_line": ("SL001", "General Trade"),
    "zone": ("Z001", "Dhaka Zone"),
    "region": ("REG001", "Dhaka"),
    "region_2": ("REG002", "Khulna"),
    "area": ("AR001", "Mirpur"),
    "unit": ("UN001", "Mirpur Unit 1"),
    "territory": ("TR001", "Kazipara"),
    "sub_territory": ("STR001", "Kazipara North"),
    "materials": (("SKU001", "Premium Tea 500g"), ("SKU002", "Premium Tea 1kg")),
}


@pytest.fixture
def seeded_engine(warehouse_engine: Engine) -> Engine:
    """Warehouse database seeded with master data and a date dimension."""
    with Session(bind=warehouse_engine, future=True) as session:
        session.add(DimCompany(company_code="C001", company_name="Example Industries Ltd."))
        session.add(DimBusinessUnit(bu_code="BU001", bu_name="Consumer Products",
                                    company_code="C001"))
        session.add(DimSalesLine(sales_line_code="SL001", sales_line_name="General Trade",
                                 bu_code="BU001"))
        session.add(DimZone(zone_code="Z001", zone_name="Dhaka Zone",
                            sales_line_code="SL001"))
        session.add(DimRegion(region_code="REG001", region_name="Dhaka", zone_code="Z001"))
        session.add(DimRegion(region_code="REG002", region_name="Khulna", zone_code="Z001"))
        session.add(DimArea(area_code="AR001", area_name="Mirpur", region_code="REG001"))
        session.add(DimArea(area_code="AR002", area_name="Khulna Sadar",
                            region_code="REG002"))
        session.add(DimUnit(unit_code="UN001", unit_name="Mirpur Unit 1",
                            area_code="AR001"))
        session.add(DimTerritory(territory_code="TR001", territory_name="Kazipara",
                                 unit_code="UN001"))
        session.add(DimSubTerritory(sub_territory_code="STR001",
                                    sub_territory_name="Kazipara North",
                                    territory_code="TR001"))
        # One plant, two storage locations under it, and the materials every
        # dataset resolves against. Since revision 0022 there is no second item
        # master: a sale, a target and a stock position all name a Material Code
        # and all reach the rows below.
        #
        # Three material groups' worth of shape, so a breakdown has something to
        # group by at every level — two groups, two brands, and two materials
        # inside one group, which is the case a group-level breakdown cannot
        # distinguish and a material-level one can.
        #
        # ``SKU001`` and ``SKU002`` keep their names because the sales and target
        # fixtures have always used them and hundreds of assertions read them.
        # They are ordinary material codes now; the letters in them mean nothing
        # to the schema, exactly as ``MAT-001`` never did.
        session.add(DimPlant(plant_key=plant_key("C001", "PL01"),
                             company_code="C001", plant_code="PL01",
                             plant_name="Dhaka Plant"))
        for sloc, sloc_name in (("SL01", "Finished Goods"), ("SL02", "Cold Store")):
            session.add(DimStorageLocation(
                storage_location_key=storage_location_key("PL01", sloc),
                plant_code="PL01", storage_location_code=sloc,
                storage_location_name=sloc_name))
        for material, description, group, group_name, brand_code, brand in (
            ("SKU001", "Premium Tea 500g", "MG01", "Tea", "MB01", "Example Brand"),
            ("SKU002", "Premium Tea 1kg", "MG01", "Tea", "MB01", "Example Brand"),
            ("MAT-001", "Premium Tea 500g Bulk", "MG01", "Tea", "MB01", "Example Brand"),
            ("MAT-003", "Premium Tea 1kg Bulk", "MG01", "Tea", "MB01", "Example Brand"),
            ("MAT-002", "Full Cream Milk 1L", "MG02", "Dairy", "MB02", "Dairy Brand"),
        ):
            session.add(DimMaterial(
                material_code=material, material_description=description,
                material_group_code=group, material_group_name=group_name,
                material_brand_code=brand_code, material_brand=brand))
        ensure_master_source_status(session)
        populate_dim_date(session, dt.date(2026, 1, 1), dt.date(2027, 12, 31))
        session.commit()
    return warehouse_engine


def make_material(code: str, description: str, *, group: str = "MG01",
                  group_name: str = "Tea", brand_code: str = "MB01",
                  brand: str = "Example Brand") -> DimMaterial:
    """A Material Master row with its four required classification columns filled.

    A test almost always cares about the code and the description; the group and
    brand are ``NOT NULL`` and have to be *something*, so they default here
    rather than being spelled out at every call site. Pass them when the test is
    about a breakdown that has to tell two groups or two brands apart.
    """
    return DimMaterial(
        material_code=code, material_description=description,
        material_group_code=group, material_group_name=group_name,
        material_brand_code=brand_code, material_brand=brand,
    )


def sales_row(**overrides: Any) -> dict[str, Any]:
    """A valid sales record; override any field to provoke a specific failure.

    ``Net Sales`` is required by the dataset spec, and is *derived* here from
    whatever gross and discount the caller supplied rather than being a fixed
    default. Both parts matter: a fixed value would contradict the figures a
    test overrode, and omitting it would make every sales fixture fail the
    required-column check. A caller that wants net to disagree with the
    breakdown can still pass it explicitly.
    """
    row = {
        "Date": "2026-08-15",
        "Invoice No": "INV-0001",
        # The dataset spec still accepts the "SKU Code" heading — a sales export
        # that has always been headed that way keeps loading — and resolves it
        # against the Material Master.
        "SKU Code": "SKU001",
        "Territory Code": "TR001",
        "Customer Code": "CUST-001",
        "Sales Force Code": "SF-001",
        "Quantity": 10,
        "Gross Sales": 1200,
        "Discount": 200,
        "Cost": 700,
        "Source Transaction Id": "SRC-1",
    }
    row.update(overrides)
    if "Net Sales" not in row:
        gross = _as_number(row.get("Gross Sales"))
        discount = _as_number(row.get("Discount"))
        if gross is not None:
            row["Net Sales"] = gross - (discount or 0)
    return row


def _as_number(value: Any) -> float | None:
    """A test may set a field to a deliberately unparseable value."""
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def material_stock_row(**overrides: Any) -> dict[str, Any]:
    """A valid material stock position.

    There is no date column. A stock position is *current* — the
    source states no posting date — and it is held per plant, storage location
    and material, each resolved against its own master. Material Group and
    Material Brand are stated too, and are checked against what the Material
    Master records for the material rather than used to identify it. The two
    dates on the row are attributes of the goods, not of the reporting period.
    """
    row = {
        "Company": "C001",
        "Plant": "PL01",
        "Storage Location": "SL01",
        "Material": "MAT-001",
        "Material Group": "MG01",
        "Material Brand": "MB01",
        "Unrestricted": 100,
        "Quality Inspection": 20,
        "Blocked": 5,
        "In Transit": 10,
        "Production Date": "2026-01-15",
        "Shelf Life Expiration Date": "2027-01-15",
    }
    row.update(overrides)
    return row


def target_row(**overrides: Any) -> dict[str, Any]:
    """A valid target row: a month, a financial year, a territory and a material.

    There is no date column. A target is set for a month of a financial year and
    the file says so; the date it is anchored to is derived. With the default
    July start, August 2026 belongs to FY 2026-27 — a row overriding one half of
    that pair and not the other is asking to be rejected, which is what several
    tests do on purpose.
    """
    row = {
        "Target Month": "2026-08",
        "Financial Year": "FY 2026-27",
        "Territory Code": "TR001",
        "SKU Code": "SKU001",
        "Target Amount": 5000000,
    }
    row.update(overrides)
    return row
