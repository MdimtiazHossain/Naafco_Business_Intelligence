"""Phase 4 extension: data upload centre and section permissions.

Revision ID: 0006_upload_permissions
Revises: 0005_platform
Create Date: 2026-08-11

Additive only. No existing table is dropped, truncated or rewritten; the three
new ``app_user`` columns carry server defaults so rows created before this
migration keep working unchanged.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_upload_permissions"
down_revision: Union[str, None] = "0005_platform"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    # --- section permissions ------------------------------------------------
    op.create_table(
        "role_section_permissions",
        sa.Column("id", PK, autoincrement=True, nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("section_key", sa.String(length=48), nullable=False),
        sa.Column("access", sa.String(length=8), nullable=False),
        sa.Column("actions", JSON_TYPE, nullable=True),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("role", "section_key", name="uq_role_section_permission"),
    )
    op.create_index("ix_role_section_permissions_role", "role_section_permissions",
                    ["role"], unique=False)

    op.create_table(
        "user_section_permissions",
        sa.Column("id", PK, autoincrement=True, nullable=False),
        sa.Column("user_id", PK, nullable=False),
        sa.Column("section_key", sa.String(length=48), nullable=False),
        sa.Column("access", sa.String(length=8), nullable=False),
        sa.Column("actions", JSON_TYPE, nullable=True),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "section_key", name="uq_user_section_permission"),
    )
    op.create_index("ix_user_section_permissions_user_id", "user_section_permissions",
                    ["user_id"], unique=False)

    # --- data upload centre -------------------------------------------------
    op.create_table(
        "upload_batches",
        sa.Column("upload_id", PK, autoincrement=True, nullable=False),
        sa.Column("upload_uuid", sa.String(length=36), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("upload_type", sa.String(length=48), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("stored_path", sa.Text(), nullable=True),
        sa.Column("import_mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("user_id", PK, nullable=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column("column_count", sa.Integer(), nullable=False),
        sa.Column("valid_rows", sa.Integer(), nullable=False),
        sa.Column("invalid_rows", sa.Integer(), nullable=False),
        sa.Column("duplicate_rows", sa.Integer(), nullable=False),
        sa.Column("inserted_rows", sa.Integer(), nullable=False),
        sa.Column("updated_rows", sa.Integer(), nullable=False),
        sa.Column("etl_batch_id", PK, nullable=True),
        sa.Column("summary", JSON_TYPE, nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_by", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["etl_batch_id"], ["etl_import_batches.batch_id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("upload_id"),
        sa.UniqueConstraint("upload_uuid", name="uq_upload_batches_uuid"),
    )
    op.create_index("ix_upload_batches_category", "upload_batches", ["category"])
    op.create_index("ix_upload_batches_upload_type", "upload_batches", ["upload_type"])
    op.create_index("ix_upload_batches_status", "upload_batches", ["status"])
    op.create_index("ix_upload_batches_started_at", "upload_batches", ["started_at"])
    op.create_index("ix_upload_batches_user_id", "upload_batches", ["user_id"])

    op.create_table(
        "upload_errors",
        sa.Column("id", PK, autoincrement=True, nullable=False),
        sa.Column("upload_id", PK, nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=True),
        sa.Column("column_name", sa.String(length=128), nullable=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=False),
        sa.Column("error_category", sa.String(length=32), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("suggested_fix", sa.Text(), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["upload_id"], ["upload_batches.upload_id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_upload_errors_upload_id", "upload_errors", ["upload_id"])
    op.create_index("ix_upload_errors_error_code", "upload_errors", ["error_code"])

    # --- app_user: department, designation, status --------------------------
    # server_default is mandatory: existing rows need a value for a NOT NULL
    # column added after the fact.
    with op.batch_alter_table("app_user") as batch:
        batch.add_column(sa.Column("department", sa.Text(), nullable=True))
        batch.add_column(sa.Column("designation", sa.Text(), nullable=True))
        batch.add_column(sa.Column("status", sa.String(length=16), nullable=False,
                                   server_default="ACTIVE"))

    # Existing deactivated accounts must not come back as ACTIVE.
    op.execute(
        sa.text("UPDATE app_user SET status = 'INACTIVE' WHERE is_active = :inactive")
        .bindparams(inactive=False)
    )


def downgrade() -> None:
    with op.batch_alter_table("app_user") as batch:
        batch.drop_column("status")
        batch.drop_column("designation")
        batch.drop_column("department")

    op.drop_index("ix_upload_errors_error_code", table_name="upload_errors")
    op.drop_index("ix_upload_errors_upload_id", table_name="upload_errors")
    op.drop_table("upload_errors")

    for index in ("ix_upload_batches_user_id", "ix_upload_batches_started_at",
                  "ix_upload_batches_status", "ix_upload_batches_upload_type",
                  "ix_upload_batches_category"):
        op.drop_index(index, table_name="upload_batches")
    op.drop_table("upload_batches")

    op.drop_index("ix_user_section_permissions_user_id",
                  table_name="user_section_permissions")
    op.drop_table("user_section_permissions")
    op.drop_index("ix_role_section_permissions_role",
                  table_name="role_section_permissions")
    op.drop_table("role_section_permissions")
