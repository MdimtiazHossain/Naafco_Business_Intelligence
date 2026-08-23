"""Bulk insert / upsert helpers.

Fact loading uses set-based statements, never a row-by-row ORM round trip. Rows
are chunked (``Settings.etl_batch_size``) so a multi-hundred-thousand row file
never builds one enormous statement.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Iterator, Sequence, TypeVar

from sqlalchemy import Table, func, select
from sqlalchemy.orm import Session

from ..config import get_settings


T = TypeVar("T")


def chunked(rows: Sequence[T], size: int) -> Iterator[list[T]]:
    """Split any sequence into fixed-size lists.

    Generic rather than row-specific because the parameter ceiling it exists to
    respect applies to any list that becomes bind parameters — the pipeline
    chunks plain row numbers through it as well as fact rows.
    """
    for start in range(0, len(rows), size):
        yield list(rows[start:start + size])


#: Maximum bind parameters a single statement may carry, per dialect.
#:
#: The real constraint on a multi-row INSERT is the number of placeholders, not
#: the number of rows: a 24-column fact table reaches PostgreSQL's 65,535 limit
#: at ~2,700 rows and SQLite's 32,766 at ~1,300. Chunking by rows alone
#: therefore fails on a wide table — the values below are deliberately below
#: each engine's hard limit to leave room for the statement's own parameters.
PARAMETER_LIMITS: dict[str, int] = {
    "postgresql": 60_000,
    "sqlite": 30_000,
    "mysql": 60_000,
}
DEFAULT_PARAMETER_LIMIT = 20_000


def parameter_limit(session: Session) -> int:
    """Bind-parameter budget for one statement on this connection."""
    try:
        dialect = session.get_bind().dialect.name
    except Exception:  # noqa: BLE001 - an unbound session falls back to the floor
        return DEFAULT_PARAMETER_LIMIT
    return PARAMETER_LIMITS.get(dialect, DEFAULT_PARAMETER_LIMIT)


def effective_chunk_size(session: Session, rows: Sequence[dict[str, Any]],
                         configured: int) -> int:
    """Rows per statement, bounded by the dialect's parameter limit.

    Returns at least 1: a single row that somehow exceeded the budget is better
    attempted and reported than silently skipped.
    """
    if not rows:
        return max(1, configured)
    columns = max(len(row) for row in rows) or 1
    return max(1, min(configured, parameter_limit(session) // columns))


def align_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give every row in a batch the same set of keys.

    Rows in one file legitimately differ in shape: a sales file may carry
    territory codes on some lines and only an area code on others, so the
    mapper resolves ``territory_id`` for the first group and not the second.
    ``executemany`` requires one uniform key set per batch, so missing keys are
    filled with ``None``.

    Only keys that appear in at least one row are added. A column absent from
    every row — ``created_at``, say — stays absent so its server default still
    applies instead of being overwritten with NULL.
    """
    rows = list(rows)
    if len(rows) < 2:
        return rows
    keys: set[str] = set()
    for row in rows:
        keys.update(row)
    if all(len(row) == len(keys) for row in rows):
        return rows
    return [{key: row.get(key) for key in keys} for row in rows]


def bulk_insert(session: Session, table: Table, rows: Sequence[dict[str, Any]],
                chunk_size: int | None = None,
                on_chunk: Callable[[int], None] | None = None) -> int:
    """Insert rows in chunks. Returns the number of rows inserted.

    ``on_chunk`` is called with the running total after each statement. The write
    is the slowest part of a large import, so this is what lets the caller report
    it as measured progress rather than leaving the bar frozen through it.
    """
    if not rows:
        return 0
    aligned = align_rows(rows)
    size = effective_chunk_size(session, aligned,
                                chunk_size or get_settings().etl_batch_size)
    total = 0
    for chunk in chunked(aligned, size):
        session.execute(table.insert(), chunk)
        total += len(chunk)
        if on_chunk is not None:
            on_chunk(total)
    return total


def _dialect_insert(session: Session):
    """The dialect-specific ``insert()`` that supports ``ON CONFLICT``."""
    name = session.get_bind().dialect.name
    if name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
        return insert
    if name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
        return insert
    raise NotImplementedError(
        f"Upsert is not implemented for the {name!r} dialect. PostgreSQL is the "
        "supported target; SQLite is supported for tests."
    )


def bulk_upsert(session: Session, table: Table, rows: Sequence[dict[str, Any]],
                conflict_column: str, update_columns: Iterable[str] | None = None,
                chunk_size: int | None = None,
                on_chunk: Callable[[int], None] | None = None) -> tuple[int, int]:
    """Insert rows, updating any whose ``conflict_column`` already exists.

    Returns ``(inserted, updated)``. The counts come from a cheap pre-query of
    the existing keys, which is also what makes a repeated import idempotent:
    the second run reports updates, not inserts, and never duplicates a row.

    ``created_at`` is deliberately excluded from the update set so the original
    load timestamp survives; ``updated_at`` is refreshed.

    ``on_chunk`` receives the running row total after each statement, so a caller
    can report the write as it happens.

    **One conflict key must not appear twice in the same call.** The rows are
    applied as separate parameter sets, so a repeated key resolves to the last
    one written rather than raising. The pipeline de-duplicates within the file
    before it gets here — that is what ``DUPLICATE_IN_FILE`` is — so this is a
    property to preserve in callers rather than a behaviour to rely on.
    """
    if not rows:
        return 0, 0

    rows = align_rows(rows)
    size = effective_chunk_size(session, rows,
                                chunk_size or get_settings().etl_batch_size)
    insert = _dialect_insert(session)
    key_column = table.c[conflict_column]

    if update_columns is None:
        update_columns = [
            c.name for c in table.columns
            if c.name not in {conflict_column, "created_at"} and not c.primary_key
        ]
    update_columns = list(update_columns)

    # Built once, outside the loop, and executed **executemany** — the chunk is
    # passed as parameters rather than baked into the statement with
    # ``.values(chunk)``.
    #
    # This is the difference between a fast import and a slow one, and it is
    # entirely a Python cost rather than a database one. ``.values(chunk)``
    # produces a different statement for every chunk — one INSERT carrying a
    # VALUES clause for every row — so SQLAlchemy has to compile each one from
    # scratch and the compiled-statement cache never hits. Profiling a 10,000-row
    # target import put 82% of the wall clock in this function while only 4% of
    # it was spent in the database; the rest was statement compilation. Handing
    # the same rows over as parameter sets measured 10-13x faster and stores
    # exactly the same rows. ``bulk_insert`` above always did it this way.
    #
    # ``align_rows`` above is what makes it legal: executemany requires one
    # uniform key set across the batch.
    statement = insert(table)
    excluded = statement.excluded
    set_ = {name: excluded[name] for name in update_columns if name in table.c}
    if "updated_at" in table.c:
        set_["updated_at"] = func.now()
    statement = statement.on_conflict_do_update(
        index_elements=[key_column], set_=set_
    )

    inserted = updated = 0
    written = 0
    for chunk in chunked(rows, size):
        keys = [row[conflict_column] for row in chunk]
        existing = set(
            session.execute(select(key_column).where(key_column.in_(keys))).scalars()
        )
        chunk_updated = sum(1 for key in keys if key in existing)
        updated += chunk_updated
        inserted += len(chunk) - chunk_updated

        session.execute(statement, chunk)
        written += len(chunk)
        if on_chunk is not None:
            on_chunk(written)

    return inserted, updated


def existing_keys(session: Session, table: Table, column: str,
                  keys: Sequence[str], chunk_size: int | None = None) -> set[str]:
    """Which of ``keys`` already exist in ``table.column``."""
    if not keys:
        return set()
    size = chunk_size or get_settings().etl_batch_size
    found: set[str] = set()
    key_column = table.c[column]
    for start in range(0, len(keys), size):
        chunk = list(keys[start:start + size])
        found.update(
            session.execute(select(key_column).where(key_column.in_(chunk))).scalars()
        )
    return found


__all__ = ["chunked", "bulk_insert", "bulk_upsert", "existing_keys"]
