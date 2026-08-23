"""Download templates.

A template is the contract made visible: the exact headers the validator
expects, one obviously-illustrative example row, and a second sheet documenting
which columns are required and what each one must contain.

Example values are placeholders taken from the schema's own documentation
(``C001``, ``SKU001``, ``Example Traders``). No real customer, product, price or
transaction is ever written into a template — a template is a public artefact
handed to whoever is preparing the file, and it must not become a data leak.
"""

from __future__ import annotations

import csv
import io

from .registry import UploadType

#: The instruction sheet's own header row.
_GUIDE_HEADERS = ("Column", "Required", "Data type", "Guidance", "Description",
                  "Example")


def _guide_rows(upload_type: UploadType) -> list[tuple[str, ...]]:
    return [
        (
            column.name,
            "REQUIRED" if column.required else "Optional",
            column.kind,
            column.guidance,
            column.description or "",
            column.example or "",
        )
        for column in upload_type.columns
    ]


def _notes(upload_type: UploadType) -> list[str]:
    notes = [
        f"Upload type: {upload_type.label} ({upload_type.category.title()} data)",
        upload_type.description,
        f"Record identity: {upload_type.business_key_description}",
        "Do not rename, reorder or delete the header row.",
        "Delete the example row before uploading.",
        "Codes are text. Format code columns as Text in Excel so leading zeros "
        "survive.",
    ]
    if upload_type.note:
        notes.append(upload_type.note)
    return [note for note in notes if note]


def build_csv(upload_type: UploadType) -> bytes:
    """A CSV template: header row plus one example row.

    CSV has nowhere to put an instruction sheet, so the guidance is delivered
    as leading comment lines. ``csv.DictReader`` would treat those as data, so
    they are omitted here and published through the API and the .xlsx template
    instead — a CSV template is the headers and the example, nothing else.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([column.name for column in upload_type.columns])
    writer.writerow([column.example for column in upload_type.columns])
    # utf-8-sig so Excel opens Bangla column values without mangling them.
    return buffer.getvalue().encode("utf-8-sig")


def build_xlsx(upload_type: UploadType) -> bytes:
    """An Excel template: a data sheet plus an instructions sheet."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"

    header_font = Font(bold=True, color="FFFFFF")
    required_fill = PatternFill("solid", fgColor="1D4ED8")
    optional_fill = PatternFill("solid", fgColor="64748B")

    for index, column in enumerate(upload_type.columns, start=1):
        cell = sheet.cell(row=1, column=index, value=column.name)
        cell.font = header_font
        cell.fill = required_fill if column.required else optional_fill
        cell.alignment = Alignment(horizontal="left", vertical="center")
        sheet.column_dimensions[get_column_letter(index)].width = max(
            14, min(32, len(column.name) + 6)
        )
        # Codes must arrive as text: an Excel "General" cell would strip the
        # leading zeros from 001 before the file ever reached the server.
        example = sheet.cell(row=2, column=index, value=column.example or None)
        if column.kind in ("code", "phone", "text"):
            example.number_format = "@"
    sheet.freeze_panes = "A2"

    guide = workbook.create_sheet("Instructions")
    guide.column_dimensions["A"].width = 30
    guide.column_dimensions["B"].width = 12
    guide.column_dimensions["C"].width = 12
    guide.column_dimensions["D"].width = 60
    guide.column_dimensions["E"].width = 60
    guide.column_dimensions["F"].width = 24

    row = 1
    for note in _notes(upload_type):
        cell = guide.cell(row=row, column=1, value=note)
        cell.font = Font(bold=(row == 1))
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        row += 1
    row += 1

    for index, header in enumerate(_GUIDE_HEADERS, start=1):
        cell = guide.cell(row=row, column=index, value=header)
        cell.font = header_font
        cell.fill = required_fill
    row += 1

    for values in _guide_rows(upload_type):
        for index, value in enumerate(values, start=1):
            cell = guide.cell(row=row, column=index, value=value)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if index == 2 and value == "REQUIRED":
                cell.font = Font(bold=True)
        row += 1

    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def build(upload_type: UploadType, file_format: str = "xlsx") -> tuple[bytes, str, str]:
    """Return ``(content, filename, media_type)`` for a template download."""
    fmt = (file_format or "xlsx").lower()
    if fmt == "csv":
        return (
            build_csv(upload_type),
            f"{upload_type.key}_template.csv",
            "text/csv; charset=utf-8",
        )
    if fmt == "xlsx":
        return (
            build_xlsx(upload_type),
            f"{upload_type.key}_template.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    raise ValueError(f"Unsupported template format {file_format!r}. Use xlsx or csv.")


__all__ = ["build", "build_csv", "build_xlsx"]
