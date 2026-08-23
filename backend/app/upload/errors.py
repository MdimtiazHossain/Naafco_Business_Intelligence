"""Upload error codes and the shape of a reported problem.

The Phase 2 ETL already has a rich error catalogue (``etl.errors``) aimed at a
data engineer reading a batch report. The upload centre reports to whoever
*prepared the file*, so every issue carries four things they can act on: the row
number, the column, the value, and a suggested fix.

Phase 2 rejection codes are mapped onto suggested fixes in
:data:`SUGGESTED_FIX_BY_CODE` rather than being restated, so a transactional
upload and a scripted import agree on why a row failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..database.models_admin import SEVERITY_ERROR


class code:
    """Upload-specific codes. Phase 2 ETL codes pass through unchanged."""

    MISSING_COLUMN = "MISSING_COLUMN"
    MISSING_REQUIRED = "MISSING_REQUIRED_FIELD"
    INVALID_TYPE = "INVALID_TYPE"
    INVALID_PARENT = "INVALID_PARENT_CODE"
    DUPLICATE_IN_FILE = "DUPLICATE_IN_FILE"
    ALREADY_EXISTS = "RECORD_ALREADY_EXISTS"
    NOT_FOUND = "RECORD_NOT_FOUND"
    EMPTY_FILE = "EMPTY_FILE"
    UNREADABLE = "UNREADABLE_FILE"


@dataclass
class UploadIssue:
    """One problem, addressed to the person who must fix the file."""

    row_number: int | None
    column: str | None
    value: str | None
    error_code: str
    message: str
    suggested_fix: str | None = None
    severity: str = SEVERITY_ERROR
    category: str = "VALIDATION"

    def to_dict(self) -> dict[str, Any]:
        return {
            "row": self.row_number,
            "column": self.column,
            "value": self.value,
            "error_code": self.error_code,
            "error": self.message,
            "suggested_fix": self.suggested_fix,
            "severity": self.severity,
            "category": self.category,
        }


#: What to tell the user for each Phase 2 rejection code. Anything not listed
#: falls back to the ETL's own message, which is already user-readable.
SUGGESTED_FIX_BY_CODE: dict[str, str] = {
    "MISSING_REQUIRED_FIELD":
        "Enter a value in this column for this row.",
    "INVALID_DATE":
        "Use an unambiguous date such as 2026-08-15, or tell the importer the "
        "file's date format.",
    "AMBIGUOUS_DATE":
        "05/06/2026 could be 5 June or 6 May. Re-save the column as YYYY-MM-DD.",
    "INVALID_NUMBER":
        "Remove currency symbols and thousands separators; enter a plain number.",
    "NEGATIVE_NOT_ALLOWED":
        "This measure cannot be negative. Check the sign of the value.",
    "INVALID_MATERIAL_CODE":
        "The material is not in the Material Master. Load the Material master "
        "first, or correct the code.",
    "MATERIAL_GROUP_MISMATCH":
        "The Material Master records a different material group for this "
        "material. Correct the file or the master — this system will not choose "
        "between them.",
    "MATERIAL_BRAND_MISMATCH":
        "The Material Master records a different material brand for this "
        "material. Correct the file or the master — this system will not choose "
        "between them.",
    "INVALID_CUSTOMER_CODE":
        "The customer is not in the customer master. Load the Customer master "
        "first, or correct the code.",
    "INVALID_SALES_FORCE_CODE":
        "The code is not in the sales-force master. Load the Sales Force master "
        "first, or correct the code.",
    "INVALID_WAREHOUSE_CODE":
        "The warehouse is not in the warehouse master. Load the Warehouse master "
        "first, or correct the code.",
    "INVALID_COMPANY_CODE": "Use a company code that exists in the master data.",
    "INVALID_BU_CODE": "Use a business unit code that exists in the master data.",
    "INVALID_SALES_LINE_CODE": "Use a sales line code that exists in the master data.",
    "INVALID_ZONE_CODE": "Use a zone code that exists in the master data.",
    "INVALID_REGION_CODE": "Use a region code that exists in the master data.",
    "INVALID_AREA_CODE": "Use an area code that exists in the master data.",
    "INVALID_UNIT_CODE": "Use a unit code that exists in the master data.",
    "INVALID_TERRITORY_CODE": "Use a territory code that exists in the master data.",
    "INVALID_SUB_TERRITORY_CODE":
        "Use a sub-territory code that exists in the master data.",
    "NO_ORGANISATIONAL_CODE":
        "Every row needs at least one organisational code — region, area, unit or "
        "territory. Add the one your source has.",
    "HIERARCHY_MISMATCH":
        "The codes on this row belong to different branches of the hierarchy. "
        "Check which region/area/territory the row really belongs to.",
    "BROKEN_MASTER_HIERARCHY":
        "The master data itself is missing a link above this code. Report it to "
        "whoever maintains the hierarchy.",
    "DUPLICATE_IN_FILE":
        "The same record appears twice in this file. Keep one and delete the other.",
    "DUPLICATE_IN_WAREHOUSE":
        "This transaction is already in the warehouse. Use 'Insert or update' to "
        "refresh it, or remove the row.",
    "MISSING_SOURCE_COLUMN":
        "Download the template and use its header row.",
    code.MISSING_COLUMN: "Download the template and use its header row.",
    code.INVALID_TYPE: "Correct the value so it matches the column's data type.",
    code.INVALID_PARENT: "Load the parent master first, or correct the code.",
    code.ALREADY_EXISTS: "Use 'Insert or update' to refresh the existing record.",
    code.NOT_FOUND: "Use 'Insert or update' to create the record.",
}


def suggested_fix(error_code: str) -> str | None:
    return SUGGESTED_FIX_BY_CODE.get(error_code)


def hierarchy_fix(error_code: str) -> str | None:
    """Fixes for the specific ``X_NOT_IN_Y`` hierarchy codes the ETL emits."""
    if error_code.endswith("_MISMATCH") or "_NOT_IN_" in error_code:
        return SUGGESTED_FIX_BY_CODE["HIERARCHY_MISMATCH"]
    return None


__all__ = ["code", "UploadIssue", "SUGGESTED_FIX_BY_CODE", "suggested_fix",
           "hierarchy_fix"]
