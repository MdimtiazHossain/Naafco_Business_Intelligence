"""Schema invariants of the agent-learning tables (revision 0025).

These are the constraints the later steps rely on being enforced by the
database rather than by the code that happens to write to it. Each one is
checked against a database built by ``alembic upgrade head``, not by
``create_all``, so what is asserted is what a real deployment gets.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect as sa_inspect, text
from sqlalchemy.exc import IntegrityError

from app.database.models_learning import (
    AliasKind,
    FeedbackRating,
    LearningSource,
    LearningStatus,
    SignalStatus,
    SignalType,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]

LEARNING_TABLES = (
    "agent_feedback",
    "agent_learning_signal",
    "agent_term_alias",
    "agent_example",
)


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option(
        "script_location", str(BACKEND_DIR / "app" / "database" / "migrations")
    )
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture()
def migrated_engine(tmp_path, monkeypatch):
    """A throwaway SQLite database at head, with foreign keys enforced.

    ``PRAGMA foreign_keys=ON`` matters here: without it SQLite accepts a
    foreign key pointing at nothing and the ON DELETE behaviour these tables
    rely on would pass vacuously.
    """
    url = f"sqlite:///{tmp_path / 'learning.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(_alembic_config(url), "head")

    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    try:
        yield engine
    finally:
        engine.dispose()


def _insert_alias(connection, *, phrase: str, status: str,
                  active_key: str | None) -> None:
    connection.execute(
        text(
            "INSERT INTO agent_term_alias "
            "(phrase, alias_kind, status, source, active_key) "
            "VALUES (:phrase, :kind, :status, :source, :active_key)"
        ),
        {
            "phrase": phrase,
            "kind": AliasKind.ENTITY,
            "status": status,
            "source": LearningSource.MANUAL,
            "active_key": active_key,
        },
    )


def test_the_migration_creates_all_four_learning_tables(migrated_engine) -> None:
    tables = set(sa_inspect(migrated_engine).get_table_names())
    assert set(LEARNING_TABLES) <= tables


def test_an_alias_phrase_may_be_retired_many_times_over(migrated_engine) -> None:
    """Many non-active rows share a phrase; their ``active_key`` is NULL.

    This is the half of the constraint that a plain unique over
    ``(phrase, language, alias_kind)`` would have broken: a mapping that turned
    out to be wrong has to be replaceable by a better one, so the retired rows
    must be allowed to pile up.
    """
    with migrated_engine.begin() as connection:
        for _ in range(3):
            _insert_alias(connection, phrase="chini",
                          status=LearningStatus.RETIRED, active_key=None)
        # A rejected proposal is equally keyless, and equally allowed to repeat.
        _insert_alias(connection, phrase="chini",
                      status=LearningStatus.REJECTED, active_key=None)
        _insert_alias(connection, phrase="chini",
                      status=LearningStatus.PROPOSED, active_key=None)

    with migrated_engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM agent_term_alias WHERE phrase = 'chini'")
        ).scalar()
    assert count == 5


def test_only_one_alias_may_be_active_for_a_phrase(migrated_engine) -> None:
    """The other half: at most one row may hold a given ``active_key``.

    Two active meanings for one phrase would make resolution depend on row
    order, which is the ambiguity this subsystem exists to refuse rather than
    to resolve arbitrarily.
    """
    key = f"{AliasKind.ENTITY}|bn|chini"
    with migrated_engine.begin() as connection:
        _insert_alias(connection, phrase="chini",
                      status=LearningStatus.ACTIVE, active_key=key)

    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            _insert_alias(connection, phrase="chini",
                          status=LearningStatus.ACTIVE, active_key=key)


def test_only_one_example_may_be_active_for_a_question(migrated_engine) -> None:
    """The example bank carries the same constraint, for the same reason."""
    statement = text(
        "INSERT INTO agent_example "
        "(question, normalized_question, tool_name, status, source, "
        " use_count, active_key) "
        "VALUES (:q, :nq, :tool, :status, :source, 0, :active_key)"
    )
    values = {
        "q": "aajker sales koto?",
        "nq": "aajker sales koto",
        "tool": "get_sales_summary",
        "status": LearningStatus.ACTIVE,
        "source": LearningSource.MINED,
        "active_key": "aajker sales koto",
    }
    with migrated_engine.begin() as connection:
        connection.execute(statement, values)

    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            connection.execute(statement, values)


def test_a_mined_signal_is_counted_rather_than_repeated(migrated_engine) -> None:
    """One row per (signal type, phrase). The miner increments; it never appends."""
    statement = text(
        "INSERT INTO agent_learning_signal "
        "(signal_type, normalized_phrase, occurrences, status) "
        "VALUES (:type, :phrase, 1, :status)"
    )
    values = {
        "type": SignalType.UNKNOWN_INTENT,
        "phrase": "koto tk baki ache",
        "status": SignalStatus.NEW,
    }
    with migrated_engine.begin() as connection:
        connection.execute(statement, values)

    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            connection.execute(statement, values)

    # The same phrase under a *different* kind of failure is a different
    # observation and stays a separate row.
    with migrated_engine.begin() as connection:
        connection.execute(statement, {**values, "type": SignalType.ZERO_ROWS})


def test_one_reader_gets_one_verdict_per_answer(migrated_engine) -> None:
    """Feedback is unique on (message, user), so nobody votes twice.

    The row is inserted against a real ``chat_messages`` row: the foreign key
    is enforced in this fixture, so a detached verdict would fail here anyway.
    """
    with migrated_engine.begin() as connection:
        # Written as raw SQL rather than through the ORM, so every NOT NULL
        # column whose default is Python-side has to be supplied here.
        connection.execute(text(
            "INSERT INTO app_user "
            "(user_id, username, role, status, is_active, theme, "
            " preferred_language) "
            "VALUES (1, 'ceo', 'MANAGEMENT', 'ACTIVE', 1, 'system', 'en')"
        ))
        connection.execute(text(
            "INSERT INTO chat_conversations "
            "(conversation_id, user_id, title, message_count) "
            "VALUES ('c1', 1, 't', 1)"
        ))
        connection.execute(text(
            "INSERT INTO chat_messages "
            "(message_id, conversation_id, user_id, role, message) "
            "VALUES (1, 'c1', 1, 'assistant', 'answer')"
        ))

    statement = text(
        "INSERT INTO agent_feedback "
        "(message_id, conversation_id, user_id, rating) "
        "VALUES (1, 'c1', 7, :rating)"
    )
    with migrated_engine.begin() as connection:
        connection.execute(statement, {"rating": FeedbackRating.UP})

    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            connection.execute(statement, {"rating": FeedbackRating.DOWN})


def test_feedback_cannot_be_attached_to_a_message_that_does_not_exist(
    migrated_engine,
) -> None:
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO agent_feedback "
                "(message_id, conversation_id, user_id, rating) "
                "VALUES (9999, 'nope', 1, 'UP')"
            ))
