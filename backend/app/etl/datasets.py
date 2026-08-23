"""Dataset specifications: the configurable contract for each transaction type.

One ``DatasetSpec`` per data type declares:

* the canonical fields and the **source column aliases** that map onto them, so
  an Excel export headed "Invoice No", a CSV headed ``invoice_number`` and a
  future SAP payload keyed ``INVOICE_NO`` all land in the same staging column;
* which fields are required, which are dates, which are numeric and where a
  negative value is legitimate;
* the **business key** used for duplicate detection and idempotent re-import.

Adding SAP or the Sales Force App means adding a reader (``etl.readers``) that
yields dictionaries — no change to the specs, the staging tables or the facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from ..utils.text import snake_case

#: Organisational levels, shallowest first. Mirrors the Phase 1 hierarchy.
ORG_LEVELS: tuple[str, ...] = (
    "company_code",
    "bu_code",
    "sales_line_code",
    "zone_code",
    "region_code",
    "area_code",
    "unit_code",
    "territory_code",
    "sub_territory_code",
)

#: Levels a target file states. A target is set for a territory, so those are
#: the only two levels it carries; everything above them is *derived* from the
#: master hierarchy by ``MasterDataIndex.resolve_org`` and stored as surrogate
#: keys on the fact row. A Target file that repeats the region or the zone is
#: duplicating master data into a fact table, which is exactly what the Target
#: structure exists to prevent — those columns are reported as unmapped.
TARGET_ORG_LEVELS: tuple[str, ...] = ORG_LEVELS[7:]


class FieldKind:
    CODE = "code"
    TEXT = "text"
    DATE = "date"
    NUMERIC = "numeric"


@dataclass(frozen=True)
class FieldSpec:
    """One canonical field and the source headers that map onto it."""

    name: str
    kind: str
    aliases: tuple[str, ...] = ()
    required: bool = False
    allow_negative: bool = True
    #: Used when the source omits an optional numeric column entirely.
    default_numeric: float | None = None
    description: str = ""
    #: Trim and upper-case this field before it goes into the business key.
    #:
    #: For codes a source writes inconsistently. `" batch-a "` and `"BATCH-A"`
    #: are the same batch, and treating them as two would let the same line in
    #: twice. The **stored** value keeps its original spacing and case — the
    #: normalisation applies to the comparison only, never to the data.
    normalise_for_key: bool = False

    def matches(self, header: str) -> bool:
        key = snake_case(header)
        return key == self.name or key in {snake_case(a) for a in self.aliases}


_ORG_ALIASES: dict[str, tuple[str, ...]] = {
    "company_code": ("company", "comp_code", "company id"),
    "bu_code": ("bu", "business unit code", "business_unit_code", "business unit"),
    "sales_line_code": ("sales line", "salesline code", "sl_code", "sales_line"),
    "zone_code": ("zone", "zone id"),
    "region_code": ("region", "region id"),
    "area_code": ("area", "area id"),
    "unit_code": ("unit", "unit id"),
    "territory_code": ("territory", "territory id", "tr_code"),
    "sub_territory_code": ("sub territory", "sub-territory code", "sub_territory",
                           "subterritory code"),
}


def _org_field(level: str, *, required: bool = False,
               description: str = "") -> FieldSpec:
    """One organisational code field."""
    return FieldSpec(
        level, FieldKind.CODE, _ORG_ALIASES[level], required=required,
        description=description
        or f"Official {level.replace('_', ' ')} from the master hierarchy.",
    )


def _org_fields() -> tuple[FieldSpec, ...]:
    """Organisational code fields. All optional individually: a source may carry
    only ``region_code``, or only ``territory_code``; the mapper derives the rest.
    A row with none of them at all is rejected (``NO_ORGANISATIONAL_CODE``)."""
    return tuple(_org_field(level) for level in ORG_LEVELS)


@dataclass(frozen=True)
class DatasetSpec:
    """Everything the ETL needs to know about one transaction data type."""

    data_type: str
    staging_table: str
    fact_table: str
    fields: tuple[FieldSpec, ...]
    #: Canonical field names forming the de-duplication key, in order.
    #: ``source_system`` is appended automatically so DEMO and production data
    #: never collide.
    business_key_fields: tuple[str, ...]
    #: Field whose value becomes ``date_id``, or ``None`` for a dataset that has
    #: no reporting date at all.
    #:
    #: Material stock is the case: the source states no posting date, so the fact
    #: table carries no ``date_id`` and the pipeline has nothing to resolve
    #: against ``dim_date``. That is different from a dataset whose date is merely
    #: optional — there is no date, and inventing one from the upload clock would
    #: turn a current position into a false history.
    date_field: str | None
    #: A better key, used whenever the source supplies every field in it.
    #:
    #: Sales is the case this exists for. An ERP that emits an invoice line
    #: number has already decided what a line *is*, and that decision beats any
    #: key reconstructed from the line's attributes. When the column is absent —
    #: as it is in most spreadsheet exports — the fallback key identifies the
    #: line by what distinguishes it: invoice, material and batch.
    preferred_key_fields: tuple[str, ...] | None = None
    #: Organisational levels this dataset may carry.
    org_levels: tuple[str, ...] = ORG_LEVELS
    #: True when a valid Material Code is mandatory.
    #:
    #: Named for the dimension it resolves against, which since revision 0022 is
    #: ``dim_material`` for every dataset that names an item — a sale, a target
    #: and a stock position all reach the same master by the same code.
    material_required: bool = False
    #: True when the Material Code is optional but validated if present.
    material_optional: bool = False
    #: True when the customer's and the sales force member's recorded
    #: territories must agree with the organisational codes on the row.
    #:
    #: Set for targets and deliberately not for actuals. A sale records where a
    #: transaction *happened*, and a customer legitimately buying outside its
    #: usual sub-territory is a fact to keep, not an error. A target records
    #: where a plan was *assigned*, and one filed against a territory its own
    #: customer or officer does not belong to cannot be achieved by them — so it
    #: is a data-entry error, and reporting it at upload is the only point at
    #: which it is cheap to fix.
    check_assignment_consistency: bool = False
    description: str = ""

    @property
    def field_map(self) -> dict[str, FieldSpec]:
        return {f.name: f for f in self.fields}

    @property
    def required_fields(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.required)

    @property
    def date_fields(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.kind == FieldKind.DATE)

    @property
    def numeric_fields(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.kind == FieldKind.NUMERIC)

    @property
    def business_key_definition(self) -> str:
        """Human-readable key definition, published in the README and the API."""
        fallback = " + ".join([*self.business_key_fields, "source_system"])
        if not self.preferred_key_fields:
            return fallback
        preferred = " + ".join([*self.preferred_key_fields, "source_system"])
        return f"{preferred} when available, otherwise {fallback}"

    def key_fields_for(self, record: dict) -> tuple[str, ...]:
        """Which key this record is identified by.

        The preferred key applies only when the source filled in every part of
        it. A half-populated line number is worse than none: it would put some
        rows on one key and some on another, and two rows that are genuinely
        the same line could end up with different keys.
        """
        if self.preferred_key_fields and all(
            not _blank(record.get(name)) for name in self.preferred_key_fields
        ):
            return self.preferred_key_fields
        return self.business_key_fields

    def resolve_header(self, header: str) -> str | None:
        """Map a source column header onto a canonical field name, or ``None``."""
        for spec in self.fields:
            if spec.matches(header):
                return spec.name
        return None

    def build_business_key(self, record: dict, source_system: str) -> str:
        """Deterministic key from the *cleaned* record.

        Cleaned values are used so that ``15/08/2026`` and ``2026-08-15`` from two
        different sources produce the same key. Codes keep their exact case, in
        line with the Phase 1 rule that official codes are never rewritten —
        except where a field declares ``normalise_for_key``, which trims and
        upper-cases *the comparison* while the stored value keeps its original
        form.

        The key names the fields it was built from, so two records identified by
        different keys can never collide: a line keyed on its ERP line number
        and one keyed on its batch produce visibly different strings even when
        their invoice and material match.
        """
        fields = self.key_fields_for(record)
        by_name = self.field_map
        parts = [":".join(fields)]
        for name in fields:
            value = record.get(name)
            text = "" if value is None else str(value)
            spec = by_name.get(name)
            if spec is not None and spec.normalise_for_key:
                text = text.strip().upper()
            parts.append(text)
        parts.append(source_system)
        return "|".join(parts)


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


_COMMON_SOURCE_ID = FieldSpec(
    "source_transaction_id", FieldKind.TEXT,
    ("source id", "transaction id", "doc no", "document number", "source_id"),
    description="Identifier of the record in the originating system.",
)

_CUSTOMER = FieldSpec(
    "customer_code", FieldKind.CODE, ("customer", "customer id", "dealer code", "party code"),
    description="Customer code. Validated once dim_customer has a source.",
)
_SALES_FORCE = FieldSpec(
    "sales_force_code", FieldKind.CODE,
    ("sales force", "sr code", "so code", "employee code", "sales_force"),
    description="Sales-force code. Validated once dim_sales_force has a source.",
)


SALES = DatasetSpec(
    data_type="sales",
    staging_table="stg_sales",
    fact_table="fact_sales",
    date_field="transaction_date",
    material_required=True,
    # An invoice line is not identified by invoice + material. The same material
    # can legitimately appear twice on one invoice from two different batches,
    # and keying on invoice + material rejected the second as a duplicate. Batch
    # is part of what makes a line distinct, and company scopes the invoice
    # number — two companies may each issue an INV001.
    business_key_fields=("company_code", "invoice_no", "material_code", "batch_code"),
    # When the source ERP numbers its own lines, that number *is* the identity
    # of the line and nothing needs reconstructing.
    preferred_key_fields=("company_code", "invoice_no", "invoice_line_no"),
    description="Invoice-line level sales transactions.",
    fields=(
        FieldSpec("transaction_date", FieldKind.DATE,
                  ("date", "invoice date", "sales date", "txn date", "posting date"),
                  required=True, description="Date the sale was invoiced."),
        FieldSpec("invoice_no", FieldKind.TEXT,
                  ("invoice", "invoice number", "bill no", "inv no",
                   "invoice code"),
                  required=True, description="Invoice number."),
        FieldSpec("invoice_line_no", FieldKind.TEXT,
                  ("invoice line", "invoice line no", "line no", "line number",
                   "invoice item no", "item no", "invoice detail id",
                   "invoice_detail_id", "line item", "posnr"),
                  description="The source system's own line identifier. When "
                              "present it identifies the line and the batch "
                              "key is not needed. Never invented — a line "
                              "number this system made up would not match the "
                              "ERP's on the next import."),
        # The aliases keep every heading a sales export has ever used, including
        # the SKU spellings: what the column is *called* in somebody's
        # spreadsheet is not what it resolves against, and a file that headed
        # this "SKU Code" last month still loads.
        FieldSpec("material_code", FieldKind.CODE,
                  ("material", "material no", "material number", "matnr",
                   "material id", "sku", "sku code", "product code", "item code",
                   "product"),
                  required=True,
                  description="Material code; must exist in the Material Master."),
        FieldSpec("batch_code", FieldKind.CODE,
                  ("batch", "batch no", "batch number", "lot", "lot no",
                   "lot number", "batch id"),
                  normalise_for_key=True,
                  description="Manufacturing batch or lot. Part of what makes "
                              "a line unique: the same material on the same "
                              "invoice from two batches is two lines. Compared trimmed "
                              "and upper-cased; stored exactly as supplied."),
        *_org_fields(),
        _CUSTOMER, _SALES_FORCE,
        FieldSpec("quantity", FieldKind.NUMERIC, ("qty", "sales qty", "quantity sold"),
                  required=True, allow_negative=True,
                  description="Quantity sold. Negative values represent returns."),
        # Net sales is the required figure and gross is optional, not the other
        # way round. Net is what every report, target and margin is computed
        # from, so a row without it cannot be used for anything; gross and
        # discount are the breakdown behind it, and plenty of sources send only
        # the settled figure. Requiring gross would reject rows the warehouse
        # can account for perfectly well.
        FieldSpec("gross_sales", FieldKind.NUMERIC,
                  ("gross", "gross amount", "gross value", "gross sales amount"),
                  allow_negative=True,
                  description="Gross sales value, before discount. Optional: "
                              "derived as net_sales + discount when absent. "
                              "Negative on a credit note."),
        FieldSpec("discount", FieldKind.NUMERIC, ("disc", "discount amount", "trade discount"),
                  allow_negative=False, default_numeric=0,
                  description="Discount value. A negative discount is rejected."),
        FieldSpec("net_sales", FieldKind.NUMERIC, ("net", "net amount", "net value"),
                  required=True, allow_negative=True,
                  description="Net sales — the settled value of the line, and "
                              "the figure every report is built on. Required."),
        FieldSpec("cost", FieldKind.NUMERIC, ("cogs", "cost amount", "total cost"),
                  allow_negative=True,
                  description="Cost of goods sold. Gross profit is NULL when absent."),
        # Total Volume is a transactional source value, like quantity and net
        # sales, and it is the *only* source of a sale's volume. It is stored
        # exactly as supplied and never derived from quantity, a unit of
        # measure or a conversion factor — see ``etl.volume``. There is no
        # companion unit column: the file states one total per line and that
        # number is what every report sums.
        FieldSpec("volume", FieldKind.NUMERIC,
                  ("vol", "volume", "total volume", "total vol", "volume qty",
                   "sales volume", "total volume qty"),
                  allow_negative=True,
                  description="Total Volume of the line, as the source system "
                              "recorded it. Stored exactly as supplied and "
                              "used as the sales volume unchanged. A row "
                              "without one loads and is flagged Volume "
                              "Missing; nothing is derived to fill the gap. "
                              "Negative only alongside a negative quantity."),
        _COMMON_SOURCE_ID,
    ),
)


# A material stock position, not a day's movement.
#
# ``date_field`` is None because the source carries no posting date: Production
# Date and Shelf Life Expiration Date describe the goods, not when the reading
# was taken. The position is therefore current by definition and a re-upload
# updates the same rows, which is what the business key says.
#
# The key includes both dates deliberately. Company + Plant + Storage Location +
# Material Code alone would collapse every expiry batch of a material into one
# row, and expiry analysis is precisely the thing that needs them apart.
#
# Material Group and Material Brand are *not* in the key. Since revision 0019
# both are attributes the Material Master records against the material code, so
# the code already determines them; including them would let a file with a
# mistyped group write a second position for stock that physically exists once.
# The row still has to state them, and the ETL still rejects the row when they
# disagree with the master — that check is what makes them safe to leave out.
MATERIAL_STOCK = DatasetSpec(
    data_type="material_stock",
    staging_table="stg_material_stock",
    fact_table="fact_material_stock",
    date_field=None,
    org_levels=(),
    # Not ``material_required``: a stock position resolves its material through
    # :meth:`MasterDataIndex.resolve_stock_masters` along with its plant and its
    # storage location, because the three are checked together and the group and
    # brand the row repeats are cross-checked against the same master row. The
    # flag drives the single-item lookup that sales and target use.
    material_required=False,
    business_key_fields=("company_code", "plant_code", "storage_location_code",
                         "material_code",
                         "production_date", "shelf_life_expiration_date"),
    description=(
        "Current material stock position per plant, storage location and "
        "material, split by what the stock is available for."
    ),
    fields=(
        FieldSpec("company_code", FieldKind.CODE, ("company", "company id", "bukrs"),
                  required=True, description="Company code."),
        FieldSpec("plant_code", FieldKind.CODE, ("plant", "plant id", "werks"),
                  required=True,
                  description="Plant code; the company + plant pair must exist "
                              "in the Plant Master."),
        FieldSpec("storage_location_code", FieldKind.CODE,
                  ("storage location", "sloc", "storage loc", "lgort"),
                  required=True,
                  description="Storage location code; the plant + storage "
                              "location pair must exist in the Storage Location "
                              "Master."),
        FieldSpec("material_code", FieldKind.CODE,
                  ("material", "material no", "material number", "matnr",
                   "material id"),
                  required=True,
                  description="The material itself; must exist in the Material "
                              "Master. A material group classifies many "
                              "materials, so it cannot identify one — this can."),
        FieldSpec("material_group_code", FieldKind.CODE,
                  ("material group", "matl group", "mat group", "matkl"),
                  required=True,
                  description="Material group code; must agree with the group "
                              "the Material Master records for this material."),
        FieldSpec("material_brand_code", FieldKind.CODE,
                  ("material brand", "brand code", "brand", "matl brand"),
                  required=True,
                  description="Material brand code; must agree with the brand "
                              "the Material Master records for this material. "
                              "The material's own brand, which is what a sales "
                              "report groups by too since revision 0022."),
        FieldSpec("unrestricted_stock", FieldKind.NUMERIC,
                  ("unrestricted", "unrestricted qty", "free stock",
                   "unrestricted use"),
                  allow_negative=True, default_numeric=0,
                  description="Stock available to use or sell without "
                              "restriction."),
        FieldSpec("quality_inspection_stock", FieldKind.NUMERIC,
                  ("quality inspection", "stock in quality inspection", "qi stock",
                   "quality stock", "qi"),
                  allow_negative=True, default_numeric=0,
                  description="Stock held pending quality inspection. Owned, but "
                              "not available."),
        FieldSpec("blocked_stock", FieldKind.NUMERIC,
                  ("blocked", "blocked qty", "block stock"),
                  allow_negative=True, default_numeric=0,
                  description="Stock blocked from use."),
        FieldSpec("stock_in_transit", FieldKind.NUMERIC,
                  ("in transit", "transit", "stock in transit", "transit stock"),
                  allow_negative=True, default_numeric=0,
                  description="Stock in transit — owned, but not yet arrived. "
                              "Reported separately from what is on hand."),
        FieldSpec("production_date", FieldKind.DATE,
                  ("prod date", "production", "manufacture date", "mfg date"),
                  description="When the goods were produced. An attribute of the "
                              "goods, never the reporting date."),
        FieldSpec("shelf_life_expiration_date", FieldKind.DATE,
                  ("expiry date", "expiration date", "shelf life", "sled",
                   "shelf life expiry"),
                  description="When the goods expire. Absent means no shelf life "
                              "is recorded, which is reported as its own group "
                              "rather than treated as valid."),
        _COMMON_SOURCE_ID,
    ),
)


# A target is a clean fact: ten columns, of which seven are master-data codes
# and three are measures. No descriptive master attribute — no territory name,
# customer name or material description — is carried here. Those are reached through the
# dimensions the codes resolve to, so renaming a customer changes every report
# at once instead of leaving last year's targets naming the old one.
#
# The period is stated the way targets are actually set — a month of a financial
# year — rather than as a date. ``etl.period`` turns the pair into the first of
# the month, which is what ``date_field`` names below; there is no ``target_date``
# column in the file or in staging, because the source does not supply one.
TARGET = DatasetSpec(
    data_type="target",
    staging_table="stg_target",
    fact_table="fact_target",
    date_field="target_date",
    org_levels=TARGET_ORG_LEVELS,
    material_required=True,
    check_assignment_consistency=True,
    # The grain, spelled out: one target per month, per financial year, per
    # territory / sub-territory, per customer, per material, per sales force
    # member. Re-uploading the same combination updates that row rather than
    # adding a second target for it.
    business_key_fields=(
        "financial_year", "target_month",
        "territory_code", "sub_territory_code", "customer_code",
        "material_code", "sales_force_code",
    ),
    description="Monthly sales targets by territory, customer, material and sales force.",
    fields=(
        FieldSpec("target_month", FieldKind.TEXT,
                  ("month", "target period", "period", "target month name",
                   "month name"),
                  required=True,
                  description="The month the target is set for: '2026-08', a "
                              "month name such as 'August', or a month number. "
                              "Validated against the financial year on the same "
                              "row and never guessed."),
        FieldSpec("financial_year", FieldKind.TEXT,
                  ("fy", "fin year", "financial yr", "fiscal year", "year"),
                  required=True,
                  description="Financial year the month must fall inside — "
                              "'FY 2026-27' or '2026-27'. The year boundary is "
                              "configuration (FINANCIAL_YEAR_START_MONTH), not "
                              "a calendar-year assumption."),
        _org_field("territory_code", required=True,
                   description="Territory the target is set for. Must exist in "
                               "dim_territory; the region, zone and everything "
                               "above are derived from it."),
        _org_field("sub_territory_code",
                   description="Optional. When supplied it must exist in "
                               "dim_sub_territory and belong to the territory "
                               "on the same row; when absent the target sits at "
                               "territory level."),
        FieldSpec("customer_code", FieldKind.CODE,
                  ("customer", "customer id", "dealer code", "party code"),
                  description="Optional. Validated against the Customer Master, "
                              "and its recorded sub-territory must agree with "
                              "the row's — a target filed under the wrong "
                              "territory is a wrong report."),
        # The planners' file still heads this column "SKU Code" and always will;
        # the aliases keep it loading. What it resolves against is the Material
        # Master, which is the only item master there is.
        FieldSpec("material_code", FieldKind.CODE,
                  ("material", "material code", "material no", "matnr",
                   "sku", "sku code", "product code", "item code", "product"),
                  required=True,
                  description="The material the target is set for; must exist "
                              "in the Material Master."),
        _SALES_FORCE,
        FieldSpec("target_quantity", FieldKind.NUMERIC,
                  ("target qty", "target quantity", "qty target", "quantity target"),
                  allow_negative=False,
                  description="Optional quantity target. Negative targets are "
                              "rejected. Absent means no quantity target was "
                              "set, which is not the same as a target of zero."),
        FieldSpec("target_volume", FieldKind.NUMERIC,
                  ("target vol", "target volume", "volume target", "vol target"),
                  allow_negative=False,
                  description="Optional volume target, stored exactly as "
                              "supplied and reported unit-free. The Material "
                              "Master states no unit of measure, so asserting "
                              "one here would invent a measurement the source "
                              "never made."),
        FieldSpec("target_amount", FieldKind.NUMERIC,
                  ("target", "target value", "budget", "target sales"),
                  required=True, allow_negative=False,
                  description="Target value. The one required measure: a target "
                              "set in taka alone is a complete target. Negative "
                              "targets are rejected."),
        _COMMON_SOURCE_ID,
    ),
)


DATASETS: tuple[DatasetSpec, ...] = (SALES, MATERIAL_STOCK, TARGET)
DATASET_BY_TYPE: dict[str, DatasetSpec] = {d.data_type: d for d in DATASETS}
DATA_TYPES: tuple[str, ...] = tuple(d.data_type for d in DATASETS)


def get_dataset(data_type: str) -> DatasetSpec:
    """Look up a dataset spec, raising a clear error for an unknown type."""
    key = data_type.strip().lower()
    if key not in DATASET_BY_TYPE:
        raise ValueError(
            f"Unknown data type {data_type!r}. Supported: {', '.join(DATA_TYPES)}."
        )
    return DATASET_BY_TYPE[key]


def map_headers(spec: DatasetSpec, headers: Iterable[str]) -> tuple[dict[str, str], list[str]]:
    """Map source headers onto canonical fields.

    Returns ``({source_header: canonical_field}, unmapped_headers)``. Unmapped
    headers are reported, never dropped silently — the whole original row is
    still preserved in ``raw_data``.
    """
    mapping: dict[str, str] = {}
    unmapped: list[str] = []
    for header in headers:
        if header is None:
            continue
        canonical = spec.resolve_header(str(header))
        if canonical is None:
            unmapped.append(str(header))
        else:
            mapping[str(header)] = canonical
    return mapping, unmapped


__all__ = [
    "ORG_LEVELS",
    "TARGET_ORG_LEVELS",
    "FieldKind",
    "FieldSpec",
    "DatasetSpec",
    "SALES",
    "MATERIAL_STOCK",
    "TARGET",
    "DATASETS",
    "DATASET_BY_TYPE",
    "DATA_TYPES",
    "get_dataset",
    "map_headers",
]
