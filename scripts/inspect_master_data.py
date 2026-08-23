"""Inspect and profile the master-data workbook.

Usage::

    python scripts/inspect_master_data.py
    python scripts/inspect_master_data.py --file "data/Master Data.xlsx" --reports reports

Outputs:

* ``reports/master_data_profile.json``    — full structure + profiling data
* ``reports/master_data_profile.xlsx``    — the same profile as a workbook
* ``reports/master_data_dictionary.md``   — data dictionary + Mermaid ER diagram
* ``reports/master_data_er_diagram.md``   — the ER diagram on its own

The workbook is opened read-only; the source file is never modified.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  (side effect: sys.path)

from app.config import get_settings
from app.master_data.inspector import inspect_workbook
from app.master_data.profiler import profile_workbook
from app.master_data.schema import EXPECTED_SHEETS, TABLE_SPECS
from app.utils.reporting import (
    build_data_dictionary,
    build_er_diagram,
    write_json,
    write_profile_workbook,
    write_text,
)

SEPARATOR = "=" * 78


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Inspect and profile Master Data.xlsx")
    parser.add_argument("--file", type=Path, default=settings.master_data_file,
                        help="Path to the master-data workbook.")
    parser.add_argument("--reports", type=Path, default=settings.reports_dir,
                        help="Directory to write reports into.")
    parser.add_argument("--fail-on-missing-sheet", action="store_true",
                        help="Exit non-zero when an expected sheet is absent.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    print(SEPARATOR)
    print("MASTER DATA INSPECTION")
    print(SEPARATOR)
    print(f"Source: {args.file}")
    print()

    try:
        inspection = inspect_workbook(args.file)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"Sheets found ({len(inspection.sheet_names)}):")
    for name in inspection.sheet_names:
        print(f"  - {name}")
    print()

    if inspection.missing_sheets:
        print("MISSING EXPECTED SHEETS:")
        for name in inspection.missing_sheets:
            print(f"  ! {name}")
        print()
    if inspection.unexpected_sheets:
        print("ADDITIONAL SHEETS (reported, kept, not imported):")
        for name in inspection.unexpected_sheets:
            print(f"  ? {name}")
        print()

    print("STRUCTURE")
    print("-" * 78)
    header = f"{'Sheet':<24}{'Layout':<16}{'Header':<14}{'Cols':>6}{'Records':>9}"
    print(header)
    print("-" * 78)
    for sheet_data in inspection.sheets.values():
        s = sheet_data.structure
        location = (
            f"row {s.header_row}" if s.header_row is not None
            else (f"col {s.header_column}" if s.header_column is not None else "-")
        )
        print(f"{s.sheet_name:<24}{s.orientation:<16}{location:<14}"
              f"{len(s.columns):>6}{s.record_count:>9}")
    print("-" * 78)
    print(f"{'TOTAL':<60}{sum(len(sd.records) for sd in inspection.sheets.values()):>18}")
    print()

    print("COLUMNS DISCOVERED")
    print("-" * 78)
    for sheet_data in inspection.sheets.values():
        print(f"{sheet_data.sheet_name} ({len(sheet_data.columns)} fields):")
        for col in sheet_data.columns:
            print(f"    - {col}")
        for note in sheet_data.structure.notes:
            print(f"    # {note}")
        print()

    profile = profile_workbook(inspection)

    reports_dir = Path(args.reports)
    json_path = write_json(reports_dir / "master_data_profile.json", {
        "structure": inspection.structure_summary(),
        "profile": profile.to_dict(),
        "expected_sheets": list(EXPECTED_SHEETS),
        "target_tables": [
            {
                "table": s.table,
                "sheet": s.sheet,
                "business_key": s.business_key,
                "surrogate_key": s.surrogate_key,
                "parent_table": s.parent_table,
                "parent_column": s.parent_column,
            }
            for s in TABLE_SPECS
        ],
    })
    xlsx_path = write_profile_workbook(reports_dir / "master_data_profile.xlsx", profile)
    dictionary_path = write_text(
        reports_dir / "master_data_dictionary.md", build_data_dictionary(profile)
    )
    er_path = write_text(
        reports_dir / "master_data_er_diagram.md",
        "# Master Data ER Diagram\n\n```mermaid\n" + build_er_diagram() + "```\n",
    )

    print("REPORTS WRITTEN")
    print("-" * 78)
    for path in (json_path, xlsx_path, dictionary_path, er_path):
        print(f"  {path}")
    print()

    if inspection.missing_sheets and args.fail_on_missing_sheet:
        print("STOPPING: expected sheets are missing from the workbook.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
