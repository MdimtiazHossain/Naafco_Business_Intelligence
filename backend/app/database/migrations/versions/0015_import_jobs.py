"""Background import jobs: progress, cancellation and the persisted preview.

Revision ID: 0015_import_jobs
Revises: 0014_target_structure
Create Date: 2026-08-16

An upload used to be a request: the browser posted a file and waited for the
finished result. It is becoming a *job* — queued, worked by a background thread,
watched while it runs and stoppable by the person who started it. This adds the
columns that a job needs and the request did not.

**Additive and nullable throughout.** No table, column or row is removed, every
new column is nullable or carries a server default, and every existing batch
stays exactly as valid as it was: an upload from last week simply has no stage,
no cancellation and a zero percentage, which is the truth about it.

``upload_batches`` already had the job's identity. ``upload_uuid`` is unique and
one-per-upload, so it becomes the Import Job ID rather than a second identifier
being introduced beside it; ``status``, ``username``, ``started_at``,
``completed_at`` and the six row counts already describe a job's outcome. What
was missing is where a job *got to* rather than where it finished:

``stage`` / ``progress_percent``
    The last phase and percentage the run reported. Live progress is served from
    the in-memory job registry, not from here — a running import holds one
    transaction and could not publish progress through it anyway — but once the
    job has left memory this is the only remaining record of how far a cancelled
    or interrupted import got, and the history has to be able to say "stopped at
    68%, 6,800 of 10,000".

``cancelled_at`` / ``cancelled_by``
    Deliberately separate from ``rolled_back_at`` / ``rolled_back_by``. The two
    look similar and mean opposite things: a rollback removes rows that were
    committed, while a cancellation stops a run whose transaction never committed
    at all. Recording both in one pair of columns would make an import that wrote
    nothing indistinguishable from one whose rows were deleted afterwards.

``file_hash``
    SHA-256 of the uploaded bytes, so a second submission of a file that is
    already being imported can adopt the running job instead of starting a
    duplicate. Deliberately *not* unique: re-uploading the same file after
    fixing the master data it referenced is a normal thing to do.

``preview``
    The rows the Data Upload Centre shows before you confirm. They used to be
    returned inline by ``POST /preview``, which worked while that call did the
    validation itself. As a background job the request returns before there is
    anything to preview, so the rows are stored on the batch and fetched with it.
    Kept out of ``summary`` because that column holds counts and warnings, and
    mixing row data into it would quietly change what it means.

**Statuses need no migration.** ``QUEUED`` and ``CANCELLED`` are new values in
the existing ``VARCHAR(16) status`` column, not a new type. Nothing in the
database constrains that column to a list — the list lives in
``models_admin.UploadStatus.ALL``, which the history endpoint publishes so the
UI's filter follows automatically.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_import_jobs"
down_revision: Union[str, None] = "0014_target_structure"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: The same variant 0006 gave ``upload_batches.summary``: JSONB on PostgreSQL,
#: TEXT-backed JSON on SQLite. Spelled identically so the two JSON columns on
#: this table cannot end up with different storage.
JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()),
                                   "postgresql")


def upgrade() -> None:
    op.add_column("upload_batches", sa.Column("file_hash", sa.String(length=64),
                                              nullable=True))
    op.add_column("upload_batches", sa.Column("stage", sa.String(length=24),
                                              nullable=True))
    # Server default rather than a back-fill: an existing batch is finished, and
    # a finished batch's progress is not a number anyone should read. Zero is the
    # honest starting value for the column, and the status column is what says
    # how each old row actually ended.
    op.add_column(
        "upload_batches",
        sa.Column("progress_percent", sa.Integer(), nullable=False,
                  server_default="0"),
    )
    op.add_column("upload_batches",
                  sa.Column("cancelled_at", sa.DateTime(timezone=True),
                            nullable=True))
    op.add_column("upload_batches",
                  sa.Column("cancelled_by", sa.String(length=64), nullable=True))
    op.add_column("upload_batches", sa.Column("preview", JSON_TYPE, nullable=True))

    # The duplicate check asks "is this file already importing?" on every upload,
    # which filters on the hash and the status together. ``status`` is already
    # indexed by 0006 and ``user_id`` by the same revision, so only the hash is
    # new here. Not unique: the same file may legitimately be imported again.
    op.create_index("ix_upload_batches_file_hash", "upload_batches", ["file_hash"])


def downgrade() -> None:
    op.drop_index("ix_upload_batches_file_hash", table_name="upload_batches")
    for column in ("preview", "cancelled_by", "cancelled_at", "progress_percent",
                   "stage", "file_hash"):
        op.drop_column("upload_batches", column)
