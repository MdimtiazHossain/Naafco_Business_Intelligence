"""Workbook inspection: structure discovery without assumptions.

The inspector opens ``Master Data.xlsx`` read-only and works out, per sheet:

* the used cell block (ignoring blank leading rows/columns and style-only cells)
* the **orientation** — records laid out in rows (normal) or the sheet being a
  transposed field list (labels running down a column)
* the actual header row (or header column) and the actual field labels
* the data rows, merged ranges, blank rows/columns and record count

It never mutates the workbook and never assumes ``header=0``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from ..utils.text import (
    cell_to_str,
    has_leading_or_trailing_space,
    is_blank,
    is_empty_string,
    normalize_text,
)

Orientation = Literal["row_records", "column_records", "empty"]

#: Called with ``(rows done, rows to do)`` during a row-wise pass over a sheet.
#:
#: The inspector is the only place that walks a worksheet a row at a time, so it
#: is the only place that can say how far a read has got. It reports rather than
#: renders: what the caller does with the pair — throttle it, turn it into a
#: percentage, raise to stop the read — is the caller's business, and a reader
#: with no observer pays one ``is None`` test per row for the privilege.
RowObserver = Callable[[int, int], None]

#: How far into a sheet we look for the header before giving up.
HEADER_SCAN_ROWS = 30
HEADER_SCAN_COLS = 30


@dataclass
class SheetStructure:
    """Everything discovered about the physical layout of one sheet."""

    sheet_name: str
    orientation: Orientation
    header_row: int | None = None          # 1-based, for row_records
    header_column: int | None = None       # 1-based, for column_records
    first_data_row: int | None = None
    first_data_column: int | None = None
    columns: list[str] = field(default_factory=list)         # raw labels, in sheet order
    duplicate_labels: list[str] = field(default_factory=list)
    record_count: int = 0
    max_row: int = 0
    max_column: int = 0
    used_range: str | None = None
    merged_ranges: list[str] = field(default_factory=list)
    blank_rows_before_header: int = 0
    blank_columns_before_header: int = 0
    blank_rows_within_data: list[int] = field(default_factory=list)
    blank_columns_within_block: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet_name": self.sheet_name,
            "orientation": self.orientation,
            "header_row": self.header_row,
            "header_column": self.header_column,
            "first_data_row": self.first_data_row,
            "first_data_column": self.first_data_column,
            "columns": list(self.columns),
            "column_count": len(self.columns),
            "duplicate_labels": list(self.duplicate_labels),
            "record_count": self.record_count,
            "max_row": self.max_row,
            "max_column": self.max_column,
            "used_range": self.used_range,
            "merged_ranges": list(self.merged_ranges),
            "blank_rows_before_header": self.blank_rows_before_header,
            "blank_columns_before_header": self.blank_columns_before_header,
            "blank_rows_within_data": list(self.blank_rows_within_data),
            "blank_columns_within_block": list(self.blank_columns_within_block),
            "notes": list(self.notes),
        }


@dataclass
class SheetData:
    """A sheet's structure plus its records as raw (untyped) values."""

    structure: SheetStructure
    #: One dict per record: ``{source_field_label: raw cell value}``.
    records: list[dict[str, Any]] = field(default_factory=list)
    #: Excel row (or column) number each record came from, for error messages.
    source_locations: list[str] = field(default_factory=list)

    @property
    def sheet_name(self) -> str:
        return self.structure.sheet_name

    @property
    def columns(self) -> list[str]:
        return self.structure.columns


# ---------------------------------------------------------------------------
# Cell block discovery
# ---------------------------------------------------------------------------


def _cell_grid(ws: Worksheet, on_row: RowObserver | None = None) -> dict[tuple[int, int], Any]:
    """Map ``(row, col) -> value`` for every cell that holds a value.

    Excel files routinely report a large ``max_row``/``max_column`` because of
    styling applied to empty cells; those cells are excluded here so that blank
    padding never influences header detection.

    Whitespace-only strings *are* kept: they are a data-quality defect the
    cleaner has to see and report (VR012), not absent cells. They score zero
    during header detection and never form a record on their own.
    """
    grid: dict[tuple[int, int], Any] = {}
    # ``max_row`` is what ``iter_rows`` will yield, so it is an exact denominator
    # for this pass even where it overstates the rows that hold data — the two
    # are different questions and this one is "how far through the sheet are we".
    total = ws.max_row or 0
    for index, row in enumerate(ws.iter_rows(), start=1):
        for cell in row:
            value = cell.value
            if value is None:
                continue
            if isinstance(value, float) and value != value:  # NaN
                continue
            grid[(cell.row, cell.column)] = value
        if on_row is not None:
            on_row(index, total)
    return grid


def _rows_with_content(grid: dict[tuple[int, int], Any]) -> set[int]:
    """Every row index holding at least one non-blank value.

    Answered for the whole grid in one pass, and deliberately *not* as a
    ``_row_has_content(grid, row)`` predicate. That is what this was, and because
    such a predicate has to scan the grid to answer for a single row, asking it
    once per row made layout detection quadratic in the row count: a 20,000-row
    workbook spent 123 of its 131 seconds here, and every doubling of the file
    quadrupled it. The set is identical to what the predicate answered row by
    row — it is built once and asked by membership.

    The ``row not in found`` guard is not a micro-optimisation: ``is_blank``
    is a call per cell, and skipping it for a row already known to be populated
    is most of the remaining cost on a wide sheet.
    """
    found: set[int] = set()
    for (row, _), value in grid.items():
        if row not in found and not is_blank(value):
            found.add(row)
    return found


def _columns_with_content(grid: dict[tuple[int, int], Any]) -> set[int]:
    """Every column index holding at least one non-blank value.

    The transposed twin of :func:`_rows_with_content`, one pass for the same
    reason: a column-oriented sheet is inspected the same way and would be
    quadratic in its column count.
    """
    found: set[int] = set()
    for (_, column), value in grid.items():
        if column not in found and not is_blank(value):
            found.add(column)
    return found


def _dominant_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if hasattr(value, "isoformat"):
        return "datetime"
    return "text"


def _homogeneity(tallies: dict[Any, dict[str, int]]) -> float:
    """Mean fraction of the dominant value-type within each vector.

    A table laid out in rows has type-homogeneous *columns*; a transposed table
    has type-homogeneous *rows*. Comparing the two scores detects transposition
    without hard-coding any field names.

    Takes tallies of ``value type -> count`` per vector rather than the vectors
    themselves. It only ever needed the counts, and building the vectors to get
    them meant materialising the grid twice over: on a 100,000-row sheet that was
    2.1 million dictionary lookups into two lists of a million slots, and every
    cell classified twice, to produce two floating-point numbers. ``max`` of the
    counts is what ``Counter.most_common(1)[0][1]`` returned and ``sum`` is what
    ``len(values)`` was. A vector with no values scores nothing, which is why an
    empty tally is absent rather than zero — a zero would drag the mean down, and
    a blank column is not evidence of a layout either way.
    """
    scores = [max(counts.values()) / sum(counts.values())
              for counts in tallies.values() if counts]
    if not scores:
        return 0.0
    return sum(scores) / len(scores)


def _type_tallies(
    grid: dict[tuple[int, int], Any], rows: set[int], columns: set[int],
) -> tuple[dict[int, dict[str, int]], dict[int, dict[str, int]]]:
    """Per-column and per-row type counts, from one pass over the grid.

    ``rows`` and ``columns`` are the bodies each tally is taken over — the
    orientation test excludes the first row from the column vectors and the first
    column from the row vectors, because those are where a header would sit and a
    header is text whichever way the sheet runs.

    Plain dictionaries rather than ``Counter``, and each fetched before it is
    created: this loop runs once per populated cell, so a ``setdefault`` that
    builds a throwaway counter on every one of a million iterations costs more
    than the tallying it was there to do.
    """
    by_column: dict[int, dict[str, int]] = {}
    by_row: dict[int, dict[str, int]] = {}
    for (row, column), value in grid.items():
        if is_blank(value):
            continue
        kind = _dominant_type(value)
        if row in rows:
            counts = by_column.get(column)
            if counts is None:
                counts = by_column[column] = {}
            counts[kind] = counts.get(kind, 0) + 1
        if column in columns:
            counts = by_row.get(row)
            if counts is None:
                counts = by_row[row] = {}
            counts[kind] = counts.get(kind, 0) + 1
    return by_column, by_row


def _label_affinity(labels: list[str], known_fields: set[str]) -> float:
    """Fraction of a candidate header vector that matches known schema fields."""
    if not labels or not known_fields:
        return 0.0
    folded = {f.strip().casefold() for f in known_fields}
    hits = sum(1 for label in labels if label.strip().casefold() in folded)
    return hits / len(labels)


def _labels_at_row(grid: dict[tuple[int, int], Any], row: int) -> list[str]:
    """The text labels along one row, left to right.

    The cells of the row are collected and then sorted, rather than the whole
    grid being sorted so that one row can be read out of it. Sorting a million
    cells to order eleven of them was the entire cost of this function.
    """
    cells = sorted((c, v) for (r, c), v in grid.items() if r == row)
    return [label for label in (normalize_text(v) for _, v in cells) if label]


def _labels_at_column(grid: dict[tuple[int, int], Any], col: int) -> list[str]:
    """The text labels down one column, top to bottom. See :func:`_labels_at_row`."""
    cells = sorted((r, v) for (r, c), v in grid.items() if c == col)
    return [label for label in (normalize_text(v) for _, v in cells) if label]


def detect_orientation(
    grid: dict[tuple[int, int], Any], known_fields: set[str] | None = None
) -> Orientation:
    """Decide whether records run across rows or down columns.

    Rules, in order:

    1. No values at all -> ``empty``.
    2. Exactly one populated column (and 2+ populated rows) -> ``column_records``:
       the sheet is a vertical field list.
    3. Exactly one populated row -> ``row_records``.
    4. Compare type-homogeneity of columns vs rows: a table laid out in rows has
       type-homogeneous columns, a transposed one has type-homogeneous rows.
    5. When those two scores are close — which happens whenever every cell is
       text — fall back to ``known_fields``: whichever of the first row and the
       first column better matches the sheet's documented field names is the
       header. This only breaks ties; it never overrides a clear structural
       signal, and with no known fields the conventional row layout wins.
    """
    if not grid:
        return "empty"
    rows = sorted({r for r, _ in grid})
    cols = sorted({c for _, c in grid})
    if len(cols) == 1 and len(rows) >= 2:
        return "column_records"
    if len(rows) == 1:
        return "row_records"

    by_column, by_row = _type_tallies(grid, set(rows[1:]), set(cols[1:]))
    row_layout_score = _homogeneity(by_column)
    column_layout_score = _homogeneity(by_row)

    if abs(row_layout_score - column_layout_score) <= 0.05 and known_fields:
        row_affinity = _label_affinity(_labels_at_row(grid, rows[0]), known_fields)
        column_affinity = _label_affinity(_labels_at_column(grid, cols[0]), known_fields)
        if column_affinity > row_affinity:
            return "column_records"
        if row_affinity > column_affinity:
            return "row_records"

    # Bias (+0.05) keeps the conventional layout as the default on near-ties.
    return "row_records" if row_layout_score + 0.05 >= column_layout_score else "column_records"


def _label_score(values: list[Any]) -> int:
    """Score a candidate header vector by how many textual labels it carries.

    Numbers, dates and blanks score nothing, so a row of data never outranks the
    row of field names above it. Ties are resolved by position (earliest wins),
    which keeps a header row with repeated labels ahead of the first data row.
    """
    return sum(
        1 for v in values if isinstance(v, str) and normalize_text(v) is not None
    )


def detect_header_row(grid: dict[tuple[int, int], Any]) -> int | None:
    """Topmost row within the scan window carrying the most text labels.

    The window's cells are gathered in one pass. Scanning the grid once per
    candidate meant thirty passes over every cell in the sheet to look at its
    first thirty rows.
    """
    rows = sorted({r for r, _ in grid})
    if not rows:
        return None
    window = set(rows[:HEADER_SCAN_ROWS])
    values_by_row: dict[int, list[Any]] = {}
    for (row, _), value in grid.items():
        if row in window:
            values_by_row.setdefault(row, []).append(value)
    best_row, best_score = None, 0
    # Ascending, and strictly greater, so the earliest of equal scores still wins.
    for r in rows[:HEADER_SCAN_ROWS]:
        score = _label_score(values_by_row.get(r, []))
        if score > best_score:
            best_row, best_score = r, score
    return best_row


def detect_header_column(grid: dict[tuple[int, int], Any]) -> int | None:
    """Leftmost column within the scan window carrying the most text labels.

    One pass, for the reason :func:`detect_header_row` gives.
    """
    cols = sorted({c for _, c in grid})
    if not cols:
        return None
    window = set(cols[:HEADER_SCAN_COLS])
    values_by_column: dict[int, list[Any]] = {}
    for (_, column), value in grid.items():
        if column in window:
            values_by_column.setdefault(column, []).append(value)
    best_col, best_score = None, 0
    for c in cols[:HEADER_SCAN_COLS]:
        score = _label_score(values_by_column.get(c, []))
        if score > best_score:
            best_col, best_score = c, score
    return best_col


def _dedupe_labels(labels: list[str]) -> tuple[list[str], list[str]]:
    """Suffix repeated labels (``Location`` -> ``Location__2``) and report them."""
    seen: Counter[str] = Counter()
    result: list[str] = []
    duplicates: list[str] = []
    for label in labels:
        seen[label] += 1
        if seen[label] == 1:
            result.append(label)
        else:
            duplicates.append(label)
            result.append(f"{label}__{seen[label]}")
    return result, duplicates


# ---------------------------------------------------------------------------
# Sheet inspection
# ---------------------------------------------------------------------------


def inspect_sheet(ws: Worksheet, orientation_override: Orientation | None = None,
                  on_row: RowObserver | None = None) -> SheetData:
    """Discover the layout of one worksheet and extract its records.

    Reads the sheet's own account of itself — its declared dimensions and its
    merged ranges — which is available because the caller opened the workbook in
    full. :func:`inspect_streamed_sheet` is the same inspection for a workbook
    opened read-only, which cannot answer either question.
    """
    grid = _cell_grid(ws, on_row)
    return _inspect_grid(
        grid, sheet_name=ws.title, max_row=ws.max_row or 0,
        max_column=ws.max_column or 0,
        merged_ranges=[str(r) for r in ws.merged_cells.ranges],
        declared_range=ws.dimensions,
        orientation_override=orientation_override,
    )


def inspect_streamed_sheet(ws: Any, on_row: RowObserver | None = None,
                           orientation_override: Orientation | None = None) -> SheetData:
    """The same inspection, over a worksheet opened ``read_only=True``.

    Worth the separate entry point because a streamed sheet answers two fewer
    questions about itself, and pretending otherwise would report an absence as
    a fact:

    * **Merged ranges are not read.** openpyxl's read-only worksheet does not
      parse them, and the information sits past the cell data in the sheet XML,
      so recovering it would mean reading the file twice. Nothing that streams a
      sheet consumes them — the ETL reader takes the columns, the records and
      their locations and nothing else — and a note says so rather than leaving
      an empty list to be read as "none".
    * **The declared range may be absent.** A workbook written by a generator
      often carries no ``<dimension>``; every file exported by Excel itself does.
      Where it is missing the used range is taken from the data, and the note
      contrasting the two is simply not made rather than being made against a
      guess.

    What this buys is the reason it exists: opened in full, two thirds of a read
    is inside one ``load_workbook`` call that reports nothing, so the bar cannot
    move through the longest part of an import. Streamed, that same work happens
    a row at a time under ``on_row``.
    """
    grid = _cell_grid(ws, on_row)
    rows = [r for r, _ in grid]
    cols = [c for _, c in grid]
    return _inspect_grid(
        grid, sheet_name=ws.title,
        # ``max_row`` is the declared dimension where the file states one and
        # ``None`` where it does not; the data itself is the fallback.
        max_row=ws.max_row or (max(rows) if rows else 0),
        max_column=ws.max_column or (max(cols) if cols else 0),
        merged_ranges=None,
        declared_range=None,
        orientation_override=orientation_override,
    )


def _inspect_grid(grid: dict[tuple[int, int], Any], *, sheet_name: str,
                  max_row: int, max_column: int,
                  merged_ranges: list[str] | None,
                  declared_range: str | None,
                  orientation_override: Orientation | None) -> SheetData:
    """Everything the inspection decides from the cells alone.

    Split out so the grid can arrive either from a fully-loaded worksheet or
    streamed from a read-only one; every rule below is the same either way.
    ``merged_ranges=None`` means "not read", which is a different statement from
    an empty list and is reported as one.
    """
    structure = SheetStructure(
        sheet_name=sheet_name,
        orientation="empty",
        max_row=max_row,
        max_column=max_column,
        merged_ranges=list(merged_ranges) if merged_ranges else [],
    )
    if merged_ranges is None:
        structure.notes.append(
            "Merged ranges were not read: the sheet was streamed. Merged cells "
            "still take their value from the top-left anchor, as they do on any "
            "other read."
        )
    elif structure.merged_ranges:
        structure.notes.append(
            f"{len(structure.merged_ranges)} merged range(s) present; merged cells are read "
            "from their top-left anchor."
        )

    if not grid:
        structure.notes.append("Sheet contains no values (styling-only cells are ignored).")
        return SheetData(structure=structure)

    rows = sorted({r for r, _ in grid})
    cols = sorted({c for _, c in grid})
    structure.used_range = (
        f"{get_column_letter(cols[0])}{rows[0]}:{get_column_letter(cols[-1])}{rows[-1]}"
    )
    if declared_range is not None and (max_row > rows[-1] or max_column > cols[-1]):
        structure.notes.append(
            f"Excel reports the used range as {declared_range}, but the last cell holding a "
            f"value is {get_column_letter(cols[-1])}{rows[-1]}; trailing cells carry "
            "formatting only."
        )

    from .schema import spec_for_sheet  # local import: avoids a cycle at module load

    spec = spec_for_sheet(sheet_name)
    known_fields = set(spec.source_fields) if spec else None
    structure.orientation = orientation_override or detect_orientation(grid, known_fields)

    if structure.orientation == "column_records":
        _inspect_column_records(grid, structure)
    else:
        _inspect_row_records(grid, structure)

    # The observer is deliberately not passed on. Extraction is a second pass
    # with a different denominator, and reporting it would restart the phase and
    # walk the bar back over the scan it has already covered.
    records, locations = _extract_records(grid, structure)
    structure.record_count = len(records)
    return SheetData(structure=structure, records=records, source_locations=locations)


def _inspect_row_records(grid: dict[tuple[int, int], Any], structure: SheetStructure) -> None:
    """Conventional layout: header across a row, one record per row below it."""
    rows = sorted({r for r, _ in grid})
    header_row = detect_header_row(grid)
    structure.header_row = header_row
    structure.blank_rows_before_header = (header_row - 1) if header_row else 0

    header_cells = sorted(
        ((c, v) for (r, c), v in grid.items() if r == header_row), key=lambda t: t[0]
    )
    labels = [normalize_text(v) or f"UNNAMED_{get_column_letter(c)}" for c, v in header_cells]
    structure.columns, structure.duplicate_labels = _dedupe_labels(labels)
    if header_cells:
        structure.first_data_column = header_cells[0][0]
        structure.blank_columns_before_header = header_cells[0][0] - 1
    structure.first_data_row = (header_row + 1) if header_row else None

    data_rows = [r for r in rows if header_row is not None and r > header_row]
    if data_rows:
        populated = _rows_with_content(grid)
        for r in range(min(data_rows), max(data_rows) + 1):
            if r not in populated:
                structure.blank_rows_within_data.append(r)
    if structure.blank_rows_within_data:
        structure.notes.append(
            f"{len(structure.blank_rows_within_data)} blank row(s) inside the data block are "
            "skipped rather than imported as empty records."
        )


def _inspect_column_records(grid: dict[tuple[int, int], Any], structure: SheetStructure) -> None:
    """Transposed layout: field labels down one column, one record per column."""
    cols = sorted({c for _, c in grid})
    header_column = detect_header_column(grid)
    structure.header_column = header_column
    structure.blank_columns_before_header = (header_column - 1) if header_column else 0

    header_cells = sorted(
        ((r, v) for (r, c), v in grid.items() if c == header_column), key=lambda t: t[0]
    )
    labels = [normalize_text(v) or f"UNNAMED_ROW_{r}" for r, v in header_cells]
    structure.columns, structure.duplicate_labels = _dedupe_labels(labels)
    if header_cells:
        structure.first_data_row = header_cells[0][0]
        structure.blank_rows_before_header = header_cells[0][0] - 1
    structure.first_data_column = (header_column + 1) if header_column else None

    data_cols = [c for c in cols if header_column is not None and c > header_column]
    # Computed outside the block below because the note at the end of this
    # function asks the same question again, and an empty ``data_cols`` still
    # has to reach it.
    populated = _columns_with_content(grid)
    if data_cols:
        for c in range(min(data_cols), max(data_cols) + 1):
            if c not in populated:
                structure.blank_columns_within_block.append(get_column_letter(c))

    structure.notes.append(
        "Field labels run vertically down column "
        f"{get_column_letter(header_column) if header_column else '?'}; each record would "
        "occupy one column to the right of the labels."
    )
    if populated.isdisjoint(data_cols):
        structure.notes.append(
            "No record columns follow the label column: this sheet defines the field list "
            "only and carries zero data records."
        )


def _extract_records(
    grid: dict[tuple[int, int], Any], structure: SheetStructure,
    on_row: RowObserver | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Pull raw records out of the grid according to the detected layout."""
    records: list[dict[str, Any]] = []
    locations: list[str] = []

    if structure.orientation == "row_records":
        if structure.header_row is None:
            return records, locations
        header_positions = sorted(c for (r, c) in grid if r == structure.header_row)
        label_by_col = dict(zip(header_positions, structure.columns))
        data_rows = sorted({r for r, _ in grid if r > structure.header_row})
        total = len(data_rows)
        for index, r in enumerate(data_rows, start=1):
            record = {label: grid.get((r, col)) for col, label in label_by_col.items()}
            if on_row is not None:
                on_row(index, total)
            if all(is_blank(v) for v in record.values()):
                continue
            records.append(record)
            locations.append(f"row {r}")
        return records, locations

    if structure.orientation == "column_records":
        if structure.header_column is None:
            return records, locations
        header_positions = sorted(r for (r, c) in grid if c == structure.header_column)
        label_by_row = dict(zip(header_positions, structure.columns))
        data_cols = sorted({c for _, c in grid if c > structure.header_column})
        for c in data_cols:
            record = {label: grid.get((row, c)) for row, label in label_by_row.items()}
            if all(is_blank(v) for v in record.values()):
                continue
            records.append(record)
            locations.append(f"column {get_column_letter(c)}")
        return records, locations

    return records, locations


# ---------------------------------------------------------------------------
# Workbook inspection
# ---------------------------------------------------------------------------


@dataclass
class WorkbookInspection:
    """Result of inspecting the whole workbook."""

    source_path: str
    sheet_names: list[str]
    expected_sheets: list[str]
    missing_sheets: list[str]
    unexpected_sheets: list[str]
    sheets: dict[str, SheetData]

    @property
    def is_complete(self) -> bool:
        return not self.missing_sheets

    def structure_summary(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "sheet_names": self.sheet_names,
            "expected_sheets": self.expected_sheets,
            "missing_sheets": self.missing_sheets,
            "unexpected_sheets": self.unexpected_sheets,
            "sheets": {name: sd.structure.to_dict() for name, sd in self.sheets.items()},
            "total_records": sum(len(sd.records) for sd in self.sheets.values()),
        }

    def iter_sheets(self) -> Iterator[SheetData]:
        return iter(self.sheets.values())


def inspect_workbook(
    path: str | Path,
    expected_sheets: tuple[str, ...] | list[str] | None = None,
    orientation_overrides: dict[str, Orientation] | None = None,
) -> WorkbookInspection:
    """Open the workbook read-only and inspect every sheet.

    ``expected_sheets`` defaults to the Phase 1 master-data contract. Missing
    sheets are reported (never fabricated); extra sheets are reported and kept.
    """
    from .schema import EXPECTED_SHEETS  # local import: avoids a cycle at module load

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Master data workbook not found: {path}")

    expected = list(expected_sheets if expected_sheets is not None else EXPECTED_SHEETS)
    overrides = orientation_overrides or {}

    # data_only=True reads cached formula results; read-only stays off so that
    # merged-cell metadata remains available.
    wb = load_workbook(path, data_only=True)
    try:
        sheets: dict[str, SheetData] = {}
        for ws in wb.worksheets:
            sheets[ws.title] = inspect_sheet(ws, overrides.get(ws.title))
        sheet_names = list(wb.sheetnames)
    finally:
        wb.close()

    lowered = {n.strip().casefold() for n in sheet_names}
    missing = [s for s in expected if s.strip().casefold() not in lowered]
    expected_lowered = {s.strip().casefold() for s in expected}
    unexpected = [n for n in sheet_names if n.strip().casefold() not in expected_lowered]

    return WorkbookInspection(
        source_path=str(path),
        sheet_names=sheet_names,
        expected_sheets=expected,
        missing_sheets=missing,
        unexpected_sheets=unexpected,
        sheets=sheets,
    )


def raw_quality_flags(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Count raw-value defects per field before cleaning.

    Reported: outer whitespace, present-but-empty strings, and numeric-typed cells
    (which matter for code fields, where Excel may already have eaten leading zeros).
    """
    flags: dict[str, dict[str, int]] = {}
    for record in records:
        for label, value in record.items():
            bucket = flags.setdefault(
                label, {"whitespace_padded": 0, "empty_string": 0, "numeric_typed": 0}
            )
            if has_leading_or_trailing_space(value):
                bucket["whitespace_padded"] += 1
            if is_empty_string(value):
                bucket["empty_string"] += 1
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                bucket["numeric_typed"] += 1
    return flags


__all__ = [
    "Orientation",
    "SheetStructure",
    "SheetData",
    "WorkbookInspection",
    "inspect_sheet",
    "inspect_streamed_sheet",
    "inspect_workbook",
    "detect_orientation",
    "detect_header_row",
    "detect_header_column",
    "raw_quality_flags",
    "cell_to_str",
]
