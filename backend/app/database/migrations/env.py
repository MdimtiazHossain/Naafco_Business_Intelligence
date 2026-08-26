"""Alembic environment.

The database URL comes from ``app.config`` (environment / ``.env``) so that no
credentials live in ``alembic.ini``. ``DIRECT_URL`` overrides it here — and only
here — because a migration cannot run through a transaction-mode pooler; see
``_migration_url``.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
import sqlalchemy as sa
from sqlalchemy import engine_from_config, pool

# <repo>/backend on sys.path so "app.*" imports resolve when alembic runs.
BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings  # noqa: E402
from app.database.connection import assert_migration_safe  # noqa: E402
from app.database.models import Base  # noqa: E402
from app.database import models_warehouse  # noqa: E402,F401  (registers Phase 2 tables)
from app.database import models_ai  # noqa: E402,F401  (registers Phase 3 tables)
from app.database import models_admin  # noqa: E402,F401  (registers Phase 4 tables)
from app.database import models_map  # noqa: E402,F401  (registers map-config tables)
from app.database import models_learning  # noqa: E402,F401  (registers agent-learning tables)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _migration_url() -> str:
    """The URL migrations run on, which is not always the one the app uses.

    ``DIRECT_URL`` overrides ``DATABASE_URL`` here and nowhere else. Why a
    transaction pooler is refused belongs with the port it is about, so the rule
    is ``connection.assert_migration_safe`` rather than a second copy here.
    """
    url = os.getenv("DIRECT_URL") or get_settings().database_url
    assert_migration_safe(url)
    return url


config.set_main_option("sqlalchemy.url", _migration_url())

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


#: Width the version table's column needs, and why it is not Alembic's default.
#:
#: Alembic hard-codes ``version_num`` as ``VARCHAR(32)``. Revision identifiers
#: here are descriptive rather than hashes, and the longest —
#: ``0011_customer_subterritory_product_company`` — is 42 characters. SQLite does
#: not enforce a declared length, so every revision stamped fine there and the
#: mismatch stayed invisible; PostgreSQL enforces it and refuses the stamp *after*
#: the revision's own DDL has succeeded, which rolls the whole revision back and
#: makes it look like the migration failed.
#:
#: 128 rather than 43: the ceiling should not need revisiting the next time a
#: revision is given a longer name.
VERSION_NUM_WIDTH = 128


def _widen_version_table(connection) -> None:  # noqa: ANN001
    """Make ``alembic_version.version_num`` wide enough for this project's ids.

    Runs before Alembic looks at the table. If it does not exist yet it is
    created here — empty, which is exactly what "no revision applied" means, so
    Alembic then adopts it instead of creating a 32-character one. If it exists
    and is narrower, it is widened in place; a ``VARCHAR`` widening rewrites no
    data and loses nothing.

    PostgreSQL only. SQLite ignores the declared length entirely, so doing this
    there would be ceremony with no effect.
    """
    if connection.dialect.name != "postgresql":
        return
    connection.execute(
        sa.text(
            "CREATE TABLE IF NOT EXISTS alembic_version ("
            f"version_num VARCHAR({VERSION_NUM_WIDTH}) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        )
    )
    connection.execute(
        sa.text(
            "ALTER TABLE alembic_version ALTER COLUMN version_num "
            f"TYPE VARCHAR({VERSION_NUM_WIDTH})"
        )
    )


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _widen_version_table(connection)
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
