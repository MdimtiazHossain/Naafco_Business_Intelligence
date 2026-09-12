"""Phase 4 extension models: section permissions and the data-upload centre.

Two independent concerns live here because both are *administration* of the
existing warehouse rather than part of it:

**Section permissions.** ``role_section_permissions`` holds the default a role
gets; ``user_section_permissions`` holds the per-user override. Neither table
stores an "effective" answer — that is computed in
:mod:`app.auth.permissions` from the section catalogue, the role default and the
override, in that order. Storing the computed answer would let it drift the
moment a role default changed.

**Upload batches.** The Phase 2 ``etl_import_batches`` table remains the record
of a *warehouse load* and is not redefined. ``upload_batches`` records a
*human upload* — who uploaded what file, whether it was master or transactional,
what mode it ran in, and how it ended — and links to the ETL batch when one ran.
That keeps master-data uploads (which do not go through the fact pipeline at
all) and transactional uploads in one auditable history without inventing a
second fact-loading path.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import SURROGATE_PK, Base
from .models_warehouse import FK_TYPE, JSON_TYPE

# ===========================================================================
# Section permissions
# ===========================================================================


class RoleSectionPermission(Base):
    """The default access a role has to a section.

    A row exists only when an administrator has overridden the catalogue
    default in :mod:`app.security.sections`; absence means "use the default".
    """

    __tablename__ = "role_section_permissions"

    id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    section_key: Mapped[str] = mapped_column(String(48), nullable=False)
    #: ALLOW / DENY.
    access: Mapped[str] = mapped_column(String(8), nullable=False)
    #: Reserved for the granular VIEW/CREATE/EDIT/DELETE/EXPORT/UPLOAD model.
    #: Written by nothing in Phase 4; read by nothing in Phase 4.
    actions: Mapped[dict | None] = mapped_column(JSON_TYPE)
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("role", "section_key", name="uq_role_section_permission"),
        Index("ix_role_section_permissions_role", "role"),
    )


class UserSectionPermission(Base):
    """One user's explicit ALLOW/DENY for one section.

    This layer sits **below** the role in the precedence chain: it can narrow a
    role's access, and it can restore access the role default withheld, but it
    can never grant a section whose ``locked_to_roles`` excludes the user's
    role. That check lives in :mod:`app.auth.permissions` so it cannot be
    bypassed by writing a row directly through the API.
    """

    __tablename__ = "user_section_permissions"

    id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        FK_TYPE, ForeignKey("app_user.user_id", ondelete="CASCADE"), nullable=False
    )
    section_key: Mapped[str] = mapped_column(String(48), nullable=False)
    #: ALLOW / DENY.
    access: Mapped[str] = mapped_column(String(8), nullable=False)
    actions: Mapped[dict | None] = mapped_column(JSON_TYPE)
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "section_key", name="uq_user_section_permission"),
        Index("ix_user_section_permissions_user_id", "user_id"),
    )


# ===========================================================================
# Data upload centre
# ===========================================================================


class UploadCategory:
    MASTER = "MASTER"
    TRANSACTIONAL = "TRANSACTIONAL"

    ALL = (MASTER, TRANSACTIONAL)


class UploadStatus:
    """The lifecycle an upload batch moves through.

    ``PARTIAL`` is what other systems call COMPLETED_WITH_ERRORS: the import ran,
    some rows loaded and some were rejected. It keeps its original name because
    every batch already in the history carries it, and renaming a stored value to
    improve its wording would rewrite the audit trail to say something it did not
    say at the time.

    ``QUEUED`` and ``CANCELLED`` arrive with background import jobs: a job can now
    be waiting for a worker before it starts, and a user can stop one that is
    running. Both are plain values in the existing ``VARCHAR(16)`` column, so they
    need no schema change — only this tuple, which ``GET /api/data-upload/history``
    publishes so the status filter picks them up on its own.
    """

    UPLOADED = "UPLOADED"
    #: Accepted and staged, waiting for a worker. Nothing has been read yet.
    QUEUED = "QUEUED"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    IMPORTING = "IMPORTING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    #: Stopped by the user. The run's transaction was rolled back, so a cancelled
    #: import has written nothing — there is no half-loaded batch to clean up.
    CANCELLED = "CANCELLED"
    ROLLED_BACK = "ROLLED_BACK"

    ALL = (UPLOADED, QUEUED, VALIDATING, VALIDATED, IMPORTING, COMPLETED,
           PARTIAL, FAILED, CANCELLED, ROLLED_BACK)
    #: Statuses that mean "nothing further will happen to this batch".
    TERMINAL = (COMPLETED, PARTIAL, FAILED, CANCELLED, ROLLED_BACK)
    #: A job is running or about to. These are what the startup sweep looks for
    #: after a restart, and what blocks a duplicate upload of the same file.
    ACTIVE = (QUEUED, VALIDATING, IMPORTING)


class ImportMode:
    """How an upload applies to existing records.

    ``REPLACE`` is deliberately absent: nothing in this system deletes existing
    rows to make room for an upload. Re-loading a transactional business key
    updates that row in place (``UPSERT``); it never truncates a fact table.
    """

    INSERT = "INSERT"
    UPDATE = "UPDATE"
    UPSERT = "UPSERT"

    ALL = (INSERT, UPDATE, UPSERT)


class UploadBatch(Base):
    """One human upload: the audit unit of the Data Upload Center."""

    __tablename__ = "upload_batches"

    upload_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                           autoincrement=True)
    upload_uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    #: MASTER / TRANSACTIONAL.
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Upload type key: a master table name (``dim_region``) or a transactional
    #: data type (``sales``). Validated against the upload registry.
    upload_type: Mapped[str] = mapped_column(String(48), nullable=False)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_type: Mapped[str | None] = mapped_column(String(128))
    #: SHA-256 of the uploaded bytes. Only used to notice that the file already
    #: has an import running, so a double-click adopts that job instead of
    #: starting a second one. Never a uniqueness constraint: re-uploading the same
    #: corrected file tomorrow is a legitimate thing to do.
    file_hash: Mapped[str | None] = mapped_column(String(64))
    #: Path of the staged copy, so a validated upload can be committed without
    #: being re-uploaded. Cleared once the batch reaches a terminal status.
    stored_path: Mapped[str | None] = mapped_column(Text)
    import_mode: Mapped[str] = mapped_column(String(16), nullable=False,
                                             default=ImportMode.UPSERT)
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=UploadStatus.UPLOADED)

    #: How this file signs a deduction, as **declared for this upload** — one of
    #: ``etl.credit.DEDUCTION_CONVENTIONS``, or NULL for an upload type that
    #: states no deductions.
    #:
    #: Defaulted from ``etl.credit.detect_convention`` and shown in the preview
    #: with the counts it was read off, so the person committing the file sees
    #: both the answer and the evidence. It is a declaration because two real
    #: receivables extracts disagree and both are ``credit_invoice``: one
    #: constant could not have been right for both, and a file whose deductions
    #: all happen to be zero carries no evidence at all and is refused rather
    #: than guessed at.
    deduction_convention: Mapped[str | None] = mapped_column(String(16))

    #: For a **scoped restatement**, the values of the dataset's
    #: ``restatement_scope_field`` this file states in full. Rows inside the
    #: scope that the file does not name are voided when it is committed.
    #:
    #: NULL for an ordinary upload, which stands nothing down. Defaulted from the
    #: values the file contains and **confirmed by a person in the preview** —
    #: never taken silently, because inferring from a file's contents what to
    #: stand down is the most dangerous form of invented data this platform can
    #: commit: a file that accidentally omitted a company would erase that
    #: company's book, and the erasure would look exactly like a correct
    #: restatement.
    restatement_scope: Mapped[list | None] = mapped_column(JSON_TYPE)

    user_id: Mapped[int | None] = mapped_column(FK_TYPE)
    username: Mapped[str | None] = mapped_column(String(64))

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Last reported phase and percentage, mirrored from the in-memory job so a
    #: batch that was cancelled or interrupted can still say *where* it stopped.
    #: Live progress is never read from here — the job registry serves that — but
    #: once the job has left memory this is all that remains of it.
    stage: Mapped[str | None] = mapped_column(String(24))
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Set when a user stops a running import. Mirrors ``rolled_back_at``/``_by``:
    #: the two are different acts — cancelling stops work that has committed
    #: nothing, rolling back removes rows that were committed — and the history
    #: has to be able to tell them apart.
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[str | None] = mapped_column(String(64))

    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    column_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inserted_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: The Phase 2 ETL batch this upload produced, when it ran the fact
    #: pipeline. NULL for master-data uploads and for validation-only runs.
    etl_batch_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("etl_import_batches.batch_id", ondelete="SET NULL"),
    )
    #: Counts by error code, unmapped columns, warnings — never row data.
    summary: Mapped[dict | None] = mapped_column(JSON_TYPE)
    #: The first ``service.PREVIEW_ROWS`` rows as the validation run judged them,
    #: with the columns that describe them. Persisted because validation is now a
    #: background job: the request that started it has long returned by the time
    #: there is a preview to show, so it is fetched from the batch afterwards
    #: rather than returned inline. Kept out of ``summary`` so that column stays
    #: what its comment says it is — counts, not row data.
    preview: Mapped[dict | None] = mapped_column(JSON_TYPE)
    message: Mapped[str | None] = mapped_column(Text)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rolled_back_by: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_upload_batches_category", "category"),
        Index("ix_upload_batches_upload_type", "upload_type"),
        Index("ix_upload_batches_status", "status"),
        Index("ix_upload_batches_started_at", "started_at"),
        Index("ix_upload_batches_user_id", "user_id"),
        # Read on every upload, to ask whether this exact file already has an
        # import running. Not unique: the same file may be imported again.
        Index("ix_upload_batches_file_hash", "file_hash"),
    )


class UploadError(Base):
    """One validation failure, addressed to the person who must fix the file.

    Deliberately row/column/value/reason/fix rather than a stack trace: this is
    what the downloadable error report is built from, and what the preview
    screen lists.
    """

    __tablename__ = "upload_errors"

    id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    upload_id: Mapped[int] = mapped_column(
        FK_TYPE, ForeignKey("upload_batches.upload_id", ondelete="CASCADE"),
        nullable=False,
    )
    row_number: Mapped[int | None] = mapped_column(Integer)
    column_name: Mapped[str | None] = mapped_column(String(128))
    value: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False)
    error_category: Mapped[str] = mapped_column(String(32), nullable=False,
                                                default="VALIDATION")
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_fix: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="ERROR")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_upload_errors_upload_id", "upload_id"),
        Index("ix_upload_errors_error_code", "error_code"),
    )


SEVERITY_ERROR = "ERROR"
SEVERITY_WARNING = "WARNING"


# ===========================================================================
# Record change history
# ===========================================================================


class ChangeAction:
    """What happened to a record."""

    CREATED = "CREATED"
    UPDATED = "UPDATED"
    DELETED = "DELETED"
    RESTORED = "RESTORED"
    VOIDED = "VOIDED"
    UNVOIDED = "UNVOIDED"

    ALL = (CREATED, UPDATED, DELETED, RESTORED, VOIDED, UNVOIDED)


class DataChangeLog(Base):
    """Field-level history for one business record.

    ``audit_logs`` answers "who did what, when" across the whole application and
    is queried by action and by user. This answers a different question — "what
    has happened to *this customer*" — and is queried by record, which is why it
    is its own table with its own index rather than a filter over the audit log.

    It also keeps what the audit log deliberately does not: the values. The old
    row is stored on every change and on every delete, so a mistaken edit can be
    read back field by field, and a deleted record can be reconstructed even
    though the row itself is only flagged.

    Values are stored as JSON of the changed fields only, not the whole record:
    a history entry should show what moved, and storing untouched columns over
    and over would bury that in noise.
    """

    __tablename__ = "data_change_log"

    change_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                           autoincrement=True)
    #: Catalogue key of the entity — ``dim_customer``, ``sales``, …
    entity_key: Mapped[str] = mapped_column(String(48), nullable=False)
    #: The record's business identity, as text: a code, or a fact's surrogate id.
    record_key: Mapped[str] = mapped_column(String(256), nullable=False)
    #: A human label captured at the time, so history still reads sensibly after
    #: the record is renamed or deleted.
    record_label: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(16), nullable=False)

    user_id: Mapped[int | None] = mapped_column(FK_TYPE)
    username: Mapped[str | None] = mapped_column(String(64))
    #: The role held at the time of the change, which may differ from the role
    #: the account holds today.
    role: Mapped[str | None] = mapped_column(String(32))

    changed_fields: Mapped[list | None] = mapped_column(JSON_TYPE)
    old_values: Mapped[dict | None] = mapped_column(JSON_TYPE)
    new_values: Mapped[dict | None] = mapped_column(JSON_TYPE)
    reason: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # The index the detail page's History tab reads.
        Index("ix_data_change_log_record", "entity_key", "record_key"),
        Index("ix_data_change_log_created_at", "created_at"),
        Index("ix_data_change_log_username", "username"),
    )


__all__ = [
    "RoleSectionPermission",
    "UserSectionPermission",
    "UploadBatch",
    "UploadError",
    "UploadCategory",
    "UploadStatus",
    "ImportMode",
    "ChangeAction",
    "DataChangeLog",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
]
