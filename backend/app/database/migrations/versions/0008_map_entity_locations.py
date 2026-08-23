"""Where business entities sit on the map.

Revision ID: 0008_map_locations
Revises: 0007_map_markers
Create Date: 2026-08-11

Additive only. The Phase 1 master dimensions are deliberately **not** given
latitude/longitude columns: coordinates live in their own table with their own
provenance, so the workbook's contract stays exactly as Phase 1 defined it.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_map_locations"
down_revision: Union[str, None] = "0007_map_markers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "map_entity_locations",
        sa.Column("location_id", PK, autoincrement=True, nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_code", sa.String(length=64), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("precision", sa.String(length=16), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("derived_from", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("location_id"),
        sa.UniqueConstraint("entity_type", "entity_code",
                            name="uq_map_entity_location"),
    )
    op.create_index("ix_map_entity_locations_entity_type", "map_entity_locations",
                    ["entity_type"])


def downgrade() -> None:
    op.drop_index("ix_map_entity_locations_entity_type",
                  table_name="map_entity_locations")
    op.drop_table("map_entity_locations")
