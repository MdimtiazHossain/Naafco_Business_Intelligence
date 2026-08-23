"""Generate synthetic transaction files for testing the Phase 2 pipeline.

Usage::

    python scripts/generate_demo_transactions.py
    python scripts/generate_demo_transactions.py --days 60 --rows-per-day 200
    python scripts/generate_demo_transactions.py --format csv --out data/transactions/demo

**No master data is ever invented.** Every organisational code and SKU written
into a demo file is read from the Phase 1 dimension tables in the database. If
those tables are empty the script refuses to run and tells you to import the
master data first — generating plausible-looking codes would create exactly the
fake master data the project forbids.

Every row is tagged ``source_system = DEMO`` (a column in the file and the
``--source-system DEMO`` flag on import), so demo data can always be excluded
from production reporting with a single filter.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  (side effect: sys.path)

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database.connection import get_engine
from app.database.models import (
    DimMaterial,
    DimPlant,
    DimStorageLocation,
    DimSubTerritory,
    DimTerritory,
)
from app.etl.calendar import FinancialYearConfig
from app.etl.mapping import LEVEL_BINDINGS, MasterDataIndex

SEPARATOR = "=" * 78
DEMO_SOURCE_SYSTEM = "DEMO"
#: Demo customers and sales force are transaction-side attributes, not master
#: data: no dim_customer or dim_sales_force row is created for them. They stay
#: as codes on the fact rows until a real master file arrives.
DEMO_CUSTOMERS = [f"DEMO-CUST-{i:03d}" for i in range(1, 21)]
DEMO_SALES_FORCE = [f"DEMO-SF-{i:03d}" for i in range(1, 11)]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Generate DEMO transaction files")
    parser.add_argument("--out", type=Path,
                        default=settings.transactions_dir / "demo",
                        help="Output directory.")
    parser.add_argument("--format", choices=["xlsx", "csv"], default="xlsx")
    parser.add_argument("--days", type=int, default=30, help="Number of days to cover.")
    parser.add_argument("--end-date", type=dt.date.fromisoformat, default=dt.date.today())
    parser.add_argument("--rows-per-day", type=int, default=40,
                        help="Sales rows per day.")
    parser.add_argument("--seed", type=int, default=20260810,
                        help="Random seed; the same seed reproduces the same files.")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--data-types", nargs="*",
                        default=["sales", "material_stock", "target"])
    return parser.parse_args(argv)


class MasterSample:
    """Real master codes read from the database, ready to be sampled."""

    def __init__(self, session: Session) -> None:
        self.index = MasterDataIndex(session)
        # The deepest populated level gives the richest hierarchy for demo rows.
        self.sub_territories = list(session.execute(
            select(DimSubTerritory.sub_territory_code)
        ).scalars())
        self.territories = list(session.execute(
            select(DimTerritory.territory_code)
        ).scalars())
        self.skus = list(session.execute(select(DimMaterial.material_code)).scalars())
        # The material side is read, never invented: an unknown plant, storage
        # location or material is rejected by the import rather than deferred
        # like a customer code. Every storage location is paired with every
        # material, which is what a plant's stock file looks like; the codes
        # themselves all come from the masters.
        plants = list(session.execute(
            select(DimPlant.company_code, DimPlant.plant_code)).all())
        storage_locations = list(session.execute(
            select(DimStorageLocation.plant_code,
                   DimStorageLocation.storage_location_code)).all())
        materials = list(session.execute(
            select(DimMaterial.material_code, DimMaterial.material_group_code,
                   DimMaterial.material_brand_code)).all())
        self.material_placements = [
            (company, plant, storage_location, material, group, brand)
            for company, plant in plants
            for sl_plant, storage_location in storage_locations if sl_plant == plant
            for material, group, brand in materials
        ]
        self.levels = {
            binding.code_field: list(self.index.ids[binding.code_field])
            for binding in LEVEL_BINDINGS
        }

    @property
    def is_usable(self) -> bool:
        return bool(self.skus) and any(self.levels.values())

    def deepest_level(self) -> str | None:
        """The deepest organisational level that actually has records."""
        for binding in reversed(LEVEL_BINDINGS):
            if self.levels.get(binding.code_field):
                return binding.code_field
        return None

    def org_row(self, rng: random.Random) -> dict[str, str]:
        """One organisational code plus every ancestor, all genuinely related.

        Only real parent-child chains are emitted, so a demo file never trips
        the hierarchy validation by accident.
        """
        level = self.deepest_level()
        if level is None:
            return {}
        code = rng.choice(self.levels[level])
        return dict(self.index.ancestors_of(level, code))


def _date_range(end: dt.date, days: int) -> list[dt.date]:
    return [end - dt.timedelta(days=offset) for offset in range(days - 1, -1, -1)]


def generate_sales(sample: MasterSample, rng: random.Random,
                   dates: list[dt.date], rows_per_day: int) -> list[dict]:
    rows = []
    invoice_seq = 1
    for day in dates:
        for _ in range(rows_per_day):
            org = sample.org_row(rng)
            quantity = rng.randint(1, 120)
            unit_price = rng.choice([45, 80, 120, 250, 480, 950])
            gross = quantity * unit_price
            discount = round(gross * rng.choice([0, 0, 0.02, 0.05, 0.08]), 2)
            cost = round((gross - discount) * rng.uniform(0.62, 0.85), 2)
            rows.append({
                "Date": day.isoformat(),
                "Invoice No": f"DEMO-INV-{day:%Y%m%d}-{invoice_seq:05d}",
                "SKU Code": rng.choice(sample.skus),
                **{_header(k): v for k, v in org.items()},
                "Customer Code": rng.choice(DEMO_CUSTOMERS),
                "Sales Force Code": rng.choice(DEMO_SALES_FORCE),
                "Quantity": quantity,
                "Gross Sales": gross,
                "Discount": discount,
                # Net is the required measure, so a generated file has to carry
                # it — a demo file the importer would reject is not a demo file.
                "Net Sales": round(gross - discount, 2),
                "Cost": cost,
                "Source Transaction Id": f"DEMO-SL-{invoice_seq:07d}",
                "Source System": DEMO_SOURCE_SYSTEM,
            })
            invoice_seq += 1
    return rows


def generate_material_stock(sample: MasterSample, rng: random.Random,
                            dates: list[dt.date], rows_per_day: int) -> list[dict]:
    """One current position per material placement — no dates, no running balance.

    Material stock is a snapshot, so there is nothing per-day to generate: the
    same placement appears once, whatever period was asked for.

    Unlike customers and sales force, the material side is **not** a
    deferred mapping — an unknown plant, storage location or material is
    rejected outright — so these rows are built only from codes already in the
    three masters, and the group and brand are the ones the Material Master
    records for the material, since a row that disagrees with it is rejected.
    With any of those masters empty this produces nothing at all, which is the
    honest outcome; inventing a plant or a material code would put demo stock
    somewhere that does not exist.
    """
    rows = []
    seq = 0
    for placement in sample.material_placements:
        (company, plant, storage_location, material,
         material_group, material_brand) = placement
        seq += 1
        # A spread of shelf lives so every expiry bucket has something in it.
        expiry = rng.choice([
            dates[-1] - dt.timedelta(days=rng.randint(10, 120)),   # expired
            dates[-1] + dt.timedelta(days=rng.randint(5, 80)),     # expiring soon
            dates[-1] + dt.timedelta(days=rng.randint(200, 900)),  # valid
            None,                                                  # no shelf life
        ])
        produced = (expiry - dt.timedelta(days=rng.randint(180, 540))
                    if expiry else dates[0] - dt.timedelta(days=rng.randint(30, 300)))
        rows.append({
            "Company": company,
            "Plant": plant,
            "Storage Location": storage_location,
            "Material": material,
            "Material Group": material_group,
            "Material Brand": material_brand,
            "Unrestricted": float(rng.randint(200, 5_000)),
            "Quality Inspection": float(rng.choice([0, 0, 0, 50, 120])),
            "Blocked": float(rng.choice([0, 0, 0, 25])),
            "In Transit": float(rng.choice([0, 0, 80, 300])),
            "Production Date": produced.isoformat(),
            "Shelf Life Expiration Date": expiry.isoformat() if expiry else None,
            "Source Transaction Id": f"DEMO-ST-{seq:07d}",
            "Source System": DEMO_SOURCE_SYSTEM,
        })
    return rows


def generate_target(sample: MasterSample, rng: random.Random,
                    dates: list[dt.date], rows_per_day: int) -> list[dict]:
    """One monthly target per territory and SKU covered by the demo period.

    A target file states a territory and a SKU and nothing above them, so the
    organisational chain the other generators emit is deliberately not used
    here: repeating the region on a target row is what the fixed structure
    exists to prevent, and the importer would report those columns as unmapped.

    Combinations are drawn without replacement inside each month because the
    business key is month + territory + sub-territory + customer + SKU + sales
    force — a repeated pair would be one target uploaded twice, which the
    importer rightly rejects as a duplicate.
    """
    config = FinancialYearConfig.from_settings()
    territories = sample.levels.get("territory_code") or []
    if not territories or not sample.skus:
        return []

    rows = []
    months = sorted({(d.year, d.month) for d in dates})
    seq = 1
    for year, month in months:
        anchor = dt.date(year, month, 1)
        wanted = max(1, rows_per_day // 8)
        combinations = [(t, s) for t in territories for s in sample.skus]
        rng.shuffle(combinations)
        for territory, sku in combinations[:wanted]:
            rows.append({
                "Target Month": f"{year}-{month:02d}",
                "Financial Year": config.label(anchor),
                "Territory Code": territory,
                "SKU Code": sku,
                "Sales Force Code": rng.choice(DEMO_SALES_FORCE),
                "Target Quantity": rng.randint(50, 900),
                "Target Amount": round(rng.uniform(500_000, 8_000_000), 2),
                "Source Transaction Id": f"DEMO-TG-{seq:07d}",
                "Source System": DEMO_SOURCE_SYSTEM,
            })
            seq += 1
    return rows


GENERATORS = {
    "sales": generate_sales,
    "material_stock": generate_material_stock,
    "target": generate_target,
}


def _header(code_field: str) -> str:
    """``region_code`` -> ``Region Code`` (a realistic export header)."""
    return code_field.replace("_", " ").title().replace("Bu ", "BU ")


def write_rows(rows: list[dict], path: Path, file_format: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    headers: list[str] = []
    for row in rows:
        for key in row:
            if key not in headers:
                headers.append(key)

    if file_format == "csv":
        import csv

        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)
        return path

    from openpyxl import Workbook

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Data"
    worksheet.append(headers)
    for row in rows:
        worksheet.append([row.get(header) for header in headers])
    workbook.save(path)
    return path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rng = random.Random(args.seed)

    print(SEPARATOR)
    print("GENERATE DEMO TRANSACTIONS")
    print(SEPARATOR)

    engine = get_engine(args.database_url)
    try:
        with Session(bind=engine, future=True) as session:
            sample = MasterSample(session)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: cannot reach the database: {exc}", file=sys.stderr)
        print("Run 'cd backend && alembic upgrade head' and check DATABASE_URL.",
              file=sys.stderr)
        return 2

    if not sample.is_usable:
        print("ERROR: the Phase 1 master dimensions hold no records.", file=sys.stderr)
        print("", file=sys.stderr)
        print("Demo transactions must reference REAL master codes, and inventing "
              "codes is explicitly forbidden, so nothing can be generated yet.",
              file=sys.stderr)
        print("", file=sys.stderr)
        print("Fix: put a populated Master Data.xlsx in data/ and run", file=sys.stderr)
        print("    python scripts/import_master_data.py", file=sys.stderr)
        return 2

    counts = {level: len(codes) for level, codes in sample.levels.items() if codes}
    print("Master codes available: " +
          ", ".join(f"{k.replace('_code', '')}={v}" for k, v in counts.items()) +
          f", sku={len(sample.skus)}")
    print(f"Deepest level used:     {sample.deepest_level()}")
    print()

    dates = _date_range(args.end_date, args.days)
    written: list[tuple[str, Path, int]] = []
    for data_type in args.data_types:
        generator = GENERATORS.get(data_type)
        if generator is None:
            print(f"  ! unknown data type '{data_type}', skipped")
            continue
        rows = generator(sample, rng, dates, args.rows_per_day)
        path = args.out / f"demo_{data_type}.{args.format}"
        write_rows(rows, path, args.format)
        written.append((data_type, path, len(rows)))
        print(f"  {data_type:12} {len(rows):>7} rows -> {path}")

    print()
    print("Import them with:")
    for data_type, path, _ in written:
        print(f"  python scripts/import_transactions.py {data_type} \"{path}\" "
              f"--source-system {DEMO_SOURCE_SYSTEM}")
    print()
    print("Every row carries source_system=DEMO; filter on it to keep demo data "
          "out of production reporting.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
