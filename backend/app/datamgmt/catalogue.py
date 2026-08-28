"""What can be managed, and by what rules.

The catalogue is **derived**, not hand-written. Master entities come from
:mod:`app.upload.registry`, which already derives them from the Phase 1 sheet
contract and the ORM models; transactional entities come from the same detail
views and column whitelists the transaction table has always used. So a column
added to a dimension, or a field added to a dataset, appears in the management
table, its edit form and its export with nothing else to change.

What this module *adds* is the part managing a record needs and uploading one
does not:

``scope_level``
    Which organisational level a row belongs to, so the data scope can be
    applied to the query. ``None`` means the entity is not scope-bearing — a
    product belongs to no region, so scoping it would hide products rather than
    protect anything.

``editable`` / ``immutable``
    The business code identifies the record everywhere in the warehouse,
    including on facts already loaded, so it is never editable. Changing it
    would not rename a customer; it would silently orphan their history.

``status_field``
    Where "active" lives for entities that model it, so deactivating can use the
    existing column rather than a second, competing notion of the same thing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..database.models_warehouse import FACT_MODEL_BY_DATA_TYPE
from ..reporting.columns import (
    SEARCH_COLUMNS,
    SECTION_BY_TYPE,
    TRANSACTION_COLUMNS,
    VIEW_BY_TYPE,
)
from ..security.sections import SectionKey
from ..upload.registry import (
    display_group,
    MASTER_MODEL_BY_TABLE,
    MASTER_TYPES,
    UploadColumn,
    UploadType,
)

MASTER = "MASTER"
TRANSACTION = "TRANSACTION"

#: Columns present on every managed record that the user never edits directly.
SYSTEM_COLUMNS: frozenset[str] = frozenset({
    "created_at", "updated_at", "is_deleted", "deleted_at", "deleted_by",
    "is_void", "voided_at", "voided_by", "void_reason", "is_placeholder",
})

#: Master table -> the organisational level a row of it *is*. Used both to scope
#: the list and to answer "may this user touch this record".
_SCOPE_LEVEL_BY_TABLE: dict[str, str] = {
    "dim_company": "company_code",
    "dim_business_unit": "bu_code",
    "dim_sales_line": "sales_line_code",
    "dim_zone": "zone_code",
    "dim_region": "region_code",
    "dim_area": "area_code",
    "dim_unit": "unit_code",
    "dim_territory": "territory_code",
    "dim_sub_territory": "sub_territory_code",
}

#: Business dimensions with no organisational column of their own. Their scope
#: is derived from the facts, exactly as the map's hierarchy resolver does it.
_FACT_SCOPED_TABLES: dict[str, str] = {
    "dim_customer": "customer",
    "dim_sales_force": "sales_force",
}

#: Where "is this record active" is recorded, for the entities that model it.
_STATUS_FIELD: dict[str, str] = {
    "dim_customer": "status",
    "dim_sales_force": "status",
}

#: Values ``status`` may take. Free text in the source data, so the management
#: UI offers these and accepts what is already stored.
STATUS_VALUES: tuple[str, ...] = ("Active", "Inactive")

#: Which section governs each transactional type, and which view it reads —
#: literally the same maps the transaction report table uses, so a user denied
#: Stock is denied it here too and a row visible on one is visible on the other.
_SECTION_BY_DATA_TYPE = SECTION_BY_TYPE
_VIEW_BY_DATA_TYPE = VIEW_BY_TYPE

#: The fact table's own primary key, which the detail view exposes and the
#: management API addresses a single record by.
_FACT_PK: dict[str, str] = {
    "sales": "sales_id",
    "material_stock": "material_stock_id",
    "target": "target_id",
}

#: The handful of measure columns a correction may legitimately change.
#:
#: Deliberately narrow. Re-pointing a transaction at a different customer or
#: date is not a correction, it is a different transaction — and the
#: organisational keys are derived by the ETL from the master hierarchy, so
#: editing them here would produce a row the pipeline could never reproduce.
#: ``volume`` is editable on sales because it is now an ordinary
#: source measure: the file states one Total Volume per line and nothing else
#: contributes to it, so correcting it here corrects it completely. While it was
#: derived from the Product Master's pack size a correction made on this screen
#: would have disagreed with the product it was computed from.
_EDITABLE_MEASURES: dict[str, tuple[str, ...]] = {
    "sales": ("quantity", "volume", "gross_sales", "discount", "net_sales",
              "cost"),
    # The four categories a correction may legitimately change. The two dates
    # are attributes of the goods and the location and material codes are the
    # row's identity, so neither is editable here: changing them would make the
    # row a different position rather than a corrected one. Group and brand are
    # not editable either — they belong to the material, so they are corrected
    # in the Material Master, once, for every position that names it.
    "material_stock": ("unrestricted_stock", "quality_inspection_stock",
                       "blocked_stock", "stock_in_transit"),
    # The three measures, and only those. The volume's unit is the SKU's pack
    # unit in the Product Master, so it is corrected there — editing it here
    # would put a unit on one target row that disagreed with its own product.
    "target": ("target_amount", "target_quantity", "target_volume"),
}

#: Read-only measures the record still carries even though no report shows them.
#:
#: ``gross_profit`` is derived by the ETL from ``net_sales - cost`` and left the
#: reporting surface along with gross sales and margin. It is kept here, hidden
#: by default, because this is the screen where a measure is corrected: an
#: editor that silently drops the figure a correction moves cannot show whether
#: the correction worked.
_DERIVED_MEASURES: dict[str, tuple[str, ...]] = {
    "sales": ("gross_profit",),
}

#: Filters offered above each transaction table, beyond the shared date range
#: and organisational filter bar.
_TRANSACTION_FILTERS: dict[str, tuple[str, ...]] = {
    "sales": ("customer_code", "sku_code", "sales_force_code"),
    "material_stock": ("plant_code", "storage_location_code", "material_code",
                       "material_group_code", "material_brand_code"),
    "target": ("target_month", "financial_year", "customer_code", "sku_code",
               "sales_force_code"),
}

_LABELS = {
    "sales": "Sales", "material_stock": "Material Stock", "target": "Target",
}


@dataclass(frozen=True)
class ManagedField:
    """One field of a managed record, as the table and the form see it."""

    name: str
    label: str
    kind: str
    required: bool = False
    editable: bool = True
    description: str = ""
    #: True when this field is part of the record's identity.
    is_key: bool = False
    #: Set when the field must reference an existing record.
    references_table: str | None = None
    references_column: str | None = None
    #: Shown in the table by default. The rest are available from the column
    #: picker — a customer table with twenty columns open is unreadable.
    default_visible: bool = True
    #: Constrained values, when the field has them.
    choices: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "required": self.required,
            "editable": self.editable,
            "description": self.description,
            "is_key": self.is_key,
            "references": self.references_table,
            "references_column": self.references_column,
            "default_visible": self.default_visible,
            "choices": list(self.choices),
        }


@dataclass(frozen=True)
class ManagedEntity:
    """One thing that appears in the Data Management menu."""

    key: str
    label: str
    category: str
    description: str
    fields: tuple[ManagedField, ...]
    #: The columns that identify a record, in URL order.
    key_fields: tuple[str, ...]
    #: Section a caller must hold, and whose actions gate every operation.
    section: str

    # -- master ------------------------------------------------------------
    table: str | None = None
    model: Any = None
    label_field: str | None = None
    parent_table: str | None = None
    parent_column: str | None = None
    scope_level: str | None = None
    #: For customer / sales force: the map hierarchy's entity type, whose scope
    #: is resolved from the facts.
    fact_scope_type: str | None = None
    status_field: str | None = None
    soft_delete: bool = True
    supports_geo: bool = False

    # -- transactional -----------------------------------------------------
    data_type: str | None = None
    view_name: str | None = None
    fact_model: Any = None
    id_field: str | None = None
    search_fields: tuple[str, ...] = ()
    filter_fields: tuple[str, ...] = ()
    voidable: bool = False

    @property
    def is_master(self) -> bool:
        return self.category == MASTER

    @property
    def field_by_name(self) -> dict[str, ManagedField]:
        return {f.name: f for f in self.fields}

    @property
    def editable_fields(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.editable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "category": self.category,
            # Taken from the upload registry rather than restated here: the two
            # screens have to group identically, and one source is the only way
            # that stays true.
            "group": display_group(self.key),
            "description": self.description,
            "section": self.section,
            "key_fields": list(self.key_fields),
            "label_field": self.label_field,
            "fields": [f.to_dict() for f in self.fields],
            "default_columns": [f.name for f in self.fields if f.default_visible],
            "status_field": self.status_field,
            "status_values": list(STATUS_VALUES) if self.status_field else [],
            "soft_delete": self.soft_delete,
            "voidable": self.voidable,
            "supports_geo": self.supports_geo,
            "scope_level": self.scope_level,
            "parent_table": self.parent_table,
            "parent_column": self.parent_column,
            "filter_fields": list(self.filter_fields),
            "search_fields": list(self.search_fields),
            "data_type": self.data_type,
            "table": self.table,
        }


# ---------------------------------------------------------------------------
# Master entities
# ---------------------------------------------------------------------------

#: Fields worth showing in a narrow table. Anything else starts hidden.
#:
#: ``_description`` earns its place for the same reason ``_name`` does: it is
#: what a record is *called*, and a table of codes with no words in it cannot be
#: read. It was not needed while the Material Master led with its code and
#: description — both were inside the ``index < 3`` window below — but revision
#: 0023 put Company Code first and pushed the description to seventh, which
#: would have hidden the only human-readable column in the table.
#: Suffixes and exact column names that start a column *visible* rather than
#: behind the column picker.
#:
#: ``conversion_factor`` and ``transfer_price`` are named outright, and are
#: worth the exception. They arrived nullable with no back-fill in revision
#: 0027, so on most deployments they are the two columns a reader most needs to
#: see are **empty** — Target Management derives Quantity and Value from them
#: and reports n/a without them. A column hidden by default gives no hint that
#: it exists, let alone that it is unfilled.
_PROMOTED_SUFFIXES = ("_code", "_name", "_description", "status",
                      "customer_type", "brand", "category", "mobile",
                      "designation", "territory_code",
                      "conversion_factor", "transfer_price")


def _master_field(column: UploadColumn, upload_type: UploadType,
                  index: int) -> ManagedField:
    is_key = column.target in upload_type.business_key
    # Two ways a column can reference another master: it is this dimension's
    # structural parent, or it is a declared lookup. Both give the form a list
    # to pick from instead of a free-text box the user can mistype.
    lookup = upload_type.lookup_by_column.get(column.target)
    references_table = (
        upload_type.parent_table
        if column.target == upload_type.parent_column
        else (lookup.table if lookup else None)
    )
    references_column = (
        upload_type.parent_column
        if column.target == upload_type.parent_column
        else (lookup.target_column if lookup else None)
    )
    promoted = (
        is_key
        or index < 3
        or any(column.target.endswith(suffix) for suffix in _PROMOTED_SUFFIXES)
    )
    return ManagedField(
        name=column.target,
        label=column.name,
        kind=column.kind,
        required=column.required,
        # The business code is the record's identity across the whole
        # warehouse. Editing it would not rename anything; it would detach the
        # record from every fact that references it.
        editable=not is_key,
        description=column.description,
        is_key=is_key,
        references_table=references_table,
        references_column=references_column,
        default_visible=promoted,
        choices=STATUS_VALUES if column.target == "status" else (),
    )


def _name_field(upload_type: UploadType) -> str | None:
    for column in upload_type.columns:
        if column.target.endswith("_name") or column.target.endswith("_name_en"):
            return column.target
    return None


def _master_entity(upload_type: UploadType) -> ManagedEntity | None:
    model = MASTER_MODEL_BY_TABLE.get(upload_type.table or "")
    if model is None or upload_type.table == "map_entity_locations":
        # Coordinates are managed by the map's own settings screens, which know
        # about centroid derivation and the marker cache. Duplicating them here
        # would give two places to change one thing.
        return None

    fields = tuple(
        _master_field(column, upload_type, index)
        for index, column in enumerate(upload_type.columns)
    )
    table = upload_type.table or ""
    return ManagedEntity(
        key=table,
        label=upload_type.label,
        category=MASTER,
        description=upload_type.description,
        fields=fields,
        key_fields=upload_type.business_key,
        section=SectionKey.MASTER_DATA,
        table=table,
        model=model,
        label_field=_name_field(upload_type),
        parent_table=upload_type.parent_table,
        parent_column=upload_type.parent_column,
        scope_level=_SCOPE_LEVEL_BY_TABLE.get(table),
        fact_scope_type=_FACT_SCOPED_TABLES.get(table),
        status_field=_STATUS_FIELD.get(table),
        # Every organisational level and every business dimension can carry a
        # coordinate, so every one of them can offer "View on map".
        supports_geo=table in _SCOPE_LEVEL_BY_TABLE or table in _FACT_SCOPED_TABLES,
    )


# ---------------------------------------------------------------------------
# Transactional entities
# ---------------------------------------------------------------------------

_NUMERIC_HINTS = ("amount", "quantity", "qty", "sales", "cost", "discount",
                  "stock", "profit", "paid", "overdue")
_DATE_HINTS = ("date",)


def _column_kind(name: str) -> str:
    if any(hint in name for hint in _DATE_HINTS):
        return "date"
    if any(hint in name for hint in _NUMERIC_HINTS):
        return "numeric"
    return "text"


def _humanize(column: str) -> str:
    words = column.replace("_", " ").split()
    return " ".join(
        word.upper() if word in {"id", "sku", "bu", "hq", "no"} else word.capitalize()
        for word in words
    )


def _transaction_entity(data_type: str) -> ManagedEntity:
    editable = _EDITABLE_MEASURES[data_type]
    # The report table's columns, plus any correctable measure it happens not to
    # show. Sales is the case: ``cost`` is not on the report — it is commercially
    # sensitive and gross profit says what the reader needs — but it is exactly
    # the kind of figure a correction targets, and a measure that cannot be
    # displayed cannot be corrected either.
    extra = (*editable, *_DERIVED_MEASURES.get(data_type, ()))
    columns = (*TRANSACTION_COLUMNS[data_type],
               *(name for name in extra
                 if name not in TRANSACTION_COLUMNS[data_type]))
    fields = tuple(
        ManagedField(
            name=name,
            label=_humanize(name),
            kind=_column_kind(name),
            editable=name in editable,
            description=(
                "Correctable measure." if name in editable
                else "Read-only: set by the import pipeline from the source data."
            ),
            # Measures added only for correction start hidden, so the table
            # still reads like the report it mirrors.
            default_visible=name in TRANSACTION_COLUMNS[data_type],
        )
        for name in columns
    )
    return ManagedEntity(
        key=data_type,
        label=_LABELS.get(data_type, data_type.title()),
        category=TRANSACTION,
        description=(
            f"{_LABELS.get(data_type, data_type.title())} transactions as loaded "
            "into the warehouse. Records are voided, never deleted."
        ),
        fields=fields,
        key_fields=(_FACT_PK[data_type],),
        # Two sections must both pass: TRANSACTION_DATA to reach the management
        # tables at all, and the data type's own reporting section, so a user
        # denied Stock cannot read stock rows through a different door.
        section=SectionKey.TRANSACTION_DATA,
        data_type=data_type,
        view_name=_VIEW_BY_DATA_TYPE[data_type],
        fact_model=FACT_MODEL_BY_DATA_TYPE[data_type],
        id_field=_FACT_PK[data_type],
        label_field="invoice_no" if "invoice_no" in columns else None,
        search_fields=SEARCH_COLUMNS[data_type],
        filter_fields=_TRANSACTION_FILTERS[data_type],
        soft_delete=False,
        voidable=True,
    )


def report_section(data_type: str) -> str:
    """The reporting section that governs a transactional data type."""
    return _SECTION_BY_DATA_TYPE[data_type]


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

MASTER_ENTITIES: tuple[ManagedEntity, ...] = tuple(
    entity for entity in (_master_entity(t) for t in MASTER_TYPES)
    if entity is not None
)

TRANSACTION_ENTITIES: tuple[ManagedEntity, ...] = tuple(
    _transaction_entity(data_type) for data_type in TRANSACTION_COLUMNS
)

ENTITIES: tuple[ManagedEntity, ...] = (*MASTER_ENTITIES, *TRANSACTION_ENTITIES)

#: Master and transactional keys are namespaced separately, because
#: ``dim_customer`` and ``sales`` are addressed through different URL prefixes
#: and one lookup that mixed them would let ``/api/master/sales`` resolve.
MASTER_BY_KEY: dict[str, ManagedEntity] = {e.key: e for e in MASTER_ENTITIES}
TRANSACTION_BY_KEY: dict[str, ManagedEntity] = {
    e.key: e for e in TRANSACTION_ENTITIES
}


class UnknownEntity(LookupError):
    """Asked for something the catalogue does not describe."""


def get_master(key: str) -> ManagedEntity:
    entity = MASTER_BY_KEY.get(key)
    if entity is None:
        raise UnknownEntity(
            f"Unknown master entity '{key}'. Available: "
            f"{', '.join(sorted(MASTER_BY_KEY))}."
        )
    return entity


def get_transaction(key: str) -> ManagedEntity:
    entity = TRANSACTION_BY_KEY.get(key)
    if entity is None:
        raise UnknownEntity(
            f"Unknown transaction type '{key}'. Available: "
            f"{', '.join(sorted(TRANSACTION_BY_KEY))}."
        )
    return entity


def catalogue() -> dict[str, Any]:
    """Both groups, as the Data Management menu renders them."""
    return {
        "groups": [
            {
                "key": MASTER,
                "label": "Master Data",
                "route": "/data-management/master",
                "description": (
                    "Reference data: the organisational hierarchy, products, "
                    "materials, plants, storage locations, customers and sales "
                    "force. Records are deactivated or retired, never destroyed."
                ),
                "entities": [e.to_dict() for e in MASTER_ENTITIES],
            },
            {
                "key": TRANSACTION,
                "label": "Transaction Data",
                "route": "/data-management/transactions",
                "description": (
                    "Business transactions as loaded by the import pipeline. "
                    "Corrections are limited to measures; removal is a void, "
                    "which reverses the record everywhere at once."
                ),
                "entities": [e.to_dict() for e in TRANSACTION_ENTITIES],
            },
        ],
    }


__all__ = [
    "MASTER",
    "TRANSACTION",
    "ManagedEntity",
    "ManagedField",
    "MASTER_ENTITIES",
    "TRANSACTION_ENTITIES",
    "ENTITIES",
    "MASTER_BY_KEY",
    "TRANSACTION_BY_KEY",
    "STATUS_VALUES",
    "SYSTEM_COLUMNS",
    "UnknownEntity",
    "get_master",
    "get_transaction",
    "report_section",
    "catalogue",
]
