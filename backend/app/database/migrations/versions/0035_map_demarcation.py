"""0035_map_demarcation — a second kind of map design, and the one it seeds.

Revision ID: 0035_map_demarcation
Revises: 0034_business_map
Create Date: 2026-09-05

The Business Map page gains a second tab, **Area Demarcation**: the same
coordinates, drawn with no figures at all. Its purpose is judging where an area
begins and ends by eye, so what matters is where the points are and being able
to tell one level from another at a glance — a different shape and colour per
level, and nothing sized or coloured by a metric.

**Why a column rather than a second pair of tables.** A demarcation map needs
exactly what an analysis map needs minus the metrics: which levels are drawn,
in what order, from which zoom, clustering above how many points. That is
``map_layers``, and a dedicated ``map_level_shapes`` table would have grown
those four columns and become ``map_layers`` under a second name — a parallel
configuration surface for "how a level is drawn", which is the duplication this
codebase keeps warning about. So designs are shared and told apart by
``purpose``.

The discriminator is not cosmetic. A design is *offered* to a reader: without
it, somebody on the analysis tab could pick "Area Demarcation" from the same
dropdown and get a map that is neither one thing nor the other.

**``is_default`` becomes unique within a purpose, not across the table.** Each
tab has to have a design to open on, so promoting a demarcation design must not
leave the analysis map with none. That rule lives in ``app.map.designs``; this
revision only has to make the second default possible, which it does by seeding
one.

**The shape lives in ``style_config``, not in a new column.**
``app.map.styles.OVERRIDABLE`` already exists to let a layer override a declared
default field by field, and it already refuses a key it does not know — so a
shape is validated at write time by the code that validates every other style
override, and an unknown one is an ``InvalidLayer`` naming the level. Adding a
column would have bought the same thing for four more edits.

**Purely additive.** One nullable-free column with a server default (so every
design written before this revision is an analysis design without a single row
being updated), one index, and one seeded design. No existing row is touched
and no existing object is altered, so the SQLite view-capture dance 0020 and
0022 needed does not apply. The seed carries **no explicit primary keys**, for
the reason ``0034`` records: an explicit id leaves a PostgreSQL identity
sequence at zero and makes the first design an administrator creates collide
with the seed.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0035_map_demarcation"
down_revision: Union[str, None] = "0034_business_map"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The identity/foreign-key pair used across this schema, as ``0034`` declares it.
PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
FK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

ANALYSIS = "analysis"
DEMARCATION = "demarcation"
DEMARCATION_DESIGN_NAME = "Area Demarcation"

#: The seeded layers: ``(level, name, order, visible, min_zoom, cluster_at,
#: shape, colour)``.
#:
#: Restated here rather than imported from application code, for the reason
#: 0027 and 0034 both give: a migration must keep producing the seed it produced
#: on the day it was written, and importing a tuple would let a later edit
#: silently change what an old database upgrades into. The shape keys are
#: checked against ``app.map.styles.SHAPE_KEYS`` by ``test_map_demarcation``,
#: which is how the two stay honest without being coupled.
#:
#: Shapes run from elaborate at the top of the hierarchy to plain at the bottom,
#: because the lower levels are the numerous ones and a circle is what reads
#: cleanest a thousand times over. Colours are eight distinguishable hues rather
#: than a ramp: these are categories, not a scale, and a sequential palette
#: would imply an order between Zone and Customer that does not exist.
#:
#: The zoom gates are ``0034``'s, with one deliberate difference: **Customer is
#: visible by default here**. On the analysis map it is off because 846 points
#: at zoom 7 is a single blue mass; here clustering is on from the start, so the
#: same 846 points arrive as a readable handful of bubbles — and a demarcation
#: map without its customers is missing the level the demarcation is *about*.
_DEMARCATION_LAYERS: tuple[
    tuple[str, str, int, bool, int, int | None, str, str], ...
] = (
    ("zone", "Zone", 1, True, 0, None, "star", "#7c3aed"),
    ("region", "Region", 2, True, 0, None, "hexagon", "#2563eb"),
    ("area", "Area", 3, True, 0, None, "diamond", "#0891b2"),
    ("unit", "Unit", 4, False, 6, None, "cross", "#65a30d"),
    ("territory", "Territory", 5, True, 6, 400, "triangle", "#ea580c"),
    ("sub_territory", "Sub Territory", 6, True, 7, 400, "square", "#db2777"),
    ("customer", "Customer", 7, True, 7, 150, "circle", "#475569"),
    ("sales_force", "Sales Force", 8, False, 7, 200, "pin", "#16a34a"),
)


def upgrade() -> None:
    # A server default rather than a back-filling UPDATE: every design that
    # exists is an analysis design, and letting the database say so costs one
    # clause instead of a statement whose row count nobody checks.
    op.add_column(
        "map_designs",
        sa.Column("purpose", sa.String(length=16), nullable=False,
                  server_default=ANALYSIS),
    )
    op.create_index("ix_map_designs_purpose", "map_designs", ["purpose"])
    _seed_demarcation_design()


def _seed_demarcation_design() -> None:
    """The design the demarcation tab opens with, keyed by nothing chosen here."""
    bind = op.get_bind()

    designs = sa.table(
        "map_designs",
        sa.column("design_id", PK),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
        sa.column("purpose", sa.String),
        sa.column("basemap", sa.String),
        sa.column("default_metric", sa.String),
        sa.column("is_default", sa.Boolean),
        sa.column("is_active", sa.Boolean),
        sa.column("is_system_default", sa.Boolean),
        sa.column("created_by", sa.String),
    )
    bind.execute(designs.insert().values(
        name=DEMARCATION_DESIGN_NAME,
        description=("Every placed coordinate, one shape and colour per level, "
                     "with no figures on it — for judging where an area begins "
                     "and ends."),
        purpose=DEMARCATION,
        basemap="standard",
        # Stored because the column is NOT NULL and a layer inherits it when it
        # names none. Nothing on this map reads a metric: it draws coordinates.
        # Left at the platform default rather than at something invented, so a
        # design duplicated across to the analysis tab behaves predictably.
        default_metric="net_sales",
        # The default *for its own purpose*: the analysis map keeps its own.
        is_default=sa.true(),
        is_active=sa.true(),
        is_system_default=sa.true(),
        created_by="system",
    ))
    design_id = bind.execute(
        sa.select(designs.c.design_id)
        .where(designs.c.name == DEMARCATION_DESIGN_NAME)
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
    for level, name, order, visible, min_zoom, cluster_at, _s, _c in _DEMARCATION_LAYERS:
        bind.execute(layers.insert().values(
            design_id=design_id,
            layer_name=name,
            point_level=level,
            view_mode="point",
            # All three NULL: they inherit, and nothing on this map reads them.
            # Storing a metric here would be a setting somebody would trust.
            metric=None,
            color_metric=None,
            size_metric=None,
            is_visible=sa.true() if visible else sa.false(),
            display_order=order,
            min_zoom=min_zoom,
            cluster_at=cluster_at,
        ))

    # Read the ids back in the order they were written, so each configuration
    # lands on its own layer. Ordering by display_order rather than by id keeps
    # this correct whatever the identity sequence does.
    rows = bind.execute(
        sa.select(layers.c.layer_id, layers.c.point_level)
        .where(layers.c.design_id == design_id)
        .order_by(layers.c.display_order)
    ).all()
    shape_by_level = {level: (shape, colour)
                      for level, _n, _o, _v, _z, _c, shape, colour
                      in _DEMARCATION_LAYERS}

    configs = sa.table(
        "map_point_configurations",
        sa.column("layer_id", FK),
        sa.column("label_field", sa.String),
        sa.column("show_label", sa.Boolean),
        sa.column("label_min_zoom", sa.Integer),
        sa.column("style_config", sa.JSON),
    )
    for layer_id, point_level in rows:
        shape, colour = shape_by_level[point_level]
        bind.execute(configs.insert().values(
            layer_id=layer_id,
            label_field="name",
            # Labels off by default and late when switched on: a demarcation
            # map is read by the position of its points, and a thousand names
            # over them is what stops that being possible.
            show_label=sa.false(),
            label_min_zoom=10,
            style_config={"shape": shape, "point_color": colour},
        ))


def downgrade() -> None:
    """Remove the demarcation design and the column that distinguished it.

    The designs go: with ``purpose`` dropped there would be no way to tell one
    from an analysis design, and leaving it behind would put a metric-less map
    in the analysis tab's dropdown. A demarcation design an administrator
    created after this revision goes too, for the same reason — a real loss,
    and the one the column's existence implies.

    **The children are deleted explicitly rather than left to the cascade.**
    ``map_layers.design_id`` and ``map_point_configurations.layer_id`` are both
    declared ``ON DELETE CASCADE``, and on PostgreSQL that would be enough — but
    SQLite enforces a foreign key only when ``PRAGMA foreign_keys=ON``, which
    the migration runner does not set. Relying on the cascade there deletes the
    design and leaves its layers behind as orphans, and because SQLite reuses
    the freed rowid the *next* upgrade inserts a design that collides with them
    on ``(design_id, point_level)``. A downgrade that only works once is worse
    than one that refuses, so the deletes are written out, children first.
    """
    bind = op.get_bind()
    designs = sa.table(
        "map_designs",
        sa.column("design_id", PK),
        sa.column("purpose", sa.String),
    )
    layers = sa.table(
        "map_layers",
        sa.column("layer_id", PK),
        sa.column("design_id", FK),
    )
    configs = sa.table(
        "map_point_configurations",
        sa.column("layer_id", FK),
    )

    doomed_designs = sa.select(designs.c.design_id).where(
        designs.c.purpose == DEMARCATION
    )
    doomed_layers = sa.select(layers.c.layer_id).where(
        layers.c.design_id.in_(doomed_designs)
    )
    bind.execute(configs.delete().where(configs.c.layer_id.in_(doomed_layers)))
    bind.execute(layers.delete().where(layers.c.design_id.in_(doomed_designs)))
    bind.execute(designs.delete().where(designs.c.purpose == DEMARCATION))

    op.drop_index("ix_map_designs_purpose", table_name="map_designs")
    op.drop_column("map_designs", "purpose")
