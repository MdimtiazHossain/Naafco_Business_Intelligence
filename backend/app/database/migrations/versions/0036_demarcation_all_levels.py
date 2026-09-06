"""0036_demarcation_all_levels — the demarcation map draws every level it has.

Revision ID: 0036_demarcation_all_levels
Revises: 0035_map_demarcation
Create Date: 2026-09-06

``0035`` seeded the Area Demarcation design the way ``0034`` seeded the
analysis one: Unit and Sales Force hidden, and no layer at all for Company,
Business Unit or Sales Line. That is right for a map of *figures*, where a
hidden level is one fewer thing competing for the eye. It is wrong for a map of
*coordinates*, where a hidden level is a hidden **row**.

The rule that tab answers to is that what it draws is exactly the rows of
``map_entity_locations`` — so the number of points on the map, unfiltered, has
to equal the number of rows Data Management lists. Against ``data/dev.db`` it
did not: 1,124 drawn against 1,139 stored, the difference being twelve derived
Unit centroids and the Company / Business Unit / Sales Line rows the design had
no layer for. A count that is off by fifteen is not a rounding difference, it is
a reader being shown less than exists with nothing saying so.

So every drawable level gets a visible layer here. A reader may still switch one
off — that is a choice they made and can see they made, which is the opposite of
a default that quietly omits.

**Only the demarcation design is touched.** The analysis design keeps Unit and
Customer hidden, and for the reason ``0034`` gives: 846 customer points at zoom
7 is a blue mass. The two designs answer different questions and this revision
does not blur them.

Additive in effect though not in form: no column, no table, no row removed. The
three new layers are inserted **without primary keys**, and the two updates are
scoped by ``purpose`` and by the layer's own level, so a design an administrator
has since edited by hand keeps whatever they chose except for the visibility
this revision exists to set.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0036_demarcation_all_levels"
down_revision: Union[str, None] = "0035_map_demarcation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
FK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

DEMARCATION = "demarcation"
DEMARCATION_DESIGN_NAME = "Area Demarcation"

#: The levels ``0035`` left hidden. Shown now, for the reason above.
_REVEAL: tuple[str, ...] = ("unit", "sales_force")

#: The levels ``0035`` gave no layer at all, above Zone.
#:
#: ``(level, name, order, min_zoom, cluster_at, shape, colour)``. They sit at
#: the top of the stacking order because there is one of each: a company point
#: is a single dot for the whole country, and drawing it under the 846 customer
#: points would bury it. Restated here rather than imported, as every seed in
#: this chain is — a migration must keep producing what it produced.
_ADDED: tuple[tuple[str, str, int, int, int | None, str, str], ...] = (
    ("company", "Company", 9, 0, None, "pin", "#0f172a"),
    ("bu", "Business Unit", 10, 0, None, "cross", "#334155"),
    ("sales_line", "Sales Line", 11, 0, None, "diamond", "#64748b"),
)


def upgrade() -> None:
    bind = op.get_bind()

    designs = sa.table(
        "map_designs",
        sa.column("design_id", PK),
        sa.column("name", sa.String),
        sa.column("purpose", sa.String),
    )
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
    configs = sa.table(
        "map_point_configurations",
        sa.column("layer_id", FK),
        sa.column("label_field", sa.String),
        sa.column("show_label", sa.Boolean),
        sa.column("label_min_zoom", sa.Integer),
        sa.column("style_config", sa.JSON),
    )

    design_ids = bind.execute(
        sa.select(designs.c.design_id).where(designs.c.purpose == DEMARCATION)
    ).scalars().all()
    if not design_ids:
        # No demarcation design to correct — a database whose 0035 seed was
        # removed by hand is not one this revision should recreate it into.
        return

    for design_id in design_ids:
        # 1. Show what 0035 hid. `= sa.true()` rather than `= 1`: SQLite stores
        #    a boolean as an integer and PostgreSQL refuses the comparison, the
        #    trap revisions 0016 to 0021 had to be repaired for.
        bind.execute(
            layers.update()
            .where(layers.c.design_id == design_id)
            .where(layers.c.point_level.in_(_REVEAL))
            .values(is_visible=sa.true())
        )

        # 2. Add the levels it had no layer for, skipping any an administrator
        #    has already added — the unique constraint on
        #    (design_id, point_level) would refuse the insert, and a migration
        #    that fails on a hand-edited design is a migration nobody can run.
        existing = set(bind.execute(
            sa.select(layers.c.point_level)
            .where(layers.c.design_id == design_id)
        ).scalars().all())

        for level, name, order, min_zoom, cluster_at, shape, colour in _ADDED:
            if level in existing:
                continue
            bind.execute(layers.insert().values(
                design_id=design_id,
                layer_name=name,
                point_level=level,
                view_mode="point",
                # NULL all three: nothing on this map reads a metric.
                metric=None, color_metric=None, size_metric=None,
                is_visible=sa.true(),
                display_order=order,
                min_zoom=min_zoom,
                cluster_at=cluster_at,
            ))
            layer_id = bind.execute(
                sa.select(layers.c.layer_id)
                .where(layers.c.design_id == design_id)
                .where(layers.c.point_level == level)
            ).scalar_one()
            bind.execute(configs.insert().values(
                layer_id=layer_id,
                label_field="name",
                show_label=sa.false(),
                label_min_zoom=10,
                style_config={"shape": shape, "point_color": colour},
            ))


def downgrade() -> None:
    """Hide the two again and remove the three this revision added.

    The point configurations go first: SQLite enforces a foreign key only when
    ``PRAGMA foreign_keys=ON``, which the migration runner does not set, so a
    cascade cannot be relied on here — the lesson ``0035``'s own downgrade
    learned when its orphaned layers made the next upgrade collide.
    """
    bind = op.get_bind()
    designs = sa.table("map_designs", sa.column("design_id", PK),
                       sa.column("purpose", sa.String))
    layers = sa.table("map_layers", sa.column("layer_id", PK),
                      sa.column("design_id", FK),
                      sa.column("point_level", sa.String),
                      sa.column("is_visible", sa.Boolean))
    configs = sa.table("map_point_configurations", sa.column("layer_id", FK))

    doomed_designs = sa.select(designs.c.design_id).where(
        designs.c.purpose == DEMARCATION)
    added_levels = [level for level, *_ in _ADDED]
    doomed_layers = sa.select(layers.c.layer_id).where(
        layers.c.design_id.in_(doomed_designs),
        layers.c.point_level.in_(added_levels),
    )
    bind.execute(configs.delete().where(configs.c.layer_id.in_(doomed_layers)))
    bind.execute(layers.delete().where(
        layers.c.design_id.in_(doomed_designs),
        layers.c.point_level.in_(added_levels),
    ))
    bind.execute(
        layers.update()
        .where(layers.c.design_id.in_(doomed_designs))
        .where(layers.c.point_level.in_(_REVEAL))
        .values(is_visible=sa.false())
    )
