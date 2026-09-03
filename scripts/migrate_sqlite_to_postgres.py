"""Copy a SQLite warehouse into an empty PostgreSQL one, row for row.

Written for the move onto Supabase, but it is not Supabase-specific: it copies
any SQLite database this application built into any PostgreSQL database this
application's migrations have built.

Usage::

    python scripts/migrate_sqlite_to_postgres.py --dry-run
    python scripts/migrate_sqlite_to_postgres.py
    python scripts/migrate_sqlite_to_postgres.py --source data/dev.db \
        --target "postgresql+psycopg://user:pw@host:5432/postgres?sslmode=require"

The target defaults to ``DIRECT_URL`` and then to the configured database URL,
the same order Alembic uses: a copy of this size is one long transaction and
belongs on a session-mode connection, not behind a transaction pooler.

Nothing here invents, repairs or reinterprets a value. Every row is read through
the same SQLAlchemy ``Table`` object it is written through, so a JSON column
decodes from SQLite's TEXT and re-encodes into ``JSONB``, a boolean stored as
``0``/``1`` arrives as ``false``/``true``, and a timestamp stored as an ISO
string arrives as a ``timestamp`` -- because the column type, not this script,
performs the conversion. A code stays a string. Surrogate keys are copied
verbatim, since every fact references them.

The whole copy is one transaction: it either lands complete or leaves the target
exactly as it found it, which is the same promise ``etl/pipeline.py`` makes about
an import.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  (side effect: sys.path)

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Table, create_engine, func, insert, inspect, select, text
from sqlalchemy.engine import Connection, Engine, make_url

from app.config import get_settings
from app.database.connection import TRANSACTION_POOLER_PORT
from app.database.models import Base
from app.database import models_admin  # noqa: F401  (registers Phase 4 tables)
from app.database import models_ai  # noqa: F401  (registers Phase 3 tables)
from app.database import models_geo  # noqa: F401  (registers the geography tables)
from app.database import models_warehouse  # noqa: F401  (registers Phase 2 tables)
from app.etl.bulk import PARAMETER_LIMITS

SEPARATOR = "=" * 78
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"

#: Exit codes, matching ``validate_master_data.py``.
EXIT_OK = 0
EXIT_PREFLIGHT_FAILED = 1
EXIT_COPY_FAILED = 2

#: Alembic's own bookkeeping table. Not copied: the target's version is written
#: by the migrations that actually built it, and overwriting it with the
#: source's would claim a schema history the target never ran.
VERSION_TABLE = "alembic_version"


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #

def head_revision() -> str:
    """The newest revision on disk, read from the migration scripts themselves."""
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option(
        "script_location", str(BACKEND_DIR / "app" / "database" / "migrations")
    )
    return ScriptDirectory.from_config(config).get_current_head()


def applied_revision(conn: Connection) -> str | None:
    """The revision a database believes it is at, or None when it has no table."""
    if not inspect(conn).has_table(VERSION_TABLE):
        return None
    row = conn.execute(text(f"SELECT version_num FROM {VERSION_TABLE}")).fetchone()
    return row[0] if row else None


def copyable_tables() -> list[Table]:
    """Every model table, in an order that satisfies the foreign keys.

    ``sorted_tables`` is derived from the declared relationships rather than
    listed here, so a table added to the models is copied without this script
    being touched -- the same reason the upload registry is derived and not
    hand-written.
    """
    return [t for t in Base.metadata.sorted_tables if t.name != VERSION_TABLE]


def preflight(source: Connection, target: Connection, tables: list[Table]) -> list[str]:
    """Everything that must be true before a single row moves.

    Returns the problems found; an empty list means the copy may proceed. Each
    one reports what is wrong *and* what to do about it, because whoever runs
    this is mid-migration and a bare assertion is no help there.
    """
    problems: list[str] = []
    head = head_revision()

    source_at = applied_revision(source)
    if source_at != head:
        problems.append(
            f"The source is at revision {source_at or 'nothing'}, not {head}. Its "
            "schema does not match the models this script copies through. Run "
            "`alembic upgrade head` against the source first."
        )

    target_at = applied_revision(target)
    if target_at is None:
        problems.append(
            "The target has no alembic_version table, so no migration has ever run "
            "on it. Run `cd backend; alembic upgrade head` against it first -- this "
            "script copies data into a schema, it does not create one."
        )
    elif target_at != head:
        problems.append(
            f"The target is at revision {target_at}, not {head}. Run "
            "`cd backend; alembic upgrade head` against it before copying."
        )

    # An empty target is not a convenience, it is the safety property: this
    # script has no upsert and no conflict handling, so anything already there
    # would either collide on a key or survive as a row nobody accounted for.
    occupied = []
    for table in tables:
        count = target.execute(select(func.count()).select_from(table)).scalar_one()
        if count:
            occupied.append(f"{table.name} ({count:,})")
    if occupied:
        problems.append(
            "The target already holds rows in: " + ", ".join(occupied) + ". This "
            "script only ever fills an empty schema; it will not merge into, "
            "update or clear an existing one."
        )

    source_tables = set(inspect(source).get_table_names())
    missing = [t.name for t in tables if t.name not in source_tables]
    if missing:
        problems.append(
            "The source is missing tables the models declare: " + ", ".join(missing)
        )

    return problems


# --------------------------------------------------------------------------- #
# Copy
# --------------------------------------------------------------------------- #

def chunk_size(table: Table) -> int:
    """Rows per INSERT, bounded by PostgreSQL's bind-parameter budget.

    Reuses ``etl.bulk.PARAMETER_LIMITS`` rather than restating the number: the
    constraint is the same one the ETL lives under, and a second copy of it here
    could drift from the one every import is chunked against.
    """
    columns = max(1, len(table.columns))
    return max(1, PARAMETER_LIMITS["postgresql"] // columns)


def copy_table(source: Connection, target: Connection, table: Table) -> int:
    """Stream one table across, returning the number of rows written."""
    size = chunk_size(table)
    written = 0
    result = source.execution_options(yield_per=size).execute(select(table))
    for partition in result.mappings().partitions(size):
        rows = [dict(row) for row in partition]
        if not rows:
            continue
        target.execute(insert(table), rows)
        written += len(rows)
    return written


def reset_sequences(target: Connection, tables: list[Table]) -> list[str]:
    """Point every identity sequence past the largest key that was copied.

    Surrogate keys arrive with their original values, which leaves each sequence
    still sitting at 1. Without this the first insert after the migration would
    collide with row 1 -- and it would do so on the *next* upload rather than
    here, where it can still be explained.

    ``pg_get_serial_sequence`` is asked rather than a name being assembled from
    the table and column, because the answer for a column no sequence backs is
    NULL, which is exactly the signal to skip it.

    Returns immediately on any other dialect: a sequence is a PostgreSQL object
    and there is nothing to reset elsewhere. That is also what lets the copy be
    exercised SQLite -> SQLite in the tests without this step pretending to have
    done something.
    """
    if target.dialect.name != "postgresql":
        return []
    adjusted: list[str] = []
    for table in tables:
        for column in table.primary_key.columns:
            sequence = target.execute(
                text("SELECT pg_get_serial_sequence(:table, :column)"),
                {"table": table.name, "column": column.name},
            ).scalar()
            if sequence is None:
                continue
            highest = target.execute(select(func.max(column))).scalar()
            if highest is None:
                # An empty table keeps its sequence untouched: setval() cannot
                # say "still unused" without is_called=false, and restarting a
                # sequence nobody has drawn from would be a change with no cause.
                continue
            target.execute(
                text("SELECT setval(:sequence, :value)"),
                {"sequence": sequence, "value": int(highest)},
            )
            adjusted.append(f"{table.name}.{column.name} -> {highest:,}")
    return adjusted


def verify(source: Connection, target: Connection, tables: list[Table]) -> list[str]:
    """Re-count both sides. Any disagreement aborts the whole transaction."""
    mismatches = []
    for table in tables:
        before = source.execute(select(func.count()).select_from(table)).scalar_one()
        after = target.execute(select(func.count()).select_from(table)).scalar_one()
        if before != after:
            mismatches.append(f"{table.name}: source {before:,}, target {after:,}")
    return mismatches


def run_copy(source: Connection, target: Connection, tables: list[Table],
             counts: dict[str, int]) -> None:
    """Copy every table, reset the sequences and verify, as one transaction.

    A failure at 90% must leave the target as empty as it started, not half a
    warehouse nobody can reason about -- the same promise ``etl/pipeline.py``
    makes about an import.

    The ``rollback()`` first is not tidying up after an error. Preflight and the
    row counts are ``SELECT``s, and in SQLAlchemy 2.0 a ``SELECT`` on a
    ``Connection`` *autobegins* a transaction; ``begin()`` on a connection that
    already has one raises rather than nesting. Ending that read transaction
    explicitly is what lets the copy own a transaction of its own, and
    ``rollback()`` is a no-op when none is open.
    """
    target.rollback()

    with target.begin():
        for table in tables:
            expected = counts[table.name]
            if not expected:
                continue
            written = copy_table(source, target, table)
            if written != expected:
                # The source was counted moments ago, so a different number now
                # means something is writing to it while this runs. Carrying on
                # would produce a target that matches no point in time at all.
                raise RuntimeError(
                    f"{table.name}: copied {written:,} rows but counted "
                    f"{expected:,} just before. The source is being modified "
                    "while it is copied — stop whatever is writing to it and "
                    "run this again."
                )
            print(f"  {written:>9,}  {table.name}")

        sequences = reset_sequences(target, tables)
        if sequences:
            print("\nResetting identity sequences...")
            for line in sequences:
                print(f"  {line}")

        print("\nVerifying row counts...")
        mismatches = verify(source, target, tables)
        if mismatches:
            print("\nRow counts disagree -- rolling the whole copy back:")
            for line in mismatches:
                print(f"  * {line}")
            raise RuntimeError("row count verification failed")
        print(f"  all {len(tables)} tables match ({sum(counts.values()):,} rows)")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy a SQLite warehouse into an empty PostgreSQL one."
    )
    parser.add_argument(
        "--source", default=None,
        help="SQLite file or URL. Defaults to the configured DATABASE_URL when "
             "that is SQLite, otherwise data/dev.db.",
    )
    parser.add_argument(
        "--target", default=None,
        help="PostgreSQL URL. Defaults to DIRECT_URL, then to the configured "
             "database URL.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be copied and write nothing.",
    )
    return parser.parse_args(argv)


def resolve_source(explicit: str | None) -> str:
    if explicit:
        if "://" in explicit:
            return explicit
        return f"sqlite:///{Path(explicit).resolve()}"
    configured = get_settings().database_url
    if configured.startswith("sqlite"):
        return configured
    return f"sqlite:///{(PROJECT_ROOT / 'data' / 'dev.db').resolve()}"


def resolve_target(explicit: str | None) -> str:
    return explicit or os.getenv("DIRECT_URL") or get_settings().database_url


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    source_url = resolve_source(args.source)
    target_url = resolve_target(args.target)

    source_parsed = make_url(source_url)
    target_parsed = make_url(target_url)

    if not source_parsed.drivername.startswith("sqlite"):
        print(f"Source must be SQLite, got {source_parsed.drivername}.")
        return EXIT_PREFLIGHT_FAILED
    if not target_parsed.drivername.startswith("postgresql"):
        print(
            f"Target must be PostgreSQL, got {target_parsed.drivername}. Set "
            "DIRECT_URL or pass --target."
        )
        return EXIT_PREFLIGHT_FAILED
    if target_parsed.port == TRANSACTION_POOLER_PORT:
        print(
            f"Target port {TRANSACTION_POOLER_PORT} is the transaction-mode pooler. "
            "This copy is one long transaction and belongs on the session-mode "
            "pooler (port 5432 on the same host) or the direct connection. Set "
            "DIRECT_URL and run again."
        )
        return EXIT_PREFLIGHT_FAILED

    print(SEPARATOR)
    print("SQLite -> PostgreSQL migration")
    print(SEPARATOR)
    print(f"source : {source_parsed.render_as_string(hide_password=True)}")
    print(f"target : {target_parsed.render_as_string(hide_password=True)}")
    print()

    tables = copyable_tables()
    source_engine: Engine = create_engine(source_url, future=True)
    target_engine: Engine = create_engine(target_url, future=True, pool_pre_ping=True)

    with source_engine.connect() as source, target_engine.connect() as target:
        problems = preflight(source, target, tables)
        if problems:
            print("Preflight failed:\n")
            for problem in problems:
                print(f"  * {problem}\n")
            return EXIT_PREFLIGHT_FAILED

        counts = {
            table.name: source.execute(
                select(func.count()).select_from(table)
            ).scalar_one()
            for table in tables
        }
        total = sum(counts.values())
        print(f"{len(tables)} tables, {total:,} rows to copy.\n")

        if args.dry_run:
            for table in tables:
                if counts[table.name]:
                    print(f"  {counts[table.name]:>9,}  {table.name}"
                          f"   (chunks of {chunk_size(table):,})")
            print(f"\n  {total:>9,}  TOTAL")
            print("\nDry run: nothing was written.")
            return EXIT_OK

        run_copy(source, target, tables, counts)

    print(f"\n{SEPARATOR}")
    print(f"Done. {total:,} rows copied.")
    print(SEPARATOR)
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print(f"\nAborted: {exc}")
        sys.exit(EXIT_COPY_FAILED)
