"""Source readers: the only layer that knows where transaction rows come from.

A reader turns a source into a stream of ``SourceRow`` objects. Everything
downstream — staging, validation, master mapping, facts — works on that stream
and never learns whether the data arrived as Excel, CSV, or a future SAP or
Sales Force App payload.

Adding SAP therefore means writing one class here and registering it; the
staging tables, validation rules and fact-table architecture stay untouched.
"""

from __future__ import annotations

import csv
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from ..utils.progress import Phase, ProgressReporter

SOURCE_TYPE_EXCEL = "EXCEL"
SOURCE_TYPE_CSV = "CSV"
SOURCE_TYPE_API = "API"
SOURCE_TYPE_MEMORY = "MEMORY"


@dataclass
class SourceRow:
    """One raw record with the provenance needed to trace it back."""

    row_number: int
    values: dict[str, Any]

    def get(self, header: str, default: Any = None) -> Any:
        return self.values.get(header, default)


class SourceReader(ABC):
    """Contract every source must satisfy."""

    #: EXCEL / CSV / API / ...
    source_type: str = "UNKNOWN"

    @property
    @abstractmethod
    def source_name(self) -> str:
        """File path or endpoint, recorded on the batch and on every fact row."""

    @property
    @abstractmethod
    def headers(self) -> list[str]:
        """Column headers as they appear in the source."""

    @abstractmethod
    def __iter__(self) -> Iterator[SourceRow]:
        """Yield rows in source order."""


class ExcelSourceReader(SourceReader):
    """Excel reader that reuses the Phase 1 header/layout detection.

    Blank leading rows and columns are handled automatically, so an export whose
    headers start on row 4 column C needs no configuration.
    """

    source_type = SOURCE_TYPE_EXCEL

    def __init__(self, path: str | Path, sheet_name: str | None = None,
                 progress: ProgressReporter | None = None) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"Source file not found: {self.path}")
        self.sheet_name = sheet_name
        #: Where the read reports itself. A reporter with no job is a working
        #: no-op, which is what every scripted import and every test gets, so
        #: the calls below stay unconditional.
        self.progress = progress or ProgressReporter(None)
        self._headers: list[str] = []
        self._rows: list[SourceRow] = []
        self._scan_total = -1
        self._loaded = False

    def _scanned(self, done: int, total: int) -> None:
        """Report how far into the sheet the read has got, and stop if asked to.

        The sheet is streamed, so this is the read: the rows arrive one at a
        time and this observer sees nearly all of the wait rather than the
        eighth of it that was left over when openpyxl built the whole sheet
        first. That is what makes a position honest to report here.

        A file that declares no dimensions gives no denominator, and then only
        the counter moves — a real number of rows read, with no claim about how
        far through that is. It is the generated workbooks that omit it; a file
        exported by Excel states its range.

        ``checkpoint`` is what makes Cancel work during a read. Until it existed
        the first cancellable moment came after the file had been parsed, so on
        a large workbook the button did nothing for the whole of the longest
        step.
        """
        if total != self._scan_total:
            self._scan_total = total
            if total > 0:
                self.progress.phase(Phase.READING, total=total)
        if total > 0:
            if self.progress.every(done, total):
                self.progress.rows(done)
        elif done % 500 == 0:
            self.progress.counts(processed_records=done)
            self.progress.checkpoint()

    @property
    def source_name(self) -> str:
        return str(self.path)

    def _load(self) -> None:
        if self._loaded:
            return
        from openpyxl import load_workbook

        from ..master_data.inspector import inspect_streamed_sheet

        # ``read_only`` is what makes the read reportable. Opened in full,
        # openpyxl builds the entire sheet inside one call that offers nothing to
        # observe — two thirds of the wait, during which the bar cannot move at
        # all. Streamed, the rows arrive one at a time and the same work is
        # counted as it happens. It costs about a seventh more wall clock and the
        # sheet's merged ranges, which nothing on this path reads; see
        # ``inspect_streamed_sheet``.
        self.progress.source(self.source_type)
        self.progress.phase(Phase.READING)
        workbook = load_workbook(self.path, data_only=True, read_only=True)
        try:
            if self.sheet_name is not None:
                if self.sheet_name not in workbook.sheetnames:
                    raise ValueError(
                        f"Sheet {self.sheet_name!r} not found in {self.path.name}. "
                        f"Available: {', '.join(workbook.sheetnames)}"
                    )
                worksheet = workbook[self.sheet_name]
            else:
                worksheet = workbook.worksheets[0]

            sheet_data = inspect_streamed_sheet(worksheet, on_row=self._scanned)
            headers = list(sheet_data.columns)
            rows = [
                SourceRow(_row_number(location), dict(record))
                for record, location in zip(sheet_data.records,
                                            sheet_data.source_locations)
            ]
        finally:
            workbook.close()
        # Published only once the whole sheet is in hand. A read abandoned part
        # way - a cancelled import raises out of the row observer - must not
        # leave a reader that looks loaded and would answer with half a file.
        self._headers = headers
        self._rows = rows
        self._loaded = True

    @property
    def headers(self) -> list[str]:
        self._load()
        return self._headers

    def __iter__(self) -> Iterator[SourceRow]:
        self._load()
        return iter(self._rows)


def _row_number(location: str) -> int:
    """``"row 7"`` -> ``7``; a column location has no row number, so use 0."""
    parts = location.split()
    if len(parts) == 2 and parts[0] == "row" and parts[1].isdigit():
        return int(parts[1])
    return 0


class CsvSourceReader(SourceReader):
    """Delimited-text reader.

    The delimiter is sniffed and can be overridden. ``utf-8-sig`` is tried first
    so that a BOM written by Excel never becomes part of the first header, and
    Bangla text in a UTF-8 file survives unchanged.
    """

    source_type = SOURCE_TYPE_CSV

    def __init__(self, path: str | Path, delimiter: str | None = None,
                 encoding: str | None = None,
                 progress: ProgressReporter | None = None) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"Source file not found: {self.path}")
        self.delimiter = delimiter
        self.encoding = encoding
        self.progress = progress or ProgressReporter(None)
        self._headers: list[str] = []
        self._rows: list[SourceRow] = []
        self._loaded = False

    @property
    def source_name(self) -> str:
        return str(self.path)

    def _decode(self) -> str:
        encodings = [self.encoding] if self.encoding else ["utf-8-sig", "utf-8", "cp1252"]
        last_error: Exception | None = None
        for encoding in encodings:
            try:
                return self.path.read_text(encoding=encoding)
            except UnicodeDecodeError as exc:
                last_error = exc
        raise ValueError(
            f"Could not decode {self.path.name} as any of {', '.join(encodings)}"
        ) from last_error

    def _load(self) -> None:
        if self._loaded:
            return
        self.progress.source(self.source_type)
        self.progress.phase(Phase.READING)
        text = self._decode()
        delimiter = self.delimiter
        if delimiter is None:
            sample = text[:8192]
            try:
                delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
            except csv.Error:
                delimiter = ","

        lines = text.splitlines()
        reader = csv.DictReader(lines, delimiter=delimiter)
        headers = [h for h in (reader.fieldnames or []) if h is not None]
        # Unlike a workbook, a delimited file is read a line at a time from the
        # first byte, so the whole of this pass is countable. The position
        # reported is ``line_num`` rather than the record count because a quoted
        # field may span several lines: lines consumed against lines present is
        # the one pair that is exactly true at both ends.
        total = len(lines)
        self.progress.phase(Phase.READING, total=total)
        step = max(1, total // 200)
        next_report = step
        rows: list[SourceRow] = []
        # Row 1 is the header, so the first data row is source row 2.
        for offset, record in enumerate(reader, start=2):
            record.pop(None, None)  # extra columns beyond the header
            if not all(_is_empty(v) for v in record.values()):
                rows.append(SourceRow(offset, dict(record)))
            if reader.line_num >= next_report:
                next_report = reader.line_num + step
                self.progress.rows(reader.line_num)
        # Assigned together and last, so a read stopped part way leaves the
        # reader unloaded rather than half loaded. See ``ExcelSourceReader``.
        self._headers = headers
        self._rows = rows
        self._loaded = True

    @property
    def headers(self) -> list[str]:
        self._load()
        return self._headers

    def __iter__(self) -> Iterator[SourceRow]:
        self._load()
        return iter(self._rows)


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


class RecordsSourceReader(SourceReader):
    """Reader over an in-memory sequence of dictionaries.

    This is the seam future integrations plug into: a SAP or Sales Force App
    client fetches its payload, normalises it to a list of dicts, and hands it
    here — the ETL pipeline is then identical to a file import. It is also what
    the API uses for JSON request bodies, and what the tests use.
    """

    source_type = SOURCE_TYPE_API

    def __init__(self, records: Sequence[dict[str, Any]], source_name: str = "in-memory",
                 source_type: str = SOURCE_TYPE_API, start_row: int = 1) -> None:
        self._records = list(records)
        self._source_name = source_name
        self.source_type = source_type
        self._start_row = start_row

    @property
    def source_name(self) -> str:
        return self._source_name

    @property
    def headers(self) -> list[str]:
        headers: list[str] = []
        for record in self._records:
            for key in record:
                if key not in headers:
                    headers.append(key)
        return headers

    def __iter__(self) -> Iterator[SourceRow]:
        for offset, record in enumerate(self._records, start=self._start_row):
            yield SourceRow(offset, dict(record))


#: Extension -> reader factory. Extend this (or pass a reader directly to the
#: pipeline) to support a new file type.
READER_BY_EXTENSION = {
    ".xlsx": ExcelSourceReader,
    ".xlsm": ExcelSourceReader,
    ".csv": CsvSourceReader,
    ".txt": CsvSourceReader,
    ".tsv": CsvSourceReader,
}


def reader_for_file(path: str | Path, sheet_name: str | None = None,
                    delimiter: str | None = None,
                    progress: ProgressReporter | None = None) -> SourceReader:
    """Pick a reader from the file extension."""
    path = Path(path)
    factory = READER_BY_EXTENSION.get(path.suffix.lower())
    if factory is None:
        raise ValueError(
            f"Unsupported source file type {path.suffix!r}. Supported: "
            f"{', '.join(sorted(READER_BY_EXTENSION))}."
        )
    if factory is ExcelSourceReader:
        return ExcelSourceReader(path, sheet_name=sheet_name, progress=progress)
    return CsvSourceReader(path, delimiter=delimiter, progress=progress)


__all__ = [
    "SourceRow",
    "SourceReader",
    "ExcelSourceReader",
    "CsvSourceReader",
    "RecordsSourceReader",
    "reader_for_file",
    "READER_BY_EXTENSION",
    "SOURCE_TYPE_EXCEL",
    "SOURCE_TYPE_CSV",
    "SOURCE_TYPE_API",
    "SOURCE_TYPE_MEMORY",
]
