"""The catalogue of what may be uploaded.

The catalogue is **derived**, never hand-written:

* master types come from :data:`app.master_data.schema.TABLE_SPECS`, the Phase 1
  contract discovered from ``Master Data.xlsx``;
* transactional types come from :data:`app.etl.datasets.DATASETS`, the Phase 2
  dataset specifications.

So adding a column to a dimension or a field to a dataset changes the upload
template, the validation rules and the preview automatically — there is no
second copy of the schema to drift out of step.

The two future-ready dimensions (``dim_customer``, ``dim_sales_force``) are
included as master upload types precisely because they are
``PENDING_SOURCE_DATA``: this is how a real customer or sales-force master
finally arrives. Their columns come from the ORM model rather than from a sheet
spec, because no source sheet exists for them yet. The three material masters
are declared the same way and for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ..database.models import (
    MODEL_BY_TABLE,
    DimMaterial,
    DimPlant,
    DimStorageLocation,
)
from ..database.models_admin import ImportMode, UploadCategory
from ..database.models_geo import GEO_MODEL_BY_TABLE
from ..database.models_map import MapEntityLocation
from ..database.models_warehouse import DimCustomer, DimSalesForce
from ..etl.datasets import DATASETS, DatasetSpec, FieldKind as EtlFieldKind
from ..master_data.schema import TABLE_SPECS, FieldKind as MasterFieldKind, TableSpec

#: Human guidance per storage kind, printed into the template.
TYPE_GUIDANCE: dict[str, str] = {
    "code": "Text. Official business code, exactly as issued. Never re-typed as a number.",
    "text": "Text. Bangla and other Unicode are preserved.",
    "phone": "Text. Keep the leading zero or '+'; format the column as Text in Excel.",
    "integer": "Whole number.",
    "decimal": "Number. Up to 4 decimal places.",
    "timestamp": "Date/time, e.g. 2026-08-15 or 2026-08-15T09:30:00.",
    "date": "Date, e.g. 2026-08-15. Ambiguous formats are rejected, not guessed.",
    "numeric": "Number. Use a plain number — no currency symbol, no thousands separator.",
}


@dataclass(frozen=True)
class UploadColumn:
    """One column of an upload template."""

    #: The header written into the template and expected in the file.
    name: str
    #: The warehouse column / canonical field the header maps onto.
    target: str
    kind: str
    required: bool
    description: str = ""
    example: str = ""
    #: Alternative headers accepted for this column.
    aliases: tuple[str, ...] = ()

    @property
    def guidance(self) -> str:
        return TYPE_GUIDANCE.get(self.kind, "Text.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "target": self.target,
            "kind": self.kind,
            "required": self.required,
            "description": self.description,
            "example": self.example,
            "aliases": list(self.aliases),
            "guidance": self.guidance,
        }


@dataclass(frozen=True)
class Lookup:
    """A column that must name an existing record in another master table.

    Distinct from ``parent_table``, which is the dimension's *structural*
    parent and is backed by a database foreign key. A lookup is a reference the
    schema deliberately does not constrain — ``dim_customer.sub_territory_code``
    and ``dim_storage_location.plant_code`` are both like this, because those
    dimensions are populated by upload and by ETL placeholders, and a
    constraint would fail a whole import where a row-level message is wanted.

    Declaring it here is what lets the validator, the upload preview and the
    edit form's dropdown all learn about the reference from one place.
    """

    column: str
    table: str
    #: The column to look the value up in, in the referenced table.
    target_column: str
    #: What to call it in an error message: "Invalid Sub-territory Code: ST999".
    label: str


@dataclass(frozen=True)
class UploadType:
    """One thing a user can upload."""

    key: str
    label: str
    category: str
    description: str
    columns: tuple[UploadColumn, ...]
    #: Column(s) that identify a record for duplicate detection and upsert.
    business_key: tuple[str, ...]
    business_key_description: str
    supported_modes: tuple[str, ...]
    default_mode: str
    #: Master only: the dimension table rows land in.
    table: str | None = None
    #: Transactional only: the ETL data type.
    data_type: str | None = None
    #: Master only: the parent dimension a row must reference.
    parent_table: str | None = None
    parent_column: str | None = None
    #: Columns referencing another master table without a foreign key.
    lookups: tuple[Lookup, ...] = ()
    #: True when the dimension has no source file yet, so this upload is how its
    #: master data first arrives.
    pending_source: bool = False
    note: str = ""

    @property
    def required_columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.required)

    @property
    def column_by_target(self) -> dict[str, UploadColumn]:
        return {c.target: c for c in self.columns}

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "category": self.category,
            # Presentation only; `category` above is still the behaviour.
            "group": display_group(self.key),
            "description": self.description,
            "table": self.table,
            "data_type": self.data_type,
            "parent_table": self.parent_table,
            "business_key": list(self.business_key),
            "business_key_description": self.business_key_description,
            "supported_modes": list(self.supported_modes),
            "default_mode": self.default_mode,
            "required_columns": list(self.required_columns),
            "column_count": len(self.columns),
            "pending_source": self.pending_source,
            "note": self.note,
            "columns": [c.to_dict() for c in self.columns],
            "lookups": [
                {"column": lookup.column, "table": lookup.table,
                 "target_column": lookup.target_column, "label": lookup.label}
                for lookup in self.lookups
            ],
        }

    @property
    def lookup_by_column(self) -> dict[str, "Lookup"]:
        return {lookup.column: lookup for lookup in self.lookups}


# ---------------------------------------------------------------------------
# Master data types
# ---------------------------------------------------------------------------

#: Master labels, taken from the sheet name minus the redundant " Master".
def _master_label(spec: TableSpec) -> str:
    return spec.sheet.replace(" Master", "").strip()


#: Lookups per master table. Kept here beside the column definitions so a
#: reference and the column it constrains cannot drift apart.
LOOKUPS_BY_TABLE: dict[str, tuple[Lookup, ...]] = {
    "dim_customer": (
        Lookup("sub_territory_code", "dim_sub_territory", "sub_territory_code",
               "Sub-territory Code"),
    ),
    # The material masters load in any order and carry no database foreign keys
    # between them, so these two checks are where a wrong reference is caught —
    # at upload time, naming the row and the code, rather than as an integrity
    # error nobody can act on.
    "dim_storage_location": (
        Lookup("plant_code", "dim_plant", "plant_code", "Plant Code"),
    ),
    # A material's group and brand are its own columns, not codes into other
    # tables, so the company is its one reference outward — checked here, at
    # upload time, naming the row and the code, rather than as a foreign key that
    # would fail the whole file on a company the Company Master has not received.
    "dim_material": (
        Lookup("company_code", "dim_company", "company_code", "Company Code"),
    ),
}


def _master_type(spec: TableSpec) -> UploadType:
    columns = tuple(
        UploadColumn(
            name=column.source_field,
            target=column.column,
            kind=column.kind.value,
            required=not column.nullable,
            description=column.description,
            example=column.example,
            aliases=(column.column,),
        )
        for column in spec.columns
    )
    return UploadType(
        key=spec.table,
        label=_master_label(spec),
        category=UploadCategory.MASTER,
        description=spec.purpose,
        columns=columns,
        business_key=(spec.business_key,),
        business_key_description=(
            f"{spec.business_key} — the official business code. A code already in "
            "the warehouse is updated in place; it is never duplicated."
        ),
        supported_modes=ImportMode.ALL,
        default_mode=ImportMode.UPSERT,
        table=spec.table,
        parent_table=spec.parent_table,
        parent_column=spec.parent_column,
        lookups=LOOKUPS_BY_TABLE.get(spec.table, ()),
        note=(
            f"Rows must reference an existing {spec.parent_column} in "
            f"{spec.parent_table}."
        ) if spec.parent_table else "",
    )


#: Column kinds for the future-ready dimensions, whose shape is defined by the
#: ORM model rather than by a workbook sheet.
_PENDING_DIMENSIONS: tuple[tuple[str, str, str, type, tuple[tuple[str, str, bool, str], ...]], ...] = (
    (
        "dim_customer", "Customer", "customer_code", DimCustomer,
        (
            # First, before the customer code. This tuple is the single source
            # of column order: the management table, the edit form and the
            # Excel template are all generated from it, so the order specified
            # for the master is the order everywhere.
            ("sub_territory_code", "code", False,
             "The sub-territory this customer belongs to. Must exist in the "
             "Sub-Territory Master."),
            ("customer_code", "code", True, "Official customer / dealer code."),
            ("customer_name", "text", True, "Customer name."),
            ("customer_type", "text", False, "Dealer, retailer, institution, …"),
            ("address", "text", False, "Street address."),
            ("district", "text", False, "District."),
            ("mobile", "phone", False, "Contact number. Stored as text."),
            ("status", "text", False, "Active / Inactive."),
        ),
    ),
    (
        "dim_sales_force", "Sales Force", "sales_force_code", DimSalesForce,
        (
            ("sales_force_code", "code", True, "Official sales-force code."),
            ("sales_force_name", "text", True, "Name of the sales person."),
            ("designation", "text", False, "SR / SO / ASM, …"),
            ("employee_id", "code", False, "HR employee identifier."),
            ("territory_code", "code", False,
             "Territory they cover. Must exist in dim_territory when supplied."),
            ("status", "text", False, "Active / Inactive."),
        ),
    ),
    (
        # The Plant Master. A plant code identifies a plant within a company, so
        # the identity is the pair — see ``_COMPOSITE_KEYS`` below.
        "dim_plant", "Plant", "plant_key", DimPlant,
        (
            ("company_code", "code", True, "Company code."),
            ("plant_code", "code", True, "Plant code."),
            ("plant_name", "text", True, "Plant name."),
        ),
    ),
    (
        # The Storage Location Master. Keyed on Plant + Storage Location,
        # because a storage location code is unique only inside its plant.
        #
        # No plant name column: that is the Plant Master's to state. Repeating
        # it here would let a renamed plant keep its old name on every storage
        # location beneath it.
        "dim_storage_location", "Storage Location", "storage_location_key",
        DimStorageLocation,
        (
            ("plant_code", "code", True,
             "The plant this storage location belongs to. Must exist in the "
             "Plant Master."),
            ("storage_location_code", "code", True, "Storage location code."),
            ("storage_location_name", "text", True, "Storage location name."),
        ),
    ),
    (
        # The Material Master, keyed on the material code alone — a material is
        # one thing, wherever it happens to be stored.
        #
        # Group and brand are stated as code/name pairs on the material because
        # that is how the source states them, and because a stock position
        # repeats both codes: the ETL rejects a position whose group or brand
        # disagrees with what this master records, which is only possible if
        # this master records them.
        #
        # Seven identifying columns, in the order the business states them:
        # Company, then the classification from the top down, then the material
        # and its description — followed since revision 0027 by the two optional
        # derivation inputs. **This tuple is the column order** — the upload
        # template, the preview, the management table, the edit form and the CSV
        # export are all derived from it, so a column moved here moves
        # everywhere and is never re-declared in a per-page list.
        #
        # Since revision 0022 this is the *only* item master: a sale, a target
        # and a stock position all name a Material Code and all resolve here.
        "dim_material", "Material", "material_code", DimMaterial,
        (
            ("company_code", "code", True,
             "The company this material belongs to. Must exist in the Company "
             "Master. First in the master's stated order, and the head of the "
             "item filter chain: Company -> Group -> Brand -> Material."),
            ("material_group_code", "code", True, "Material group code."),
            ("material_group_name", "text", True, "Material group name."),
            ("material_brand_code", "code", True, "Material brand code."),
            ("material_brand", "text", True, "Material brand name."),
            ("material_code", "code", True,
             "The material itself. A material group classifies many materials, "
             "so it cannot identify one — this can. Unique across every company: "
             "a material belongs to one company, and the upload reports a file "
             "that places the same code under two."),
            ("material_description", "text", True, "Material description."),
            # Added by revision 0027, and the two entries here are the whole
            # change: the template, the validation, the preview, the edit form
            # and the CSV export are all derived from this tuple.
            #
            # Optional, unlike every column above them. They are what the Target
            # Management module derives Quantity and Value from, and a Material
            # Master extract produced before they were asked for does not carry
            # them — requiring them would reject every such file. A material
            # missing either yields no derived figure and reports n/a; it is
            # never defaulted, because 1.0 would read as a real conversion
            # rather than as "unknown".
            ("conversion_factor", "decimal", False,
             "How many volume units make one saleable unit. Target Quantity = "
             "Target Volume / Conversion Factor. Optional: a material without "
             "one reports no quantity rather than a guessed one."),
            ("transfer_price", "decimal", False,
             "Transfer price of one saleable unit. Target Value = Target "
             "Quantity x Transfer Price. Optional: a material without one "
             "reports no value rather than a guessed one."),
        ),
    ),
)

#: Masters whose identity is more than one column.
#:
#: ``_pending_type`` assumes a single business code, which every dimension had
#: until the material side arrived: a plant code is unique only within its
#: company and a storage location code only within its plant, so neither stands
#: alone as a key. (The Material Master is not here — ``material_code`` is a
#: single code and identifies a material outright.)
#:
#: The order matters — :func:`app.database.models.plant_key` and
#: :func:`app.database.models.storage_location_key` are called with these
#: tuples' values positionally.
_COMPOSITE_KEYS: dict[str, tuple[str, ...]] = {
    "dim_plant": ("company_code", "plant_code"),
    "dim_storage_location": ("plant_code", "storage_location_code"),
}

_PENDING_EXAMPLES = {
    "sub_territory_code": "STR001",
    "customer_code": "CUST-001", "customer_name": "Example Traders",
    "customer_type": "Dealer", "address": "12 Market Road", "district": "Dhaka",
    "mobile": "+8801700000000", "status": "Active",
    "sales_force_code": "SF-001", "sales_force_name": "Md. Anis",
    "designation": "Sales Officer", "employee_id": "EMP0660",
    "territory_code": "TR001",
    "company_code": "C001",
    "plant_code": "P100", "plant_name": "Gazipur Plant",
    "storage_location_code": "SL01", "storage_location_name": "Finished Goods",
    "material_code": "MAT-1001", "material_description": "Urea 50 KG Bag",
    "material_group_code": "MG20", "material_group_name": "Fertiliser",
    "material_brand_code": "MB07", "material_brand": "Shobuj",
    "conversion_factor": "0.5", "transfer_price": "240",
}


def _pending_type(table: str, label: str, business_key: str,
                  fields: Iterable[tuple[str, str, bool, str]]) -> UploadType:
    columns = tuple(
        UploadColumn(
            name=_header_for(target),
            target=target,
            kind=kind,
            required=required,
            description=description,
            example=_PENDING_EXAMPLES.get(target, ""),
            aliases=(target,),
        )
        for target, kind, required, description in fields
    )
    composite = _COMPOSITE_KEYS.get(table)
    return UploadType(
        key=table,
        label=label,
        category=UploadCategory.MASTER,
        description=(
            f"{label} master. No source sheet exists in Master Data.xlsx, so this "
            "upload is how the dimension is first populated. Transactions already "
            "carry the codes; loading this master back-fills their links."
        ),
        columns=columns,
        business_key=composite or (business_key,),
        business_key_description=(
            (" + ".join(composite) + " — the combination identifies the record. "
             "An existing combination is updated, never duplicated.")
            if composite else
            f"{business_key} — the official code. An existing code is updated, "
            "never duplicated."
        ),
        supported_modes=ImportMode.ALL,
        default_mode=ImportMode.UPSERT,
        table=table,
        lookups=LOOKUPS_BY_TABLE.get(table, ()),
        pending_source=True,
        note=(
            "Loading this master switches the dimension from PENDING_SOURCE_DATA "
            "to AVAILABLE, after which transactions carrying an unknown code are "
            "rejected instead of tolerated."
        ),
    )


#: Headers whose column name is not what an operator calls the field. ``volume``
#: is the warehouse's spelling; the file states a *Total Volume* per line, and
#: the template has to say so — the word "total" is what tells the operator this
#: is the whole line's figure and not a per-unit one to be multiplied out.
_HEADER_OVERRIDES: dict[str, str] = {
    "volume": "Total Volume",
}


def _header_for(column: str) -> str:
    """``customer_code`` -> ``Customer Code``; keeps SKU-style acronyms upper."""
    if column in _HEADER_OVERRIDES:
        return _HEADER_OVERRIDES[column]
    words = column.split("_")
    return " ".join(
        word.upper() if word in {"id", "sku", "bu", "hq"} else word.capitalize()
        for word in words
    )


# ---------------------------------------------------------------------------
# Transactional data types
# ---------------------------------------------------------------------------

#: Example values for the transactional template, chosen to be obviously
#: illustrative rather than plausible production data.
_TRANSACTION_EXAMPLES: dict[str, str] = {
    "transaction_date": "2026-08-15",
    "invoice_no": "INV-0001",
    "customer_code": "CUST-001",
    "sales_force_code": "SF-001",
    "company_code": "C001",
    "plant_code": "P100",
    "storage_location_code": "SL01",
    "material_code": "MAT-1001",
    "material_group_code": "MG20",
    "material_brand_code": "MB07",
    "unrestricted_stock": "1200",
    "quality_inspection_stock": "0",
    "blocked_stock": "0",
    "stock_in_transit": "300",
    "production_date": "2026-05-01",
    "shelf_life_expiration_date": "2027-05-01",
    "bu_code": "BU001",
    "sales_line_code": "SL001",
    "zone_code": "Z001",
    "region_code": "REG001",
    "area_code": "AR001",
    "unit_code": "UN001",
    "territory_code": "TR001",
    "sub_territory_code": "STR001",
    "quantity": "10",
    # A decimal, deliberately: the total volume is whatever the source settled
    # on for the line, not a whole multiple of anything.
    "volume": "250.50",
    "gross_sales": "12000",
    "discount": "1000",
    "net_sales": "11000",
    "cost": "8000",
    "target_month": "2026-08",
    "financial_year": "FY 2026-27",
    "target_amount": "2000000",
    "target_quantity": "500",
    "target_volume": "1200",
    "source_transaction_id": "SRC-0001",
}

_TRANSACTION_LABELS = {
    "sales": "Sales",
    "material_stock": "Material Stock",
    "target": "Target",
}


def _transaction_type(spec: DatasetSpec) -> UploadType:
    columns = tuple(
        UploadColumn(
            name=_header_for(field.name),
            target=field.name,
            kind="date" if field.kind == EtlFieldKind.DATE else (
                "numeric" if field.kind == EtlFieldKind.NUMERIC else field.kind
            ),
            required=field.required,
            description=field.description,
            example=_TRANSACTION_EXAMPLES.get(field.name, ""),
            aliases=field.aliases,
        )
        for field in spec.fields
    )
    return UploadType(
        key=spec.data_type,
        label=_TRANSACTION_LABELS.get(spec.data_type, spec.data_type.title()),
        category=UploadCategory.TRANSACTIONAL,
        description=spec.description,
        columns=columns,
        business_key=spec.business_key_fields,
        business_key_description=(
            f"{spec.business_key_definition}. A row whose key is already in the "
            "warehouse updates that row; it is never inserted twice, and existing "
            "transactions are never deleted to make room for it."
        ),
        # INSERT means "reject anything already loaded" (the Phase 2 INITIAL load
        # mode); UPSERT re-applies. UPDATE-only is not offered because a
        # transaction that has never been loaded has nothing to update.
        supported_modes=(ImportMode.INSERT, ImportMode.UPSERT),
        default_mode=ImportMode.UPSERT,
        data_type=spec.data_type,
        note=_org_note(spec),
    )


def _org_note(spec: DatasetSpec) -> str:
    """How this dataset states where a row belongs.

    Most sources may carry any organisational level and the mapper derives the
    rest. A target names one level and must: the note has to say which, because
    "at least one code" would leave an operator guessing why a file with only a
    region was rejected.
    """
    required = [
        field.name for field in spec.fields
        if field.required and field.name in spec.org_levels
    ]
    if required:
        named = " and ".join(name.replace("_", " ") for name in required)
        return (
            f"{named.capitalize()} is required on every row; the rest of the "
            "hierarchy above it is derived from the master data, so no other "
            "organisational column belongs in the file."
        )
    return (
        "At least one organisational code is required on every row; the rest "
        "of the hierarchy is derived from the deepest code supplied."
    )


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

#: Map coordinates. Modelled as a master upload because that is exactly what it
#: is — reference data keyed on an official business code — so it inherits the
#: whole validated pipeline: template, preview, row-level errors and history.
GEO_LOCATION_TYPE = UploadType(
    key="map_entity_locations",
    label="Map Locations",
    category=UploadCategory.MASTER,
    description=(
        "Latitude and longitude for entities shown on the business map. Place "
        "the territories and every level above them is derived automatically."
    ),
    columns=(
        UploadColumn("Entity Type", "entity_type", "code", True,
                     "Which kind of entity the code belongs to, e.g. territory, "
                     "region, customer, sales force.",
                     "territory", ("type", "level")),
        UploadColumn("Entity Code", "entity_code", "code", True,
                     "The official business code, exactly as in the master data.",
                     "TR001", ("code",)),
        UploadColumn("Latitude", "latitude", "decimal", True,
                     "Decimal degrees, -90 to 90. Leave blank rather than "
                     "entering 0.", "23.7808"),
        UploadColumn("Longitude", "longitude", "decimal", True,
                     "Decimal degrees, -180 to 180.", "90.4008"),
        UploadColumn("Label", "label", "text", False,
                     "Optional display name; the master data's name is used "
                     "when this is empty.", "Kazipara"),
    ),
    business_key=("entity_type", "entity_code"),
    business_key_description=(
        "entity_type + entity_code — one coordinate per entity. Re-uploading a "
        "code moves it; it is never duplicated."
    ),
    supported_modes=ImportMode.ALL,
    default_mode=ImportMode.UPSERT,
    table="map_entity_locations",
    note=(
        "Codes must already exist in the master data. After loading, parent "
        "levels are re-derived as the centroid of their children unless a "
        "coordinate was placed by hand."
    ),
)

#: Administrative geography. A second hierarchy, and reference data about the
#: country rather than about the company — so it is uploadable in its own right,
#: with the same validated pipeline every other dimension gets.
#:
#: The boundary polygons are deliberately *not* here. A spreadsheet column
#: cannot carry a multi-thousand-vertex ring in any form a person could check,
#: so geometry arrives through ``scripts/import_admin_areas.py`` from a
#: published GeoJSON file, and these uploads carry the names and codes.
_ADMIN_DIMENSIONS: tuple[tuple[str, str, str, tuple[tuple[str, str, bool, str], ...],
                               str | None, str | None], ...] = (
    (
        "dim_division", "Division", "division_code",
        (
            ("division_code", "code", True, "Official division code."),
            ("division_name", "text", True, "Division name in English."),
            ("division_name_bn", "text", False, "Division name in Bangla."),
        ),
        None, None,
    ),
    (
        "dim_district", "District", "district_code",
        (
            ("district_code", "code", True, "Official district (zila) code."),
            ("district_name", "text", True, "District name in English."),
            ("district_name_bn", "text", False, "District name in Bangla."),
            ("division_code", "code", True,
             "The division this district belongs to."),
        ),
        "dim_division", "division_code",
    ),
    (
        "dim_upazila", "Upazila", "upazila_code",
        (
            ("upazila_code", "code", True, "Official upazila (sub-district) code."),
            ("upazila_name", "text", True, "Upazila name in English."),
            ("upazila_name_bn", "text", False, "Upazila name in Bangla."),
            ("district_code", "code", True,
             "The district this upazila belongs to."),
            ("latitude", "decimal", False,
             "Administrative centre, decimal degrees. Set by the boundary "
             "import when a polygon is loaded."),
            ("longitude", "decimal", False, "Administrative centre, -180 to 180."),
        ),
        "dim_district", "district_code",
    ),
)

_ADMIN_EXAMPLES = {
    "division_code": "BD-30", "division_name": "Dhaka",
    "district_code": "BD-3026", "district_name": "Mymensingh",
    "upazila_code": "BD-302690", "upazila_name": "Trishal",
    "latitude": "24.5745", "longitude": "90.3948",
}


def _admin_type(table: str, label: str, business_key: str,
                fields: Iterable[tuple[str, str, bool, str]],
                parent_table: str | None,
                parent_column: str | None) -> UploadType:
    columns = tuple(
        UploadColumn(
            name=_header_for(target), target=target, kind=kind, required=required,
            description=description,
            example=_ADMIN_EXAMPLES.get(target, ""), aliases=(target,),
        )
        for target, kind, required, description in fields
    )
    return UploadType(
        key=table,
        label=label,
        category=UploadCategory.MASTER,
        description=(
            f"{label} master — Bangladesh administrative geography. Independent "
            "of the organisational hierarchy: an administrative area exists "
            "whether or not the company sells there."
        ),
        columns=columns,
        business_key=(business_key,),
        business_key_description=(
            f"{business_key} — the official code. An existing code is updated, "
            "never duplicated."
        ),
        supported_modes=ImportMode.ALL,
        default_mode=ImportMode.UPSERT,
        table=table,
        parent_table=parent_table,
        parent_column=parent_column,
        note=(
            f"Rows must reference an existing {parent_column} in {parent_table}."
            if parent_table else
            "The top of the administrative hierarchy; it references nothing."
        ),
    )


MASTER_TYPES: tuple[UploadType, ...] = (
    *(_master_type(spec) for spec in TABLE_SPECS),
    *(_pending_type(table, label, key, fields)
      for table, label, key, _model, fields in _PENDING_DIMENSIONS),
    *(_admin_type(table, label, key, fields, parent_table, parent_column)
      for table, label, key, fields, parent_table, parent_column
      in _ADMIN_DIMENSIONS),
    GEO_LOCATION_TYPE,
)

TRANSACTION_TYPES: tuple[UploadType, ...] = tuple(
    _transaction_type(spec) for spec in DATASETS
)

UPLOAD_TYPES: tuple[UploadType, ...] = (*MASTER_TYPES, *TRANSACTION_TYPES)
UPLOAD_TYPE_BY_KEY: dict[str, UploadType] = {t.key: t for t in UPLOAD_TYPES}

#: How the two data screens *group* what they list, which is not the same
#: question as :class:`UploadCategory`.
#:
#: ``category`` is MASTER or TRANSACTIONAL and is **behaviour**: it picks the
#: processing path in ``upload/service.py`` and is stored on every
#: ``upload_batches`` row, so a batch loaded last year still means what it said.
#: Repurposing it to group a menu would change how uploads run and would
#: retroactively change what those stored rows claim.
#:
#: This is presentation only. Nothing reads it but the two screens.
DISPLAY_GROUPS: tuple[dict[str, str], ...] = (
    {"key": "SALES", "label": "Sales",
     "description": "The organisational hierarchy, company down to sub-territory."},
    {"key": "MARKET", "label": "Market",
     "description": "Administrative geography: division, district and upazila."},
    {"key": "PEOPLE", "label": "People",
     "description": "The customers and the sales force who serve them."},
    {"key": "MATERIAL", "label": "Material",
     "description": "Plants, storage locations and the material master."},
    {"key": "TRANSACTIONS", "label": "Transactions",
     "description": "Sales, material stock and target facts."},
)

#: Which group each upload type is listed under.
#:
#: Hand-written, because no property of an upload type says which heading a
#: reader expects to find it beneath. That makes it exactly the kind of list
#: this codebase warns about, so :func:`display_group` **raises** on a key it
#: does not know rather than defaulting: a new upload type must be placed here
#: deliberately, and can never be silently dropped from the two screens that
#: are the only way to load data.
GROUP_BY_KEY: dict[str, str] = {
    "dim_company": "SALES",
    "dim_business_unit": "SALES",
    "dim_sales_line": "SALES",
    "dim_zone": "SALES",
    "dim_region": "SALES",
    "dim_area": "SALES",
    "dim_unit": "SALES",
    "dim_territory": "SALES",
    "dim_sub_territory": "SALES",

    "dim_division": "MARKET",
    "dim_district": "MARKET",
    # The business calls this a thana; the master, the boundaries and the map
    # all call it an upazila, and one name across the platform beats two.
    "dim_upazila": "MARKET",
    "map_entity_locations": "MARKET",

    "dim_customer": "PEOPLE",
    "dim_sales_force": "PEOPLE",

    "dim_plant": "MATERIAL",
    "dim_storage_location": "MATERIAL",
    "dim_material": "MATERIAL",

    "sales": "TRANSACTIONS",
    "material_stock": "TRANSACTIONS",
    "target": "TRANSACTIONS",
}


def display_group(key: str) -> str:
    """The group an upload type is listed under, or a loud failure."""
    try:
        return GROUP_BY_KEY[key]
    except KeyError:  # pragma: no cover - guarded by test
        raise KeyError(
            f"Upload type '{key}' has no display group. Add it to GROUP_BY_KEY "
            "in upload/registry.py, or it will not appear on the Data "
            "Management or Data Upload screens."
        ) from None


CATEGORIES: tuple[dict[str, Any], ...] = (
    {
        "key": UploadCategory.MASTER,
        "label": "Master Data",
        "description": (
            "The organisational hierarchy and the product catalogue. Loaded by "
            "official business code: an existing code is updated, never duplicated."
        ),
    },
    {
        "key": UploadCategory.TRANSACTIONAL,
        "label": "Transactional Data",
        "description": (
            "Sales, material stock and target facts. Every row is validated "
            "against the master data before it reaches the warehouse."
        ),
    },
)

#: Master upload keys that map onto a model.
MASTER_MODEL_BY_TABLE = {
    **MODEL_BY_TABLE,
    "dim_customer": DimCustomer,
    "dim_sales_force": DimSalesForce,
    **GEO_MODEL_BY_TABLE,
    "map_entity_locations": MapEntityLocation,
}


def get_upload_type(key: str) -> UploadType:
    """Look up an upload type, raising a clear error for an unknown key."""
    upload_type = UPLOAD_TYPE_BY_KEY.get(key)
    if upload_type is None:
        raise ValueError(
            f"Unknown upload type {key!r}. Supported: "
            f"{', '.join(sorted(UPLOAD_TYPE_BY_KEY))}."
        )
    return upload_type


def catalogue() -> dict[str, Any]:
    """The whole catalogue, grouped by category, as the API returns it."""
    return {
        "groups": [
            {
                **group,
                "types": [
                    t.to_dict() for t in UPLOAD_TYPES
                    if display_group(t.key) == group["key"]
                ],
            }
            for group in DISPLAY_GROUPS
        ],
        "categories": [
            {
                **category,
                "types": [
                    t.to_dict() for t in UPLOAD_TYPES if t.category == category["key"]
                ],
            }
            for category in CATEGORIES
        ],
        "import_modes": [
            {
                "key": ImportMode.INSERT,
                "label": "Insert only",
                "description": "Load new records. A record that already exists is "
                               "reported, not overwritten.",
            },
            {
                "key": ImportMode.UPDATE,
                "label": "Update only",
                "description": "Update existing records. A record that does not "
                               "exist yet is reported, not created.",
            },
            {
                "key": ImportMode.UPSERT,
                "label": "Insert or update",
                "description": "Load new records and update existing ones. Nothing "
                               "is deleted.",
            },
        ],
    }


__all__ = [
    "UploadColumn",
    "UploadType",
    "Lookup",
    "LOOKUPS_BY_TABLE",
    "UPLOAD_TYPES",
    "UPLOAD_TYPE_BY_KEY",
    "MASTER_TYPES",
    "TRANSACTION_TYPES",
    "MASTER_MODEL_BY_TABLE",
    "CATEGORIES",
    "TYPE_GUIDANCE",
    "get_upload_type",
    "catalogue",
]
