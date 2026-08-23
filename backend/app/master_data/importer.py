"""Master-data importer: Excel -> inspect -> clean -> validate -> load.

Loading is an **upsert keyed on the official business code**: an existing code is
updated, a new code is inserted. Surrogate keys are never reused as the business
identity, and the importer never creates a master record that is not in the
source workbook.

Import order follows the hierarchy so that every foreign key resolves:
Company, Business Unit, Sales Line, Zone, Region, Area, Unit, Territory,
Sub-Territory, then Product.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from ..database.models import MODEL_BY_TABLE
from ..utils.cleaning import CleanedTable, clean_workbook
from .inspector import WorkbookInspection, inspect_workbook
from .schema import TABLE_SPECS, TableSpec
from .validator import ValidationReport, validate


@dataclass
class TableImportResult:
    table: str
    sheet: str
    total_records: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    skipped: bool = False
    skip_reason: str | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "sheet": self.sheet,
            "total_records": self.total_records,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "failed": self.failed,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "errors": list(self.errors),
        }


@dataclass
class ImportSummary:
    source_path: str
    started_at: str
    finished_at: str | None = None
    dry_run: bool = False
    committed: bool = False
    validation_passed: bool = False
    tables: list[TableImportResult] = field(default_factory=list)
    validation_errors: int = 0
    validation_warnings: int = 0
    duplicates: int = 0
    invalid_references: int = 0
    missing_required_values: int = 0

    @property
    def total_records(self) -> int:
        return sum(t.total_records for t in self.tables)

    @property
    def successful(self) -> int:
        return sum(t.inserted + t.updated + t.unchanged for t in self.tables)

    @property
    def failed(self) -> int:
        return sum(t.failed for t in self.tables)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "dry_run": self.dry_run,
            "committed": self.committed,
            "validation_passed": self.validation_passed,
            "totals": {
                "total_records": self.total_records,
                "successful": self.successful,
                "failed": self.failed,
                "duplicates": self.duplicates,
                "invalid_references": self.invalid_references,
                "missing_required_values": self.missing_required_values,
                "validation_errors": self.validation_errors,
                "validation_warnings": self.validation_warnings,
            },
            "tables": [t.to_dict() for t in self.tables],
        }

    def render(self) -> str:
        lines = ["Master Data Import Summary", "=" * 60, ""]
        lines.append(f"Source:      {self.source_path}")
        lines.append(f"Started:     {self.started_at}")
        lines.append(f"Finished:    {self.finished_at or '-'}")
        mode = "DRY RUN (nothing written)" if self.dry_run else (
            "COMMITTED" if self.committed else "ROLLED BACK"
        )
        lines.append(f"Mode:        {mode}")
        lines.append(f"Validation:  {'PASSED' if self.validation_passed else 'FAILED'}")
        lines.append("")
        for result in self.tables:
            lines.append(f"{result.table} ({result.sheet}):")
            if result.skipped:
                lines.append(f"  SKIPPED - {result.skip_reason}")
            lines.append(f"  Records:  {result.total_records}")
            lines.append(f"  Inserted: {result.inserted}")
            lines.append(f"  Updated:  {result.updated}")
            lines.append(f"  Unchanged:{result.unchanged}")
            lines.append(f"  Failed:   {result.failed}")
            for err in result.errors[:5]:
                lines.append(f"    ! {err}")
            if len(result.errors) > 5:
                lines.append(f"    ... {len(result.errors) - 5} more")
            lines.append("")
        lines.append("-" * 60)
        lines.append(f"Total Records:           {self.total_records}")
        lines.append(f"Successful:              {self.successful}")
        lines.append(f"Failed:                  {self.failed}")
        lines.append(f"Duplicates:              {self.duplicates}")
        lines.append(f"Invalid References:      {self.invalid_references}")
        lines.append(f"Missing Required Values: {self.missing_required_values}")
        lines.append(f"Validation Errors:       {self.validation_errors}")
        lines.append(f"Validation Warnings:     {self.validation_warnings}")
        return "\n".join(lines)


def upsert_table(session: Session, spec: TableSpec, table: CleanedTable) -> TableImportResult:
    """Insert or update every cleaned row of one dimension, keyed on its code."""
    model = MODEL_BY_TABLE[spec.table]
    key_column = spec.business_key
    result = TableImportResult(table=spec.table, sheet=spec.sheet,
                               total_records=len(table.rows))

    for row, location in table.rows_with_locations():
        code = row.get(key_column)
        if code is None:
            result.failed += 1
            result.errors.append(f"{location}: missing business key '{key_column}'")
            continue
        try:
            existing = session.execute(
                select(model).where(getattr(model, key_column) == code)
            ).scalar_one_or_none()

            if existing is None:
                session.add(model(**row))
                session.flush()
                result.inserted += 1
                continue

            changed = False
            for column, value in row.items():
                if column == key_column:
                    continue
                if getattr(existing, column) != value:
                    setattr(existing, column, value)
                    changed = True
            session.flush()
            if changed:
                result.updated += 1
            else:
                result.unchanged += 1
        except Exception as exc:  # noqa: BLE001 - surfaced in the summary, never swallowed
            session.rollback()
            result.failed += 1
            result.errors.append(f"{location} ({key_column}={code}): {exc}")
    return result


def run_import(
    source: str | Path,
    engine: Engine,
    *,
    dry_run: bool = False,
    allow_invalid: bool = False,
) -> tuple[ImportSummary, ValidationReport, WorkbookInspection]:
    """Full pipeline. Returns ``(summary, validation_report, inspection)``.

    Validation errors abort the load unless ``allow_invalid`` is set, and even
    then every finding is reported — nothing is ignored silently.
    """
    summary = ImportSummary(
        source_path=str(source),
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        dry_run=dry_run,
    )

    inspection = inspect_workbook(source)
    cleaned = clean_workbook(inspection, TABLE_SPECS)
    report = validate(inspection, cleaned)

    summary.validation_passed = report.is_valid
    summary.validation_errors = len(report.errors)
    summary.validation_warnings = len(report.warnings)
    summary.duplicates = sum(1 for f in report.findings if f.rule_id in {"VR005", "VR009"})
    summary.invalid_references = sum(1 for f in report.findings if f.rule_id in {"VR007", "VR008"})
    summary.missing_required_values = sum(
        1 for f in report.findings if f.rule_id in {"VR004", "VR006"}
    )

    blocked = {f.table for f in report.errors}

    if not report.is_valid and not allow_invalid:
        for spec in TABLE_SPECS:
            table = cleaned[spec.table]
            summary.tables.append(TableImportResult(
                table=spec.table, sheet=spec.sheet, total_records=len(table.rows),
                skipped=True,
                skip_reason="validation failed; import aborted before any write",
            ))
        summary.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return summary, report, inspection

    session = Session(bind=engine, expire_on_commit=False, future=True)
    try:
        for spec in TABLE_SPECS:
            table = cleaned[spec.table]
            if allow_invalid and spec.table in blocked:
                summary.tables.append(TableImportResult(
                    table=spec.table, sheet=spec.sheet, total_records=len(table.rows),
                    skipped=True,
                    skip_reason="table has blocking validation errors; skipped under --allow-invalid",
                ))
                continue
            summary.tables.append(upsert_table(session, spec, table))

        if dry_run:
            session.rollback()
            summary.committed = False
        else:
            session.commit()
            summary.committed = True
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    summary.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return summary, report, inspection


__all__ = ["TableImportResult", "ImportSummary", "upsert_table", "run_import"]
