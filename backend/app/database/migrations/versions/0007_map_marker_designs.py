"""Marker and shape designs for the business map.

Revision ID: 0007_map_markers
Revises: 0006_upload_permissions
Create Date: 2026-08-11

Additive only: four new tables, no existing table touched and no data removed.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_map_markers"
down_revision: Union[str, None] = "0006_upload_permissions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    # Assets first: designs reference them.
    op.create_table(
        "map_marker_assets",
        sa.Column("asset_id", PK, autoincrement=True, nullable=False),
        sa.Column("asset_uuid", sa.String(length=36), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sanitised_report", JSON_TYPE, nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("uploaded_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("asset_id"),
        sa.UniqueConstraint("asset_uuid", name="uq_map_marker_assets_uuid"),
    )
    op.create_index("ix_map_marker_assets_checksum", "map_marker_assets", ["checksum"])

    op.create_table(
        "map_marker_designs",
        sa.Column("design_id", PK, autoincrement=True, nullable=False),
        sa.Column("design_uuid", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("design_type", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("definition", JSON_TYPE, nullable=False),
        sa.Column("asset_id", PK, nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_system_default", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["map_marker_assets.asset_id"],
                                ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("design_id"),
        sa.UniqueConstraint("design_uuid", name="uq_map_marker_designs_uuid"),
    )
    op.create_index("ix_map_marker_designs_entity_type", "map_marker_designs",
                    ["entity_type"])
    op.create_index("ix_map_marker_designs_status", "map_marker_designs", ["status"])
    op.create_index("ix_map_marker_designs_entity_status", "map_marker_designs",
                    ["entity_type", "status"])

    op.create_table(
        "map_marker_design_versions",
        sa.Column("version_id", PK, autoincrement=True, nullable=False),
        sa.Column("design_id", PK, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("design_type", sa.String(length=24), nullable=False),
        sa.Column("definition", JSON_TYPE, nullable=False),
        sa.Column("asset_id", PK, nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["design_id"], ["map_marker_designs.design_id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("version_id"),
        sa.UniqueConstraint("design_id", "version", name="uq_marker_design_version"),
    )
    op.create_index("ix_map_marker_design_versions_design_id",
                    "map_marker_design_versions", ["design_id"])

    op.create_table(
        "map_marker_assignments",
        sa.Column("assignment_id", PK, autoincrement=True, nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_code", sa.String(length=64), nullable=True),
        sa.Column("design_id", PK, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("condition", JSON_TYPE, nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["design_id"], ["map_marker_designs.design_id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("assignment_id"),
        sa.UniqueConstraint("entity_type", "entity_code",
                            name="uq_map_marker_assignment_entity"),
    )
    op.create_index("ix_map_marker_assignments_entity_type", "map_marker_assignments",
                    ["entity_type"])
    op.create_index("ix_map_marker_assignments_design_id", "map_marker_assignments",
                    ["design_id"])


def downgrade() -> None:
    op.drop_index("ix_map_marker_assignments_design_id",
                  table_name="map_marker_assignments")
    op.drop_index("ix_map_marker_assignments_entity_type",
                  table_name="map_marker_assignments")
    op.drop_table("map_marker_assignments")

    op.drop_index("ix_map_marker_design_versions_design_id",
                  table_name="map_marker_design_versions")
    op.drop_table("map_marker_design_versions")

    for index in ("ix_map_marker_designs_entity_status",
                  "ix_map_marker_designs_status",
                  "ix_map_marker_designs_entity_type"):
        op.drop_index(index, table_name="map_marker_designs")
    op.drop_table("map_marker_designs")

    op.drop_index("ix_map_marker_assets_checksum", table_name="map_marker_assets")
    op.drop_table("map_marker_assets")
