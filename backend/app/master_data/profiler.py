"""Data profiling: per-sheet, per-column statistics for the master workbook.

Produces the numbers required by Phase 1 for every column:
name, inferred type, total records, non-null, null, null %, unique count,
duplicate count and example values — plus sheet-level duplicate detection.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from ..utils.text import cell_to_str, is_blank
from .inspector import SheetData, WorkbookInspection, raw_quality_flags
from .schema import FieldKind, TableSpec, spec_for_sheet

MAX_EXAMPLES = 5


@dataclass
class ColumnProfile:
    column_name: str
    warehouse_column: str | None
    inferred_type: str
    declared_type: str | None
    total_records: int
    non_null_records: int
    null_records: int
    null_percent: float
    unique_count: int
    duplicate_count: int
    min_length: int | None
    max_length: int | None
    example_values: list[str]
    whitespace_padded: int = 0
    empty_string: int = 0
    numeric_typed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SheetProfile:
    sheet_name: str
    table_name: str | None
    orientation: str
    header_row: int | None
    header_column: int | None
    record_count: int
    column_count: int
    columns: list[ColumnProfile] = field(default_factory=list)
    fully_duplicated_rows: int = 0
    missing_expected_fields: list[str] = field(default_factory=list)
    unexpected_fields: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet_name": self.sheet_name,
            "table_name": self.table_name,
            "orientation": self.orientation,
            "header_row": self.header_row,
            "header_column": self.header_column,
            "record_count": self.record_count,
            "column_count": self.column_count,
            "fully_duplicated_rows": self.fully_duplicated_rows,
            "missing_expected_fields": list(self.missing_expected_fields),
            "unexpected_fields": list(self.unexpected_fields),
            "notes": list(self.notes),
            "columns": [c.to_dict() for c in self.columns],
        }


def _infer_type(values: list[Any]) -> str:
    """Infer a column's python-level type from its non-null raw values."""
    present = [v for v in values if not is_blank(v)]
    if not present:
        return "unknown (no data)"
    kinds = set()
    for v in present:
        if isinstance(v, bool):
            kinds.add("boolean")
        elif isinstance(v, int):
            kinds.add("integer")
        elif isinstance(v, float):
            kinds.add("integer" if float(v).is_integer() else "decimal")
        elif hasattr(v, "isoformat"):
            kinds.add("datetime")
        else:
            kinds.add("string")
    if len(kinds) == 1:
        return kinds.pop()
    if kinds <= {"integer", "decimal"}:
        return "decimal"
    return "mixed(" + ",".join(sorted(kinds)) + ")"


def profile_sheet(sheet_data: SheetData, spec: TableSpec | None = None) -> SheetProfile:
    """Profile one inspected sheet."""
    structure = sheet_data.structure
    spec = spec if spec is not None else spec_for_sheet(structure.sheet_name)
    total = len(sheet_data.records)
    flags = raw_quality_flags(sheet_data.records)
    column_map = spec.column_map if spec else {}

    profile = SheetProfile(
        sheet_name=structure.sheet_name,
        table_name=spec.table if spec else None,
        orientation=structure.orientation,
        header_row=structure.header_row,
        header_column=structure.header_column,
        record_count=total,
        column_count=len(structure.columns),
        notes=list(structure.notes),
    )

    if spec:
        present = set(structure.columns)
        profile.missing_expected_fields = [f for f in spec.source_fields if f not in present]
        profile.unexpected_fields = [
            c for c in structure.columns if c not in set(spec.source_fields)
        ]

    for label in structure.columns:
        values = [r.get(label) for r in sheet_data.records]
        present_values = [v for v in values if not is_blank(v)]
        rendered = [cell_to_str(v) for v in present_values]
        counts = Counter(rendered)
        unique_count = len(counts)
        duplicate_count = sum(n - 1 for n in counts.values() if n > 1)
        lengths = [len(s) for s in rendered if s is not None]
        col_spec = column_map.get(label)
        flag = flags.get(label, {})

        profile.columns.append(ColumnProfile(
            column_name=label,
            warehouse_column=col_spec.column if col_spec else None,
            inferred_type=_infer_type(values),
            declared_type=col_spec.sql_type if col_spec else None,
            total_records=total,
            non_null_records=len(present_values),
            null_records=total - len(present_values),
            null_percent=round(((total - len(present_values)) / total * 100), 2) if total else 0.0,
            unique_count=unique_count,
            duplicate_count=duplicate_count,
            min_length=min(lengths) if lengths else None,
            max_length=max(lengths) if lengths else None,
            example_values=[s for s, _ in counts.most_common(MAX_EXAMPLES) if s is not None],
            whitespace_padded=flag.get("whitespace_padded", 0),
            empty_string=flag.get("empty_string", 0),
            numeric_typed=flag.get("numeric_typed", 0),
        ))

    signatures = Counter(
        tuple(cell_to_str(r.get(label)) for label in structure.columns)
        for r in sheet_data.records
    )
    profile.fully_duplicated_rows = sum(n - 1 for n in signatures.values() if n > 1)

    if total == 0:
        profile.notes.append(
            "Zero data records: profiling statistics are structural only."
        )
    return profile


@dataclass
class WorkbookProfile:
    source_path: str
    generated_at: str
    sheet_names: list[str]
    missing_sheets: list[str]
    unexpected_sheets: list[str]
    total_records: int
    sheets: list[SheetProfile] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "generated_at": self.generated_at,
            "sheet_names": self.sheet_names,
            "missing_sheets": self.missing_sheets,
            "unexpected_sheets": self.unexpected_sheets,
            "total_records": self.total_records,
            "sheets": [s.to_dict() for s in self.sheets],
        }


def profile_workbook(inspection: WorkbookInspection) -> WorkbookProfile:
    """Profile every sheet in an inspected workbook, in the workbook's own order."""
    from datetime import datetime, timezone

    profiles = [profile_sheet(sd) for sd in inspection.sheets.values()]
    return WorkbookProfile(
        source_path=inspection.source_path,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        sheet_names=list(inspection.sheet_names),
        missing_sheets=list(inspection.missing_sheets),
        unexpected_sheets=list(inspection.unexpected_sheets),
        total_records=sum(len(sd.records) for sd in inspection.sheets.values()),
        sheets=profiles,
    )


__all__ = [
    "ColumnProfile",
    "SheetProfile",
    "WorkbookProfile",
    "profile_sheet",
    "profile_workbook",
]
