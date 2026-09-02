"""Would a candidate sales export resolve its sales force codes? Report only.

    python scripts/check_sales_force_codes.py "Sales for dashboard.xlsx"
    python scripts/check_sales_force_codes.py sales.xlsx --sheet Data
    python scripts/check_sales_force_codes.py sales.csv --csv reports/sf_check

Answers one question before a re-import is committed: of the rows in this file,
how many would end up with a sales force the Business Map can actually draw?

That question has three separate hurdles, and the report keeps them apart
because they are fixed in three different places:

1. **Does the file state a code at all?** Every sales export produced so far has
   left ``Sales Force Code`` blank on every row, or filled it with the literal
   ``0``. Either way the column is present and empty of meaning, so the count of
   rows carrying a real value is the first thing worth knowing.
2. **Does the code exist in ``dim_sales_force``?** A code the master does not
   hold resolves to nothing. Unknown codes are listed rather than counted, since
   the usual cause is a format mismatch that one look reveals.
3. **Has that sales force been placed on the map?** A resolved code with no
   coordinate is still invisible. ``map_entity_locations`` is what decides this,
   and it is the difference between "the data would import" and "the map would
   show it".

**Nothing is written and nothing is imported.** The file is read through the
ETL's own reader and the ETL's own header aliases, so a header this script
accepts is one ``run_import`` would accept too — the point is to predict that
run, not to reimplement it. The database is opened read-only in the sense that
only SELECTs are issued; it is the same ``DATABASE_URL`` the application uses,
which on this machine means the check reports on whichever database the
environment currently names. It says which one, up front, because a sales force
question has a different answer on the SQLite development database than on the
PostgreSQL deployment.

Exit status: ``0`` when at least one row would resolve, ``1`` when none would,
``2`` when the file cannot be read — so this can gate an import in a script.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

import _bootstrap  # noqa: F401  - puts backend/ on sys.path, forces UTF-8

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.database.connection import get_engine  # noqa: E402
from app.database.models_warehouse import DimSalesForce  # noqa: E402
from app.database.models_map import MapEntityLocation  # noqa: E402
from app.etl.datasets import SALES, map_headers  # noqa: E402
from app.etl.readers import reader_for_file  # noqa: E402

FIELD = "sales_force_code"

#: Values that are present in the cell but name no sales force. ``0`` is the one
#: this export has actually produced; the rest are the usual spreadsheet ways of
#: writing "nothing" and are treated the same so the report does not call them
#: real codes.
PLACEHOLDERS = {"0", "0.0", "-", "n/a", "na", "null", "none", "#n/a"}


def is_placeholder(value: str) -> bool:
    return value.strip().lower() in PLACEHOLDERS


def read_codes(path: Path, sheet: str | None) -> tuple[list[str], str, list[str]]:
    """Every row's sales force cell, plus the header it came from.

    Returns ``(values, header, unmapped_headers)`` where ``values`` has one entry
    per data row — including the blanks, because the proportion that are blank is
    most of the answer.
    """
    reader = reader_for_file(path, sheet_name=sheet)
    headers = list(reader.headers)
    mapping, unmapped = map_headers(SALES, headers)

    source_header = next((h for h, field in mapping.items() if field == FIELD), None)
    if source_header is None:
        raise LookupError(
            "No column in this file maps to the sales force field. Looked for "
            f"{FIELD!r} and its aliases "
            f"({', '.join(SALES.field_map[FIELD].aliases)}). "
            f"Headers found: {', '.join(headers[:20])}"
        )

    values = [
        "" if (raw := row.get(source_header)) is None else str(raw).strip()
        for row in reader
    ]
    return values, source_header, unmapped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", help="The sales export to check. Not imported.")
    parser.add_argument("--sheet", default=None,
                        help="Worksheet name, for a workbook with more than one.")
    parser.add_argument("--samples", type=int, default=10,
                        help="How many unknown codes to list (default 10).")
    parser.add_argument("--csv", default=None,
                        help="Write the unknown codes to this path as CSV.")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"No such file: {path}")
        return 2

    try:
        values, header, unmapped = read_codes(path, args.sheet)
    except (LookupError, ValueError) as exc:
        print(f"Cannot check this file:\n  {exc}")
        return 2

    total = len(values)
    if not total:
        print(f"{path.name} has no data rows.")
        return 1

    blank = sum(1 for v in values if not v)
    placeholder = sum(1 for v in values if v and is_placeholder(v))
    stated = [v for v in values if v and not is_placeholder(v)]
    file_codes = Counter(stated)

    engine = get_engine()
    with Session(engine) as session:
        known = {
            code for (code,) in session.execute(select(DimSalesForce.sales_force_code))
        }
        placed = {
            code for (code,) in session.execute(
                select(MapEntityLocation.entity_code)
                .where(MapEntityLocation.entity_type == "sales_force")
            )
        }

    resolvable = {c for c in file_codes if c in known}
    unknown = {c: n for c, n in file_codes.items() if c not in known}
    drawable = {c for c in resolvable if c in placed}

    rows_resolvable = sum(file_codes[c] for c in resolvable)
    rows_drawable = sum(file_codes[c] for c in drawable)

    print(f"\nFile      {path.name}")
    print(f"Column    {header!r} -> {FIELD}")
    print(f"Database  {engine.dialect.name}: {engine.url.database}")
    if unmapped:
        print(f"Unmapped headers (kept in raw_data, not imported as fields): "
              f"{', '.join(unmapped[:8])}")

    print(f"\n  Rows in file                 {total:>10,}")
    print(f"    blank                      {blank:>10,}  ({100 * blank / total:.1f}%)")
    print(f"    placeholder (0, -, n/a)    {placeholder:>10,}  "
          f"({100 * placeholder / total:.1f}%)")
    print(f"    stating a code             {len(stated):>10,}  "
          f"({100 * len(stated) / total:.1f}%)")

    print(f"\n  Distinct codes in file       {len(file_codes):>10,}")
    print(f"    in dim_sales_force         {len(resolvable):>10,}")
    print(f"    unknown to the master      {len(unknown):>10,}")
    print(f"    also placed on the map     {len(drawable):>10,}")

    print(f"\n  Rows that would resolve      {rows_resolvable:>10,}  "
          f"({100 * rows_resolvable / total:.1f}%)")
    print(f"  Rows the map could draw      {rows_drawable:>10,}  "
          f"({100 * rows_drawable / total:.1f}%)")

    print(f"\n  Master holds                 {len(known):>10,} sales force records")
    print(f"  Of those, placed             {len(placed & known):>10,}")

    if unknown:
        print(f"\n  Unknown codes (top {min(args.samples, len(unknown))} by row count):")
        for code, count in sorted(unknown.items(), key=lambda kv: -kv[1])[:args.samples]:
            print(f"    {code!r:<24} {count:>8,} rows")

    if args.csv:
        out = Path(args.csv)
        if out.suffix.lower() != ".csv":
            out = out.with_suffix(".csv")
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sales_force_code", "rows_in_file", "in_master", "placed"])
            for code, count in sorted(file_codes.items(), key=lambda kv: -kv[1]):
                writer.writerow([code, count, code in known, code in placed])
        print(f"\n  Wrote {out}")

    # The verdict, in the terms the question was asked in.
    print()
    if not stated:
        print("  VERDICT: no row states a sales force code. Re-importing this file "
              "would leave the sales force layer exactly as it is — the fix is in "
              "the export, not the import.")
    elif not resolvable:
        print("  VERDICT: every stated code is unknown to dim_sales_force. Check the "
              "code format against the master before importing.")
    elif not drawable:
        print(f"  VERDICT: {rows_resolvable:,} rows would resolve, but none of those "
              "sales force records has a coordinate, so the map would still draw "
              "nothing. Upload Map Locations for them.")
    else:
        print(f"  VERDICT: {rows_drawable:,} of {total:,} rows would both resolve and "
              f"draw ({len(drawable):,} sales force on the map).")

    return 0 if rows_resolvable else 1


if __name__ == "__main__":
    raise SystemExit(main())
