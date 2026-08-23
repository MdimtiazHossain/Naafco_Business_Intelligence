"""Populate the date dimension.

Usage::

    python scripts/build_dim_date.py                       # 2020-01-01 .. +3 years
    python scripts/build_dim_date.py --start 2024-07-01 --end 2030-06-30
    python scripts/build_dim_date.py --fy-start-month 1    # calendar financial year

Idempotent: dates already present are left alone, so widening the range later is
safe and never disturbs facts that already reference a ``date_id``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import _bootstrap  # noqa: F401  (side effect: sys.path)

from sqlalchemy.orm import Session

from app.config import get_settings
from app.database.connection import get_engine
from app.etl.calendar import FinancialYearConfig, populate_dim_date

SEPARATOR = "=" * 78


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    today = dt.date.today()
    parser = argparse.ArgumentParser(description="Populate dim_date")
    parser.add_argument("--start", type=dt.date.fromisoformat, default=dt.date(2020, 1, 1))
    parser.add_argument("--end", type=dt.date.fromisoformat,
                        default=dt.date(today.year + 3, 12, 31))
    parser.add_argument("--fy-start-month", type=int,
                        default=settings.financial_year_start_month,
                        help="Month the financial year starts (1-12).")
    parser.add_argument("--database-url", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = FinancialYearConfig(
            start_month=args.fy_start_month,
            label_prefix=get_settings().financial_year_label_prefix,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(SEPARATOR)
    print("BUILD DATE DIMENSION")
    print(SEPARATOR)
    print(f"Range:              {args.start} .. {args.end}")
    print(f"Financial year:     starts in month {config.start_month} "
          f"({config.label(args.start)} for {args.start})")
    print()

    engine = get_engine(args.database_url)
    try:
        with Session(bind=engine, future=True) as session:
            inserted = populate_dim_date(session, args.start, args.end, config)
            session.commit()
    except Exception as exc:  # noqa: BLE001 - reported, not raised at the user
        print(f"ERROR: could not populate dim_date: {exc}", file=sys.stderr)
        print("Run 'alembic upgrade head' first, and check DATABASE_URL.", file=sys.stderr)
        return 2

    total_days = (args.end - args.start).days + 1
    print(f"Inserted {inserted} new date row(s) of {total_days} in range "
          f"({total_days - inserted} already present).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
