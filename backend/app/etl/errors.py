"""ETL error codes and categories.

Every rejection carries a stable ``error_code`` (machine-readable, used by the
data-quality API) and an ``error_category`` (the bucket shown in the import
summary). Nothing is ever rejected without one of these.
"""

from __future__ import annotations

from dataclasses import dataclass


class ErrorCategory:
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    INVALID_DATE = "INVALID_DATE"
    INVALID_NUMERIC = "INVALID_NUMERIC"
    INVALID_MASTER = "INVALID_MASTER"
    INVALID_HIERARCHY = "INVALID_HIERARCHY"
    DUPLICATE = "DUPLICATE"
    STRUCTURE = "STRUCTURE"
    #: Findings that describe a row rather than disqualify it. A sale whose
    #: volume is absent is still a sale, and its net sales figure is still
    #: right, so it loads and is counted here for correction instead of being
    #: thrown away. Only ``severity`` distinguishes the two: everything in this
    #: category is a WARNING.
    DATA_QUALITY = "DATA_QUALITY"


@dataclass(frozen=True)
class ErrorSpec:
    code: str
    category: str
    description: str


def _e(code: str, category: str, description: str) -> ErrorSpec:
    return ErrorSpec(code, category, description)


# --- structure / required fields -------------------------------------------
MISSING_REQUIRED_FIELD = _e(
    "MISSING_REQUIRED_FIELD", ErrorCategory.MISSING_REQUIRED_FIELD,
    "A field the dataset declares as required is empty.")
MISSING_SOURCE_COLUMN = _e(
    "MISSING_SOURCE_COLUMN", ErrorCategory.STRUCTURE,
    "A required column is absent from the source file.")
NO_ORGANISATIONAL_CODE = _e(
    "NO_ORGANISATIONAL_CODE", ErrorCategory.MISSING_REQUIRED_FIELD,
    "The row carries no organisational code at any level, so it cannot be "
    "attached to the hierarchy.")

# --- dates ------------------------------------------------------------------
INVALID_DATE = _e(
    "INVALID_DATE", ErrorCategory.INVALID_DATE,
    "The value is not a valid date, or does not match the configured format.")
AMBIGUOUS_DATE = _e(
    "AMBIGUOUS_DATE", ErrorCategory.INVALID_DATE,
    "The value could be read as either DD/MM/YYYY or MM/DD/YYYY. The source's "
    "date format must be configured; the ETL never guesses.")
IMPOSSIBLE_DATE = _e(
    "IMPOSSIBLE_DATE", ErrorCategory.INVALID_DATE,
    "The date does not exist in the calendar (for example 31/02/2026).")

# --- target period ----------------------------------------------------------
# A target names a month and a financial year rather than a date, so it is the
# pair that has to resolve. Three distinct failures, because the correction
# differs: the month is unreadable, the year is unreadable, or both parse and
# contradict each other.
INVALID_TARGET_MONTH = _e(
    "INVALID_TARGET_MONTH", ErrorCategory.INVALID_DATE,
    "The target month is not one of the accepted forms (YYYY-MM, an English "
    "month name or abbreviation, a month number 1-12, or a real date). It is "
    "rejected rather than interpreted.")
INVALID_FINANCIAL_YEAR = _e(
    "INVALID_FINANCIAL_YEAR", ErrorCategory.INVALID_DATE,
    "The financial year is not one of the accepted forms (FY 2026-27, "
    "2026-27, 2026-2027, or a single year where the financial year starts in "
    "January).")
TARGET_PERIOD_MISMATCH = _e(
    "TARGET_PERIOD_MISMATCH", ErrorCategory.INVALID_DATE,
    "The target month does not fall inside the financial year stated on the "
    "same row. Both values are readable and they disagree, so neither can be "
    "trusted to override the other.")

# --- numerics ---------------------------------------------------------------
INVALID_NUMERIC = _e(
    "INVALID_NUMERIC", ErrorCategory.INVALID_NUMERIC,
    "The value is not numeric once thousands separators are removed.")
NEGATIVE_NOT_ALLOWED = _e(
    "NEGATIVE_NOT_ALLOWED", ErrorCategory.INVALID_NUMERIC,
    "A negative value was supplied for a measure whose business rules forbid it.")

# --- master data ------------------------------------------------------------
INVALID_COMPANY_CODE = _e("INVALID_COMPANY_CODE", ErrorCategory.INVALID_MASTER,
                          "Company code does not exist in dim_company.")
INVALID_BU_CODE = _e("INVALID_BU_CODE", ErrorCategory.INVALID_MASTER,
                     "Business unit code does not exist in dim_business_unit.")
INVALID_SALES_LINE_CODE = _e("INVALID_SALES_LINE_CODE", ErrorCategory.INVALID_MASTER,
                             "Sales line code does not exist in dim_sales_line.")
INVALID_ZONE_CODE = _e("INVALID_ZONE_CODE", ErrorCategory.INVALID_MASTER,
                       "Zone code does not exist in dim_zone.")
INVALID_REGION_CODE = _e("INVALID_REGION_CODE", ErrorCategory.INVALID_MASTER,
                         "Region code does not exist in dim_region.")
INVALID_AREA_CODE = _e("INVALID_AREA_CODE", ErrorCategory.INVALID_MASTER,
                       "Area code does not exist in dim_area.")
INVALID_UNIT_CODE = _e("INVALID_UNIT_CODE", ErrorCategory.INVALID_MASTER,
                       "Unit code does not exist in dim_unit.")
INVALID_TERRITORY_CODE = _e("INVALID_TERRITORY_CODE", ErrorCategory.INVALID_MASTER,
                            "Territory code does not exist in dim_territory.")
INVALID_SUB_TERRITORY_CODE = _e("INVALID_SUB_TERRITORY_CODE", ErrorCategory.INVALID_MASTER,
                                "Sub-territory code does not exist in dim_sub_territory.")
# A stock position resolves against three masters, so it fails in three distinct
# ways and each names the master that has to be corrected. One combined code
# would leave an operator re-reading every one of them to find which.
# All three reject rather than tolerate: unlike the PENDING_SOURCE_DATA
# dimensions below, a stock position whose plant, storage location or material
# is unknown has no name to report under and could not appear in a report at all.
#
# ``INVALID_MATERIAL_CODE`` serves sales and target as well since revision 0022,
# which removed the SKU dimension and its ``INVALID_SKU_CODE``: one item master
# means one way for an item code to be wrong.
INVALID_PLANT_CODE = _e(
    "INVALID_PLANT_CODE", ErrorCategory.INVALID_MASTER,
    "The company + plant combination does not exist in the Plant Master. A "
    "plant code identifies a plant within a company, so both are checked "
    "together.")
INVALID_STORAGE_LOCATION_CODE = _e(
    "INVALID_STORAGE_LOCATION_CODE", ErrorCategory.INVALID_MASTER,
    "The plant + storage location combination does not exist in the Storage "
    "Location Master. A storage location code is unique only within its plant, "
    "so both are checked together.")
INVALID_MATERIAL_CODE = _e(
    "INVALID_MATERIAL_CODE", ErrorCategory.INVALID_MASTER,
    "Material code does not exist in dim_material. The Material Master is what "
    "gives the row its description, group and brand, so a material it does not "
    "know cannot be reported.")
MATERIAL_GROUP_MISMATCH = _e(
    "MATERIAL_GROUP_MISMATCH", ErrorCategory.INVALID_MASTER,
    "The Material Master records this material under a different material group "
    "than the row states. A material group classifies the material, so the two "
    "cannot disagree; which of them is wrong this system cannot know, so it "
    "reports the disagreement instead of choosing.")
MATERIAL_BRAND_MISMATCH = _e(
    "MATERIAL_BRAND_MISMATCH", ErrorCategory.INVALID_MASTER,
    "The Material Master records this material under a different material brand "
    "than the row states. Reported the same way as a group disagreement, and "
    "for the same reason: the brand is an attribute of the material, so one of "
    "the two sources is wrong and neither may silently win.")
#: Retired with revision 0019, which replaced the single Material Master with a
#: Plant, a Storage Location and a Material Master. Kept defined so the
#: rejections already recorded under it still resolve to a description in
#: ``/api/etl/errors`` and the data-quality views; nothing emits it any more.
INVALID_MATERIAL_LOCATION = _e(
    "INVALID_MATERIAL_LOCATION", ErrorCategory.INVALID_MASTER,
    "Retired. The company + plant + storage location + material + material "
    "group combination did not exist in the single Material Master that "
    "preceded revision 0019. Superseded by INVALID_PLANT_CODE, "
    "INVALID_STORAGE_LOCATION_CODE and INVALID_MATERIAL_CODE, each of which "
    "names the one master at fault.")
INVALID_CUSTOMER_CODE = _e(
    "INVALID_CUSTOMER_CODE", ErrorCategory.INVALID_MASTER,
    "Customer code does not exist in dim_customer. Only enforced once the "
    "customer master has a real source; while it is PENDING_SOURCE_DATA the code "
    "is preserved on the fact row instead.")
INVALID_SALES_FORCE_CODE = _e(
    "INVALID_SALES_FORCE_CODE", ErrorCategory.INVALID_MASTER,
    "Sales-force code does not exist in dim_sales_force. Only enforced once the "
    "sales-force master has a real source.")
#: Retired with revision 0020, which removed ``dim_warehouse``. Kept defined so
#: any rejection already recorded under it still resolves to a description;
#: nothing emits it any more.
INVALID_WAREHOUSE_CODE = _e(
    "INVALID_WAREHOUSE_CODE", ErrorCategory.INVALID_MASTER,
    "Retired. A warehouse code did not exist in the dim_warehouse dimension "
    "that preceded revision 0020. Stock is located by Plant and Storage "
    "Location, and a sale states no warehouse, so there is no code to check.")

#: Optional dimension -> (code field, error code).
OPTIONAL_DIMENSION_ERRORS: dict[str, ErrorSpec] = {
    "dim_customer": INVALID_CUSTOMER_CODE,
    "dim_sales_force": INVALID_SALES_FORCE_CODE,
}

#: Organisational level -> error code, used by the master-data resolver.
MASTER_ERROR_BY_LEVEL: dict[str, ErrorSpec] = {
    "company_code": INVALID_COMPANY_CODE,
    "bu_code": INVALID_BU_CODE,
    "sales_line_code": INVALID_SALES_LINE_CODE,
    "zone_code": INVALID_ZONE_CODE,
    "region_code": INVALID_REGION_CODE,
    "area_code": INVALID_AREA_CODE,
    "unit_code": INVALID_UNIT_CODE,
    "territory_code": INVALID_TERRITORY_CODE,
    "sub_territory_code": INVALID_SUB_TERRITORY_CODE,
}

# --- hierarchy --------------------------------------------------------------
BU_COMPANY_MISMATCH = _e("BU_COMPANY_MISMATCH", ErrorCategory.INVALID_HIERARCHY,
                         "The business unit does not belong to the given company.")
SALES_LINE_BU_MISMATCH = _e("SALES_LINE_BU_MISMATCH", ErrorCategory.INVALID_HIERARCHY,
                            "The sales line does not belong to the given business unit.")
ZONE_SALES_LINE_MISMATCH = _e("ZONE_SALES_LINE_MISMATCH", ErrorCategory.INVALID_HIERARCHY,
                              "The zone does not belong to the given sales line.")
REGION_ZONE_MISMATCH = _e("REGION_ZONE_MISMATCH", ErrorCategory.INVALID_HIERARCHY,
                          "The region does not belong to the given zone.")
AREA_REGION_MISMATCH = _e("AREA_REGION_MISMATCH", ErrorCategory.INVALID_HIERARCHY,
                          "The area does not belong to the given region.")
UNIT_AREA_MISMATCH = _e("UNIT_AREA_MISMATCH", ErrorCategory.INVALID_HIERARCHY,
                        "The unit does not belong to the given area.")
TERRITORY_UNIT_MISMATCH = _e("TERRITORY_UNIT_MISMATCH", ErrorCategory.INVALID_HIERARCHY,
                             "The territory does not belong to the given unit.")
SUB_TERRITORY_TERRITORY_MISMATCH = _e(
    "SUB_TERRITORY_TERRITORY_MISMATCH", ErrorCategory.INVALID_HIERARCHY,
    "The sub-territory does not belong to the given territory.")
HIERARCHY_MISMATCH = _e(
    "HIERARCHY_MISMATCH", ErrorCategory.INVALID_HIERARCHY,
    "Two organisational codes on the row belong to different branches of the "
    "hierarchy. Used when the two levels are not directly adjacent.")
CUSTOMER_HIERARCHY_MISSING = _e(
    "CUSTOMER_HIERARCHY_MISSING", ErrorCategory.INVALID_HIERARCHY,
    "The row carries a customer code but no organisational code, and the "
    "Customer Master has no sub-territory recorded for that customer — so the "
    "hierarchy cannot be derived. It is reported rather than guessed.")
CUSTOMER_TERRITORY_MISMATCH = _e(
    "CUSTOMER_TERRITORY_MISMATCH", ErrorCategory.INVALID_HIERARCHY,
    "The customer belongs to a different territory or sub-territory than the "
    "row states. The Customer Master records where a customer sits, so a row "
    "claiming otherwise is naming the wrong customer or the wrong territory — "
    "and a target attributed to the wrong territory is a wrong report.")
SALES_FORCE_TERRITORY_MISMATCH = _e(
    "SALES_FORCE_TERRITORY_MISMATCH", ErrorCategory.INVALID_HIERARCHY,
    "The sales force member is assigned to a different territory than the row "
    "states. Checked only when the Sales Force Master records a territory for "
    "them; a blank assignment is a gap in the master, not a bad target.")
BROKEN_MASTER_HIERARCHY = _e(
    "BROKEN_MASTER_HIERARCHY", ErrorCategory.INVALID_HIERARCHY,
    "The master data itself is missing a parent link, so the row's ancestors "
    "cannot be derived. Fix the master data, not the transaction.")

#: ``(child_level, parent_level) -> error code``.
HIERARCHY_ERROR_BY_PAIR: dict[tuple[str, str], ErrorSpec] = {
    ("bu_code", "company_code"): BU_COMPANY_MISMATCH,
    ("sales_line_code", "bu_code"): SALES_LINE_BU_MISMATCH,
    ("zone_code", "sales_line_code"): ZONE_SALES_LINE_MISMATCH,
    ("region_code", "zone_code"): REGION_ZONE_MISMATCH,
    ("area_code", "region_code"): AREA_REGION_MISMATCH,
    ("unit_code", "area_code"): UNIT_AREA_MISMATCH,
    ("territory_code", "unit_code"): TERRITORY_UNIT_MISMATCH,
    ("sub_territory_code", "territory_code"): SUB_TERRITORY_TERRITORY_MISMATCH,
}

# --- duplicates -------------------------------------------------------------
DUPLICATE_IN_FILE = _e("DUPLICATE_IN_FILE", ErrorCategory.DUPLICATE,
                       "The same business key appears more than once in this file.")
DUPLICATE_IN_WAREHOUSE = _e(
    "DUPLICATE_IN_WAREHOUSE", ErrorCategory.DUPLICATE,
    "The business key already exists in the fact table. Under an incremental "
    "load the existing row is updated instead of duplicated.")

# --- volume ------------------------------------------------------------------
# Total Volume arrives from the source file and is authoritative. These two
# findings say what is wrong with the value that arrived; neither replaces it,
# and only NEGATIVE_VOLUME rejects a row.
VOLUME_MISSING = _e(
    "VOLUME_MISSING", ErrorCategory.DATA_QUALITY,
    "The source supplied no Total Volume for the line. Reported, never "
    "calculated: a volume derived from the quantity would be "
    "indistinguishable from one the source actually sent.")
VOLUME_NEGATIVE = _e(
    "VOLUME_NEGATIVE", ErrorCategory.INVALID_NUMERIC,
    "A negative Total Volume was supplied on a line whose quantity is not "
    "negative. A return carries both negative, and that is accepted; a "
    "negative volume against a positive quantity is a source error.")

ALL_ERRORS: tuple[ErrorSpec, ...] = tuple(
    spec for spec in list(globals().values()) if isinstance(spec, ErrorSpec)
)

ERROR_BY_CODE: dict[str, ErrorSpec] = {spec.code: spec for spec in ALL_ERRORS}

__all__ = [
    "ErrorCategory",
    "ErrorSpec",
    "ALL_ERRORS",
    "ERROR_BY_CODE",
    "MASTER_ERROR_BY_LEVEL",
    "HIERARCHY_ERROR_BY_PAIR",
    *[spec.code for spec in ALL_ERRORS],
]
