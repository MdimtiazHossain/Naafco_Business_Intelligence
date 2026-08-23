"""Import transaction data (sales / material_stock / target).

Usage::

    python scripts/import_transactions.py sales data/transactions/sales_2026_08.xlsx
    python scripts/import_transactions.py material_stock stock.csv --source-system SAP
    python scripts/import_transactions.py sales sales.csv --date-format DD/MM/YYYY
    python scripts/import_transactions.py sales sales.xlsx --dry-run

Pipeline: read -> stage -> validate -> map to master data -> load facts ->
summary. A row that fails any check is written to ``etl_rejected_records`` with
its reason and is never loaded.

Exit codes: ``0`` clean, ``1`` completed with rejections, ``2`` the import could
not run at all.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  (side effect: sys.path)

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database.connection import get_engine
from app.etl.datasets import DATA_TYPES
from app.etl.mapping import MasterDataIndex
from app.etl.pipeline import (
    LOAD_MODE_INCREMENTAL,
    LOAD_MODE_INITIAL,
    LOAD_MODE_REPROCESS,
    run_import,
)
from app.etl.quality import batch_quality
from app.utils.reporting import write_json

SEPARATOR = "=" * 78


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Import transaction data")
    parser.add_argument("data_type", choices=list(DATA_TYPES))
    parser.add_argument("file", type=Path, help="Excel or CSV source file.")
    parser.add_argument("--source-system", default="MANUAL",
                        help="e.g. SAP, SALES_APP, DEMO. Keeps sources separable.")
    parser.add_argument("--load-mode", default=LOAD_MODE_INCREMENTAL,
                        choices=[LOAD_MODE_INITIAL, LOAD_MODE_INCREMENTAL,
                                 LOAD_MODE_REPROCESS])
    parser.add_argument("--date-format", default=settings.default_date_format,
                        help="DD/MM/YYYY, MM/DD/YYYY, YYYY-MM-DD or 'auto'.")
    parser.add_argument("--sheet", default=None, help="Excel sheet name.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run every step and roll back.")
    parser.add_argument("--reports", type=Path, default=settings.reports_dir)
    parser.add_argument("--database-url", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    print(SEPARATOR)
    print(f"TRANSACTION IMPORT - {args.data_type.upper()}")
    print(SEPARATOR)
    print(f"Source:        {args.file}")
    print(f"Source system: {args.source_system}")
    print(f"Load mode:     {args.load_mode}")
    print(f"Date format:   {args.date_format}")
    print()

    if not args.file.exists():
        print(f"ERROR: source file not found: {args.file}", file=sys.stderr)
        return 2

    engine = get_engine(args.database_url)

    try:
        tables = set(sa_inspect(engine).get_table_names())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: cannot reach the database: {exc}", file=sys.stderr)
        print("Start PostgreSQL ('docker compose up -d db') and check DATABASE_URL.",
              file=sys.stderr)
        return 2

    missing = {"dim_date", "etl_import_batches", f"fact_{args.data_type}"} - tables
    if missing:
        print(f"ERROR: missing table(s): {', '.join(sorted(missing))}", file=sys.stderr)
        print("Run 'cd backend && alembic upgrade head' first.", file=sys.stderr)
        return 2

    # A master-data health check up front turns the confusing "everything was
    # rejected" outcome into a clear, actionable message.
    with Session(bind=engine, future=True) as session:
        index = MasterDataIndex(session)
        if index.is_empty:
            print("ERROR: the master dimensions are empty.", file=sys.stderr)
            print("Every transaction would be rejected as an invalid master code.",
                  file=sys.stderr)
            print("Load master data first:", file=sys.stderr)
            print("    python scripts/import_master_data.py", file=sys.stderr)
            return 2
        print("Master data loaded: " + ", ".join(
            f"{level.replace('_code', '')}={count}"
            for level, count in index.counts().items() if count
        ))
        print()

    try:
        result = run_import(
            engine, args.data_type, args.file,
            source_system=args.source_system, load_mode=args.load_mode,
            date_format=args.date_format, sheet_name=args.sheet, dry_run=args.dry_run,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(result.render())
    print()

    payload = result.to_dict()
    if result.batch_id is not None:
        with Session(bind=engine, future=True) as session:
            payload["quality"] = batch_quality(session, result.batch_id)

    path = write_json(
        Path(args.reports) / f"import_{args.data_type}_summary.json", payload
    )
    print(f"Report written: {path}")
    print()

    if result.status == "FAILED":
        print("RESULT: IMPORT FAILED - nothing was loaded.")
        return 2
    if result.rejected_rows:
        print(f"RESULT: COMPLETED WITH {result.rejected_rows} REJECTED ROW(S) - "
              f"inspect batch {result.batch_id} for reasons.")
        return 1
    print("RESULT: IMPORT COMPLETED" + (" (dry run, rolled back)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
