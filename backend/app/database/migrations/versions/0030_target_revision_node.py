"""A revision names a node, and the approval chain runs bottom-up.

Revision ID: 0030_target_revision_node
Revises: 0029_target_adjustments
Create Date: 2026-08-27

Four new columns on ``target_revision`` and one correction to seeded
configuration. Nothing is dropped and no business row is touched.

**A revision is about a node, not about a row.** ``target_revision`` was created
with a single ``allocation_id``, and an allocation row is one node's volume for
one material in one month. What a reviewer actually revises is a *node's figure
for the period* — a territory's 80,000 — which spans every month of the plan and
every material in it. Anchoring the request on one of those rows and leaving the
node implicit made the commonest question about the table ("which revisions are
open on this territory?") a join through the allocation, and made the answer
depend on which row happened to be picked.

So ``version_id``, ``level``, ``node_code`` and ``material_code`` are stated
outright. ``material_code`` is nullable and means the same thing it means on
``target_adjustment``: NULL revises the node's whole figure, a stated code
revises one material's share of it.

``allocation_id`` stays, stays NOT NULL, and keeps its CASCADE — it is the
*anchor row*, the lowest month of the lowest material at that node, and it is
what makes a pending revision vanish when the allocation it questioned is
regenerated. That is the correct behaviour rather than a side effect: an
allocation run replaces every row it produced, and a request to change a figure
that no longer exists is not a request anybody can act on.

**The approval sequence was seeded upside down.** ``DEFAULT_APPROVAL_MATRIX``
documents a chain that "runs upward from the sub-territory, where a target is
first questioned, to Management" — and then seeded Management at 1 and the Sales
Officer at 8, which is that chain read backwards. Taken literally it means the
CEO signs off on a target before the regional manager has looked at it, which is
not a workflow anyone runs.

This corrects the eight sequenced roles to ascend from the bottom, and does it
**only where the row still holds the value revision 0027 seeded**. A deployment
whose administrator has already reordered the chain has expressed a decision,
and a migration correcting its own earlier defect has no business overwriting
one. The two administrator rows are not touched at all: their sequence is NULL,
which means outside the chain, and NULL is not a number that can be wrong.

**Portability.** Four nullable columns, two indexes and eight single-row
UPDATEs with integer comparisons. Nothing dialect-specific; verified on SQLite
and PostgreSQL.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030_target_revision_node"
down_revision: Union[str, None] = "0029_target_adjustments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
CODE = sa.String(64)

#: ``role -> (seeded_by_0027, corrected)``. The correction is applied only where
#: the stored value still equals the first element, so an administrator's own
#: ordering survives this revision untouched.
SEQUENCE_CORRECTIONS: tuple[tuple[str, int, int], ...] = (
    ("SALES_OFFICER", 8, 1),
    ("TERRITORY_MANAGER", 7, 2),
    ("UNIT_MANAGER", 6, 3),
    ("AREA_MANAGER", 5, 4),
    ("REGIONAL_MANAGER", 4, 5),
    ("ZONE_MANAGER", 3, 6),
    ("BUSINESS_UNIT_HEAD", 2, 7),
    ("MANAGEMENT", 1, 8),
)


def upgrade() -> None:
    op.add_column("target_revision", sa.Column("version_id", FK, nullable=True))
    op.add_column("target_revision",
                  sa.Column("level", sa.String(length=24), nullable=True))
    op.add_column("target_revision", sa.Column("node_code", CODE, nullable=True))
    op.add_column("target_revision",
                  sa.Column("material_code", CODE, nullable=True))

    # Back-filled from the anchor row rather than left blank. The columns are
    # new, so in practice there is nothing to fill — but a revision that existed
    # and lost its node would be a request nobody could route, and deriving it
    # from the row the request was already attached to invents nothing.
    connection = op.get_bind()
    connection.execute(sa.text("""
        UPDATE target_revision
           SET version_id = (SELECT a.version_id FROM target_allocation a
                              WHERE a.allocation_id = target_revision.allocation_id),
               level       = (SELECT a.level FROM target_allocation a
                              WHERE a.allocation_id = target_revision.allocation_id),
               node_code   = (SELECT a.node_code FROM target_allocation a
                              WHERE a.allocation_id = target_revision.allocation_id)
         WHERE version_id IS NULL
    """))

    op.create_index("ix_target_revision_version", "target_revision",
                    ["version_id"])
    op.create_index("ix_target_revision_node", "target_revision",
                    ["version_id", "level", "node_code"])

    # The chain, the right way up. Two statements per role rather than a CASE:
    # the guard is per row, and a role whose sequence somebody has already
    # changed must be skipped rather than swept along with the rest.
    #
    # Applied in two passes through a negative holding value, because the source
    # and target sequences overlap — writing SALES_OFFICER to 1 while
    # MANAGEMENT still holds 1 would collide with any uniqueness an
    # administrator has added, and would make the second pass's guard read a
    # value this migration itself had just written.
    for role, seeded, _corrected in SEQUENCE_CORRECTIONS:
        connection.execute(
            sa.text("UPDATE target_approval_matrix SET approval_sequence = :hold "
                    "WHERE role = :role AND approval_sequence = :seeded"),
            {"hold": -seeded, "role": role, "seeded": seeded},
        )
    for role, seeded, corrected in SEQUENCE_CORRECTIONS:
        connection.execute(
            sa.text("UPDATE target_approval_matrix SET approval_sequence = :corrected "
                    "WHERE role = :role AND approval_sequence = :hold"),
            {"corrected": corrected, "role": role, "hold": -seeded},
        )


def downgrade() -> None:
    """Put the sequence back and drop the four columns.

    Costs the node identity on any open revision, which is why the anchor row
    was never removed: ``allocation_id`` still says which figure was questioned.
    """
    connection = op.get_bind()
    for role, _seeded, corrected in SEQUENCE_CORRECTIONS:
        connection.execute(
            sa.text("UPDATE target_approval_matrix SET approval_sequence = :hold "
                    "WHERE role = :role AND approval_sequence = :corrected"),
            {"hold": -corrected, "role": role, "corrected": corrected},
        )
    for role, seeded, corrected in SEQUENCE_CORRECTIONS:
        connection.execute(
            sa.text("UPDATE target_approval_matrix SET approval_sequence = :seeded "
                    "WHERE role = :role AND approval_sequence = :hold"),
            {"seeded": seeded, "role": role, "hold": -corrected},
        )

    op.drop_index("ix_target_revision_node", table_name="target_revision")
    op.drop_index("ix_target_revision_version", table_name="target_revision")
    op.drop_column("target_revision", "material_code")
    op.drop_column("target_revision", "node_code")
    op.drop_column("target_revision", "level")
    op.drop_column("target_revision", "version_id")
