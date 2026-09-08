"""Empty chosen groups of data from the warehouse.

Renamed from ``clear_demo_data.py``: the original was written for the seeded
demo dataset, and a script whose name says "demo" pointed at real master data
is a trap for whoever reads the command next. It does the same job; what it
gained is having to be told *which* groups to clear.

Groups:

``masters``
    The master dimensions — the organisational hierarchy, materials, plants,
    storage locations, customers, sales force, the administrative geography,
    and the map coordinates keyed on their codes.
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

Nothing runs without ``--yes``. A dry run prints exactly what would go — and
refuses in exactly the cases the real run would, so a rehearsal that prints a
plan is a plan that will run.

Exit codes, because "it printed something and exited 0" is not a report:

``0``
    Everything named was emptied, or was already empty, or this was a dry run.
``1``
    At least one table could not be emptied, and it is named. **Nothing was
    written** — the run is one transaction and it was rolled back, for the same
    reason migration 0020 counts every table before dropping any: a selection
    half-cleared is harder to reason about than one left alone.
``2``
    Usage — no groups named, or a name that is not a group.
``3``
    Refused before touching anything: rows *outside* the selection reference
    rows inside it, so the deletes are forbidden. The message names the group
    to add.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import delete, inspect, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

# The *package* import is what registers every table on ``Base.metadata`` — the
# foreign-key graph read below is only complete because of it.
from app.database import Base, get_engine  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3

#: Children before parents, and flattened in **this dict's order** rather than
#: in the order the operator typed the groups: ``ordered_tables`` iterates
#: ``GROUPS``, so ``--groups masters,transactions`` deletes exactly what
#: ``--groups transactions,masters`` does. Without that, the guarantee below
#: would depend on the command line.
#:
#: The guarantee is that for every foreign key whose *parent* this script
#: empties, the child is emptied first. It holds within a group by the order of
#: each tuple, and across groups by the order of this dict — but only for the
#: groups actually named. A child that lives in a group the operator did not
#: name is not an ordering problem and no ordering can fix it;
#: ``blocking_references`` refuses the run up front instead.
#:
#: Both halves matter because the two dialects disagree about the consequence.
#: SQLite leaves foreign keys unenforced — ``connection._configure_sqlite`` sets
#: WAL and a busy timeout, not ``PRAGMA foreign_keys`` — so a wrong order there
#: does not fail, it orphans. PostgreSQL refuses. ``test_clear_data.py`` pins
#: this order against the model metadata so that neither dialect has to find out.
GROUPS: dict[str, tuple[str, ...]] = {
    "transactions": (
        "fact_sales", "fact_material_stock", "fact_target",
        "stg_sales", "stg_material_stock", "stg_target",
        "etl_rejected_records", "etl_import_batches",
    ),
    "uploads": ("upload_errors", "upload_batches"),
    "masters": (
        # Coordinates and boundaries are keyed on master codes, so they go with
        # the codes rather than being left pointing at nothing.
        "map_entity_locations", "map_area_boundaries",
        # Administrative geography, deepest first.
        "dim_upazila", "dim_district", "dim_division",
        # Dimensions with no children of their own. The material masters are
        # independent of the organisational hierarchy below them; among
        # themselves a storage location names a plant, so it goes first.
        "dim_customer", "dim_sales_force",
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

#: Which group empties a given table, so that a refusal can name the fix rather
#: than leave the operator to read ``GROUPS``.
GROUP_OF_TABLE: dict[str, str] = {
    table: group for group, tables in GROUPS.items() for table in tables
}

#: ``ON DELETE`` actions that make deleting a referenced row *fail* rather than
#: fix the child up. ``None`` is a plain reference — NO ACTION — which both
#: dialects enforce; ``fact_target.material_id`` is the one declared that way,
#: and it is no weaker than the RESTRICT every other fact key carries.
BLOCKING_ON_DELETE = frozenset({"NO ACTION", "RESTRICT"})

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

#: The tables ``clear_users`` empties for the doomed accounts, children first.
#: Rows are removed explicitly rather than through ``ON DELETE CASCADE``,
#: because SQLite does not enforce a cascade unless the pragma is on and a
#: cascade that works in PostgreSQL would silently leave orphans here.
USER_CHILD_TABLES: tuple[tuple[str, str], ...] = (
    ("user_section_permissions", "user_id"),
    ("notifications", "user_id"),
    ("chat_tool_calls", "user_id"),
    ("chat_messages", "user_id"),
    ("chat_conversations", "user_id"),
)

ROOT = Path(__file__).resolve().parents[1]


def ordered_tables(chosen: set[str]) -> tuple[str, ...]:
    """The tables a selection empties, children first — see ``GROUPS``."""
    return tuple(
        table for group in GROUPS if group in chosen for table in GROUPS[group]
    )


def counts(session: Session, tables: tuple[str, ...]) -> dict[str, int]:
    return {
        table: session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        for table in tables
    }


class Blocker(NamedTuple):
    """Rows that forbid one of the deletes the operator asked for."""

    table: str
    columns: str
    referenced: str
    rows: int

    @property
    def remedy(self) -> str:
        group = GROUP_OF_TABLE.get(self.table)
        return f"add group '{group}'" if group else "no group empties it"


def blocking_references(
    session: Session, doomed: set[str], live: set[str]
) -> list[Blocker]:
    """Foreign keys from outside the selection into it that still hold rows.

    Asked *before* anything is deleted, and answered by counting rows rather
    than by letting a DELETE fail, because the two dialects disagree about
    whether it would fail at all: PostgreSQL refuses, while SQLite — foreign
    keys unenforced through ``get_engine`` — succeeds and leaves the child
    pointing at nothing, which is both the worse outcome and the silent one. A
    count gives the same answer on both, so a rehearsal on the development
    database predicts what production will do.

    The graph is read from the model metadata rather than restated here: a
    hand-written list of what blocks what is exactly the kind of list that
    outlives the schema it describes.
    """
    found: list[Blocker] = []
    for name in sorted(live - doomed):
        table = Base.metadata.tables.get(name)
        if table is None:
            # A table the models do not describe. Nothing here can say what its
            # keys mean, so it is left to the database to object if it must.
            continue
        constraints = sorted(
            table.foreign_key_constraints,
            key=lambda c: [column.name for column in c.columns],
        )
        for constraint in constraints:
            referenced = constraint.referred_table.name
            if referenced not in doomed or referenced not in live:
                continue
            if (constraint.ondelete or "NO ACTION").upper() not in BLOCKING_ON_DELETE:
                continue
            columns = [column.name for column in constraint.columns]
            # A key with a NULL in it references nothing and so forbids nothing
            # — MATCH SIMPLE, which is the default on both dialects.
            where = " AND ".join(f"{column} IS NOT NULL" for column in columns)
            rows = session.execute(
                text(f"SELECT COUNT(*) FROM {name} WHERE {where}")
            ).scalar_one()
            if rows:
                found.append(Blocker(name, ", ".join(columns), referenced, rows))
    return found


def first_line(exc: Exception) -> str:
    """A driver error's headline, without the SQL and parameters it appends."""
    message = str(exc).strip()
    return message.splitlines()[0] if message else exc.__class__.__name__


def clear(
    session: Session, tables: tuple[str, ...]
) -> tuple[int, list[tuple[str, str]]]:
    """Empty each table; return the rows removed and the tables that refused.

    Each DELETE runs in its own SAVEPOINT. Not so the run can survive a failure
    — the caller rolls the whole transaction back — but so that the operator is
    told about *every* blocked table rather than only the first: PostgreSQL
    aborts a transaction on the failing statement and would then refuse each
    later one for a reason that has nothing to do with the table it names.
    """
    total = 0
    failures: list[tuple[str, str]] = []
    for table in tables:
        try:
            with session.begin_nested():
                removed = session.execute(text(f"DELETE FROM {table}")).rowcount
            total += removed or 0
        except Exception as exc:  # noqa: BLE001 - reported by the caller, not swallowed
            failures.append((table, first_line(exc)))
    return total, failures


def clear_users(
    session: Session, keep: set[str], live: set[str]
) -> tuple[int, list[str]]:
    from app.database.models_ai import AppUser

    doomed = session.execute(
        select(AppUser.user_id, AppUser.username).where(AppUser.username.notin_(keep))
    ).all()
    if not doomed:
        return 0, []
    ids = ",".join(str(row[0]) for row in doomed)

    for table, column in USER_CHILD_TABLES:
        # Absence is checked, not caught: a DELETE that fails for any other
        # reason has to reach the caller, which names it and rolls back.
        if table not in live:
            continue
        session.execute(text(f"DELETE FROM {table} WHERE {column} IN ({ids})"))

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
        return EXIT_USAGE
    if not chosen:
        parser.print_help()
        print("\nNothing selected. Pass --groups.")
        return EXIT_USAGE

    dry_run = not args.yes
    engine = get_engine()

    tables = ordered_tables(chosen)
    live = set(inspect(engine).get_table_names())
    present = tuple(table for table in tables if table in live)
    absent = [table for table in tables if table not in live]

    print(f"Database: {engine.url}")
    print(f"Groups:   {', '.join(sorted(chosen))}")
    print("Mode:", "DRY RUN — nothing will be written" if dry_run else "DELETING")

    with Session(engine) as session:
        before = counts(session, present)
        populated = {t: n for t, n in before.items() if n}

        print(f"\nTables to empty ({sum(populated.values()):,} rows):")
        for table, n in populated.items():
            print(f"  {table:<28} {n:>8,}")
        if not populated:
            print("  (already empty)")

        if absent:
            # A name this schema does not have holds no rows, so nothing was
            # left behind and the run is not a failure. It is still a fault in
            # GROUPS, and printing it is how it gets noticed.
            print("\nNot in this schema (so nothing to empty):")
            for table in absent:
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

        blocked = blocking_references(session, set(present), live)
        if blocked:
            print(
                f"\nREFUSED. {len(blocked)} reference(s) from outside the "
                "selection still hold rows:"
            )
            for blocker in blocked:
                origin = f"{blocker.table}.{blocker.columns}"
                noun = "row" if blocker.rows == 1 else "rows"
                print(f"  {origin:<44} -> {blocker.referenced:<26}"
                      f"{blocker.rows:>10,} {noun:<4}  [{blocker.remedy}]")
            print(
                "\nEmptying the referenced tables is forbidden while those rows"
                " exist — and on SQLite, where foreign keys are not enforced, "
                "it would succeed and leave them pointing at nothing. Name the"
                " group that empties them in the same run, or leave the "
                "referenced rows in place."
            )
            print("Nothing was changed.")
            return EXIT_REFUSED

        if dry_run:
            print("\nNothing was changed. Re-run with --yes to apply.")
            return EXIT_OK

        removed, failures = clear(session, present)

        user_count, usernames = 0, []
        if "users" in chosen and not failures:
            try:
                with session.begin_nested():
                    user_count, usernames = clear_users(session, keep_users, live)
            except Exception as exc:  # noqa: BLE001 - reported below, not swallowed
                failures.append(("app_user", first_line(exc)))
                user_count, usernames = 0, []

        if failures:
            # Rolled back whole rather than committed in part: the operator
            # asked for a selection, and half of one is a state nobody
            # described. The files are left alone for the same reason.
            session.rollback()
            print(f"\nFAILED. {len(failures)} table(s) could not be emptied:")
            for table, message in failures:
                print(f"  {table:<28} {message}")
            print(
                "\nNothing was changed — the run is one transaction and it has"
                " been rolled back. No files were deleted."
            )
            return EXIT_FAILED

        session.commit()

        for path in doomed_files:
            path.unlink(missing_ok=True)

        print(f"\nDeleted {removed:,} rows.")
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

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
