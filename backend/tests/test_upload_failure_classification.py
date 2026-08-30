"""A failed upload says whose fault it was, and never leaks how it failed.

Written after a real incident. A Credit Invoice file was uploaded to a database
where migration 0031 had not been applied, and the upload centre reported:

    UNREADABLE_FILE
    (psycopg.errors.UndefinedTable) relation "stg_credit_invoice" does not exist
    LINE 1: INSERT INTO stg_credit_invoice (company_code, invoice_no, pl...
    Suggested Fix: Re-save the file from the downloaded template.

Wrong three times over. The file was perfectly good, so the suggested fix sent
somebody to re-export a spreadsheet that could never have worked; the real
problem — a missing table — was invisible; and a live INSERT statement naming
internal tables was written into a CSV the browser downloads, which is exactly
what ``CLAUDE.md``'s "Errors reveal nothing" invariant exists to prevent.

The cause was a bare ``except Exception`` that assumed every failure was the
file's. These tests pin the classification that replaced it, in both directions
— because the failure mode is asymmetric and the *default* is the part that
matters.
"""

from __future__ import annotations

import csv
import zipfile

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.upload.errors import code, failure_issue, is_file_fault

JOB = "3f2b8c10-0000-4000-8000-000000000111"

#: The exact shape of the incident: a missing staging table, carrying the SQL.
MISSING_TABLE = ProgrammingError(
    "INSERT INTO stg_credit_invoice (company_code, invoice_no, plant_code) "
    "VALUES (%s, %s, %s)",
    {},
    Exception('relation "stg_credit_invoice" does not exist'),
)

#: Things that mean the platform is broken, not the spreadsheet.
SYSTEM_FAULTS = [
    pytest.param(MISSING_TABLE, id="missing-table"),
    pytest.param(OperationalError("SELECT 1", {}, Exception("server closed")),
                 id="database-down"),
    pytest.param(OSError(28, "No space left on device"), id="disk-full"),
    pytest.param(MemoryError(), id="out-of-memory"),
    # An exception nobody anticipated. The *default* is the whole point: an
    # unexpected error is far more likely to be ours than the uploader's.
    pytest.param(KeyError("plant_code"), id="our-own-bug"),
    pytest.param(AttributeError("'NoneType' has no attribute 'rows'"),
                 id="our-own-bug-2"),
]

#: Things that really do mean "this file cannot be read".
FILE_FAULTS = [
    pytest.param(zipfile.BadZipFile("File is not a zip file"), id="truncated-xlsx"),
    pytest.param(ValueError("openpyxl does not support the old .xls format"),
                 id="wrong-format"),
    pytest.param(csv.Error("line contains NUL"), id="corrupt-csv"),
    pytest.param(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
                 id="bad-encoding"),
]


@pytest.mark.parametrize("exc", SYSTEM_FAULTS)
def test_a_platform_failure_is_not_blamed_on_the_file(exc) -> None:
    assert not is_file_fault(exc)
    message, issue = failure_issue(exc, job_id=JOB)

    assert issue.error_code == code.SYSTEM
    # The two sentences that matter to whoever uploaded the file.
    assert "not with your file" in message
    assert "Nothing was imported" in message
    assert "re-save" not in (issue.suggested_fix or "").lower()


@pytest.mark.parametrize("exc", SYSTEM_FAULTS)
def test_a_platform_failure_reveals_nothing_about_the_platform(exc) -> None:
    """No SQL, no table name, no driver class reaches the client.

    Asserted against the *reported* text rather than against a formatting
    function, because the leak happened in what was stored and downloaded.
    """
    _message, issue = failure_issue(exc, job_id=JOB)
    reported = " ".join(str(part) for part in issue.to_dict().values())

    for forbidden in ("INSERT", "SELECT", "stg_credit_invoice", "psycopg",
                      "relation", "ProgrammingError", "OperationalError",
                      "Traceback", "NoneType", "/", "\\"):
        assert forbidden not in reported, f"{forbidden!r} leaked: {reported}"


def test_a_platform_failure_names_the_job_so_it_can_be_traced() -> None:
    """The reader gets nothing useful *about* the error, so they get a handle.

    The Import Job ID, which is already on screen and already in the log line —
    ``upload_batches.upload_uuid``, not a second identifier invented for this.
    """
    message, issue = failure_issue(MISSING_TABLE, job_id=JOB)
    assert JOB in message
    assert JOB in issue.message


@pytest.mark.parametrize("exc", FILE_FAULTS)
def test_a_bad_file_still_says_so_and_keeps_its_detail(exc) -> None:
    """The other direction. Over-correcting here would be its own failure.

    "File is not a zip file" is written for whoever prepared the file and tells
    them their download truncated; hiding it behind a generic message would make
    a genuinely fixable problem unfixable.
    """
    assert is_file_fault(exc)
    message, issue = failure_issue(exc, job_id=JOB)

    assert issue.error_code == code.UNREADABLE
    assert "valid Excel or CSV" in message
    assert issue.suggested_fix and "template" in issue.suggested_fix
    assert issue.message  # the reader's own words, not swallowed


def test_the_incident_itself_would_now_be_classified_correctly() -> None:
    """The regression test proper: the exact failure from the reported CSV."""
    _message, issue = failure_issue(MISSING_TABLE, job_id=JOB)

    assert issue.error_code != code.UNREADABLE
    assert "INSERT INTO stg_credit_invoice" not in issue.message
    assert issue.suggested_fix != "Re-save the file from the downloaded template."
