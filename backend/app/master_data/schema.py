"""Canonical master-data schema: the single source of truth for Phase 1.

Every field listed here was **discovered programmatically** from
``data/Master Data.xlsx`` (see ``reports/master_data_profile.json``), not assumed.
The mapping below binds each source sheet field to its warehouse column, its
storage type, and the validation rules that apply to it.

Naming: source labels are human/title-case ("Sub Territory Head_Phone Number");
warehouse columns are ``snake_case`` and consistent across DB / Python / API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class FieldKind(str, Enum):
    """How a value must be handled during cleaning, typing and storage."""

    CODE = "code"          # official business code -> TEXT, never numeric
    TEXT = "text"          # free text (may contain Bangla / Unicode)
    PHONE = "phone"        # phone number -> TEXT, never numeric
    INTEGER = "integer"    # whole number
    DECIMAL = "decimal"    # money / ratio
    TIMESTAMP = "timestamp"


#: SQL type emitted for each kind (PostgreSQL).
SQL_TYPE_BY_KIND: dict[FieldKind, str] = {
    FieldKind.CODE: "VARCHAR(64)",
    FieldKind.TEXT: "TEXT",
    FieldKind.PHONE: "VARCHAR(32)",
    FieldKind.INTEGER: "INTEGER",
    FieldKind.DECIMAL: "NUMERIC(18, 4)",
    FieldKind.TIMESTAMP: "TIMESTAMP",
}


@dataclass(frozen=True)
class ColumnSpec:
    """One warehouse column and the sheet field it originates from."""

    source_field: str          # exact label as it appears in the Excel sheet
    column: str                # snake_case warehouse column name
    kind: FieldKind
    nullable: bool = True
    description: str = ""
    example: str = ""
    #: True when the warehouse has this column but the sheet need not.
    #:
    #: The default is False, and that is the contract: a declared field missing
    #: from its sheet is an error, because a silently absent column is how a
    #: whole attribute goes missing unnoticed.
    #:
    #: It is set only for a column added to the warehouse *after* the workbook's
    #: shape was fixed — one the sheet may grow later and may equally never
    #: carry. Such a column is offered in the upload template and accepted when
    #: present; its absence is not a fault in a workbook that predates it.
    source_optional: bool = False

    @property
    def sql_type(self) -> str:
        return SQL_TYPE_BY_KIND[self.kind]


@dataclass(frozen=True)
class TableSpec:
    """One dimension table sourced from one master sheet."""

    sheet: str                       # exact Excel sheet name
    table: str                       # PostgreSQL table name
    surrogate_key: str               # BIGSERIAL primary key column
    business_key: str                # official code column (UNIQUE)
    columns: tuple[ColumnSpec, ...]
    purpose: str
    parent_table: str | None = None  # referenced dimension table
    parent_column: str | None = None # FK column on THIS table
    parent_key: str | None = None    # referenced column on the parent table
    indexed_columns: tuple[str, ...] = ()

    @property
    def column_map(self) -> dict[str, ColumnSpec]:
        """``{source_field: ColumnSpec}`` for the sheet reader."""
        return {c.source_field: c for c in self.columns}

    @property
    def by_column(self) -> dict[str, ColumnSpec]:
        return {c.column: c for c in self.columns}

    @property
    def source_fields(self) -> tuple[str, ...]:
        """Sheet fields the workbook is *required* to carry.

        Excludes the optional ones, so a workbook predating a later column
        validates cleanly instead of being reported as missing a field it was
        never meant to have. The column is still read when the sheet does have
        it — see :attr:`all_source_fields`.
        """
        return tuple(c.source_field for c in self.columns if not c.source_optional)

    @property
    def all_source_fields(self) -> tuple[str, ...]:
        """Every sheet field the reader will map, optional ones included."""
        return tuple(c.source_field for c in self.columns)

    @property
    def required_columns(self) -> tuple[str, ...]:
        return tuple(c.column for c in self.columns if not c.nullable)

    def has_foreign_key(self) -> bool:
        return self.parent_table is not None


def _c(source: str, column: str, kind: FieldKind, *, nullable: bool = True,
       description: str = "", example: str = "",
       source_optional: bool = False) -> ColumnSpec:
    return ColumnSpec(source, column, kind, nullable, description, example,
                      source_optional)


# --------------------------------------------------------------------------
# Organisational hierarchy
#   Company > Business Unit > Sales Line > Zone > Region > Area > Unit >
#   Territory > Sub-Territory
# Product is an independent dimension (no relationship was discovered in the
# workbook, and none is invented here).
# --------------------------------------------------------------------------

COMPANY = TableSpec(
    sheet="Company Master",
    table="dim_company",
    surrogate_key="company_id",
    business_key="company_code",
    purpose="Top level of the organisational hierarchy: the legal/operating company.",
    indexed_columns=("company_code",),
    columns=(
        _c("Company Code", "company_code", FieldKind.CODE, nullable=False,
           description="Official company code. Business key, unique.", example="C001"),
        _c("Company", "company_name", FieldKind.TEXT, nullable=False,
           description="Company name.", example="Example Industries Ltd."),
        _c("Company Head ID", "company_head_id", FieldKind.CODE,
           description="Employee ID of the company head.", example="EMP0001"),
        _c("Company Head Name", "company_head_name", FieldKind.TEXT,
           description="Name of the company head.", example="Md. Rahman"),
    ),
)

BUSINESS_UNIT = TableSpec(
    sheet="Business Unit Master",
    table="dim_business_unit",
    surrogate_key="business_unit_id",
    business_key="bu_code",
    purpose="Business unit within a company.",
    parent_table="dim_company",
    parent_column="company_code",
    parent_key="company_code",
    indexed_columns=("bu_code", "company_code"),
    columns=(
        _c("Company Code", "company_code", FieldKind.CODE, nullable=False,
           description="Parent company code. FK -> dim_company.company_code.", example="C001"),
        _c("BU Code", "bu_code", FieldKind.CODE, nullable=False,
           description="Official business unit code. Business key, unique.", example="BU001"),
        _c("BU Name", "bu_name", FieldKind.TEXT, nullable=False,
           description="Business unit name.", example="Consumer Products"),
        _c("BU Head ID", "bu_head_id", FieldKind.CODE,
           description="Employee ID of the business unit head.", example="EMP0021"),
        _c("BU Head Name", "bu_head_name", FieldKind.TEXT,
           description="Name of the business unit head.", example="Md. Karim"),
    ),
)

SALES_LINE = TableSpec(
    sheet="Sales Line Master",
    table="dim_sales_line",
    surrogate_key="sales_line_id",
    business_key="sales_line_code",
    purpose="Sales line within a business unit.",
    parent_table="dim_business_unit",
    parent_column="bu_code",
    parent_key="bu_code",
    indexed_columns=("sales_line_code", "bu_code"),
    columns=(
        _c("BU Code", "bu_code", FieldKind.CODE, nullable=False,
           description="Parent business unit code. FK -> dim_business_unit.bu_code.", example="BU001"),
        _c("Sales Line Code", "sales_line_code", FieldKind.CODE, nullable=False,
           description="Official sales line code. Business key, unique.", example="SL001"),
        _c("Sales Line Name", "sales_line_name", FieldKind.TEXT, nullable=False,
           description="Sales line name.", example="General Trade"),
        _c("Sales Line Head ID", "sales_line_head_id", FieldKind.CODE,
           description="Employee ID of the sales line head.", example="EMP0105"),
        _c("Sales Line Head", "sales_line_head_name", FieldKind.TEXT,
           description="Name of the sales line head.", example="Md. Alam"),
    ),
)

ZONE = TableSpec(
    sheet="Zone Master",
    table="dim_zone",
    surrogate_key="zone_id",
    business_key="zone_code",
    purpose="Zone within a sales line.",
    parent_table="dim_sales_line",
    parent_column="sales_line_code",
    parent_key="sales_line_code",
    indexed_columns=("zone_code", "sales_line_code"),
    columns=(
        _c("Sales Line Code", "sales_line_code", FieldKind.CODE, nullable=False,
           description="Parent sales line code. FK -> dim_sales_line.sales_line_code.", example="SL001"),
        _c("Zone Code", "zone_code", FieldKind.CODE, nullable=False,
           description="Official zone code. Business key, unique.", example="Z001"),
        _c("Zone Name", "zone_name", FieldKind.TEXT, nullable=False,
           description="Zone name.", example="Dhaka Zone"),
        _c("Zone Head ID", "zone_head_id", FieldKind.CODE,
           description="Employee ID of the zone head.", example="EMP0210"),
        _c("Zone Head", "zone_head_name", FieldKind.TEXT,
           description="Name of the zone head.", example="Md. Hasan"),
    ),
)

REGION = TableSpec(
    sheet="Region Master",
    table="dim_region",
    surrogate_key="region_id",
    business_key="region_code",
    purpose="Region within a zone.",
    parent_table="dim_zone",
    parent_column="zone_code",
    parent_key="zone_code",
    indexed_columns=("region_code", "zone_code"),
    columns=(
        _c("Zone Code", "zone_code", FieldKind.CODE, nullable=False,
           description="Parent zone code. FK -> dim_zone.zone_code.", example="Z001"),
        _c("Region Code", "region_code", FieldKind.CODE, nullable=False,
           description="Official region code. Business key, unique.", example="REG001"),
        _c("Region", "region_name", FieldKind.TEXT, nullable=False,
           description="Region name.", example="Dhaka"),
        _c("Region Head ID", "region_head_id", FieldKind.CODE,
           description="Employee ID of the region head.", example="EMP0330"),
        _c("Region Head", "region_head_name", FieldKind.TEXT,
           description="Name of the region head.", example="Md. Selim"),
        _c("Region HQ", "region_hq", FieldKind.TEXT,
           description="Head-office town/city of the region.", example="Dhaka"),
        _c("Location", "location", FieldKind.TEXT,
           description="Free-text location descriptor recorded on the sheet.", example="Dhaka North"),
    ),
)

AREA = TableSpec(
    sheet="Area Master",
    table="dim_area",
    surrogate_key="area_id",
    business_key="area_code",
    purpose="Area within a region.",
    parent_table="dim_region",
    parent_column="region_code",
    parent_key="region_code",
    indexed_columns=("area_code", "region_code"),
    columns=(
        _c("Region Code", "region_code", FieldKind.CODE, nullable=False,
           description="Parent region code. FK -> dim_region.region_code.", example="REG001"),
        _c("Area Code", "area_code", FieldKind.CODE, nullable=False,
           description="Official area code. Business key, unique.", example="AR001"),
        _c("Area", "area_name", FieldKind.TEXT, nullable=False,
           description="Area name.", example="Mirpur"),
        _c("Area Head ID", "area_head_id", FieldKind.CODE,
           description="Employee ID of the area head.", example="EMP0440"),
        _c("Area Head", "area_head_name", FieldKind.TEXT,
           description="Name of the area head.", example="Md. Jamal"),
        _c("Area HQ", "area_hq", FieldKind.TEXT,
           description="Head-office town/city of the area.", example="Mirpur"),
        _c("Location", "location", FieldKind.TEXT,
           description="Free-text location descriptor recorded on the sheet.", example="Mirpur-10"),
    ),
)

UNIT = TableSpec(
    sheet="Unit Master",
    table="dim_unit",
    surrogate_key="unit_id",
    business_key="unit_code",
    purpose="Unit within an area.",
    parent_table="dim_area",
    parent_column="area_code",
    parent_key="area_code",
    indexed_columns=("unit_code", "area_code"),
    columns=(
        _c("Area Code", "area_code", FieldKind.CODE, nullable=False,
           description="Parent area code. FK -> dim_area.area_code.", example="AR001"),
        _c("Unit Code", "unit_code", FieldKind.CODE, nullable=False,
           description="Official unit code. Business key, unique.", example="UN001"),
        _c("Unit", "unit_name", FieldKind.TEXT, nullable=False,
           description="Unit name.", example="Mirpur Unit 1"),
        _c("Unit Head ID", "unit_head_id", FieldKind.CODE,
           description="Employee ID of the unit head.", example="EMP0550"),
        _c("Unit Head Name", "unit_head_name", FieldKind.TEXT,
           description="Name of the unit head.", example="Md. Faruk"),
        _c("Unit HQ", "unit_hq", FieldKind.TEXT,
           description="Head-office town/city of the unit.", example="Mirpur"),
        _c("Location", "location", FieldKind.TEXT,
           description="Free-text location descriptor recorded on the sheet.", example="Mirpur-12"),
    ),
)

TERRITORY = TableSpec(
    sheet="Territory Master",
    table="dim_territory",
    surrogate_key="territory_id",
    business_key="territory_code",
    purpose="Territory within a unit.",
    parent_table="dim_unit",
    parent_column="unit_code",
    parent_key="unit_code",
    indexed_columns=("territory_code", "unit_code"),
    columns=(
        _c("Unit Code", "unit_code", FieldKind.CODE, nullable=False,
           description="Parent unit code. FK -> dim_unit.unit_code.", example="UN001"),
        _c("Territory Code", "territory_code", FieldKind.CODE, nullable=False,
           description="Official territory code. Business key, unique.", example="TR001"),
        _c("Territory", "territory_name", FieldKind.TEXT, nullable=False,
           description="Territory name.", example="Kazipara"),
        _c("Territory Head ID", "territory_head_id", FieldKind.CODE,
           description="Employee ID of the territory head.", example="EMP0660"),
        _c("Territory Head", "territory_head_name", FieldKind.TEXT,
           description="Name of the territory head.", example="Md. Anis"),
        _c("Territory Head_Phone Number", "territory_head_phone", FieldKind.PHONE,
           description="Phone number of the territory head. Stored as text.", example="+8801700000000"),
        _c("Territory HQ", "territory_hq", FieldKind.TEXT,
           description="Head-office town/city of the territory.", example="Kazipara"),
        _c("Location", "location", FieldKind.TEXT,
           description="Free-text location descriptor recorded on the sheet.", example="Kazipara Bazar"),
    ),
)

SUB_TERRITORY = TableSpec(
    sheet="Sub-Territory Master",
    table="dim_sub_territory",
    surrogate_key="sub_territory_id",
    business_key="sub_territory_code",
    purpose="Sub-territory within a territory: the lowest level of the hierarchy.",
    parent_table="dim_territory",
    parent_column="territory_code",
    parent_key="territory_code",
    indexed_columns=("sub_territory_code", "territory_code"),
    columns=(
        _c("Territory Code", "territory_code", FieldKind.CODE, nullable=False,
           description="Parent territory code. FK -> dim_territory.territory_code.", example="TR001"),
        _c("Sub Territory Code", "sub_territory_code", FieldKind.CODE, nullable=False,
           description="Official sub-territory code. Business key, unique.", example="STR001"),
        _c("Sub Territory", "sub_territory_name", FieldKind.TEXT, nullable=False,
           description="Sub-territory name.", example="Kazipara North"),
        _c("Sub Territory Head ID", "sub_territory_head_id", FieldKind.CODE,
           description="Employee ID of the sub-territory head.", example="EMP0770"),
        _c("Sub Territory Head", "sub_territory_head_name", FieldKind.TEXT,
           description="Name of the sub-territory head.", example="Md. Rakib"),
        _c("Sub Territory Head_Phone Number", "sub_territory_head_phone", FieldKind.PHONE,
           description="Phone number of the sub-territory head. Stored as text.", example="+8801800000000"),
        _c("Sub Territory HQ", "sub_territory_hq", FieldKind.TEXT,
           description="Head-office town/city of the sub-territory.", example="Kazipara"),
        _c("Location", "location", FieldKind.TEXT,
           description="Free-text location descriptor recorded on the sheet.", example="Kazipara North Block"),
    ),
)

# No Product Master here. Revision 0022 removed ``dim_product`` outright: it
# held a second identity for goods the Material Master already identifies, and
# the two could disagree. The Material Master is **not** a sheet in this
# workbook — it arrives from its own SAP-side extract and is declared in
# ``app.upload.registry`` alongside the Plant and Storage Location masters, for
# the same reason those are. That is why this module's specs stop at
# Sub-Territory: it describes the workbook, and the workbook describes the
# organisational hierarchy.
#
# The workbook still *contains* a "Product Master" sheet. Nothing reads it, and
# nothing rewrites the file to remove it — the source workbook is opened
# read-only by invariant. ``EXPECTED_SHEETS`` no longer names it, and the
# validator ignores a sheet it was not asked about.


#: Import order. Parents strictly before children.
TABLE_SPECS: tuple[TableSpec, ...] = (
    COMPANY,
    BUSINESS_UNIT,
    SALES_LINE,
    ZONE,
    REGION,
    AREA,
    UNIT,
    TERRITORY,
    SUB_TERRITORY,
)

#: Sheets the workbook is contracted to provide.
EXPECTED_SHEETS: tuple[str, ...] = tuple(spec.sheet for spec in TABLE_SPECS)

SPEC_BY_SHEET: dict[str, TableSpec] = {s.sheet: s for s in TABLE_SPECS}
SPEC_BY_TABLE: dict[str, TableSpec] = {s.table: s for s in TABLE_SPECS}

#: Hierarchy, top-down, for reporting and diagram generation.
HIERARCHY: tuple[str, ...] = (
    "dim_company",
    "dim_business_unit",
    "dim_sales_line",
    "dim_zone",
    "dim_region",
    "dim_area",
    "dim_unit",
    "dim_territory",
    "dim_sub_territory",
)


def iter_specs() -> Iterable[TableSpec]:
    return TABLE_SPECS


def spec_for_sheet(sheet: str) -> TableSpec | None:
    """Look up a spec by sheet name, tolerating case/whitespace differences."""
    if sheet in SPEC_BY_SHEET:
        return SPEC_BY_SHEET[sheet]
    key = sheet.strip().casefold()
    for name, spec in SPEC_BY_SHEET.items():
        if name.strip().casefold() == key:
            return spec
    return None


@dataclass(frozen=True)
class ValidationRule:
    """A declarative data-quality rule applied by the validator."""

    rule_id: str
    name: str
    severity: str  # "error" blocks import; "warning" is reported only
    description: str
    applies_to: str = "all tables"


#: The full rule catalogue, published in the data dictionary.
VALIDATION_RULES: tuple[ValidationRule, ...] = (
    ValidationRule("VR001", "Sheet present", "error",
                   "Every expected master sheet must exist in the workbook.",
                   "workbook"),
    ValidationRule("VR002", "Header detected", "error",
                   "A header row/column must be detectable in every sheet.", "workbook"),
    ValidationRule("VR003", "Required source fields present", "error",
                   "Every field defined in the schema must exist in its source sheet."),
    ValidationRule("VR004", "Business key not null", "error",
                   "The official business code of each row must be present."),
    ValidationRule("VR005", "Business key unique", "error",
                   "The official business code must be unique within its dimension."),
    ValidationRule("VR006", "Required fields not null", "error",
                   "Columns declared NOT NULL must carry a value."),
    ValidationRule("VR007", "Parent reference present", "error",
                   "A child row must carry its parent code.",
                   "all tables with a foreign key"),
    ValidationRule("VR008", "Parent reference resolves", "error",
                   "A child row's parent code must exist in the parent dimension "
                   "(orphan detection).", "all tables with a foreign key"),
    ValidationRule("VR009", "Fully duplicated row", "warning",
                   "Two rows identical across every mapped column."),
    ValidationRule("VR010", "Type coercion succeeds", "error",
                   "Integer / decimal / timestamp fields must parse to their declared type."),
    ValidationRule("VR011", "No leading or trailing whitespace", "warning",
                   "Raw values must not carry outer whitespace; the cleaner trims them."),
    ValidationRule("VR012", "No empty strings", "warning",
                   "Present-but-empty strings are normalised to NULL."),
    ValidationRule("VR013", "Case consistency of codes", "warning",
                   "Codes differing only by letter case are flagged as probable duplicates."),
    ValidationRule("VR014", "Codes stored as text in Excel", "warning",
                   "A code cell typed as a number in Excel may already have lost leading "
                   "zeros at source."),
    # VR015 checked for a duplicate SKU Id on the Product Master. That master
    # was removed in revision 0022 and the Material Master has no second
    # identifier to collide — ``material_code`` is the only one — so the rule has
    # nothing left to check. The number is retired rather than reused: an error
    # code in a report someone kept is a promise about what it meant.
    ValidationRule("VR016", "Phone stored as text", "warning",
                   "Phone numbers must not be numeric in the source sheet.",
                   "dim_territory, dim_sub_territory"),
)

BLOCKING_SEVERITY = "error"
