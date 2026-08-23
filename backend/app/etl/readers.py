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

    def __init__(self, path: str | Path, sheet_name: str | None = None) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"Source file not found: {self.path}")
        self.sheet_name = sheet_name
        self._headers: list[str] = []
        self._rows: list[SourceRow] = []
        self._loaded = False

    @property
    def source_name(self) -> str:
        return str(self.path)

    def _load(self) -> None:
        if self._loaded:
            return
        from openpyxl import load_workbook

        from ..master_data.inspector import inspect_sheet

        workbook = load_workbook(self.path, data_only=True)
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

            sheet_data = inspect_sheet(worksheet)
            self._headers = list(sheet_data.columns)
            for record, location in zip(sheet_data.records, sheet_data.source_locations):
                self._rows.append(SourceRow(_row_number(location), dict(record)))
        finally:
            workbook.close()
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
                 encoding: str | None = None) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"Source file not found: {self.path}")
        self.delimiter = delimiter
        self.encoding = encoding
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
        text = self._decode()
        delimiter = self.delimiter
        if delimiter is None:
            sample = text[:8192]
            try:
                delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
            except csv.Error:
                delimiter = ","

        reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
        self._headers = [h for h in (reader.fieldnames or []) if h is not None]
        # Row 1 is the header, so the first data row is source row 2.
        for offset, record in enumerate(reader, start=2):
            record.pop(None, None)  # extra columns beyond the header
            if all(_is_empty(v) for v in record.values()):
                continue
            self._rows.append(SourceRow(offset, dict(record)))
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
                    delimiter: str | None = None) -> SourceReader:
    """Pick a reader from the file extension."""
    path = Path(path)
    factory = READER_BY_EXTENSION.get(path.suffix.lower())
    if factory is None:
        raise ValueError(
            f"Unsupported source file type {path.suffix!r}. Supported: "
            f"{', '.join(sorted(READER_BY_EXTENSION))}."
        )
    if factory is ExcelSourceReader:
        return ExcelSourceReader(path, sheet_name=sheet_name)
    return CsvSourceReader(path, delimiter=delimiter)


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
