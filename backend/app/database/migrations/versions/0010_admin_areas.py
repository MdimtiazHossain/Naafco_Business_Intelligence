"""Administrative areas: division, district, upazila, boundaries and area styles.

Revision ID: 0010_admin_areas
Revises: 0009_data_management
Create Date: 2026-08-12

Purely additive. Nothing existing is altered — the organisational hierarchy, the
facts and the reporting views are untouched, because administrative geography is
a second, independent hierarchy rather than an extension of the first.

The area style rows are seeded here rather than in application code so a fresh
database draws the layer correctly before anyone opens the settings screen, and
so the documented default (blue, 0.20 fill, 1.5 border) has exactly one home.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_admin_areas"
down_revision: Union[str, None] = "0009_data_management"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
CODE = sa.String(length=64)
JSON_TYPE = sa.JSON()

#: The documented default: a blue area at 20% fill with a 1.5px blue border.
#: Hex rather than "blue" so it can be interpolated and rendered identically by
#: Google Maps, SVG and the legend swatch.
DEFAULT_FILL = "#2563EB"
DEFAULT_FILL_OPACITY = 0.20
DEFAULT_STROKE = "#2563EB"
DEFAULT_STROKE_WIDTH = 1.5

#: One row per administrative level. Only upazila is drawn today; the other two
#: are seeded so switching the layer to district or division is a query change
#: rather than a migration.
SEEDED_LEVELS: tuple[tuple[str, int], ...] = (
    ("upazila", 3),
    ("district", 2),
    ("division", 1),
)


def _dimension(name: str, code_column: str, name_column: str,
               parent: tuple[str, str] | None, extra: list[sa.Column]) -> None:
    """One administrative dimension, in the shape every other dimension has."""
    columns = [
        sa.Column(f"{name}_id", PK, autoincrement=True, nullable=False),
        sa.Column(code_column, CODE, nullable=False),
        sa.Column(name_column, sa.Text(), nullable=False),
        sa.Column(f"{name_column}_bn", sa.Text(), nullable=True),
    ]
    if parent is not None:
        columns.append(sa.Column(parent[1], CODE, nullable=False))
    columns.extend(extra)
    columns.extend([
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.String(length=64), nullable=True),
    ])

    constraints: list = [
        sa.PrimaryKeyConstraint(f"{name}_id"),
        sa.UniqueConstraint(code_column, name=f"uq_dim_{name}_code"),
    ]
    if parent is not None:
        constraints.append(sa.ForeignKeyConstraint(
            [parent[1]], [f"dim_{parent[0]}.{parent[1]}"],
            ondelete="RESTRICT", onupdate="CASCADE",
        ))

    op.create_table(f"dim_{name}", *columns, *constraints)
    op.create_index(f"ix_dim_{name}_{code_column}", f"dim_{name}", [code_column])
    if parent is not None:
        op.create_index(f"ix_dim_{name}_{parent[1]}", f"dim_{name}", [parent[1]])


def upgrade() -> None:
    _dimension("division", "division_code", "division_name", None, [])
    _dimension("district", "district_code", "district_name",
               ("division", "division_code"), [])
    _dimension("upazila", "upazila_code", "upazila_name",
               ("district", "district_code"),
               [sa.Column("latitude", sa.Float(), nullable=True),
                sa.Column("longitude", sa.Float(), nullable=True)])

    op.create_table(
        "map_area_boundaries",
        sa.Column("boundary_id", PK, autoincrement=True, nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_code", CODE, nullable=False),
        sa.Column("geometry", JSON_TYPE, nullable=False),
        sa.Column("bbox_north", sa.Float(), nullable=False),
        sa.Column("bbox_south", sa.Float(), nullable=False),
        sa.Column("bbox_east", sa.Float(), nullable=False),
        sa.Column("bbox_west", sa.Float(), nullable=False),
        sa.Column("centroid_latitude", sa.Float(), nullable=False),
        sa.Column("centroid_longitude", sa.Float(), nullable=False),
        sa.Column("point_count", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("source_point_count", sa.Integer(), nullable=True),
        sa.Column("simplify_tolerance", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False,
                  server_default="IMPORT"),
        sa.Column("source_file", sa.Text(), nullable=True),
        sa.Column("imported_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("boundary_id"),
        sa.UniqueConstraint("entity_type", "entity_code",
                            name="uq_map_area_boundary"),
    )
    op.create_index("ix_map_area_boundaries_entity_type", "map_area_boundaries",
                    ["entity_type"])
    op.create_index("ix_map_area_boundaries_bbox", "map_area_boundaries",
                    ["entity_type", "bbox_south", "bbox_north"])

    op.create_table(
        "map_area_styles",
        sa.Column("style_id", PK, autoincrement=True, nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("fill_color", sa.String(length=16), nullable=False),
        sa.Column("fill_opacity", sa.Float(), nullable=False),
        sa.Column("stroke_color", sa.String(length=16), nullable=False),
        sa.Column("stroke_opacity", sa.Float(), nullable=False,
                  server_default="1.0"),
        sa.Column("stroke_width", sa.Float(), nullable=False),
        sa.Column("hover_fill_color", sa.String(length=16), nullable=True),
        sa.Column("hover_fill_opacity", sa.Float(), nullable=True),
        sa.Column("selected_fill_color", sa.String(length=16), nullable=True),
        sa.Column("selected_fill_opacity", sa.Float(), nullable=True),
        sa.Column("z_index", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("rules", JSON_TYPE, nullable=True),
        sa.Column("is_system_default", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("style_id"),
        sa.UniqueConstraint("entity_type", name="uq_map_area_style_entity"),
    )
    op.create_index("ix_map_area_styles_entity_type", "map_area_styles",
                    ["entity_type"])

    styles = sa.table(
        "map_area_styles",
        sa.column("entity_type", sa.String),
        sa.column("fill_color", sa.String),
        sa.column("fill_opacity", sa.Float),
        sa.column("stroke_color", sa.String),
        sa.column("stroke_opacity", sa.Float),
        sa.column("stroke_width", sa.Float),
        sa.column("hover_fill_opacity", sa.Float),
        sa.column("selected_fill_opacity", sa.Float),
        sa.column("z_index", sa.Integer),
        sa.column("is_system_default", sa.Boolean),
    )
    op.bulk_insert(styles, [
        {
            "entity_type": level,
            "fill_color": DEFAULT_FILL,
            "fill_opacity": DEFAULT_FILL_OPACITY,
            "stroke_color": DEFAULT_STROKE,
            "stroke_opacity": 1.0,
            "stroke_width": DEFAULT_STROKE_WIDTH,
            # Hover and selection lift the same blue rather than introducing a
            # second colour, so the layer stays one colour as specified.
            "hover_fill_opacity": 0.35,
            "selected_fill_opacity": 0.45,
            "z_index": z_index,
            "is_system_default": True,
        }
        for level, z_index in SEEDED_LEVELS
    ])


def downgrade() -> None:
    op.drop_index("ix_map_area_styles_entity_type", table_name="map_area_styles")
    op.drop_table("map_area_styles")

    op.drop_index("ix_map_area_boundaries_bbox", table_name="map_area_boundaries")
    op.drop_index("ix_map_area_boundaries_entity_type",
                  table_name="map_area_boundaries")
    op.drop_table("map_area_boundaries")

    for name, code_column, parent_column in (
        ("upazila", "upazila_code", "district_code"),
        ("district", "district_code", "division_code"),
        ("division", "division_code", None),
    ):
        if parent_column:
            op.drop_index(f"ix_dim_{name}_{parent_column}", table_name=f"dim_{name}")
        op.drop_index(f"ix_dim_{name}_{code_column}", table_name=f"dim_{name}")
        op.drop_table(f"dim_{name}")
