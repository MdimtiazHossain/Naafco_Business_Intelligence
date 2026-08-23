"""Seed a **local development** database so the web app can be exercised.

Why this exists
---------------
``data/Master Data.xlsx`` is a schema-specification workbook with zero records
(see the README), so every dimension is empty and
``generate_demo_transactions.py`` correctly refuses to invent master codes.
Without master data there is nothing to log in and look at.

This script fills that gap **for local development only**. It creates a small,
obviously-fictional organisation and product list, then hands over to the
existing demo-transaction generator and importer — so transactions still flow
through the real Phase 2 ETL, with real validation and real hierarchy checks.

Safety rules
------------
* Refuses to run when ``APP_ENV=production``.
* Refuses to run when the dimensions already contain records, unless ``--force``
  is given, so it can never overwrite imported master data.
* Every organisation, product and user it creates is prefixed ``DEMO`` or named
  so that it cannot be mistaken for real data.
* Every transaction is tagged ``source_system = DEMO`` and can be excluded from
  any report with a single filter.

Usage::

    python scripts/seed_dev_data.py
    python scripts/seed_dev_data.py --days 45 --rows-per-day 25
    python scripts/seed_dev_data.py --password 'My-Dev-Pass-1'
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import secrets
import string
import subprocess
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  (side effect: sys.path)

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.security import hash_password
from app.config import get_settings
from app.database.connection import get_engine
from app.database.models import (
    DimArea,
    DimBusinessUnit,
    DimCompany,
    DimProduct,
    DimRegion,
    DimSalesLine,
    DimSubTerritory,
    DimTerritory,
    DimUnit,
    DimZone,
)
from app.database.models_ai import AppUser, Notification, Role
from app.etl.calendar import populate_dim_date
from app.etl.mapping import ensure_master_source_status

SEPARATOR = "=" * 78
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------
# The fictional organisation. Names are deliberately generic and every code is
# ordinary-looking so the hierarchy behaves realistically, but nothing here
# corresponds to a real company, region or product.
# --------------------------------------------------------------------------

COMPANY = ("C001", "Demo Industries Ltd.")
BUSINESS_UNIT = ("BU001", "Consumer Products")
SALES_LINES = [("SL001", "General Trade"), ("SL002", "Modern Trade")]
ZONES = [("Z001", "Central Zone", "SL001"), ("Z002", "Coastal Zone", "SL001")]
REGIONS = [
    ("REG001", "Dhaka", "Z001"),
    ("REG002", "Rajshahi", "Z001"),
    ("REG003", "Chattogram", "Z002"),
    ("REG004", "Khulna", "Z002"),
]
AREAS = [
    ("AR001", "Mirpur", "REG001"),
    ("AR002", "Gulshan", "REG001"),
    ("AR003", "Boalia", "REG002"),
    ("AR004", "Agrabad", "REG003"),
    ("AR005", "Khalishpur", "REG004"),
]
UNITS = [
    ("UN001", "Mirpur Unit 1", "AR001"),
    ("UN002", "Gulshan Unit 1", "AR002"),
    ("UN003", "Boalia Unit 1", "AR003"),
    ("UN004", "Agrabad Unit 1", "AR004"),
    ("UN005", "Khalishpur Unit 1", "AR005"),
]
TERRITORIES = [
    ("TR001", "Kazipara", "UN001"),
    ("TR002", "Pallabi", "UN001"),
    ("TR003", "Banani", "UN002"),
    ("TR004", "Shaheb Bazar", "UN003"),
    ("TR005", "Halishahar", "UN004"),
    ("TR006", "Daulatpur", "UN005"),
]
SUB_TERRITORIES = [
    ("STR001", "Kazipara North", "TR001"),
    ("STR002", "Kazipara South", "TR001"),
    ("STR003", "Pallabi East", "TR002"),
    ("STR004", "Banani North", "TR003"),
    ("STR005", "Shaheb Bazar West", "TR004"),
    ("STR006", "Halishahar Port", "TR005"),
    ("STR007", "Daulatpur Central", "TR006"),
]
PRODUCTS = [
    ("SKU001", "Premium Tea 500g", "প্রিমিয়াম চা ৫০০ গ্রাম", "Beverage", "DemoLeaf"),
    ("SKU002", "Premium Tea 1kg", "প্রিমিয়াম চা ১ কেজি", "Beverage", "DemoLeaf"),
    ("SKU003", "Green Tea 250g", "গ্রিন টি ২৫০ গ্রাম", "Beverage", "DemoLeaf"),
    ("SKU004", "Instant Coffee 200g", "ইনস্ট্যান্ট কফি ২০০ গ্রাম", "Beverage", "DemoBrew"),
    ("SKU005", "Biscuit Family Pack", "বিস্কুট ফ্যামিলি প্যাক", "Snacks", "DemoBake"),
    ("SKU006", "Salted Crackers 300g", "সল্টেড ক্র্যাকার ৩০০ গ্রাম", "Snacks", "DemoBake"),
    ("SKU007", "Milk Powder 500g", "গুঁড়ো দুধ ৫০০ গ্রাম", "Dairy", "DemoDairy"),
    ("SKU008", "Butter 200g", "মাখন ২০০ গ্রাম", "Dairy", "DemoDairy"),
]

#: Development users. Each exercises a different level of the RBAC model, so the
#: data scope can actually be tested from the browser.
DEV_USERS = [
    {
        "username": "demo",
        "email": "demo@company.local",
        "display_name": "System Demo User",
        "role": Role.MANAGEMENT,
        "scope": None,
        "phone": "+8801700000001",
        "note": "Sees every region.",
    },
    {
        "username": "admin",
        "email": "admin@company.local",
        "display_name": "Demo Administrator",
        "role": Role.SUPER_ADMIN,
        "scope": None,
        "phone": "+8801700000002",
        "note": "Admin panel access.",
    },
    {
        "username": "dhaka.rm",
        "email": "dhaka.rm@company.local",
        "display_name": "Dhaka Regional Manager",
        "role": Role.REGIONAL_MANAGER,
        "scope": {"region_code": ["REG001"]},
        "phone": "+8801700000003",
        "note": "Dhaka region only — use this to see RBAC refuse other regions.",
    },
    {
        "username": "mirpur.am",
        "email": "mirpur.am@company.local",
        "display_name": "Mirpur Area Manager",
        "role": Role.AREA_MANAGER,
        "scope": {"area_code": ["AR001"]},
        "phone": "+8801700000004",
        "note": "Mirpur area only.",
    },
]


def generate_password(length: int = 16) -> str:
    """A strong throwaway password for local development."""
    alphabet = string.ascii_letters + string.digits
    core = "".join(secrets.choice(alphabet) for _ in range(length - 4))
    return f"Dev-{core}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed a local development database")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--days", type=int, default=60,
                        help="Days of transaction history to generate.")
    parser.add_argument("--rows-per-day", type=int, default=18)
    parser.add_argument("--end-date", type=dt.date.fromisoformat, default=dt.date.today())
    parser.add_argument("--password", default=None,
                        help="Password for every dev user (generated when omitted).")
    parser.add_argument("--force", action="store_true",
                        help="Seed even when master data already exists.")
    parser.add_argument("--skip-transactions", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if os.getenv("APP_ENV", "development").lower() == "production":
        print("REFUSING: APP_ENV=production. This script seeds demo data and must "
              "never run against production.", file=sys.stderr)
        return 2

    engine = get_engine(args.database_url)
    database_url = args.database_url or get_settings().database_url

    print(SEPARATOR)
    print("SEED DEVELOPMENT DATA  (demo only — not real business data)")
    print(SEPARATOR)
    print(f"Database: {_mask(database_url)}")
    print()

    try:
        with Session(engine, expire_on_commit=False) as session:
            existing = session.execute(
                select(func.count()).select_from(DimRegion)
            ).scalar_one()
            if existing and not args.force:
                print(f"Master data already contains {existing} region(s).")
                print("Refusing to overwrite it. Pass --force only on a database you "
                      "are sure is disposable.", file=sys.stderr)
                return 2

            created = seed_master(session)
            password = args.password or generate_password()
            users = seed_users(session, password)
            populate_dim_date(session, dt.date(args.end_date.year - 2, 1, 1),
                              dt.date(args.end_date.year + 2, 12, 31))
            ensure_master_source_status(session)
            seed_notifications(session, users)
            session.commit()
    except Exception as exc:  # noqa: BLE001 - reported, never raised at the user
        print(f"ERROR: could not seed the database: {exc}", file=sys.stderr)
        print("Run 'cd backend && alembic upgrade head' first.", file=sys.stderr)
        return 2

    print(f"Master data: {created}")
    print(f"Users:       {', '.join(u['username'] for u in DEV_USERS)}")
    print()

    if not args.skip_transactions:
        code = seed_transactions(database_url, args)
        if code != 0:
            return code

    print()
    print(SEPARATOR)
    print("DEVELOPMENT CREDENTIALS")
    print(SEPARATOR)
    for user in DEV_USERS:
        print(f"  {user['username']:<12} {password:<20} {user['role']:<18} {user['note']}")
    print()
    print("These are demo credentials for a local database. Never reuse them "
          "anywhere real.")
    return 0


def _mask(url: str) -> str:
    """Hide any password in a database URL before printing it."""
    if "@" in url and "//" in url:
        scheme, _, rest = url.partition("//")
        credentials, _, host = rest.rpartition("@")
        if ":" in credentials:
            user = credentials.split(":", 1)[0]
            return f"{scheme}//{user}:***@{host}"
    return url


def seed_master(session: Session) -> str:
    """Insert the fictional hierarchy and product list."""
    session.add(DimCompany(company_code=COMPANY[0], company_name=COMPANY[1],
                           company_head_name="Demo Managing Director"))
    session.add(DimBusinessUnit(bu_code=BUSINESS_UNIT[0], bu_name=BUSINESS_UNIT[1],
                                company_code=COMPANY[0]))
    for code, name in SALES_LINES:
        session.add(DimSalesLine(sales_line_code=code, sales_line_name=name,
                                 bu_code=BUSINESS_UNIT[0]))
    for code, name, parent in ZONES:
        session.add(DimZone(zone_code=code, zone_name=name, sales_line_code=parent))
    for code, name, parent in REGIONS:
        session.add(DimRegion(region_code=code, region_name=name, zone_code=parent,
                              region_hq=name))
    for code, name, parent in AREAS:
        session.add(DimArea(area_code=code, area_name=name, region_code=parent,
                            area_hq=name))
    for code, name, parent in UNITS:
        session.add(DimUnit(unit_code=code, unit_name=name, area_code=parent))
    for code, name, parent in TERRITORIES:
        session.add(DimTerritory(territory_code=code, territory_name=name,
                                 unit_code=parent, territory_hq=name))
    for code, name, parent in SUB_TERRITORIES:
        session.add(DimSubTerritory(sub_territory_code=code, sub_territory_name=name,
                                    territory_code=parent))
    for code, name_en, name_bn, category, brand in PRODUCTS:
        session.add(DimProduct(sku_code=code, sku_name_en=name_en, sku_name_bn=name_bn,
                               category=category, brand=brand, status="Active",
                               retailer_unit="CTN", consumer_unit="PCS"))
    session.flush()
    return (f"{len(REGIONS)} regions, {len(AREAS)} areas, {len(TERRITORIES)} "
            f"territories, {len(PRODUCTS)} products")


def seed_users(session: Session, password: str) -> list[AppUser]:
    """Create or refresh the development users."""
    hashed = hash_password(password)
    users: list[AppUser] = []
    for spec in DEV_USERS:
        user = session.execute(
            select(AppUser).where(AppUser.username == spec["username"])
        ).scalar_one_or_none()
        if user is None:
            user = AppUser(username=spec["username"])
            session.add(user)
        user.display_name = spec["display_name"]
        user.email = spec["email"]
        user.role = spec["role"]
        user.data_scope = spec["scope"]
        user.phone_number = spec["phone"]
        user.password_hash = hashed
        user.is_active = True
        user.preferred_language = "en"
        users.append(user)
    session.flush()
    return users


def seed_notifications(session: Session, users: list[AppUser]) -> None:
    """A couple of notifications so the header bell has something to show."""
    management = next((u for u in users if u.role == Role.MANAGEMENT), None)
    if management is None:
        return
    already = session.execute(
        select(func.count()).select_from(Notification)
        .where(Notification.user_id == management.user_id)
    ).scalar_one()
    if already:
        return
    session.add(Notification(
        user_id=management.user_id, category="SYSTEM", severity="LOW",
        title="Demo data loaded",
        body="This database contains DEMO transactions only.", link="/data-quality",
    ))
    session.add(Notification(
        user_id=management.user_id, category="STOCK", severity="HIGH",
        title="Expired stock needs review",
        body="Some stock has passed its shelf life expiration date.", link="/stock",
    ))


def seed_transactions(database_url: str, args: argparse.Namespace) -> int:
    """Generate demo files, then import them through the real Phase 2 ETL."""
    out = PROJECT_ROOT / "data" / "transactions" / "demo"
    python = sys.executable

    print("Generating demo transaction files…")
    generate = subprocess.run(
        [python, str(PROJECT_ROOT / "scripts" / "generate_demo_transactions.py"),
         "--database-url", database_url, "--out", str(out), "--format", "csv",
         "--days", str(args.days), "--rows-per-day", str(args.rows_per_day),
         "--end-date", args.end_date.isoformat()],
        capture_output=True, text=True, encoding="utf-8",
    )
    if generate.returncode != 0:
        print(generate.stdout)
        print(generate.stderr, file=sys.stderr)
        return generate.returncode

    for data_type in ("sales", "material_stock", "target"):
        path = out / f"demo_{data_type}.csv"
        if not path.exists():
            continue
        result = subprocess.run(
            [python, str(PROJECT_ROOT / "scripts" / "import_transactions.py"),
             data_type, str(path), "--source-system", "DEMO",
             "--database-url", database_url],
            capture_output=True, text=True, encoding="utf-8",
        )
        summary = [line for line in (result.stdout or "").splitlines()
                   if line.startswith(("Total:", "Valid:", "Rejected:", "RESULT:"))]
        print(f"  {data_type:<12} " + " | ".join(summary[:4]))
        # Exit code 1 means "completed with rejections", which is still a load.
        if result.returncode not in (0, 1):
            print(result.stderr, file=sys.stderr)
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
