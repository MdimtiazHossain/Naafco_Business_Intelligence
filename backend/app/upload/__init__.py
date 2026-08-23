"""The Data Upload Center.

Everything here is a *front end* onto machinery that already exists:

* master-data uploads are validated against ``master_data.schema.TABLE_SPECS``
  — the same specs the Phase 1 workbook importer uses — and loaded with the same
  upsert-on-business-code rule;
* transactional uploads run the Phase 2 ETL pipeline unchanged, so staging,
  master mapping, hierarchy checks, business-key de-duplication and the fact
  tables behave exactly as they do for a scripted import.

No master table and no fact table is defined here. The only new storage is the
audit trail of who uploaded what (``upload_batches`` / ``upload_errors``).
"""

from .registry import (
    CATEGORIES,
    UPLOAD_TYPES,
    UPLOAD_TYPE_BY_KEY,
    UploadColumn,
    UploadType,
    get_upload_type,
)

__all__ = [
    "UPLOAD_TYPES",
    "UPLOAD_TYPE_BY_KEY",
    "CATEGORIES",
    "UploadType",
    "UploadColumn",
    "get_upload_type",
]
