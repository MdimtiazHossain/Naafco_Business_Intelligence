"""0034_business_map — the rebuilt business map: coordinates and composition.

Revision ID: 0034_business_map
Revises: 0033_remove_map
Create Date: 2026-09-03

``0033`` dropped eleven map tables so the map could be rebuilt from nothing.
This is the rebuild's schema, and it is four tables rather than eleven: the
marker library, the boundary store and the administrative points do not come
back. What does is the pair a configuration-driven map cannot exist without —
somewhere to hold *where* an entity is, and somewhere to hold *which* levels a
map draws and how.

``map_entity_locations`` is ``0008``'s table, column for column. Its rows were
destroyed by ``0033``, on instruction, but the removal exported them first to
``reports/map_pre0033_20260903_092802/map_entity_locations.csv`` — 846 uploaded
customer coordinates, 256 uploaded sales-force coordinates and 551 derived
centroids — and ``scripts/reload_map_locations.py`` puts the *uploaded* rows
back through the same validation an upload gets and re-derives the rest. This
revision creates the table empty and loads nothing: a migration that reached
into ``reports/`` for data would be a migration whose result depends on what a
developer's checkout happens to contain.

``map_designs`` / ``map_layers`` / ``map_point_configurations`` are ``0032``'s
three tables with one column added and one removed. ``view_mode`` is added
because the specification asks for Boundary / Point / Both per layer, and the
renderer must be able to read a layer's answer rather than assume one.
``marker_design_id`` is removed with the library it referenced; a layer's
appearance lives in its point configuration's ``style_config``.

**Seeded with one design, and the seed carries no identifiers.** Configuration
with no rows is a map that draws nothing, so ``Business Overview`` is seeded as
the protected system default with every level the master data has. ``0032``
inserted its rows with explicit primary keys, which leaves a PostgreSQL identity
sequence at zero and makes the first design an administrator creates collide
with the seed; here the design is inserted without a key and its id is read
back, and the layers are read back the same way to attach their point
configurations. Every boolean is ``sa.true()`` / ``sa.false()`` rather than an
integer literal, for the reason revisions 0016 to 0021 had to be repaired.

**Unit is a layer.** The specification lists Zone, Region, Area, Territory,
Sub-Territory and Customer, and this platform's chain has Unit between Area and
Territory. A seventh layer that ships hidden costs nothing; a missing link in
the parent walk costs correctness.

Purely additive: four new tables, no existing object touched, so the SQLite
view-capture dance 0020 and 0022 needed does not apply here.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0034_business_map"
down_revision: Union[str, None] = "0033_remove_map"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The identity/foreign-key pair used across this schema: a PostgreSQL bigint
#: that SQLite stores as its own rowid-backed integer.
PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
FK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

DEFAULT_DESIGN_NAME = "Business Overview"

#: The seeded layers: ``(level, order, visible, min_zoom, cluster_at)``.
#:
#: Restated here rather than imported from application code, for the reason
#: 0027 gives: a migration must keep producing the seed it produced on the day
#: it was written, and importing a tuple would let a later edit silently change
#: what an old database upgrades into.
#:
#: The zoom gates are the coverage figures turned into a rule. Zone, region and
#: area are a handful of derived centroids each and are legible from the whole
#: country; territory and below are hundreds of points and only mean anything
#: once the reader has zoomed into somewhere. Customer is off by default and
#: clusters early — 846 placed customers is precisely the "single blue mass" the
#: previous design removed multi-layer rendering to avoid.
_DEFAULT_LAYERS: tuple[tuple[str, str, int, bool, int, int | None], ...] = (
    ("zone", "Zone", 1, True, 0, None),
    ("region", "Region", 2, True, 0, None),
    ("area", "Area", 3, True, 6, None),
    ("unit", "Unit", 4, False, 7, None),
    ("territory", "Territory", 5, True, 7, 400),
    ("sub_territory", "Sub Territory", 6, True, 8, 400),
    ("customer", "Customer", 7, False, 9, 200),
)


def _timestamps() -> tuple[sa.Column, sa.Column]:
    """``created_at``/``updated_at`` as ``TimestampMixin`` declares them."""
    return (
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )


def upgrade() -> None:
    # ---------------------------------------------------------- where things are
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
        *_timestamps(),
        sa.PrimaryKeyConstraint("location_id"),
        sa.UniqueConstraint("entity_type", "entity_code",
                            name="uq_map_entity_location"),
    )
    op.create_index("ix_map_entity_locations_entity_type", "map_entity_locations",
                    ["entity_type"])

    # ---------------------------------------------------------------- the design
    op.create_table(
        "map_designs",
        sa.Column("design_id", PK, autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=True),
        sa.Column("basemap", sa.String(length=32), nullable=False,
                  server_default="standard"),
        sa.Column("default_metric", sa.String(length=32), nullable=False,
                  server_default="net_sales"),
        sa.Column("is_default", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("is_system_default", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("design_id"),
        sa.UniqueConstraint("name", name="uq_map_designs_name"),
    )
    op.create_index("ix_map_designs_active", "map_designs", ["is_active"])
    op.create_index("ix_map_designs_default", "map_designs", ["is_default"])

    # ----------------------------------------------------------------- the layer
    op.create_table(
        "map_layers",
        sa.Column("layer_id", PK, autoincrement=True, nullable=False),
        sa.Column("design_id", FK, nullable=False),
        sa.Column("layer_name", sa.String(length=128), nullable=False),
        sa.Column("point_level", sa.String(length=32), nullable=False),
        sa.Column("view_mode", sa.String(length=16), nullable=False,
                  server_default="point"),
        sa.Column("metric", sa.String(length=32), nullable=True),
        sa.Column("color_metric", sa.String(length=32), nullable=True),
        sa.Column("size_metric", sa.String(length=32), nullable=True),
        sa.Column("is_visible", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("display_order", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("min_zoom", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cluster_at", sa.Integer(), nullable=True),
        sa.Column("configuration_json", sa.JSON(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("layer_id"),
        sa.ForeignKeyConstraint(["design_id"], ["map_designs.design_id"],
                                ondelete="CASCADE"),
        sa.UniqueConstraint("design_id", "point_level",
                            name="uq_map_layers_design_level"),
    )
    op.create_index("ix_map_layers_design", "map_layers", ["design_id"])
    op.create_index("ix_map_layers_order", "map_layers",
                    ["design_id", "display_order"])

    # ------------------------------------------------------- the point behaviour
    op.create_table(
        "map_point_configurations",
        sa.Column("config_id", PK, autoincrement=True, nullable=False),
        sa.Column("layer_id", FK, nullable=False),
        sa.Column("label_field", sa.String(length=64), nullable=True),
        sa.Column("show_label", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("label_min_zoom", sa.Integer(), nullable=False,
                  server_default="8"),
        sa.Column("tooltip_fields", sa.JSON(), nullable=True),
        sa.Column("style_config", sa.JSON(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("config_id"),
        sa.ForeignKeyConstraint(["layer_id"], ["map_layers.layer_id"],
                                ondelete="CASCADE"),
        sa.UniqueConstraint("layer_id", name="uq_map_point_config_layer"),
    )

    _seed_default_design()


def _seed_default_design() -> None:
    """One design the page can open with, keyed by nothing this file chooses."""
    bind = op.get_bind()

    designs = sa.table(
        "map_designs",
        sa.column("design_id", PK),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
        sa.column("basemap", sa.String),
        sa.column("default_metric", sa.String),
        sa.column("is_default", sa.Boolean),
        sa.column("is_active", sa.Boolean),
        sa.column("is_system_default", sa.Boolean),
        sa.column("created_by", sa.String),
    )
    bind.execute(designs.insert().values(
        name=DEFAULT_DESIGN_NAME,
        description=("Every organisational level the master data places, "
                     "sized by net sales and coloured by achievement. The "
                     "design the map opens with."),
        basemap="standard",
        default_metric="net_sales",
        is_default=True,
        is_active=True,
        is_system_default=True,
        created_by="system",
    ))
    design_id = bind.execute(
        sa.select(designs.c.design_id).where(designs.c.name == DEFAULT_DESIGN_NAME)
    ).scalar_one()

    layers = sa.table(
        "map_layers",
        sa.column("layer_id", PK),
        sa.column("design_id", FK),
        sa.column("layer_name", sa.String),
        sa.column("point_level", sa.String),
        sa.column("view_mode", sa.String),
        sa.column("metric", sa.String),
        sa.column("color_metric", sa.String),
        sa.column("size_metric", sa.String),
        sa.column("is_visible", sa.Boolean),
        sa.column("display_order", sa.Integer),
        sa.column("min_zoom", sa.Integer),
        sa.column("cluster_at", sa.Integer),
    )
    for level, name, order, visible, min_zoom, cluster_at in _DEFAULT_LAYERS:
        bind.execute(layers.insert().values(
            design_id=design_id,
            layer_name=name,
            point_level=level,
            view_mode="point",
            # NULL: inherit the design's default_metric, so re-pointing the
            # whole map is one edit.
            metric=None,
            color_metric="achievement",
            size_metric="net_sales",
            is_visible=visible,
            display_order=order,
            min_zoom=min_zoom,
            cluster_at=cluster_at,
        ))

    layer_ids = bind.execute(
        sa.select(layers.c.layer_id).where(layers.c.design_id == design_id)
    ).scalars().all()

    configs = sa.table(
        "map_point_configurations",
        sa.column("layer_id", FK),
        sa.column("label_field", sa.String),
        sa.column("show_label", sa.Boolean),
        sa.column("label_min_zoom", sa.Integer),
    )
    for layer_id in layer_ids:
        bind.execute(configs.insert().values(
            layer_id=layer_id,
            label_field="name",
            show_label=False,
            label_min_zoom=8,
        ))


def downgrade() -> None:
    """Drop the four tables, children first.

    A downgrade discards whatever coordinates and designs were loaded after the
    upgrade. That is the same loss ``0033`` records, and the same remedy applies:
    export first, or restore the backup taken beside the migration.
    """
    op.drop_table("map_point_configurations")
    op.drop_index("ix_map_layers_order", table_name="map_layers")
    op.drop_index("ix_map_layers_design", table_name="map_layers")
    op.drop_table("map_layers")
    op.drop_index("ix_map_designs_default", table_name="map_designs")
    op.drop_index("ix_map_designs_active", table_name="map_designs")
    op.drop_table("map_designs")
    op.drop_index("ix_map_entity_locations_entity_type",
                  table_name="map_entity_locations")
    op.drop_table("map_entity_locations")
