"""The Supabase deployment path: pooler handling and the SQLite -> PostgreSQL copy.

None of these tests need a PostgreSQL server. What they pin is the reasoning
that is easy to get wrong and expensive to discover in production: which URL
gets which engine options, which URL a migration refuses to run over, and that
the copy carries a JSON column, a boolean and a timestamp across unchanged
rather than as whatever SQLite happened to store them as.

The copy itself is exercised SQLite -> SQLite. That is not a weaker test than it
looks: ``copy_table`` reads and writes through the same SQLAlchemy ``Table``
objects in both directions, so what is being pinned is that the round trip goes
through the column types at all -- which is the mechanism that makes the
PostgreSQL side correct.
"""

from __future__ import annotations

import datetime as dt
import importlib
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, insert, select

from app.database.connection import (
    TRANSACTION_POOLER_PORT,
    _engine_options,
    alembic_ini_value,
    assert_migration_safe,
)
from app.database.models import Base
from app.etl.bulk import PARAMETER_LIMITS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

POOLER_HOST = "aws-0-ap-south-1.pooler.supabase.com"
TRANSACTION_POOLER = (
    f"postgresql+psycopg://postgres.ref:pw@{POOLER_HOST}:{TRANSACTION_POOLER_PORT}/postgres"
)
SESSION_POOLER = f"postgresql+psycopg://postgres.ref:pw@{POOLER_HOST}:5432/postgres"


@pytest.fixture(scope="module")
def migrate_script():
    """The migration script, imported as a module rather than run as one."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    return importlib.import_module("migrate_sqlite_to_postgres")


# --------------------------------------------------------------------------
# Engine options
# --------------------------------------------------------------------------


def test_sqlite_takes_no_postgres_engine_options() -> None:
    """Pool sizing is meaningless for a file and must not reach create_engine."""
    assert _engine_options("sqlite:///./data/dev.db") == {}


def test_an_unparseable_url_is_left_to_create_engine() -> None:
    """Not this function's error to raise, so it declines rather than guessing."""
    assert _engine_options("this is not a url") == {}


def test_postgres_gets_pool_sizing() -> None:
    options = _engine_options(SESSION_POOLER)
    assert set(options) == {"pool_size", "max_overflow", "pool_recycle"}


def test_the_transaction_pooler_disables_prepared_statements() -> None:
    """psycopg prepares after five executions; in transaction mode the sixth
    may land on a server connection that never saw the PREPARE."""
    options = _engine_options(TRANSACTION_POOLER)
    assert options["connect_args"] == {"prepare_threshold": None}


def test_the_session_pooler_keeps_prepared_statements() -> None:
    """Session mode holds one server connection, so the optimisation is safe."""
    assert "connect_args" not in _engine_options(SESSION_POOLER)


# --------------------------------------------------------------------------
# Migration safety
# --------------------------------------------------------------------------


def test_migrating_through_the_transaction_pooler_is_refused() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        assert_migration_safe(TRANSACTION_POOLER)
    # The message has to say what to do next: whoever hits this is mid-deploy.
    assert "DIRECT_URL" in str(excinfo.value)


@pytest.mark.parametrize(
    "url",
    [
        SESSION_POOLER,
        "postgresql+psycopg://postgres:pw@db.ref.supabase.co:5432/postgres",
        "postgresql+psycopg://postgres:postgres@localhost:5432/ai_business_agent",
        "sqlite:///./data/dev.db",
    ],
)
def test_these_urls_may_carry_a_migration(url: str) -> None:
    assert_migration_safe(url)


# --------------------------------------------------------------------------
# What gets copied
# --------------------------------------------------------------------------


def test_every_model_table_is_copied(migrate_script) -> None:
    """Derived from the metadata, so a new table needs no edit to this script."""
    copied = {t.name for t in migrate_script.copyable_tables()}
    assert copied == set(Base.metadata.tables)


def test_alembic_version_is_never_copied(migrate_script) -> None:
    """The target's revision is written by the migrations that built it."""
    assert migrate_script.VERSION_TABLE not in {
        t.name for t in migrate_script.copyable_tables()
    }


def test_the_copy_order_satisfies_foreign_keys(migrate_script) -> None:
    """A referenced table must be filled before the table referencing it."""
    order = {t.name: i for i, t in enumerate(migrate_script.copyable_tables())}
    for table in migrate_script.copyable_tables():
        for fk in table.foreign_keys:
            parent = fk.column.table.name
            if parent == table.name:  # self-reference is satisfied within the table
                continue
            assert order[parent] < order[table.name], (
                f"{table.name} is copied before {parent}, which it references"
            )


def test_chunk_size_stays_inside_the_bind_parameter_budget(migrate_script) -> None:
    """The real limit is placeholders, not rows -- a wide table must chunk smaller."""
    budget = PARAMETER_LIMITS["postgresql"]
    for table in migrate_script.copyable_tables():
        size = migrate_script.chunk_size(table)
        assert size >= 1
        assert size * len(table.columns) <= budget


# --------------------------------------------------------------------------
# The copy itself
# --------------------------------------------------------------------------


@pytest.fixture
def two_databases(tmp_path: Path):
    """A populated source and an empty target, both with the real schema."""
    source = create_engine(f"sqlite:///{tmp_path / 'source.db'}", future=True)
    target = create_engine(f"sqlite:///{tmp_path / 'target.db'}", future=True)
    Base.metadata.create_all(source)
    Base.metadata.create_all(target)
    return source, target


def test_copy_carries_json_booleans_and_timestamps_across(
    migrate_script, two_databases
) -> None:
    """The three types SQLite does not really have, round-tripped through the
    column definitions rather than through whatever bytes were on disk."""
    source, target = two_databases
    audit = Base.metadata.tables["audit_logs"]
    written = dt.datetime(2026, 8, 24, 9, 30, 0)
    rows = [
        {
            "audit_id": 41,
            "username": "ceo",
            "action": "LOGIN",
            # Bangla must survive the trip verbatim -- no case folding, no NFKC.
            "detail": {"note": "রিপোর্ট", "rows": 12},
            "success": True,
            "created_at": written,
        },
        {
            "audit_id": 42,
            "username": "rm",
            "action": "EXPORT_DENIED",
            "detail": None,
            "success": False,
            "created_at": written,
        },
    ]
    with source.begin() as conn:
        conn.execute(insert(audit), rows)

    with source.connect() as src, target.connect() as dst:
        with dst.begin():
            copied = migrate_script.copy_table(src, dst, audit)
    assert copied == 2

    with target.connect() as conn:
        landed = conn.execute(select(audit).order_by(audit.c.audit_id)).mappings().all()

    assert [row["audit_id"] for row in landed] == [41, 42]
    # A dict, not the string "{'note': ...}" that a naive copy would leave behind.
    assert landed[0]["detail"] == {"note": "রিপোর্ট", "rows": 12}
    assert landed[1]["detail"] is None
    # True/False, not 1/0.
    assert landed[0]["success"] is True
    assert landed[1]["success"] is False
    # A datetime, not an ISO string.
    assert landed[0]["created_at"] == written


def test_copy_spans_more_than_one_chunk(migrate_script, two_databases, monkeypatch) -> None:
    """Chunking is where a streamed copy loses or duplicates rows, so force it."""
    source, target = two_databases
    audit = Base.metadata.tables["audit_logs"]
    monkeypatch.setattr(migrate_script, "chunk_size", lambda table: 7)

    rows = [
        {"audit_id": i, "action": "LOGIN", "success": True,
         "created_at": dt.datetime(2026, 8, 24)}
        for i in range(1, 51)
    ]
    with source.begin() as conn:
        conn.execute(insert(audit), rows)

    with source.connect() as src, target.connect() as dst:
        with dst.begin():
            copied = migrate_script.copy_table(src, dst, audit)

    assert copied == 50
    with target.connect() as conn:
        assert conn.execute(select(func.count()).select_from(audit)).scalar_one() == 50
        # Every key preserved verbatim: facts reference surrogate keys.
        ids = conn.execute(select(audit.c.audit_id).order_by(audit.c.audit_id)).scalars().all()
    assert ids == list(range(1, 51))


def test_the_copy_can_open_a_transaction_after_preflight_has_read(
    migrate_script, two_databases
) -> None:
    """Preflight SELECTs autobegin a transaction on the target connection, and
    ``Connection.begin()`` raises on a connection that already has one rather
    than nesting. ``run_copy`` ends the read transaction first; without that the
    real migration fails on its first table, after every check has passed."""
    source, target = two_databases
    audit = Base.metadata.tables["audit_logs"]
    with source.begin() as conn:
        conn.execute(insert(audit), [
            {"audit_id": 1, "action": "LOGIN", "success": True,
             "created_at": dt.datetime(2026, 8, 24)},
        ])

    tables = [audit]
    with source.connect() as src, target.connect() as dst:
        # Exactly what main() does before copying: preflight, then count.
        migrate_script.preflight(src, dst, tables)
        counts = {
            audit.name: src.execute(select(func.count()).select_from(audit)).scalar_one()
        }
        migrate_script.run_copy(src, dst, tables, counts)

    with target.connect() as conn:
        assert conn.execute(select(func.count()).select_from(audit)).scalar_one() == 1


def test_a_short_copy_aborts_and_rolls_back(migrate_script, two_databases) -> None:
    """Fewer rows written than were counted means the source is changing under
    the copy. The target must be left as empty as it started, not half filled."""
    source, target = two_databases
    audit = Base.metadata.tables["audit_logs"]
    with source.begin() as conn:
        conn.execute(insert(audit), [
            {"audit_id": i, "action": "LOGIN", "success": True,
             "created_at": dt.datetime(2026, 8, 24)}
            for i in range(1, 6)
        ])

    # Claim one more row than the source holds, standing in for a row that was
    # deleted between the count and the copy.
    with source.connect() as src, target.connect() as dst:
        with pytest.raises(RuntimeError, match="being modified"):
            migrate_script.run_copy(src, dst, [audit], {audit.name: 6})

    with target.connect() as conn:
        assert conn.execute(select(func.count()).select_from(audit)).scalar_one() == 0


def test_sequences_are_only_reset_on_postgres(migrate_script, two_databases) -> None:
    """pg_get_serial_sequence does not exist elsewhere, and there is nothing to
    reset there — so the step reports having done nothing rather than failing."""
    _, target = two_databases
    audit = Base.metadata.tables["audit_logs"]
    with target.connect() as conn:
        assert migrate_script.reset_sequences(conn, [audit]) == []


def test_verify_reports_a_row_count_disagreement(migrate_script, two_databases) -> None:
    """The check that turns a partial copy into a rollback instead of a silent loss."""
    source, target = two_databases
    audit = Base.metadata.tables["audit_logs"]
    with source.begin() as conn:
        conn.execute(insert(audit), [
            {"audit_id": 1, "action": "LOGIN", "success": True,
             "created_at": dt.datetime(2026, 8, 24)},
        ])

    with source.connect() as src, target.connect() as dst:
        mismatches = migrate_script.verify(src, dst, [audit])

    assert any("audit_logs" in line for line in mismatches)


# --------------------------------------------------------------------------
# Entry-point guards
# --------------------------------------------------------------------------


def test_a_non_postgres_target_is_refused(migrate_script, tmp_path, capsys) -> None:
    code = migrate_script.main([
        "--source", str(tmp_path / "source.db"),
        "--target", f"sqlite:///{tmp_path / 'target.db'}",
        "--dry-run",
    ])
    assert code == migrate_script.EXIT_PREFLIGHT_FAILED
    assert "must be PostgreSQL" in capsys.readouterr().out


def test_the_transaction_pooler_is_refused_as_a_target(
    migrate_script, tmp_path, capsys
) -> None:
    """The copy is one long transaction; it belongs on a session connection."""
    code = migrate_script.main([
        "--source", str(tmp_path / "source.db"),
        "--target", TRANSACTION_POOLER,
        "--dry-run",
    ])
    assert code == migrate_script.EXIT_PREFLIGHT_FAILED
    out = capsys.readouterr().out
    assert str(TRANSACTION_POOLER_PORT) in out
    assert "DIRECT_URL" in out


def test_a_non_sqlite_source_is_refused(migrate_script, capsys) -> None:
    code = migrate_script.main([
        "--source", SESSION_POOLER, "--target", SESSION_POOLER, "--dry-run",
    ])
    assert code == migrate_script.EXIT_PREFLIGHT_FAILED
    assert "must be SQLite" in capsys.readouterr().out


def test_direct_url_is_preferred_for_the_target(migrate_script, monkeypatch) -> None:
    """The copy follows Alembic's rule, not the application's."""
    monkeypatch.setenv("DIRECT_URL", SESSION_POOLER)
    assert migrate_script.resolve_target(None) == SESSION_POOLER


def test_an_explicit_target_beats_direct_url(migrate_script, monkeypatch) -> None:
    monkeypatch.setenv("DIRECT_URL", SESSION_POOLER)
    assert migrate_script.resolve_target(TRANSACTION_POOLER) == TRANSACTION_POOLER


def test_a_bare_path_source_becomes_a_sqlite_url(migrate_script, tmp_path) -> None:
    resolved = migrate_script.resolve_source(str(tmp_path / "dev.db"))
    assert resolved.startswith("sqlite:///")
    assert resolved.endswith("dev.db")


# ---------------------------------------------------------------------------
# Writing the URL into Alembic's config
# ---------------------------------------------------------------------------


def test_a_percent_in_the_url_survives_alembics_config() -> None:
    """A password's encoded characters must not stop a migration starting.

    ``env.py`` writes the connection URL into Alembic's config, which goes
    through configparser — and configparser reads ``%`` as the start of an
    interpolation. A percent is not exotic in a URL: it is how a password
    containing ``@``, ``/`` or ``:`` is encoded, so an ordinary Supabase
    credential was enough to make ``alembic upgrade`` refuse before running a
    single revision.
    """
    from alembic.config import Config

    url = "postgresql+psycopg://user:p%40ssw%2Frd@host:5432/db"
    config = Config()
    config.set_main_option("sqlalchemy.url", alembic_ini_value(url))
    # Escaped on the way in and unescaped on the way out, so both the offline
    # and the online path in ``env.py`` see the URL that was passed.
    assert config.get_main_option("sqlalchemy.url") == url


def test_a_url_with_no_percent_is_unchanged() -> None:
    plain = "postgresql+psycopg://user:secret@host:5432/db"
    assert alembic_ini_value(plain) == plain


def test_connection_options_survive_too() -> None:
    """``options=-csearch_path%3Dx`` is how a schema is selected on psycopg.

    The same escape covers it, which is what let the whole revision chain be
    verified against an empty schema rather than only against the live database.
    """
    from alembic.config import Config

    url = "postgresql+psycopg://u:p@h:5432/db?options=-csearch_path%3Dscratch"
    config = Config()
    config.set_main_option("sqlalchemy.url", alembic_ini_value(url))
    assert config.get_main_option("sqlalchemy.url") == url
