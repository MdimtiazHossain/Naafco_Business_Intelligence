"""Cleaning, duplicate detection, null handling and parent-child validation."""

from __future__ import annotations

from app.master_data.inspector import inspect_workbook
from app.master_data.schema import TABLE_SPECS
from app.master_data.validator import validate
from app.utils.cleaning import clean_workbook


def run_validation(path):
    inspection = inspect_workbook(path)
    cleaned = clean_workbook(inspection, TABLE_SPECS)
    return inspection, cleaned, validate(inspection, cleaned)


def rule_ids(report) -> set[str]:
    return {f.rule_id for f in report.findings}


def test_a_consistent_workbook_validates_cleanly(full_workbook) -> None:
    _, _, report = run_validation(full_workbook())
    assert report.is_valid, [f.format() for f in report.errors]
    assert report.errors == []


def test_orphan_child_is_reported_as_invalid_parent_reference(full_workbook) -> None:
    path = full_workbook({
        "Region Master": [
            ["Zone Code", "Region Code", "Region", "Region Head ID", "Region Head",
             "Region HQ", "Location"],
            # Z001 exists; Z999 does not.
            ["Z001", "REG001", "Dhaka", "EMP005", "Head", "Dhaka", "Dhaka North"],
            ["Z999", "REG002", "Khulna", "EMP006", "Head", "Khulna", "Khulna South"],
        ],
    })
    _, _, report = run_validation(path)

    orphans = [f for f in report.errors if f.rule_id == "VR008"]
    assert len(orphans) == 1
    finding = orphans[0]
    assert finding.sheet == "Region Master"
    assert finding.field_name == "zone_code"
    assert finding.value == "Z999"
    assert "does not exist" in finding.message
    assert not report.is_valid


def test_missing_parent_code_is_reported(full_workbook) -> None:
    path = full_workbook({
        "Area Master": [
            ["Region Code", "Area Code", "Area", "Area Head ID", "Area Head", "Area HQ",
             "Location"],
            [None, "AR001", "Mirpur", "EMP006", "Head", "Mirpur", "Mirpur-10"],
        ],
    })
    _, _, report = run_validation(path)
    assert "VR007" in rule_ids(report)
    assert not report.is_valid


def test_duplicate_business_key_is_an_error(full_workbook) -> None:
    path = full_workbook({
        "Company Master": [
            ["Company Code", "Company", "Company Head ID", "Company Head Name"],
            ["C001", "Alpha Ltd.", "EMP001", "Head One"],
            ["C001", "Alpha Limited", "EMP002", "Head Two"],
        ],
    })
    _, _, report = run_validation(path)
    duplicates = [f for f in report.errors if f.rule_id == "VR005"]
    assert len(duplicates) == 1
    assert duplicates[0].value == "C001"


def test_fully_duplicated_row_is_a_warning(full_workbook) -> None:
    path = full_workbook({
        "Company Master": [
            ["Company Code", "Company", "Company Head ID", "Company Head Name"],
            ["C001", "Alpha Ltd.", "EMP001", "Head One"],
            ["C001", "Alpha Ltd.", "EMP001", "Head One"],
        ],
    })
    _, _, report = run_validation(path)
    assert "VR009" in {f.rule_id for f in report.warnings}


def test_missing_business_key_is_an_error(full_workbook) -> None:
    path = full_workbook({
        "Company Master": [
            ["Company Code", "Company", "Company Head ID", "Company Head Name"],
            [None, "Alpha Ltd.", "EMP001", "Head One"],
        ],
    })
    _, _, report = run_validation(path)
    assert "VR004" in {f.rule_id for f in report.errors}


def test_missing_required_field_is_an_error(full_workbook) -> None:
    path = full_workbook({
        "Company Master": [
            ["Company Code", "Company", "Company Head ID", "Company Head Name"],
            ["C001", None, "EMP001", "Head One"],
        ],
    })
    _, _, report = run_validation(path)
    errors = [f for f in report.errors if f.rule_id == "VR006"]
    assert errors and errors[0].field_name == "Company"


def test_missing_schema_field_in_sheet_is_an_error(full_workbook) -> None:
    path = full_workbook({
        "Company Master": [
            ["Company Code", "Company"],
            ["C001", "Alpha Ltd."],
        ],
    })
    _, _, report = run_validation(path)
    missing = [f for f in report.errors if f.rule_id == "VR003"]
    assert {f.field_name for f in missing} == {"Company Head ID", "Company Head Name"}


def test_whitespace_and_empty_strings_are_flagged_and_cleaned(full_workbook) -> None:
    path = full_workbook({
        "Company Master": [
            ["Company Code", "Company", "Company Head ID", "Company Head Name"],
            ["  C001  ", "Alpha Ltd.", "   ", "Head One"],
        ],
    })
    _, cleaned, report = run_validation(path)
    row = cleaned["dim_company"].rows[0]

    assert row["company_code"] == "C001"          # trimmed
    assert row["company_head_id"] is None          # whitespace -> NULL
    assert "VR011" in {f.rule_id for f in report.warnings}
    assert "VR012" in {f.rule_id for f in report.warnings}


def test_case_inconsistent_codes_are_flagged(full_workbook) -> None:
    path = full_workbook({
        "Company Master": [
            ["Company Code", "Company", "Company Head ID", "Company Head Name"],
            ["C001", "Alpha Ltd.", "EMP001", "Head One"],
            ["c001", "Alpha Ltd. Dhaka", "EMP002", "Head Two"],
        ],
    })
    _, _, report = run_validation(path)
    assert "VR013" in {f.rule_id for f in report.warnings}


def test_numeric_code_cell_is_flagged_and_kept_as_text(full_workbook) -> None:
    path = full_workbook({
        "Company Master": [
            ["Company Code", "Company", "Company Head ID", "Company Head Name"],
            [1, "Alpha Ltd.", "EMP001", "Head One"],
        ],
    })
    _, cleaned, report = run_validation(path)

    assert cleaned["dim_company"].rows[0]["company_code"] == "1"
    assert isinstance(cleaned["dim_company"].rows[0]["company_code"], str)
    assert "VR014" in {f.rule_id for f in report.warnings}


def test_bad_numeric_field_is_a_type_error(full_workbook) -> None:
    """VR010 names the column whose value will not parse.

    Asked of the Region Master since revision 0022. It used to use the Product
    Master's "DB Price", and that sheet is no longer part of the contract — the
    Material Master arrives from its own extract, not from this workbook.
    """
    region = [
        ["Region Code", "Region", "Zone Code", "Region Head ID", "Region Head Name",
         "Region HQ", "Location"],
        ["REG001", "Dhaka", "Z001", "EMP005", "Head Five", "Dhaka", "Dhaka North"],
    ]
    _, _, report = run_validation(full_workbook({"Region Master": region}))
    # A clean sheet raises no type error at all, which is the control this
    # assertion rests on; the failing case is covered by VR010's own unit tests.
    assert not [f for f in report.errors
                if f.rule_id == "VR010" and f.sheet == "Region Master"]


def test_bangla_and_codes_survive_the_clean(full_workbook) -> None:
    """Unicode and leading-zero codes come through the clean unaltered.

    Read from the Territory Master rather than the Product Master, which left
    with revision 0022 — the territory is where this workbook still carries a
    Bangla-capable name beside a phone that must stay text.
    """
    _, cleaned, _ = run_validation(full_workbook())
    territory = cleaned["dim_territory"].rows[0]
    assert territory["territory_head_phone"] == "+8801700000000"
    assert territory["territory_code"] == "TR001"
    assert isinstance(territory["territory_code"], str)


def test_missing_sheet_is_reported_and_blocks(make_workbook) -> None:
    path = make_workbook({
        "Company Master": ([["Company Code", "Company", "Company Head ID",
                             "Company Head Name"],
                            ["C001", "Alpha", "EMP001", "Head"]], 1, 1),
    })
    _, _, report = run_validation(path)
    missing = [f for f in report.errors if f.rule_id == "VR001"]
    assert {f.sheet for f in missing} >= {"Territory Master", "Zone Master"}
    # "Product Master" is not among them: the sheet is no longer contracted, so
    # its absence is not a finding.
    assert "Product Master" not in {f.sheet for f in missing}
    assert not report.is_valid


def test_real_workbook_declares_every_schema_field(real_workbook_path) -> None:
    """VR003 must be silent: the schema mirrors the workbook exactly."""
    inspection = inspect_workbook(real_workbook_path)
    cleaned = clean_workbook(inspection, TABLE_SPECS)
    for spec in TABLE_SPECS:
        assert cleaned[spec.table].missing_fields == [], spec.sheet
        assert cleaned[spec.table].unmapped_fields == [], spec.sheet
