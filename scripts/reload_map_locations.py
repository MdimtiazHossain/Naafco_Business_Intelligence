"""Put the coordinates ``0033_remove_map`` destroyed back, from the export it left.

    python scripts/reload_map_locations.py                 # report only
    python scripts/reload_map_locations.py --apply         # write
    python scripts/reload_map_locations.py --file <csv>    # another export

``0033`` dropped ``map_entity_locations`` on instruction, and the removal
exported the table first to ``reports/map_pre0033_20260903_092802/``. That
export is 1,397 rows: 846 uploaded customer coordinates, 256 uploaded
sales-force coordinates and 551 centroids derived from them. ``0034`` recreates
the table empty, and this script is how the *uploaded* rows return.

Only the authoritative rows are written — ``UPLOAD``, ``MANUAL`` and
``GEOCODED`` — each through the same validation an upload passes, with the
source, precision and label it was exported with. The derived rows are skipped
and then recomputed by ``app.map.geo.derive_parents``, because a centroid
recomputed from today's customer mapping is more honest than a snapshot of one
that may predate a re-mapping. A code the master data no longer holds is
reported and not written: a coordinate nothing can draw is coverage the table
would be claiming falsely.

Dry by default: without ``--apply`` the report is produced inside a transaction
that is rolled back, so the numbers can be read before anything changes. The
target is whatever ``DATABASE_URL`` names — ``data/dev.db`` on a developer box,
and the PostgreSQL deployment when run through ``deploy/local/env.ps1`` — and
the script prints which before it does anything.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  (side effect: sys.path, UTF-8 stdout)

from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database.connection import get_engine  # noqa: E402
from app.map import geo  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPORT = (ROOT / "reports" / "map_pre0033_20260903_092802"
                  / "map_entity_locations.csv")


def read_export(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def show(report: geo.RestoreReport, samples: int) -> None:
    print("\nRestored (authoritative rows written):")
    for level, count in sorted(report.restored.items()):
        print(f"  {level:<14} {count:>6,}")
    print(f"  {'total':<14} {report.restored_total:>6,}")

    if report.skipped_derived:
        print("\nSkipped, recomputed instead (derived rows in the export):")
        for level, count in sorted(report.skipped_derived.items()):
            print(f"  {level:<14} {count:>6,}")

    if report.unknown:
        print(f"\nNot written — code absent from the master data "
              f"({len(report.unknown):,}, first {min(samples, len(report.unknown))}):")
        for level, code in report.unknown[:samples]:
            print(f"  {level} {code}")

    if report.invalid:
        print(f"\nNot written — coordinate refused ({len(report.invalid):,}):")
        for level, code, reason in report.invalid[:samples]:
            print(f"  {level} {code}: {reason}")

    print("\nDerived afterwards (centroids, by level):")
    if not report.derived:
        print("  none")
    for level, count in report.derived.items():
        print(f"  {level:<14} {count:>6,}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, default=DEFAULT_EXPORT,
                        help="The exported map_entity_locations.csv.")
    parser.add_argument("--apply", action="store_true",
                        help="Write. Without this the run is rolled back.")
    parser.add_argument("--actor", default="reload_map_locations",
                        help="Recorded as updated_by on every row written.")
    parser.add_argument("--samples", type=int, default=10)
    args = parser.parse_args(argv)

    if not args.file.exists():
        print(f"No export at {args.file}", file=sys.stderr)
        return 2

    url = get_settings().database_url
    print(f"Target: {re.sub(r':[^:@/]+@', ':****@', url)}")
    print(f"Export: {args.file}")
    records = read_export(args.file)
    print(f"Rows in export: {len(records):,}")

    with Session(get_engine(), future=True) as session:
        report = geo.restore_locations(session, records, actor=args.actor)
        show(report, args.samples)
        if args.apply:
            session.commit()
            print("\nCommitted.")
        else:
            session.rollback()
            print("\nDry run: nothing written. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
