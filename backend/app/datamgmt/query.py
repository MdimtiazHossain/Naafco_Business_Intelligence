"""Listing records: search, filter, sort and page — all on the server.

The warehouse holds thousands of customers and can hold millions of
transactions, so nothing here loads a table into memory to work on it. Every
operation becomes a clause:

* search is one ``OR`` of ``ILIKE`` over the entity's declared searchable
  columns, never a scan of every column;
* filters and sorts are looked up in the entity's own field list, so no value
  from the client ever becomes a column name;
* the total is a ``COUNT`` over the same predicates as the page, so "1–50 of
  12,450" is the count of what the caller may actually see, not of the table.

The scope filter is applied first, before any other predicate, because it is
the one that must never be optional.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Table, and_, asc, desc, func, or_, select
from sqlalchemy.orm import Session

from ..ai import queries as q
from ..ai.permission_filter import UserContext
from ..ai.schemas import ScopeFilters
from .catalogue import ManagedEntity
from .scope import ScopeResult, resolve as resolve_scope

#: Page sizes the API accepts, mirroring what the UI offers.
PAGE_SIZES: tuple[int, ...] = (10, 25, 50, 100)
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200


class QueryProblem(ValueError):
    """The request asked for something the entity does not offer."""


@dataclass
class ListRequest:
    """Everything that shapes one page of a table."""

    search: str | None = None
    filters: dict[str, str] = field(default_factory=dict)
    sort_by: str | None = None
    sort_dir: str = "asc"
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    #: Master only: whether retired records are included.
    include_deleted: bool = False
    #: Transactions only: whether voided records are included.
    include_voided: bool = False
    #: Transactions only.
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    business_filters: ScopeFilters | None = None
    #: Restrict to specific records, for "show selected on the map" and export
    #: of a selection.
    codes: tuple[str, ...] = ()

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


@dataclass
class ListResult:
    rows: list[dict[str, Any]]
    total: int
    page: int
    page_size: int
    scope_description: str
    columns: list[str]

    @property
    def total_pages(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)

    def to_dict(self) -> dict[str, Any]:
        start = 0 if not self.total else (self.page - 1) * self.page_size + 1
        return {
            "rows": self.rows,
            "columns": self.columns,
            "page": self.page,
            "page_size": self.page_size,
            "total": self.total,
            "total_pages": self.total_pages,
            # Precomputed so "Showing 1–50 of 12,450" cannot drift between the
            # two table pages that render it.
            "range_from": start,
            "range_to": min(self.total, self.page * self.page_size),
            "scope_description": self.scope_description,
        }


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------


def list_master(session: Session, user: UserContext, entity: ManagedEntity,
                request: ListRequest) -> ListResult:
    """One page of a master dimension."""
    model = entity.model
    scope = resolve_scope(session, user, entity)

    conditions = _master_conditions(entity, request, scope)
    statement = select(model)
    if conditions:
        statement = statement.where(and_(*conditions))

    total = session.execute(
        select(func.count()).select_from(model.__table__)
        .where(and_(*conditions)) if conditions
        else select(func.count()).select_from(model.__table__)
    ).scalar_one()

    statement = statement.order_by(*_master_order(entity, request))
    records = session.execute(
        statement.limit(request.page_size).offset(request.offset)
    ).scalars().all()

    return ListResult(
        rows=[master_row(entity, record) for record in records],
        total=total,
        page=request.page,
        page_size=request.page_size,
        scope_description=scope.description,
        columns=[f.name for f in entity.fields],
    )


def _master_conditions(entity: ManagedEntity, request: ListRequest,
                       scope: ScopeResult) -> list:
    model = entity.model
    conditions: list = []

    # Scope first, and unconditionally.
    if not scope.unrestricted:
        key = getattr(model, entity.key_fields[0])
        conditions.append(key.in_(sorted(scope.codes or {""})) if scope.codes
                          else key.is_(None))
        if scope.empty:
            # ``code IS NULL`` on a NOT NULL column: an always-false predicate
            # that still runs as SQL, so the count and the page agree.
            conditions[-1] = key.is_(None)

    # Only for an entity that models retirement. A coordinate has no
    # ``is_deleted`` column — it is removed outright — and asking for one would
    # be an AttributeError rather than an empty table.
    if entity.soft_delete and not request.include_deleted:
        conditions.append(model.is_deleted.is_(False))

    if request.codes:
        conditions.append(or_(*(
            and_(*key_conditions(entity, code)) for code in request.codes
        )))

    if request.search:
        needle = f"%{request.search.strip()}%"
        searchable = [
            getattr(model, name).ilike(needle)
            for name in _searchable_master_fields(entity)
        ]
        if searchable:
            conditions.append(or_(*searchable))

    for name, value in request.filters.items():
        column = _master_column(entity, name)
        conditions.append(column == value)

    return conditions


def _searchable_master_fields(entity: ManagedEntity) -> list[str]:
    """Text-ish columns worth searching.

    Codes, names, phone numbers and free-text attributes — not dates or
    numbers, where a substring match would be meaningless.
    """
    return [
        f.name for f in entity.fields
        if f.kind in ("code", "text", "phone")
        and hasattr(entity.model, f.name)
    ]


def _master_column(entity: ManagedEntity, name: str):
    field_spec = entity.field_by_name.get(name)
    if field_spec is None or not hasattr(entity.model, name):
        raise QueryProblem(
            f"'{entity.label}' has no field '{name}'. Available: "
            f"{', '.join(f.name for f in entity.fields)}."
        )
    return getattr(entity.model, name)


def _master_order(entity: ManagedEntity, request: ListRequest) -> list:
    """How the page is ordered, always down to something unique.

    A composite-key entity ordered by its first key column alone has no stable
    order at all — every coordinate of a customer shares the entity type
    ``customer`` — and an unstable order under LIMIT/OFFSET repeats rows on one
    page and drops them from another. So the remaining key columns are always
    appended as the tie-break, including behind a column the reader chose to
    sort by.
    """
    direction = desc if request.sort_dir == "desc" else asc
    chosen = request.sort_by
    names = [chosen] if chosen else []
    names += [name for name in entity.key_fields if name != chosen]
    return [direction(_master_column(entity, name)) for name in names]


def master_row(entity: ManagedEntity, record: Any) -> dict[str, Any]:
    row = {f.name: q.normalize_value(getattr(record, f.name, None))
           for f in entity.fields}
    # The lifecycle columns are not editable fields, but the table needs them to
    # show a retired record differently from a live one.
    row["is_deleted"] = bool(getattr(record, "is_deleted", False))
    row["deleted_at"] = getattr(record, "deleted_at", None)
    row["deleted_by"] = getattr(record, "deleted_by", None)
    row["_key"] = _record_key(entity, record)
    return row


#: What joins the parts of a composite business key into one URL segment.
#:
#: A single character that appears in no business code in this warehouse, so
#: splitting is unambiguous, and one that survives ``encodeURIComponent`` as
#: ``%7C`` rather than being mistaken for a path separator.
KEY_SEPARATOR = "|"


def _record_key(entity: ManagedEntity, record: Any) -> str:
    return KEY_SEPARATOR.join(
        str(getattr(record, name)) for name in entity.key_fields
    )


def split_record_key(entity: ManagedEntity, code: str) -> dict[str, str]:
    """One URL segment back into the key columns it names.

    Most dimensions are keyed on a single code and this is the identity. Three
    are not — ``dim_plant`` on company + plant, ``dim_storage_location`` on
    plant + location, and ``map_entity_locations`` on entity type + entity code
    — and for those, reading only the first part would silently answer with
    *some* record sharing it rather than the one asked for.
    """
    parts = str(code).split(KEY_SEPARATOR)
    if len(parts) != len(entity.key_fields):
        expected = KEY_SEPARATOR.join(entity.key_fields)
        raise QueryProblem(
            f"'{entity.label}' is identified by {expected}, so "
            f"'{code}' does not name one record."
        )
    return dict(zip(entity.key_fields, parts))


def key_conditions(entity: ManagedEntity, code: str) -> list:
    """The WHERE clauses that address exactly one record."""
    values = split_record_key(entity, code)
    return [getattr(entity.model, name) == value
            for name, value in values.items()]


def get_master_record(session: Session, entity: ManagedEntity, code: str,
                      include_deleted: bool = True) -> Any:
    """One master record by its business code, or ``None``."""
    model = entity.model
    conditions = key_conditions(entity, code)
    if entity.soft_delete and not include_deleted:
        conditions.append(model.is_deleted.is_(False))
    return session.execute(
        select(model).where(and_(*conditions))
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Transactional data
# ---------------------------------------------------------------------------


def list_transactions(session: Session, user: UserContext,
                      entity: ManagedEntity, request: ListRequest,
                      ) -> ListResult:
    """One page of a transactional table.

    Reads the same detail view every report reads, through the same scope
    filter, so a row visible here is a row visible on the Sales page and vice
    versa. The one difference is ``include_voided``: the management table can
    show what the reports must not.
    """
    from ..api.routes_dashboard import tool_context

    context = tool_context(session, user)
    scoped = context.scoped(request.business_filters or ScopeFilters())
    table = _table_for(session, entity, request.include_voided)

    conditions = q.filter_conditions(table, scoped, request.date_from,
                                     request.date_to)
    conditions += _transaction_conditions(table, entity, request)

    columns = [table.c[name] for name in _transaction_columns(table, entity)]
    base = select(*columns).select_from(table)
    if conditions:
        base = base.where(and_(*conditions))

    total = session.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()

    base = base.order_by(_transaction_order(table, entity, request))
    rows = q.normalize_rows([
        dict(row._mapping)
        for row in session.execute(
            base.limit(request.page_size).offset(request.offset)
        )
    ])
    for row in rows:
        row["_key"] = str(row.get(entity.id_field))

    return ListResult(
        rows=rows,
        total=total,
        page=request.page,
        page_size=request.page_size,
        scope_description=user.describe_scope(),
        columns=_transaction_columns(table, entity),
    )


def _table_for(session: Session, entity: ManagedEntity,
               include_voided: bool) -> Table:
    """The detail view, or the fact table when voided rows are wanted.

    The views exist precisely to hide voided rows, so asking them to show one is
    a contradiction. Showing voided rows therefore reads the fact table — which
    is why that path is only reachable with the transaction-management section
    and never from a report.
    """
    if not include_voided:
        return q.view(session, entity.view_name or "")
    return entity.fact_model.__table__


def _transaction_columns(table: Table, entity: ManagedEntity) -> list[str]:
    """The declared columns this source actually carries.

    The fact table and the detail view expose different columns — the view
    resolves the organisational names, the fact table holds only the keys — so
    the list is intersected with whichever is in use rather than assumed.
    """
    names = [f.name for f in entity.fields if f.name in table.c]
    identifier = entity.id_field or ""
    if identifier in table.c and identifier not in names:
        names.insert(0, identifier)
    for lifecycle in ("is_void", "voided_at", "voided_by", "void_reason"):
        if lifecycle in table.c:
            names.append(lifecycle)
    return names


def _transaction_conditions(table: Table, entity: ManagedEntity,
                            request: ListRequest) -> list:
    conditions: list = []

    if request.search:
        needle = f"%{request.search.strip()}%"
        searchable = [
            table.c[name].ilike(needle)
            for name in entity.search_fields if name in table.c
        ]
        if searchable:
            conditions.append(or_(*searchable))

    for name, value in request.filters.items():
        if name not in entity.filter_fields:
            raise QueryProblem(
                f"'{entity.label}' cannot be filtered by '{name}'. Available: "
                f"{', '.join(entity.filter_fields)}."
            )
        if name in table.c:
            conditions.append(table.c[name] == value)

    if request.codes and entity.id_field in table.c:
        conditions.append(table.c[entity.id_field].in_(
            [int(code) for code in request.codes if str(code).isdigit()]
        ))

    return conditions


def _transaction_order(table: Table, entity: ManagedEntity,
                       request: ListRequest):
    name = request.sort_by
    if name is not None and name not in {f.name for f in entity.fields}:
        raise QueryProblem(
            f"Cannot sort '{entity.label}' by '{name}'. Available: "
            f"{', '.join(f.name for f in entity.fields)}."
        )
    if name is None or name not in table.c:
        # Newest first is the only useful default for a transaction table.
        name = "full_date" if "full_date" in table.c else (entity.id_field or "")
    column = table.c[name]
    return desc(column) if request.sort_dir == "desc" else asc(column)


def get_transaction_record(session: Session, entity: ManagedEntity,
                           record_id: int) -> Any:
    """One fact row by its primary key. Reads the table, so a voided row is
    still retrievable — its detail page is how someone finds out why."""
    return session.get(entity.fact_model, record_id)


__all__ = [
    "ListRequest",
    "ListResult",
    "QueryProblem",
    "PAGE_SIZES",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "master_row",
    "list_master",
    "list_transactions",
    "get_master_record",
    "get_transaction_record",
]
