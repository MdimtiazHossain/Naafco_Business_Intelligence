"""Map composition: saved designs, their layers, and each layer's point config.

Revision ID: 0032_map_composition
Revises: 0031_credit_control
Create Date: 2026-09-02

**This is composition, not symbology, and the distinction is the whole point.**
``map_marker_designs`` (0007) already answers "what does one entity type's marker
look like" — shape, icon, colour, label. It is per *entity type* and says nothing
about which levels a map draws or what they measure. These three tables answer
the other question: *which* layers make up a map, in what order, aggregated at
which level, measured by which metric. A layer therefore **references** a marker
design rather than restating its colours, so a change to the Territory marker
still reaches every design that draws territories, and neither table grows a
second opinion about how a point looks.

**One map, many layers — which is a reversal, deliberately.** The Business Map
draws exactly one sales level today, and the code says why: seven layers of
toggles were replaced because 2,561 customer markers stacked into a single blue
mass. That reasoning was about *drawing*, not about *configuration*, and it is
answered here by the two columns that removal lacked — ``min_zoom`` and
``cluster_at``. A layer that only appears past a zoom, and clusters above a
point count, can coexist with five others; a layer that always drew everything
could not. Without those columns this revision would simply reintroduce the
problem 0020-era code removed.

**Seeded with one design that reproduces today's map exactly.** Configuration
with no rows is not a blank slate — it is a map that draws nothing, and the
first person to open the page would think the feature broke. The seeded
``Business Overview`` is marked ``is_system_default`` so it cannot be deleted
(the rule the marker library already applies), and every other design a user
creates is ordinary, editable, deletable data.

**Unit is a layer.** The spec this implements lists Zone, Region, Area,
Territory, Sub-Territory and Customer, and this platform's chain is Zone →
Region → Area → **Unit** → Territory → Sub-Territory: ``etl.mapping``'s bindings
are built on it and Area's children are Units. A seventh layer that can be
switched off costs nothing; a missing link in the parent walk costs correctness.
It is seeded ``is_visible = FALSE`` so the default map matches the spec's picture
while the level remains available.

**No metric is invented.** The seeded metric list is what
``app.map.data.METRICS`` actually publishes plus the two derivable from what is
already stored. Active Customer Count and New Customer Count are **not** seeded:
no definition of "active" or "new" exists in this warehouse, and a metric key
the UI offers but nothing can compute is worse than an absent one — the same
reason ``collection`` is deliberately absent from the agent's metric keywords.
They become rows the day somebody writes the rule down, with no schema change.

**Portability.** Every boolean is ``sa.Boolean`` with a ``sa.true()``/
``sa.false()`` server default rather than an integer literal, because PostgreSQL
rejects ``boolean = integer`` outright while SQLite silently accepts it — the
defect revisions 0016 to 0021 had to be repaired for. Identity and foreign-key
columns use the ``BigInteger``/``Integer`` variant pair the rest of the schema
uses, and every unique-constrained text column is a bounded ``String``.

Purely additive: three new tables, no existing object touched, so the SQLite
view-capture dance 0020 and 0022 needed does not apply here.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0032_map_composition"
down_revision: Union[str, None] = "0031_credit_control"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The identity/foreign-key pair used across this schema: a PostgreSQL bigint
#: that SQLite stores as its own rowid-backed integer.
PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
FK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
LEVEL = sa.String(32)


def _timestamps() -> tuple[sa.Column, sa.Column]:
    """``created_at``/``updated_at`` as ``TimestampMixin`` declares them."""
    return (
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )


#: The seeded layers: ``(level, order, visible, min_zoom, cluster_at)``.
#:
#: Restated here rather than imported from application code, for the reason 0027
#: gives: a migration must keep producing the seed it produced on the day it was
#: written, and importing a tuple would let a later edit silently change what an
#: old database upgrades into.
#:
#: The zoom gates are the coverage figures turned into a rule. Zone, region and
#: area are a handful of derived centroids each and are legible from the whole
#: country; territory and below are hundreds of points and only mean anything
#: once the reader has zoomed into somewhere. Customer is off by default and
#: clusters early — 846 placed customers is precisely the "single blue mass" the
#: previous design removed multi-layer rendering to avoid.
_DEFAULT_LAYERS: tuple[tuple[str, int, bool, int, int | None], ...] = (
    ("zone", 1, True, 0, None),
    ("region", 2, True, 0, None),
    ("area", 3, True, 6, None),
    ("unit", 4, False, 7, None),
    ("territory", 5, True, 7, 400),
    ("sub_territory", 6, True, 8, 400),
    ("customer", 7, False, 9, 200),
)


def upgrade() -> None:
    # ---------------------------------------------------------------- the design
    op.create_table(
        "map_designs",
        sa.Column("design_id", PK, autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=True),
        sa.Column("basemap", sa.String(length=32), nullable=False,
                  server_default="standard"),
        # The metric a layer inherits when it names none of its own, so a design
        # can be re-pointed at Achievement % in one edit rather than seven.
        sa.Column("default_metric", sa.String(length=32), nullable=False,
                  server_default="net_sales"),
        sa.Column("is_default", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        # A system design is the fallback the page opens with and cannot be
        # deleted, the same rule and the same reason as a system marker design.
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
        # Which business level this layer aggregates and draws. Validated in the
        # service against the entity registry rather than by a CHECK, so adding
        # a level stays a code change in one place instead of a migration.
        sa.Column("point_level", LEVEL, nullable=False),
        # NULL means "inherit the design's default_metric".
        sa.Column("metric", sa.String(length=32), nullable=True),
        sa.Column("color_metric", sa.String(length=32), nullable=True),
        sa.Column("size_metric", sa.String(length=32), nullable=True),
        # The symbology this layer borrows. NULL falls back to the marker
        # resolver's own chain, which already ends in a built-in circle, so a
        # layer can never fail to draw for want of a design.
        sa.Column("marker_design_id", FK, nullable=True),
        sa.Column("is_visible", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("display_order", sa.Integer(), nullable=False,
                  server_default="0"),
        # The two columns that make simultaneous layers workable rather than a
        # return to the blue mass. See this revision's docstring.
        sa.Column("min_zoom", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cluster_at", sa.Integer(), nullable=True),
        sa.Column("configuration_json", sa.JSON(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("layer_id"),
        sa.ForeignKeyConstraint(["design_id"], ["map_designs.design_id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["marker_design_id"],
                                ["map_marker_designs.design_id"],
                                ondelete="SET NULL"),
        # One layer per level per design. A second layer for the same level is
        # two answers to one question, and the renderer would draw both.
        sa.UniqueConstraint("design_id", "point_level",
                            name="uq_map_layers_design_level"),
    )
    op.create_index("ix_map_layers_design", "map_layers", ["design_id"])
    op.create_index("ix_map_layers_order", "map_layers",
                    ["design_id", "display_order"])

    # ------------------------------------------------------- the point behaviour
    #
    # Separate from the layer because it is edited by a different person for a
    # different reason: a layer is composition ("draw territories, sized by
    # sales"), and this is presentation ("label them by name, show these five
    # fields on hover"). One row per layer, created with it.
    op.create_table(
        "map_point_configurations",
        sa.Column("config_id", PK, autoincrement=True, nullable=False),
        sa.Column("layer_id", FK, nullable=False),
        sa.Column("label_field", sa.String(length=64), nullable=True),
        sa.Column("show_label", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        # Above this zoom the label appears; below it the point is a dot. The
        # answer to "too many labels" that does not require choosing between
        # legible and informative.
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

    # ------------------------------------------------------------------- the seed
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
    op.bulk_insert(designs, [{
        "design_id": 1,
        "name": "Business Overview",
        "description": ("Every organisational level the master data places, "
                        "sized by net sales and coloured by achievement. The "
                        "design the map opens with."),
        "basemap": "standard",
        "default_metric": "net_sales",
        "is_default": True,
        "is_active": True,
        "is_system_default": True,
        "created_by": "system",
    }])

    layers = sa.table(
        "map_layers",
        sa.column("layer_id", PK),
        sa.column("design_id", FK),
        sa.column("layer_name", sa.String),
        sa.column("point_level", sa.String),
        sa.column("metric", sa.String),
        sa.column("color_metric", sa.String),
        sa.column("size_metric", sa.String),
        sa.column("is_visible", sa.Boolean),
        sa.column("display_order", sa.Integer),
        sa.column("min_zoom", sa.Integer),
        sa.column("cluster_at", sa.Integer),
    )
    op.bulk_insert(layers, [
        {
            "layer_id": order,
            "design_id": 1,
            "layer_name": level.replace("_", " ").title(),
            "point_level": level,
            # NULL: inherit the design's default_metric, so re-pointing the
            # whole map is one edit.
            "metric": None,
            "color_metric": "achievement",
            "size_metric": "net_sales",
            "is_visible": visible,
            "display_order": order,
            "min_zoom": min_zoom,
            "cluster_at": cluster_at,
        }
        for level, order, visible, min_zoom, cluster_at in _DEFAULT_LAYERS
    ])

    configs = sa.table(
        "map_point_configurations",
        sa.column("layer_id", FK),
        sa.column("label_field", sa.String),
        sa.column("show_label", sa.Boolean),
        sa.column("label_min_zoom", sa.Integer),
    )
    op.bulk_insert(configs, [
        {
            "layer_id": order,
            "label_field": "name",
            "show_label": False,
            "label_min_zoom": 8,
        }
        for _level, order, _visible, _min_zoom, _cluster_at in _DEFAULT_LAYERS
    ])


def downgrade() -> None:
    # Reverse creation order: the children hold the foreign keys.
    op.drop_table("map_point_configurations")
    op.drop_index("ix_map_layers_order", table_name="map_layers")
    op.drop_index("ix_map_layers_design", table_name="map_layers")
    op.drop_table("map_layers")
    op.drop_index("ix_map_designs_default", table_name="map_designs")
    op.drop_index("ix_map_designs_active", table_name="map_designs")
    op.drop_table("map_designs")
