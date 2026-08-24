"""Database engine and session management."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None

#: How long a SQLite writer waits for the write lock before giving up, in ms.
#: Named rather than inlined because the upload endpoint's error message depends
#: on the distinction between "waited and still could not write" and "failed
#: instantly", and a reader of that code needs to find this number.
SQLITE_BUSY_TIMEOUT_MS = 30_000


def _configure_sqlite(engine: Engine) -> None:
    """Put SQLite in WAL so a reader is not blocked by a running import.

    An ETL run is one transaction and holds SQLite's single write lock for its
    whole duration. Under the default rollback journal that also blocks *readers*,
    so every request arriving during a large import — including the progress poll
    that exists to report on it, which re-reads the user's role like any other —
    would stall until the import finished. WAL lets readers proceed against the
    last committed state while the writer works, which is the behaviour the code
    already assumes it has on PostgreSQL.

    Applied per connection because the pool opens more than one, and only for
    SQLite; PostgreSQL needs none of it.
    """
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            # Wait rather than fail when a write lock is genuinely held; without
            # it a concurrent writer raises "database is locked" immediately.
            #
            # Deliberately *not* sized to outlast an import. A 9,229-row upload
            # holds its single transaction for around 110 seconds and a large one
            # far longer, so no timeout short enough to keep a request responsive
            # can cover that case — the endpoint answers 409 for it instead (see
            # ``api.routes_data_upload._write_conflict``). What this covers is the
            # brief contention that used to fail for no good reason: an audit row
            # written while a small import commits, two quick uploads landing
            # together. 30s is comfortably above those and still well inside any
            # sane client timeout.
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        finally:
            cursor.close()


#: Port Supabase's Supavisor pooler serves *transaction* mode on.
#:
#: Supabase publishes three ways in and only the port tells the last two apart,
#: which is why this is a port number rather than a host pattern:
#:
#: * the direct connection, 5432 on ``db.<ref>.supabase.co`` — IPv6-only unless
#:   the project buys the IPv4 add-on, so an IPv4 host cannot reach it at all;
#: * the pooler in *session* mode, 5432 on ``…pooler.supabase.com`` — one server
#:   connection held for the life of the client connection, so it behaves like a
#:   plain PostgreSQL and is what migrations must use;
#: * the pooler in *transaction* mode, 6543 on the same host — a server
#:   connection per transaction, which is what makes it scale and also what
#:   makes a prepared statement unusable (see ``_engine_options``).
TRANSACTION_POOLER_PORT = 6543


def assert_migration_safe(database_url: str) -> None:
    """Raise when a migration must not be run over this URL.

    Lives here rather than in Alembic's ``env.py`` because it is a fact about
    ``TRANSACTION_POOLER_PORT``, and that is declared here; ``env.py`` is the
    caller, not the owner. It also makes the rule reachable from a test without
    running a migration.

    Behind a transaction-mode pooler each transaction may be handed a different
    server connection, and a migration depends on one connection twice over: the
    DDL and the guards that decide whether to apply it belong to a single
    transaction, and the advisory lock Alembic takes to stop two migrations
    racing is held *by a connection* -- released the moment the pooler moves the
    session elsewhere, which reduces the lock to a formality. A half-applied
    revision on a live warehouse is the outcome this refusal exists to prevent.
    """
    try:
        url = make_url(database_url)
    except Exception:  # noqa: BLE001 - an unparseable URL is the engine's error to raise
        return
    if not url.drivername.startswith("postgresql"):
        return
    if url.port != TRANSACTION_POOLER_PORT:
        return
    raise RuntimeError(
        f"Refusing to migrate through port {TRANSACTION_POOLER_PORT}, which is the "
        "transaction-mode pooler: DDL and Alembic's version lock both need one "
        "connection for the whole migration and transaction mode does not promise "
        "one. Set DIRECT_URL to the session-mode pooler (port 5432 on the same "
        "pooler host) or to the direct connection, and leave DATABASE_URL pointing "
        "at the transaction pooler for the application."
    )


def _engine_options(database_url: str) -> dict[str, Any]:
    """Extra ``create_engine`` options this URL needs.

    SQLite needs none — its pool settings are wrong for a file and its pragmas
    are applied per connection by ``_configure_sqlite`` instead. PostgreSQL gets
    the pool sizing from configuration, and behind Supavisor's transaction-mode
    pooler it additionally has to stop psycopg preparing statements.

    psycopg prepares a statement after it has seen it five times, then refers to
    it by name on the connection that prepared it. In transaction mode the next
    transaction may land on a *different* server connection, where that name was
    never declared, so the query fails with ``prepared statement "_pg3_0" does
    not exist`` — and only under load, once something has run five times, which
    is the worst possible way to discover it. ``prepare_threshold=None`` turns
    the mechanism off; nothing else in this application depends on it.
    """
    try:
        url = make_url(database_url)
    except Exception:  # noqa: BLE001 - an unparseable URL is create_engine's error to raise
        return {}
    if not url.drivername.startswith("postgresql"):
        return {}

    settings = get_settings()
    options: dict[str, Any] = {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_recycle": settings.db_pool_recycle_seconds,
    }
    if url.port == TRANSACTION_POOLER_PORT:
        options["connect_args"] = {"prepare_threshold": None}
    return options


def get_engine(database_url: str | None = None, echo: bool = False) -> Engine:
    """Return the process-wide engine, creating it on first use.

    Passing an explicit ``database_url`` builds a fresh engine (used by tests
    against a throwaway SQLite or Postgres instance).
    """
    global _engine, _SessionFactory
    if database_url is not None:
        engine = create_engine(database_url, echo=echo, future=True,
                               pool_pre_ping=True, **_engine_options(database_url))
        _configure_sqlite(engine)
        return engine
    if _engine is None:
        url = get_settings().database_url
        _engine = create_engine(
            url, echo=echo, future=True, pool_pre_ping=True, **_engine_options(url)
        )
        _configure_sqlite(_engine)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _SessionFactory is None:
        get_engine()
    assert _SessionFactory is not None
    return _SessionFactory


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception."""
    factory = (
        sessionmaker(bind=engine, expire_on_commit=False, future=True)
        if engine is not None
        else get_session_factory()
    )
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_connection(engine: Engine | None = None) -> bool:
    """True when the database answers ``SELECT 1``."""
    engine = engine or get_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


__all__ = [
    "get_engine",
    "get_session_factory",
    "session_scope",
    "check_connection",
    "assert_migration_safe",
    "TRANSACTION_POOLER_PORT",
]
