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

import csv
import zipfile
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
    #: The upload failed for a reason that is **not** the file's fault — the
    #: database was unreachable, a table was missing, the disk was full. Its own
    #: code because it sends the reader somewhere completely different:
    #: ``UNREADABLE_FILE`` asks them to fix and re-save their spreadsheet, and
    #: telling somebody that when their file is perfectly good wastes their
    #: afternoon and hides a real outage.
    SYSTEM = "SYSTEM_ERROR"
    #: The file is perfectly readable but does not settle something the load
    #: has to be told — today, how it signs a deduction. Its own code for the
    #: same reason ``SYSTEM_ERROR`` has one: "re-save your spreadsheet" is
    #: useless advice here, and the message this carries already says what to
    #: do instead.
    UNDECLARED = "UNDECLARED_SETTING"


class UndeclaredSetting(ValueError):
    """The file cannot settle something the load must be told, and nobody has.

    A refusal, never a default. The two deduction conventions produce identical
    figures for a file with no non-zero deduction in it, so picking one would be
    right half the time and silently wrong the other half — discovered months
    later as a balance off by twice the payment.

    Subclasses ``ValueError`` so the readers' own file faults and this share a
    type where callers only care that the upload cannot proceed;
    :func:`failure_issue` tells them apart, because the advice differs entirely.
    """


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


#: What each coordinate fault is counted as, keyed on the column
#: :class:`app.map.geo.LocationProblem` names.
#:
#: The rule and its wording live in ``map.geo.location_problems``; this is the
#: one thing that module has no business knowing, because an error code belongs
#: to the upload vocabulary. It lives here rather than in either caller so a
#: coordinate rejected from a file and the same coordinate rejected from the
#: Data Management form are filed under the same code — two maps would let one
#: fault be an INVALID_TYPE on one screen and an INVALID_PARENT_CODE on the
#: other.
LOCATION_ERROR_CODE: dict[str, str] = {
    "Entity Type": code.INVALID_TYPE,
    "Entity Code": code.INVALID_PARENT,
    "Latitude / Longitude": code.INVALID_TYPE,
}


def suggested_fix(error_code: str) -> str | None:
    return SUGGESTED_FIX_BY_CODE.get(error_code)


def hierarchy_fix(error_code: str) -> str | None:
    """Fixes for the specific ``X_NOT_IN_Y`` hierarchy codes the ETL emits."""
    if error_code.endswith("_MISMATCH") or "_NOT_IN_" in error_code:
        return SUGGESTED_FIX_BY_CODE["HIERARCHY_MISMATCH"]
    return None


#: Exception types that genuinely mean "this file cannot be read".
#:
#: A **whitelist**, and the direction matters. Anything not listed here is
#: treated as a system failure, because an exception nobody anticipated is far
#: more likely to be ours than the uploader's — and the two mistakes are not
#: symmetrical. Calling our outage a bad file sends somebody to re-export a
#: spreadsheet that was always correct; calling a bad file our outage merely
#: sends them to an administrator, who can read the log and put them right.
#:
#: ``ValueError`` is here because the readers raise it for a malformed sheet;
#: ``KeyError``/``TypeError`` are deliberately absent, being what a bug in our
#: own code raises.
_FILE_FAULTS: tuple[type[BaseException], ...] = (
    UnicodeDecodeError,   # a text file in an encoding we cannot read
    ValueError,           # the readers' own "this is not a usable sheet"
    zipfile.BadZipFile,   # .xlsx is a zip; a truncated upload lands here
    csv.Error,
)

#: The message a system failure shows. It names no table, no driver and no SQL —
#: see the "Errors reveal nothing" invariant in ``CLAUDE.md``. What it does give
#: is the Import Job ID, which is already on screen and already in the log line,
#: so an administrator can find the detail without the user having to relay it.
_SYSTEM_MESSAGE = (
    "The upload could not be completed because of a problem on the server, not "
    "with your file. Nothing was imported. Quote Import Job ID {job_id} to your "
    "administrator, who can see the details in the server log."
)

_SYSTEM_FIX = (
    "There is nothing to fix in the file. Try again shortly, and if it keeps "
    "failing send the Import Job ID to your administrator."
)


def is_file_fault(exc: BaseException) -> bool:
    """Whether ``exc`` means the *file* is bad, rather than the platform."""
    return isinstance(exc, _FILE_FAULTS)


def failure_issue(exc: BaseException, *, job_id: str) -> tuple[str, UploadIssue]:
    """Classify a failed upload into a batch message and one reportable issue.

    Returns ``(batch_message, issue)``. Two outcomes, deliberately far apart:

    * a **file fault** keeps the reader's exception text, because it was written
      for whoever prepared the file — "File is not a zip file" tells them their
      download truncated — and asks them to re-save;
    * anything else is a **system error**, and carries none of the exception:
      no SQL, no table name, no driver class, no path. The detail belongs in the
      server log, which is where the caller is pointed.

    The exception is never interpolated into the system message. That is the
    whole point: a missing table used to arrive at the user as a full ``INSERT``
    statement in a downloadable CSV.
    """
    # Checked before the file-fault branch: it *is* a ValueError, and would
    # otherwise be reported as an unreadable file, which is both wrong and the
    # least useful thing anybody could be told about it.
    if isinstance(exc, UndeclaredSetting):
        message = str(exc)
        return message, UploadIssue(
            row_number=None, column=None, value=None,
            error_code=code.UNDECLARED, message=message,
            suggested_fix=("State the setting explicitly on the upload and "
                           "preview it again. Nothing is guessed here because "
                           "both answers are plausible and only one is right."),
        )

    if is_file_fault(exc):
        message = ("The file could not be read. Check that it is a valid Excel "
                   "or CSV file exported from the template.")
        return message, UploadIssue(
            row_number=None, column=None, value=None,
            error_code=code.UNREADABLE, message=str(exc)[:500],
            suggested_fix="Re-save the file from the downloaded template.",
        )

    message = _SYSTEM_MESSAGE.format(job_id=job_id)
    return message, UploadIssue(
        row_number=None, column=None, value=None,
        error_code=code.SYSTEM, message=message, suggested_fix=_SYSTEM_FIX,
        category="SYSTEM",
    )


__all__ = ["code", "UploadIssue", "SUGGESTED_FIX_BY_CODE", "suggested_fix",
           "hierarchy_fix", "failure_issue", "is_file_fault",
           "UndeclaredSetting"]
