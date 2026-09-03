"""Empty chosen groups of data from the warehouse.

Renamed from ``clear_demo_data.py``: the original was written for the seeded
demo dataset, and a script whose name says "demo" pointed at real master data
is a trap for whoever reads the command next. It does the same job; what it
gained is having to be told *which* groups to clear.

Groups:

``masters``
    The master dimensions — the organisational hierarchy, products, materials,
    plants, storage locations, customers, sales force, the administrative
    geography, and the map coordinates keyed on their codes.
``transactions``
    Facts, staging and the ETL batches that produced them.
``uploads``
    The Data Upload Center's history: batches, their row-level errors, and the
    staged files still on disk.
``changelog``
    ``data_change_log`` — the field-level history of individual records.
``history``
    Audit log, chat conversations and notifications.
``users``
    Accounts, minus whoever ``--keep-users`` names.

**Never touched.** ``dim_date`` (a generated calendar nothing can load without),
``etl_master_source_status`` (a registry the ETL maintains itself), the marker
designs (application configuration, re-seeded at startup), role permissions, and
the schema.

Nothing runs without ``--yes``. A dry run prints exactly what would go.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import delete, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.database.connection import get_engine  # noqa: E402
from app.etl.datasets import DATASETS  # noqa: E402

#: Every transactional dataset's fact and staging table, derived rather than
#: listed. The list used to be written out by hand and went stale the moment
#: revision 0031 added Credit Control: ``fact_credit_invoice`` still referenced
#: ``etl_import_batches``, so emptying that table raised a foreign-key violation
#: and the whole group failed. Deriving from ``DATASETS`` is the same rule the
#: upload registry and the data-management catalogue already follow — declare a
#: dataset and everything that iterates datasets follows.
DATASET_TABLES: tuple[str, ...] = tuple(
    table for spec in DATASETS for table in (spec.fact_table, spec.staging_table)
)

#: Children before parents. SQLite leaves foreign keys unenforced by default,
#: but the same script has to be correct against PostgreSQL, where they are not.
GROUPS: dict[str, tuple[str, ...]] = {
    # Facts and staging first: both reference ``etl_import_batches``, which can
    # only go once nothing points at it.
    "transactions": (*DATASET_TABLES, "etl_rejected_records", "etl_import_batches"),
    "uploads": ("upload_errors", "upload_batches"),
    "masters": (
        # Administrative geography, deepest first.
        "dim_upazila", "dim_district", "dim_division",
        # Dimensions with no children of their own. The material masters are
        # independent of the organisational hierarchy below them; among
        # themselves a storage location names a plant, so it goes first.
        "dim_customer", "dim_sales_force", "dim_product",
        "dim_material", "dim_storage_location", "dim_plant",
        # The organisational hierarchy, deepest first.
        "dim_sub_territory", "dim_territory", "dim_unit", "dim_area",
        "dim_region", "dim_zone", "dim_sales_line", "dim_business_unit",
        "dim_company",
    ),
    # Field-level history of individual records. Its own group, because it
    # belongs to the master data it describes: clearing the masters and leaving
    # it keeps a change log for records that no longer exist. The audit log is
    # a different thing — a platform-wide record of who did what — and is not
    # swept up with it.
    "changelog": ("data_change_log",),
    "history": (
        "chat_tool_calls", "chat_messages", "chat_conversations",
        "notifications", "audit_logs",
    ),
}

#: Directories whose contents belong to a group.
FILES_BY_GROUP: dict[str, tuple[str, ...]] = {
    "uploads": ("data/uploads",),
    "transactions": ("data/transactions/demo",),
}

PROTECTED: dict[str, str] = {
    "dim_date": "generated calendar; the ETL cannot load a row without it",
    "etl_master_source_status": "registry the ETL pipeline maintains itself",
    "map_marker_designs": "application configuration, re-seeded at startup",
    "map_marker_design_versions": "history of that configuration",
    "map_marker_assets": "uploaded marker artwork",
    "map_marker_assignments": "which design each entity type uses",
    "map_area_styles": "how administrative areas are drawn",
    "role_section_permissions": "role defaults, not data",
    "alembic_version": "schema revision",
}

ROOT = Path(__file__).resolve().parents[1]


def counts(session: Session, tables: tuple[str, ...]) -> dict[str, int]:
    result: dict[str, int] = {}
    for table in tables:
        try:
            result[table] = session.execute(
                text(f"SELECT COUNT(*) FROM {table}")
            ).scalar_one()
        except Exception:  # noqa: BLE001 - an absent table is not an error here
            continue
    return result


def resolve(engine, tables: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a group's tables into the ones this schema has and the ones it does not.

    Asked once, up front, because a table that no longer exists is the only
    failure this script should tolerate — ``dim_product`` went in revision 0022
    and is still named in the masters group. Every other failure means something
    is wrong, and :func:`clear` is free to let it stop the run.
    """
    from sqlalchemy import inspect

    known = set(inspect(engine).get_table_names())
    return (tuple(t for t in tables if t in known),
            tuple(t for t in tables if t not in known))


def clear(session: Session, tables: tuple[str, ...]) -> None:
    """Empty every table, in one transaction, or none of them.

    Deliberately catches nothing. It used to catch each statement and carry on,
    which works on SQLite and is wrong on PostgreSQL: there, one failed
    statement aborts the whole transaction, so every later DELETE fails too and
    the commit rolls the lot back — while the script printed the number of rows
    it had *attempted* and exited 0. A destructive script that reports success
    having changed nothing is worse than one that fails loudly, because the next
    person reads the output as "already done" and moves on.

    Tables absent from the schema are filtered out by :func:`resolve` before this
    runs, which was the only reason the catch existed.
    """
    for table in tables:
        session.execute(text(f"DELETE FROM {table}"))


def clear_users(session: Session, keep: set[str]) -> tuple[int, list[str]]:
    from app.database.models_ai import AppUser

    doomed = session.execute(
        select(AppUser.user_id, AppUser.username).where(AppUser.username.notin_(keep))
    ).all()
    if not doomed:
        return 0, []
    ids = ",".join(str(row[0]) for row in doomed)

    # Explicitly, rather than relying on ON DELETE CASCADE: SQLite does not
    # enforce foreign keys unless the pragma is on, so a cascade that works in
    # PostgreSQL would silently leave orphans here.
    # Resolved against the schema rather than attempted and forgiven: swallowing
    # a failure here has the same effect as it did in ``clear`` — on PostgreSQL
    # the transaction is already dead and the commit takes everything with it.
    present, _ = resolve(session.get_bind(), (
        "user_section_permissions", "notifications",
        "chat_tool_calls", "chat_messages", "chat_conversations",
    ))
    columns = {"user_section_permissions": "user_id", "notifications": "user_id",
               "chat_tool_calls": "user_id", "chat_messages": "user_id",
               "chat_conversations": "user_id"}
    for table in present:
        session.execute(
            text(f"DELETE FROM {table} WHERE {columns[table]} IN ({ids})"))

    session.execute(delete(AppUser).where(AppUser.user_id.in_(
        [row[0] for row in doomed])))
    return len(doomed), [row[1] for row in doomed]


def files_for(groups: set[str]) -> list[Path]:
    found: list[Path] = []
    for group in groups:
        for relative in FILES_BY_GROUP.get(group, ()):
            directory = ROOT / relative
            if directory.exists():
                found.extend(p for p in sorted(directory.iterdir()) if p.is_file())
    return found


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--groups", default="",
        help="Comma-separated: " + ", ".join([*GROUPS, "users", "files"]) +
             ". Required — there is no default, so nothing is cleared by "
             "accident.",
    )
    parser.add_argument("--keep-users", default="admin",
                        help="Usernames to keep when 'users' is in --groups.")
    parser.add_argument("--yes", action="store_true",
                        help="Actually delete. Without it, nothing is written.")
    args = parser.parse_args()

    chosen = {g.strip() for g in args.groups.split(",") if g.strip()}
    unknown = chosen - {*GROUPS, "users", "files"}
    if unknown:
        print(f"Unknown group(s): {', '.join(sorted(unknown))}")
        return 2
    if not chosen:
        parser.print_help()
        print("\nNothing selected. Pass --groups.")
        return 2

    dry_run = not args.yes
    engine = get_engine()

    requested: tuple[str, ...] = tuple(
        table for group in GROUPS if group in chosen for table in GROUPS[group]
    )
    tables, missing = resolve(engine, requested)

    print(f"Database: {engine.url}")
    print(f"Groups:   {', '.join(sorted(chosen))}")
    print("Mode:", "DRY RUN — nothing will be written" if dry_run else "DELETING")

    with Session(engine) as session:
        before = counts(session, tables)
        populated = {t: n for t, n in before.items() if n}

        print(f"\nTables to empty ({sum(populated.values()):,} rows):")
        for table, n in populated.items():
            print(f"  {table:<28} {n:>8,}")
        if not populated:
            print("  (already empty)")

        if missing:
            # Named rather than passed over: a table in a group and not in the
            # schema is either a dataset that left the platform or a typo, and
            # the reader is the one who can tell which.
            print("\nNot in this schema, so not cleared:")
            for table in missing:
                print(f"  {table}")

        keep_users = {u.strip() for u in args.keep_users.split(",") if u.strip()}
        if "users" in chosen:
            print(f"\nUsers: keeping {', '.join(sorted(keep_users)) or 'none'}")

        # A group's files go with its rows: clearing the upload history but
        # leaving its staged files behind would orphan them permanently, since
        # the rows naming them would be gone.
        doomed_files = files_for(chosen)
        if doomed_files:
            print(f"\nFiles to delete ({len(doomed_files)}):")
            for path in doomed_files:
                print(f"  {path.relative_to(ROOT)}")

        print("\nKept:")
        for table, reason in PROTECTED.items():
            print(f"  {table:<28} {reason}")

        if dry_run:
            print("\nNothing was changed. Re-run with --yes to apply.")
            return 0

        user_count, usernames = 0, []
        try:
            clear(session, tables)
            if "users" in chosen:
                user_count, usernames = clear_users(session, keep_users)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            session.rollback()
            print(f"\nFAILED — nothing was deleted and no file was touched.\n\n  {exc}")
            return 1

        # Only once the rows are gone: a file deleted beside a transaction that
        # rolled back would be gone with its record still naming it.
        for path in doomed_files:
            path.unlink(missing_ok=True)

        # Counted from the database after the commit, never from the row counts
        # the DELETEs reported. Those are what was attempted; this is what is
        # actually no longer there, and the two used to differ by everything.
        after = counts(session, tables)
        removed = sum(before.get(t, 0) - after.get(t, 0) for t in tables)
        still_there = {t: n for t, n in after.items() if n}
        print(f"\nDeleted {removed:,} rows (verified against the database).")
        if still_there:
            print("Still holding rows:")
            for table, n in still_there.items():
                print(f"  {table:<28} {n:>8,}")
        if user_count:
            print(f"Removed {user_count} user(s): {', '.join(usernames)}")
        if doomed_files:
            print(f"Deleted {len(doomed_files)} file(s).")

        if "masters" in chosen:
            print(
                "\nNote: etl_master_source_status still marks the workbook "
                "dimensions AVAILABLE, so transaction imports will reject "
                "unknown codes until master data is loaded again. That is the "
                "designed guard rail, not a fault."
            )

    # SQLite keeps the freed pages unless told otherwise, so the file would stay
    # its old size and look as though nothing had been removed.
    if engine.dialect.name == "sqlite":
        with engine.connect() as connection:
            connection.exec_driver_sql("VACUUM")
        print("Reclaimed free space (VACUUM).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
