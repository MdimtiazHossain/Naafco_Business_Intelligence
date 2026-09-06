"""0037_demarcation_level_order — the demarcation levels read down the hierarchy.

Revision ID: 0037_demarcation_level_order
Revises: 0036_demarcation_all_levels
Create Date: 2026-09-06

``0035`` seeded the Area Demarcation design starting at Zone, and ``0036``
appended Company, Business Unit and Sales Line after Sales Force because they
were the levels it was adding. The result reads
``Zone … Sales Force, Company, Business Unit, Sales Line`` — the order the
layers were *written* rather than the order they mean, so the three widest
levels sit at the bottom of a list whose whole subject is a hierarchy.

This puts them in the order ``app.map.levels.MAP_LEVELS`` already states: the
organisational chain from the top down, then the two levels that hang off it.
A reader scanning the legend or the layer toggles now reads the business
structure in the order the business describes it.

**It moves the stacking too, and that is deliberate rather than overlooked.**
``display_order`` is one field with two consumers: the legend and the toggles
list in it, and the renderer stacks in it, bottom to top. ``0036`` put the three
new levels *last* precisely so they would be drawn last — on top — reasoning
that "a company point is a single dot for the whole country, and drawing it
under the 846 customer points would bury it".

That reasoning has since expired. ``af81425`` stopped drawing ``DERIVED``
coordinates, and Company, Business Unit and Sales Line are derived centroids in
both ``data/dev.db`` and the deployment — every one of them, checked before this
was written. There is no company dot left to bury, so the two orders no longer
pull against each other and one field can honestly serve both. Should a company
coordinate ever be *uploaded*, it would be drawn beneath the finer levels, and
the fix then is a stacking rule of its own rather than a list nobody can read.

Additive in effect: no column, table or row is created or removed, and only the
``display_order`` of layers belonging to a ``demarcation`` design is touched. A
level an administrator has added by hand and this revision does not name keeps
whatever order it had.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0037_demarcation_level_order"
down_revision: Union[str, None] = "0036_demarcation_all_levels"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
FK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

DEMARCATION = "demarcation"

#: The hierarchy, top down, then the two levels that hang off it.
#:
#: Restated here rather than imported from ``MAP_LEVELS``, as every seed in this
#: chain is: a migration must keep producing what it produced on the day it ran,
#: and a later change to the registry must not silently rewrite history. The two
#: are pinned equal by ``test_map_demarcation`` instead, which is where a
#: divergence should be reported.
_ORDER: tuple[str, ...] = (
    "company", "bu", "sales_line", "zone", "region", "area", "unit",
    "territory", "sub_territory", "customer", "sales_force",
)

#: What ``0035`` and ``0036`` left between them, for the downgrade.
_PREVIOUS: tuple[str, ...] = (
    "zone", "region", "area", "unit", "territory", "sub_territory",
    "customer", "sales_force", "company", "bu", "sales_line",
)


def _apply(order: tuple[str, ...]) -> None:
    bind = op.get_bind()
    designs = sa.table("map_designs", sa.column("design_id", PK),
                       sa.column("purpose", sa.String))
    layers = sa.table("map_layers", sa.column("design_id", FK),
                      sa.column("point_level", sa.String),
                      sa.column("display_order", sa.Integer))

    demarcation = sa.select(designs.c.design_id).where(
        designs.c.purpose == DEMARCATION)
    for position, level in enumerate(order, start=1):
        bind.execute(
            layers.update()
            .where(layers.c.design_id.in_(demarcation))
            .where(layers.c.point_level == level)
            .values(display_order=position)
        )


def upgrade() -> None:
    _apply(_ORDER)


def downgrade() -> None:
    _apply(_PREVIOUS)
