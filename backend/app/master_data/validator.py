"""Master-data validation: structure, keys, required fields and hierarchy.

Nothing is validated silently. Every finding becomes a ``ValidationFinding`` with
a rule id, a severity and the exact sheet / location / field / value so it can be
printed, exported to JSON, and used to block the import.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from ..utils.cleaning import CleanedTable, CleaningIssue
from .inspector import WorkbookInspection
from .schema import TABLE_SPECS, TableSpec, VALIDATION_RULES

ERROR = "error"
WARNING = "warning"


@dataclass
class ValidationFinding:
    rule_id: str
    rule_name: str
    severity: str
    sheet: str
    table: str
    field_name: str | None
    location: str | None
    value: str | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def format(self) -> str:
        parts = [f"[{self.severity.upper()}] {self.rule_id} {self.rule_name}"]
        parts.append(f"  Sheet:    {self.sheet}")
        parts.append(f"  Table:    {self.table}")
        if self.field_name:
            parts.append(f"  Field:    {self.field_name}")
        if self.location:
            parts.append(f"  Location: {self.location}")
        if self.value is not None:
            parts.append(f"  Value:    {self.value}")
        parts.append(f"  Error:    {self.message}")
        return "\n".join(parts)


_RULE_NAMES = {r.rule_id: r.name for r in VALIDATION_RULES}


def _finding(rule_id: str, severity: str, spec: TableSpec, message: str, *,
             field_name: str | None = None, location: str | None = None,
             value: str | None = None) -> ValidationFinding:
    return ValidationFinding(
        rule_id=rule_id,
        rule_name=_RULE_NAMES.get(rule_id, rule_id),
        severity=severity,
        sheet=spec.sheet,
        table=spec.table,
        field_name=field_name,
        location=location,
        value=value,
        message=message,
    )


@dataclass
class ValidationReport:
    generated_at: str
    source_path: str
    findings: list[ValidationFinding] = field(default_factory=list)
    record_counts: dict[str, int] = field(default_factory=dict)

    @property
    def errors(self) -> list[ValidationFinding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self) -> list[ValidationFinding]:
        return [f for f in self.findings if f.severity == WARNING]

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def by_table(self) -> dict[str, list[ValidationFinding]]:
        grouped: dict[str, list[ValidationFinding]] = defaultdict(list)
        for f in self.findings:
            grouped[f.table].append(f)
        return dict(grouped)

    def counts_by_rule(self) -> dict[str, int]:
        return dict(Counter(f.rule_id for f in self.findings))

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "source_path": self.source_path,
            "is_valid": self.is_valid,
            "summary": {
                "total_findings": len(self.findings),
                "errors": len(self.errors),
                "warnings": len(self.warnings),
                "record_counts": self.record_counts,
                "counts_by_rule": self.counts_by_rule(),
            },
            "rules": [
                {
                    "rule_id": r.rule_id,
                    "name": r.name,
                    "severity": r.severity,
                    "description": r.description,
                    "applies_to": r.applies_to,
                }
                for r in VALIDATION_RULES
            ],
            "findings": [f.to_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def validate_workbook_structure(inspection: WorkbookInspection,
                                specs: Iterable[TableSpec] = TABLE_SPECS) -> list[ValidationFinding]:
    """VR001 / VR002: sheets present and a header detectable in each."""
    findings: list[ValidationFinding] = []
    by_name = {n.strip().casefold(): d for n, d in inspection.sheets.items()}

    for spec in specs:
        sheet_data = by_name.get(spec.sheet.strip().casefold())
        if sheet_data is None:
            findings.append(_finding(
                "VR001", ERROR, spec,
                f"Expected sheet '{spec.sheet}' is missing from the workbook.",
            ))
            continue
        structure = sheet_data.structure
        if structure.orientation == "empty" or (
            structure.header_row is None and structure.header_column is None
        ):
            findings.append(_finding(
                "VR002", ERROR, spec,
                "No header row or header column could be detected in this sheet.",
            ))

    for extra in inspection.unexpected_sheets:
        findings.append(ValidationFinding(
            rule_id="VR001", rule_name=_RULE_NAMES["VR001"], severity=WARNING,
            sheet=extra, table="-", field_name=None, location=None, value=None,
            message=("Sheet is not part of the Phase 1 master-data contract. It has been "
                     "left untouched and is not imported."),
        ))
    return findings


def validate_columns(table: CleanedTable) -> list[ValidationFinding]:
    """VR003: every schema field must exist in its source sheet."""
    findings: list[ValidationFinding] = []
    spec = table.spec
    for missing in table.missing_fields:
        findings.append(_finding(
            "VR003", ERROR, spec,
            f"Field '{missing}' is declared in the master-data schema but was not found "
            "in this sheet.",
            field_name=missing,
        ))
    for extra in table.unmapped_fields:
        findings.append(_finding(
            "VR003", WARNING, spec,
            f"Sheet field '{extra}' is not mapped to any warehouse column; it is reported "
            "here rather than dropped silently.",
            field_name=extra,
        ))
    return findings


def validate_keys(table: CleanedTable) -> list[ValidationFinding]:
    """VR004 / VR005 / VR013: business key presence, uniqueness, case.

    VR015 used to live here too — a duplicate SKU Id on the Product Master. That
    master left in revision 0022 and the Material Master has no second
    identifier to collide, so there is nothing left to check.
    """
    findings: list[ValidationFinding] = []
    spec = table.spec
    key = spec.business_key
    seen: dict[str, str] = {}
    case_map: dict[str, list[tuple[str, str]]] = defaultdict(list)

    for row, location in table.rows_with_locations():
        value = row.get(key)
        if value is None:
            findings.append(_finding(
                "VR004", ERROR, spec,
                f"Business key '{key}' is missing; the record cannot be identified.",
                field_name=key, location=location,
            ))
            continue
        if value in seen:
            findings.append(_finding(
                "VR005", ERROR, spec,
                f"Duplicate business key: '{value}' already appears at {seen[value]}.",
                field_name=key, location=location, value=value,
            ))
        else:
            seen[value] = location
        case_map[value.casefold()].append((value, location))

    for _, variants in case_map.items():
        distinct = {v for v, _ in variants}
        if len(distinct) > 1:
            findings.append(_finding(
                "VR013", WARNING, spec,
                "Codes differ only by letter case, which usually means a duplicate entry: "
                + ", ".join(f"'{v}' at {loc}" for v, loc in variants),
                field_name=key,
            ))

    return findings


def validate_required_fields(table: CleanedTable) -> list[ValidationFinding]:
    """VR006: NOT NULL columns must carry a value."""
    findings: list[ValidationFinding] = []
    spec = table.spec
    required = [c for c in spec.columns if not c.nullable and c.column != spec.business_key]
    for row, location in table.rows_with_locations():
        for col in required:
            if row.get(col.column) is None:
                findings.append(_finding(
                    "VR006", ERROR, spec,
                    f"Required field '{col.source_field}' is empty.",
                    field_name=col.source_field, location=location,
                ))
    return findings


def validate_duplicate_rows(table: CleanedTable) -> list[ValidationFinding]:
    """VR009: fully identical rows."""
    findings: list[ValidationFinding] = []
    spec = table.spec
    signatures: dict[tuple, str] = {}
    for row, location in table.rows_with_locations():
        sig = tuple(str(row.get(c.column)) for c in spec.columns)
        if sig in signatures:
            findings.append(_finding(
                "VR009", WARNING, spec,
                f"Row is identical to the record at {signatures[sig]} across every mapped "
                "column.",
                location=location, value=row.get(spec.business_key),
            ))
        else:
            signatures[sig] = location
    return findings


def validate_relationships(tables: dict[str, CleanedTable]) -> list[ValidationFinding]:
    """VR007 / VR008: parent code present and resolving (orphan detection)."""
    findings: list[ValidationFinding] = []
    parent_keys: dict[str, set[str]] = {}
    for name, table in tables.items():
        key = table.spec.business_key
        parent_keys[name] = {
            row[key] for row in table.rows if row.get(key) is not None
        }

    for table in tables.values():
        spec = table.spec
        if not spec.has_foreign_key():
            continue
        known = parent_keys.get(spec.parent_table, set())
        for row, location in table.rows_with_locations():
            value = row.get(spec.parent_column)
            if value is None:
                findings.append(_finding(
                    "VR007", ERROR, spec,
                    f"Parent reference '{spec.parent_column}' is empty; the record cannot be "
                    f"placed under {spec.parent_table}.",
                    field_name=spec.parent_column, location=location,
                ))
                continue
            if value not in known:
                findings.append(_finding(
                    "VR008", ERROR, spec,
                    "INVALID PARENT REFERENCE: parent "
                    f"{spec.parent_table}.{spec.parent_key} = '{value}' does not exist.",
                    field_name=spec.parent_column, location=location, value=value,
                ))
    return findings


def _from_cleaning_issue(issue: CleaningIssue, spec: TableSpec) -> ValidationFinding:
    return ValidationFinding(
        rule_id=issue.rule_id,
        rule_name=_RULE_NAMES.get(issue.rule_id, issue.rule_id),
        severity=issue.severity,
        sheet=issue.sheet,
        table=spec.table,
        field_name=issue.source_field,
        location=issue.location,
        value=issue.value,
        message=issue.message,
    )


def validate(inspection: WorkbookInspection,
             tables: dict[str, CleanedTable]) -> ValidationReport:
    """Run every rule over an inspected + cleaned workbook."""
    report = ValidationReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source_path=inspection.source_path,
        record_counts={name: len(t.rows) for name, t in tables.items()},
    )
    report.findings.extend(validate_workbook_structure(inspection))
    for table in tables.values():
        report.findings.extend(_from_cleaning_issue(i, table.spec) for i in table.issues)
        report.findings.extend(validate_columns(table))
        report.findings.extend(validate_keys(table))
        report.findings.extend(validate_required_fields(table))
        report.findings.extend(validate_duplicate_rows(table))
    report.findings.extend(validate_relationships(tables))
    return report


def tables_blocked_by(report: ValidationReport) -> set[str]:
    """Table names that carry at least one blocking error."""
    return {f.table for f in report.errors}


__all__ = [
    "ValidationFinding",
    "ValidationReport",
    "validate",
    "validate_workbook_structure",
    "validate_columns",
    "validate_keys",
    "validate_required_fields",
    "validate_duplicate_rows",
    "validate_relationships",
    "tables_blocked_by",
    "ERROR",
    "WARNING",
]
