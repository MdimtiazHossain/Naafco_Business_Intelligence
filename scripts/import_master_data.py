"""Import the master data into the PostgreSQL dimension tables.

Usage::

    python scripts/import_master_data.py
    python scripts/import_master_data.py --dry-run
    python scripts/import_master_data.py --database-url postgresql+psycopg://...

Pipeline: Excel -> inspect -> clean -> validate -> load dimensions -> summary.

Rows are upserted on their official business code: an existing code is updated,
a new code is inserted, so re-running never duplicates a master record. A failing
validation aborts the load before anything is written unless ``--allow-invalid``
is passed, and even then every finding is reported.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  (side effect: sys.path)

from sqlalchemy import inspect as sa_inspect

from app.config import get_settings
from app.database.connection import get_engine
from app.database.models import Base
from app.master_data.importer import run_import
from app.utils.reporting import write_json, write_text

SEPARATOR = "=" * 78


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Import master data into PostgreSQL")
    parser.add_argument("--file", type=Path, default=settings.master_data_file)
    parser.add_argument("--reports", type=Path, default=settings.reports_dir)
    parser.add_argument("--database-url", default=None,
                        help="Override DATABASE_URL for this run.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run the whole pipeline and roll back instead of committing.")
    parser.add_argument("--allow-invalid", action="store_true",
                        help="Load the tables that pass validation and skip the ones that "
                             "do not, instead of aborting the whole import.")
    parser.add_argument("--create-tables", action="store_true",
                        help="Create the dimension tables directly instead of using Alembic. "
                             "Intended for local experiments; use 'alembic upgrade head' "
                             "for real environments.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    print(SEPARATOR)
    print("MASTER DATA IMPORT")
    print(SEPARATOR)
    print(f"Source: {args.file}")
    print()

    engine = get_engine(args.database_url)

    try:
        if args.create_tables:
            Base.metadata.create_all(engine)
            print("Dimension tables created (or already present).")
            print()

        existing = set(sa_inspect(engine).get_table_names())
        missing = [t for t in Base.metadata.tables if t not in existing]
        if missing:
            print("ERROR: the following tables do not exist in the database:",
                  file=sys.stderr)
            for name in missing:
                print(f"  - {name}", file=sys.stderr)
            print("Run 'alembic upgrade head' (or pass --create-tables) first.",
                  file=sys.stderr)
            return 2
    except Exception as exc:  # noqa: BLE001 - connection failures are reported, not raised
        print(f"ERROR: cannot reach the database: {exc}", file=sys.stderr)
        print("Check DATABASE_URL / .env, or start PostgreSQL with 'docker compose up -d db'.",
              file=sys.stderr)
        return 2

    try:
        summary, report, _ = run_import(
            args.file, engine, dry_run=args.dry_run, allow_invalid=args.allow_invalid
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if report.errors:
        print(f"VALIDATION ERRORS ({len(report.errors)})")
        print("-" * 78)
        for finding in report.errors[:25]:
            print(finding.format())
            print()
        if len(report.errors) > 25:
            print(f"  ... {len(report.errors) - 25} more (see the validation report)")
            print()

    print(summary.render())
    print()

    reports_dir = Path(args.reports)
    json_path = write_json(reports_dir / "master_data_import_summary.json", summary.to_dict())
    txt_path = write_text(reports_dir / "master_data_import_summary.txt", summary.render() + "\n")
    validation_path = write_json(
        reports_dir / "master_data_validation.json", report.to_dict()
    )
    print("Reports written:")
    for path in (json_path, txt_path, validation_path):
        print(f"  {path}")
    print()

    if not summary.validation_passed and not args.allow_invalid:
        print("RESULT: IMPORT ABORTED - fix the validation errors and re-run.")
        return 1
    if summary.failed:
        print(f"RESULT: COMPLETED WITH {summary.failed} FAILED RECORD(S)")
        return 1
    print("RESULT: IMPORT COMPLETED" + (" (dry run, rolled back)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
