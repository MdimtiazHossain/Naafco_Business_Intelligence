"""Database engine and session management."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, create_engine, event, text
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


def get_engine(database_url: str | None = None, echo: bool = False) -> Engine:
    """Return the process-wide engine, creating it on first use.

    Passing an explicit ``database_url`` builds a fresh engine (used by tests
    against a throwaway SQLite or Postgres instance).
    """
    global _engine, _SessionFactory
    if database_url is not None:
        engine = create_engine(database_url, echo=echo, future=True,
                               pool_pre_ping=True)
        _configure_sqlite(engine)
        return engine
    if _engine is None:
        _engine = create_engine(
            get_settings().database_url, echo=echo, future=True, pool_pre_ping=True
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


__all__ = ["get_engine", "get_session_factory", "session_scope", "check_connection"]
