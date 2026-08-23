"""Cleaning layer: raw sheet records -> typed, normalised warehouse rows.

The cleaner is deliberately conservative. It trims whitespace, normalises blanks
to NULL and coerces declared numeric/timestamp fields. It never rewrites an
official code, never changes letter case, and never drops a row silently: a row
that fails coercion is kept and carries an issue so the validator can block it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..master_data.schema import ColumnSpec, FieldKind, TableSpec
from .text import (
    cell_to_str,
    has_leading_or_trailing_space,
    is_blank,
    is_empty_string,
    normalize_code,
    normalize_phone,
    normalize_text,
    parse_datetime,
    parse_decimal,
    parse_int,
)


@dataclass
class CleaningIssue:
    """A defect found while cleaning one cell."""

    sheet: str
    location: str          # e.g. "row 7" or "column D"
    source_field: str
    column: str
    rule_id: str
    severity: str          # "error" | "warning"
    message: str
    value: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet": self.sheet,
            "location": self.location,
            "source_field": self.source_field,
            "column": self.column,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "message": self.message,
            "value": self.value,
        }


@dataclass
class CleanedTable:
    """Cleaned rows for one dimension, plus the issues raised while cleaning."""

    spec: TableSpec
    rows: list[dict[str, Any]] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    issues: list[CleaningIssue] = field(default_factory=list)
    #: Source fields declared in the schema but absent from the sheet.
    missing_fields: list[str] = field(default_factory=list)
    #: Sheet fields that the schema does not map (reported, never dropped silently).
    unmapped_fields: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[CleaningIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[CleaningIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def rows_with_locations(self):
        return zip(self.rows, self.locations)


def _clean_value(spec: ColumnSpec, raw: Any) -> tuple[Any, str | None]:
    """Return ``(cleaned_value, coercion_error)`` for one cell."""
    if is_blank(raw):
        return None, None
    if spec.kind is FieldKind.CODE:
        return normalize_code(raw), None
    if spec.kind is FieldKind.PHONE:
        return normalize_phone(raw), None
    if spec.kind is FieldKind.TEXT:
        return normalize_text(raw), None
    if spec.kind is FieldKind.INTEGER:
        return parse_int(raw)
    if spec.kind is FieldKind.DECIMAL:
        return parse_decimal(raw)
    if spec.kind is FieldKind.TIMESTAMP:
        return parse_datetime(raw)
    return normalize_text(raw), None


def clean_sheet(sheet_data, spec: TableSpec) -> CleanedTable:
    """Clean one inspected sheet against its table spec.

    ``sheet_data`` is an ``inspector.SheetData``; it is typed loosely to keep the
    utils package free of an import cycle.
    """
    table = CleanedTable(spec=spec)
    present = set(sheet_data.columns)
    # Missing is judged against the *required* fields, so a workbook predating
    # a later column is not reported as incomplete. Unmapped is judged against
    # *every* declared field, so that same column is read when the sheet does
    # carry it rather than being flagged as something the schema ignores.
    declared = set(spec.all_source_fields)
    table.missing_fields = [f for f in spec.source_fields if f not in present]
    table.unmapped_fields = [c for c in sheet_data.columns if c not in declared]

    for raw_record, location in zip(sheet_data.records, sheet_data.source_locations):
        row: dict[str, Any] = {}
        for column_spec in spec.columns:
            raw = raw_record.get(column_spec.source_field)

            if has_leading_or_trailing_space(raw):
                table.issues.append(CleaningIssue(
                    sheet=spec.sheet, location=location,
                    source_field=column_spec.source_field, column=column_spec.column,
                    rule_id="VR011", severity="warning",
                    message="Value carried leading/trailing whitespace; it has been trimmed.",
                    value=cell_to_str(raw),
                ))
            if is_empty_string(raw):
                table.issues.append(CleaningIssue(
                    sheet=spec.sheet, location=location,
                    source_field=column_spec.source_field, column=column_spec.column,
                    rule_id="VR012", severity="warning",
                    message="Empty string normalised to NULL.",
                ))
            if column_spec.kind is FieldKind.CODE and isinstance(raw, (int, float)) \
                    and not isinstance(raw, bool):
                table.issues.append(CleaningIssue(
                    sheet=spec.sheet, location=location,
                    source_field=column_spec.source_field, column=column_spec.column,
                    rule_id="VR014", severity="warning",
                    message=("Code cell is numeric in Excel; any leading zeros were already "
                             "lost at source. Format the source column as Text."),
                    value=cell_to_str(raw),
                ))
            if column_spec.kind is FieldKind.PHONE and isinstance(raw, (int, float)) \
                    and not isinstance(raw, bool):
                table.issues.append(CleaningIssue(
                    sheet=spec.sheet, location=location,
                    source_field=column_spec.source_field, column=column_spec.column,
                    rule_id="VR016", severity="warning",
                    message=("Phone number is numeric in Excel; a leading zero or '+' may "
                             "already be lost at source."),
                    value=cell_to_str(raw),
                ))

            value, error = _clean_value(column_spec, raw)
            if error:
                table.issues.append(CleaningIssue(
                    sheet=spec.sheet, location=location,
                    source_field=column_spec.source_field, column=column_spec.column,
                    rule_id="VR010", severity="error",
                    message=f"Type coercion failed for {column_spec.kind.value}: {error}.",
                    value=cell_to_str(raw),
                ))
            row[column_spec.column] = value
        table.rows.append(row)
        table.locations.append(location)

    return table


def clean_workbook(inspection, specs) -> dict[str, CleanedTable]:
    """Clean every sheet that has a spec. Returns ``{table_name: CleanedTable}``."""
    from ..master_data.inspector import SheetData  # noqa: F401  (documentation of the contract)

    cleaned: dict[str, CleanedTable] = {}
    by_name = {name.strip().casefold(): data for name, data in inspection.sheets.items()}
    for spec in specs:
        sheet_data = by_name.get(spec.sheet.strip().casefold())
        if sheet_data is None:
            table = CleanedTable(spec=spec)
            table.missing_fields = list(spec.source_fields)
            cleaned[spec.table] = table
            continue
        cleaned[spec.table] = clean_sheet(sheet_data, spec)
    return cleaned


__all__ = ["CleaningIssue", "CleanedTable", "clean_sheet", "clean_workbook"]
