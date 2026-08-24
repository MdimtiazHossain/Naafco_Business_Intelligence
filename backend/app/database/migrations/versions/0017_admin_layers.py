"""Country level and administrative reference points for the Business Map.

Revision ID: 0017_admin_layers
Revises: 0016_material_stock
Create Date: 2026-08-19

Revision ``0010_admin_areas`` built the administrative layer around a single
drawn level — the upazila — with divisions and districts existing as dimension
rows that nothing had a polygon for. Loading a published boundary set makes all
four levels drawable at once, and the map is meant to show them together:
Bangladesh, division, district and upazila as independent overlays rather than
one level chosen from a list.

Three things follow, and this revision adds exactly those:

**A country to hang the outline on.** ``map_area_boundaries`` keys a polygon on
``(entity_type, entity_code)``, so the national outline needs a code, and the
level registry needs a row to read a name from. ``dim_country`` is that row.
``dim_division.country_code`` is **nullable**: divisions may already exist from
an earlier import that had no country file behind it, and this migration will
not invent a parent for them.

**Somewhere for the published points.** The capital and administrative-point
layers are reference geography — they arrive with the boundary files and are
replaced wholesale by a re-import. ``map_entity_locations`` is the wrong home
for them: it records where somebody *placed* a business entity, with a source
and a precision describing that choice. ``map_admin_points`` keeps the two
apart so provenance stays meaningful in both.

**A style row for the new level.** Every drawn level reads its appearance from
``map_area_styles``; a level without one cannot be painted. The country row is
seeded with the same blue ``0010`` established as the documented default, at a
heavier stroke because a national outline should read as the strongest boundary
on the map. The three existing rows are **not touched** — the upazila stroke
stays the blue that revision set, which is a documented requirement.

Nothing here drops or rewrites anything. The revision is additive, and its
downgrade removes only what it created.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_admin_layers"
down_revision: Union[str, None] = "0016_material_stock"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The country style, seeded once.
#:
#: Deliberately the same hue as the levels below it rather than a new colour:
#: the levels are distinguished by *stroke weight*, which is what makes a nested
#: hierarchy readable, and adding a fourth colour would say the country is a
#: different kind of thing rather than a broader one. The fill is transparent
#: because a filled country would tint the entire map.
COUNTRY_STYLE = {
    "entity_type": "country",
    "fill_color": "#2563EB",
    "fill_opacity": 0.0,
    "stroke_color": "#2563EB",
    "stroke_opacity": 1.0,
    "stroke_width": 3.0,
    "z_index": 4,
    "is_system_default": True,
}

#: Weights for the levels ``0010`` seeded, now that they can all be drawn at
#: once. It gave the three an identical 1.5px stroke and 0.20 fill, which was
#: right while exactly one was ever on screen and is unreadable when they
#: overlap — three identical blue outlines on top of each other.
#:
#: The colour does not change. ``#2563EB`` stays on every level, including the
#: upazila stroke, which is a documented requirement. What changes is weight and
#: fill: broader boundaries draw heavier and on top, and only the deepest level
#: carries a fill, so stacked layers tint the map once rather than four times.
#:
#: ``is_system_default`` gates every update below — a style an operator has
#: customised through the Map Settings screen is left exactly as they set it.
LEVEL_WEIGHTS = (
    # entity_type, stroke_width, fill_opacity, z_index
    ("division", 2.0, 0.0, 3),
    ("district", 1.25, 0.0, 2),
    ("upazila", 0.75, 0.18, 1),
)


def upgrade() -> None:
    op.create_table(
        "dim_country",
        sa.Column("country_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("country_code", sa.String(64), nullable=False),
        sa.Column("country_name", sa.Text(), nullable=False),
        sa.Column("country_name_bn", sa.Text(), nullable=True),
        sa.Column("iso2", sa.String(2), nullable=True),
        sa.Column("iso3", sa.String(3), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.false(),
                  nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.String(64), nullable=True),
        sa.UniqueConstraint("country_code", name="uq_dim_country_country_code"),
    )
    op.create_index("ix_dim_country_country_code", "dim_country", ["country_code"])

    # Nullable, and added with a named constraint so SQLite's batch rewrite can
    # find it again on downgrade.
    with op.batch_alter_table("dim_division") as batch:
        batch.add_column(sa.Column("country_code", sa.String(64), nullable=True))
        batch.create_foreign_key(
            "fk_dim_division_country_code", "dim_country",
            ["country_code"], ["country_code"],
            ondelete="RESTRICT", onupdate="CASCADE",
        )
    op.create_index("ix_dim_division_country_code", "dim_division",
                    ["country_code"])

    op.create_table(
        "map_admin_points",
        sa.Column("point_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("admin_level", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_bn", sa.Text(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("country_code", sa.String(64), nullable=True),
        sa.Column("division_code", sa.String(64), nullable=True),
        sa.Column("district_code", sa.String(64), nullable=True),
        sa.Column("upazila_code", sa.String(64), nullable=True),
        sa.Column("source_file", sa.Text(), nullable=True),
        sa.Column("imported_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("kind", "admin_level", "latitude", "longitude",
                            name="uq_map_admin_point"),
    )
    op.create_index("ix_map_admin_points_kind_level", "map_admin_points",
                    ["kind", "admin_level"])
    op.create_index("ix_map_admin_points_bbox", "map_admin_points",
                    ["kind", "latitude", "longitude"])

    # Seed the country style, but never overwrite one an operator already set.
    styles = sa.table(
        "map_area_styles",
        sa.column("entity_type", sa.String),
        sa.column("fill_color", sa.String),
        sa.column("fill_opacity", sa.Float),
        sa.column("stroke_color", sa.String),
        sa.column("stroke_opacity", sa.Float),
        sa.column("stroke_width", sa.Float),
        sa.column("z_index", sa.Integer),
        sa.column("is_system_default", sa.Boolean),
    )
    bind = op.get_bind()
    exists = bind.execute(
        sa.select(sa.func.count())
        .select_from(styles)
        .where(styles.c.entity_type == "country")
    ).scalar_one()
    if not exists:
        op.bulk_insert(styles, [COUNTRY_STYLE])

    # Re-weight the levels 0010 seeded, so four overlapping layers stay legible.
    # Only where the row is still the system default — see LEVEL_WEIGHTS.
    for entity_type, width, fill_opacity, z_index in LEVEL_WEIGHTS:
        bind.execute(
            sa.text(
                "UPDATE map_area_styles "
                "SET stroke_width = :width, fill_opacity = :fill, "
                "    z_index = :z "
                "WHERE entity_type = :entity_type AND is_system_default = TRUE"
            ),
            {"width": width, "fill": fill_opacity, "z": z_index,
             "entity_type": entity_type},
        )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM map_area_styles WHERE entity_type = 'country'"))
    # Restore the uniform weights 0010 seeded, on the same is_system_default
    # condition the upgrade used.
    op.execute(sa.text(
        "UPDATE map_area_styles "
        "SET stroke_width = 1.5, fill_opacity = 0.2, z_index = 1 "
        "WHERE entity_type IN ('division', 'district', 'upazila') "
        "  AND is_system_default = TRUE"
    ))

    op.drop_index("ix_map_admin_points_bbox", table_name="map_admin_points")
    op.drop_index("ix_map_admin_points_kind_level", table_name="map_admin_points")
    op.drop_table("map_admin_points")

    op.drop_index("ix_dim_division_country_code", table_name="dim_division")
    with op.batch_alter_table("dim_division") as batch:
        batch.drop_constraint("fk_dim_division_country_code", type_="foreignkey")
        batch.drop_column("country_code")

    op.drop_index("ix_dim_country_country_code", table_name="dim_country")
    op.drop_table("dim_country")
