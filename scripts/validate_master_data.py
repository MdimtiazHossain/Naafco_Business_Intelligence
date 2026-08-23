"""Validate the master-data workbook: keys, required fields and hierarchy.

Usage::

    python scripts/validate_master_data.py
    python scripts/validate_master_data.py --file "data/Master Data.xlsx" --max-detail 100

Exit codes: ``0`` valid (warnings allowed), ``1`` blocking errors found,
``2`` the workbook could not be read.

Every finding is printed and written to ``reports/master_data_validation.json``.
No validation error is ever ignored silently.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import _bootstrap  # noqa: F401  (side effect: sys.path)

from app.config import get_settings
from app.master_data.inspector import inspect_workbook
from app.master_data.schema import TABLE_SPECS
from app.master_data.validator import validate
from app.utils.cleaning import clean_workbook
from app.utils.reporting import write_json

SEPARATOR = "=" * 78


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Validate the master-data workbook")
    parser.add_argument("--file", type=Path, default=settings.master_data_file)
    parser.add_argument("--reports", type=Path, default=settings.reports_dir)
    parser.add_argument("--max-detail", type=int, default=50,
                        help="How many findings to print per severity (all go to JSON).")
    parser.add_argument("--warnings-as-errors", action="store_true",
                        help="Exit non-zero when warnings are present.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    print(SEPARATOR)
    print("MASTER DATA VALIDATION")
    print(SEPARATOR)
    print(f"Source: {args.file}")
    print()

    try:
        inspection = inspect_workbook(args.file)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    cleaned = clean_workbook(inspection, TABLE_SPECS)
    report = validate(inspection, cleaned)

    print("RECORD COUNTS")
    print("-" * 78)
    for spec in TABLE_SPECS:
        print(f"  {spec.table:<22} {spec.sheet:<24} {len(cleaned[spec.table].rows):>8}")
    print()

    print("HIERARCHY REFERENCE CHECK")
    print("-" * 78)
    for spec in TABLE_SPECS:
        if not spec.has_foreign_key():
            print(f"  {spec.table:<22} (independent dimension)")
            continue
        table = cleaned[spec.table]
        orphans = [
            f for f in report.findings
            if f.table == spec.table and f.rule_id in {"VR007", "VR008"}
        ]
        status = "OK" if not orphans else f"{len(orphans)} INVALID"
        print(f"  {spec.table:<22} -> {spec.parent_table:<22} "
              f"rows={len(table.rows):<6} {status}")
    print()

    errors, warnings = report.errors, report.warnings

    if errors:
        print(f"ERRORS ({len(errors)})")
        print("-" * 78)
        for finding in errors[: args.max_detail]:
            print(finding.format())
            print()
        if len(errors) > args.max_detail:
            print(f"  ... {len(errors) - args.max_detail} further errors in the JSON report")
            print()

    if warnings:
        print(f"WARNINGS ({len(warnings)})")
        print("-" * 78)
        for finding in warnings[: args.max_detail]:
            print(finding.format())
            print()
        if len(warnings) > args.max_detail:
            print(f"  ... {len(warnings) - args.max_detail} further warnings in the JSON report")
            print()

    counts = Counter(f.rule_id for f in report.findings)
    if counts:
        print("FINDINGS BY RULE")
        print("-" * 78)
        for rule_id, n in sorted(counts.items()):
            print(f"  {rule_id}: {n}")
        print()

    path = write_json(Path(args.reports) / "master_data_validation.json", report.to_dict())
    print(f"Report written: {path}")
    print()

    print(SEPARATOR)
    if errors:
        print(f"RESULT: FAILED - {len(errors)} error(s), {len(warnings)} warning(s)")
        print(SEPARATOR)
        return 1
    if warnings and args.warnings_as_errors:
        print(f"RESULT: FAILED (warnings-as-errors) - {len(warnings)} warning(s)")
        print(SEPARATOR)
        return 1
    print(f"RESULT: PASSED - 0 error(s), {len(warnings)} warning(s)")
    print(SEPARATOR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
