"""Derive the two new master links from data that already exists, and report.

    python scripts/map_master_data.py            # report only, writes nothing
    python scripts/map_master_data.py --apply    # write the safe mappings
    python scripts/map_master_data.py --csv out  # also write the record lists

Two links are derived:

* ``dim_customer.sub_territory_code`` — from the sub-territory the ETL resolved
  on that customer's transactions, and only where every transaction agrees;
* ``dim_product.company_code`` — from ``producer_company``, where it matches a
  company in the master by code or by normalised name.

Nothing is guessed. A customer whose transactions span two sub-territories, and
a product whose producer company matches nothing or matches two companies, are
left NULL and listed. Nothing already set is overwritten — an existing value
that contradicts the evidence is reported as a conflict instead.

Dry by default: without ``--apply`` the database is not written to at all, so
the report can be read before anything changes.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy.orm import Session  # noqa: E402

from app.database.connection import get_engine  # noqa: E402
from app.datamgmt import mapping  # noqa: E402


def show(report: mapping.MappingReport, samples: int) -> None:
    print(f"\n{report.entity}.{report.field_name}")
    print(f"  Total:       {report.total:,}")
    print(f"  Already set: {report.already_set:,}")
    print(f"  Mappable:    {report.mapped_count:,}")
    print(f"  Unmapped:    {report.unmapped_count:,}")
    print(f"  Conflicts:   {report.conflict_count:,}")

    if report.unmapped:
        print(f"\n  Unmapped (first {min(samples, report.unmapped_count)}):")
        for outcome in report.unmapped[:samples]:
            extra = (f" [candidates: {', '.join(outcome.candidates)}]"
                     if outcome.candidates else "")
            print(f"    {outcome.code}: {outcome.reason}{extra}")

    if report.conflicts:
        print(f"\n  Conflicts (first {min(samples, report.conflict_count)}):")
        for outcome in report.conflicts[:samples]:
            print(f"    {outcome.code}: {outcome.reason}")


def write_csv(directory: Path, report: mapping.MappingReport) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report.entity}_{report.field_name}_mapping.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["code", "outcome", "value", "reason", "candidates"])
        for group in (report.mapped, report.unmapped, report.conflicts):
            for outcome in group:
                writer.writerow([
                    outcome.code, outcome.outcome, outcome.value or "",
                    outcome.reason, "|".join(outcome.candidates),
                ])
    print(f"  wrote {path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true",
                        help="Write the mappings. Without it, nothing changes.")
    # No ``--products-only`` counterpart: customer → sub-territory is the only
    # derivation left since revision 0022 removed the SKU master that the
    # product → company mapping wrote to. The flag stays so an existing habit
    # (and any script that passes it) keeps working.
    parser.add_argument("--customers-only", action="store_true",
                        help="Accepted and redundant — customers are all there is.")
    parser.add_argument("--samples", type=int, default=20,
                        help="How many unmapped/conflicting records to print.")
    parser.add_argument("--csv", type=Path, default=None,
                        help="Directory to write the full record lists to.")
    args = parser.parse_args()

    engine = get_engine()
    print(f"Database: {engine.url}")
    print("Mode:", "APPLYING" if args.apply else "DRY RUN — nothing is written")

    with Session(engine) as session:
        reports = [mapping.map_customers(session, apply=args.apply)]

        for report in reports:
            show(report, args.samples)
            if args.csv:
                write_csv(args.csv, report)

        quality = mapping.quality_issues(session)
        print("\nData quality")
        for entity, findings in quality.items():
            for name, values in findings.items():
                if values:
                    print(f"  {entity}.{name}: {len(values)}")
                    for value in values[:5]:
                        print(f"    {value}")

        if args.apply:
            session.commit()
            total = sum(r.mapped_count for r in reports)
            print(f"\nApplied {total:,} mapping(s).")
        else:
            print("\nNothing was written. Re-run with --apply to save.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
