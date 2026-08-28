"""Upload file handling: what is accepted, and where it is put.

Only ``.xlsx`` and ``.csv`` are accepted. That is a deliberate narrowing of the
Phase 2 script-side reader set (which also tolerates ``.xlsm``, ``.tsv`` and
``.txt``): a browser upload is an untrusted input path, so it takes the smallest
surface that satisfies the requirement.

Four things are checked before a single byte is parsed — extension, declared
MIME type, size, and the file's own magic bytes — because any one of them alone
is trivial to spoof by renaming a file.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from ..config import get_settings
from ..etl.readers import CsvSourceReader, ExcelSourceReader, SourceReader
from ..utils.progress import ProgressReporter

EXTENSION_XLSX = ".xlsx"
EXTENSION_CSV = ".csv"

#: The only extensions a browser upload may carry.
ALLOWED_EXTENSIONS: tuple[str, ...] = (EXTENSION_XLSX, EXTENSION_CSV)

#: Content types browsers and Excel actually send for those two extensions.
#: ``application/octet-stream`` is included because several browsers fall back
#: to it for .xlsx; the magic-byte check below is what actually proves the type.
ALLOWED_CONTENT_TYPES: dict[str, tuple[str, ...]] = {
    EXTENSION_XLSX: (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/octet-stream",
        "application/zip",
        "",
    ),
    EXTENSION_CSV: (
        "text/csv",
        "text/plain",
        "application/csv",
        "application/vnd.ms-excel",
        "application/octet-stream",
        "",
    ),
}

#: 25 MB. Large enough for a year of transactions, small enough that a single
#: request cannot exhaust the server's memory or disk.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

#: An .xlsx is a ZIP container; every one starts with the ZIP local-file header.
_ZIP_MAGIC = b"PK\x03\x04"


class UploadFileError(ValueError):
    """A file the upload centre refuses, with a message safe to show a user."""


@dataclass(frozen=True)
class StoredUpload:
    """A staged upload on disk."""

    path: Path
    original_name: str
    extension: str
    size: int
    content_type: str | None
    #: SHA-256 of the stored bytes, computed while streaming so the file is not
    #: read a second time. Used only to recognise that this exact file already
    #: has an import running; it is never a uniqueness rule, because uploading
    #: the same file again after fixing the master data it referenced is normal.
    sha256: str | None = None

    def reader(self, sheet_name: str | None = None,
               progress: ProgressReporter | None = None) -> SourceReader:
        """A Phase 2 source reader over the staged file.

        ``progress`` is the job's reporter, and reading is the one step it could
        not previously see: the file is parsed before the pipeline starts, so a
        job spent the whole of the longest phase claiming to be preparing. A
        reader given no reporter reports nothing, which is what the scripts and
        the tests get.
        """
        if self.extension == EXTENSION_XLSX:
            return ExcelSourceReader(self.path, sheet_name=sheet_name,
                                     progress=progress)
        return CsvSourceReader(self.path, progress=progress)


def upload_dir() -> Path:
    """Where staged uploads live, created on first use."""
    directory = Path(get_settings().transactions_dir).parent / "uploads"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def validate_name(file_name: str | None) -> str:
    """Check the extension and return it. Raises :class:`UploadFileError`."""
    if not file_name:
        raise UploadFileError("The upload had no file name.")
    extension = Path(file_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise UploadFileError(
            f"'{extension or file_name}' is not a supported file type. Upload an "
            f"Excel (.xlsx) or CSV (.csv) file."
        )
    return extension


def validate_content_type(extension: str, content_type: str | None) -> None:
    allowed = ALLOWED_CONTENT_TYPES[extension]
    declared = (content_type or "").split(";")[0].strip().lower()
    if declared not in allowed:
        raise UploadFileError(
            f"The file's content type ('{declared or 'unknown'}') does not match a "
            f"{extension} file. Upload an Excel (.xlsx) or CSV (.csv) file."
        )


def _check_magic(path: Path, extension: str) -> None:
    """Confirm the bytes match the extension.

    A ``.csv`` that is really a renamed executable, or an ``.xlsx`` that is not a
    ZIP container, is rejected here rather than being handed to a parser.
    """
    with path.open("rb") as handle:
        head = handle.read(8)
    if extension == EXTENSION_XLSX:
        if not head.startswith(_ZIP_MAGIC):
            raise UploadFileError(
                "The file is named .xlsx but is not a valid Excel workbook. "
                "Re-save it from Excel as 'Excel Workbook (.xlsx)'."
            )
        return
    # CSV: reject anything that begins with a known binary container magic.
    for magic in (_ZIP_MAGIC, b"\x7fELF", b"MZ", b"%PDF", b"\xd0\xcf\x11\xe0"):
        if head.startswith(magic):
            raise UploadFileError(
                "The file is named .csv but contains binary data. Save it as "
                "'CSV UTF-8 (Comma delimited)'."
            )


def store(source: BinaryIO, file_name: str | None,
          content_type: str | None = None) -> StoredUpload:
    """Validate and stage an uploaded file.

    The file is copied in bounded chunks and abandoned the moment it exceeds
    :data:`MAX_UPLOAD_BYTES`, so an oversized upload never lands on disk in full.
    The stored name is a UUID: the original name is recorded on the batch for
    audit, but is never used as a path, so ``../`` in a file name is inert.
    """
    extension = validate_name(file_name)
    validate_content_type(extension, content_type)

    target = upload_dir() / f"{uuid.uuid4()}{extension}"
    size = 0
    # Hashed as the bytes go past rather than by re-reading the file afterwards:
    # the digest is only used to spot a duplicate submission, and that is not
    # worth a second pass over 25 MB.
    digest = hashlib.sha256()
    try:
        with target.open("wb") as handle:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise UploadFileError(
                        f"The file is larger than the "
                        f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit. Split it into "
                        "smaller files and upload them one at a time."
                    )
                digest.update(chunk)
                handle.write(chunk)
        if size == 0:
            raise UploadFileError("The file is empty.")
        _check_magic(target, extension)
    except Exception:
        target.unlink(missing_ok=True)
        raise

    return StoredUpload(
        path=target,
        original_name=Path(file_name or "upload").name,
        extension=extension,
        size=size,
        content_type=content_type,
        sha256=digest.hexdigest(),
    )


def discard(path: str | Path | None) -> None:
    """Delete a staged file. Never raises: cleanup must not fail a request."""
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:  # pragma: no cover - permissions / locked file
        pass


__all__ = [
    "ALLOWED_EXTENSIONS",
    "ALLOWED_CONTENT_TYPES",
    "MAX_UPLOAD_BYTES",
    "EXTENSION_CSV",
    "EXTENSION_XLSX",
    "StoredUpload",
    "UploadFileError",
    "store",
    "discard",
    "upload_dir",
    "validate_name",
    "validate_content_type",
]
