"""Management adjustments, and what a run projected before it started.

Revision ID: 0029_target_adjustments
Revises: 0028_target_allocation_job
Create Date: 2026-08-27

One new table and four new columns on ``target_allocation_job``. Additive
throughout; nothing is dropped, rewritten or back-filled.

**``target_adjustment``: node-level, absolute, and an input rather than an
edit.** A management adjustment names a node and a volume — ``+500``, ``-1200``
— never a percentage. A uniform percentage applied to every child and then
re-normalised is a mathematical no-op, so a global slider is worse than no
slider: it appears to do something and cannot. The adjustment is stored against
the *version* and re-applied on every allocation run, which is what stops a
re-run after loading more sales from silently discarding management's decisions.

``reason`` is NOT NULL. An unexplained change to the number a whole sales force
is measured on is exactly what this column exists to prevent.

``material_code`` is nullable and is part of the unique key: NULL adjusts every
material at that node, a stated code adjusts one, and the two coexist so "raise
Dhaka by 500, except Glyfon by 800" is expressible. NULLs are distinct in a
unique constraint on both dialects, which here is *not* the device it is on
``target_version.current_plan_id`` — it simply means several material-specific
adjustments can sit beside one general one, which is the intent.

**The four job columns are about telling the truth afterwards.**

``projected_rows``
    What the pre-flight expected. Kept beside ``rows_processed`` because a run
    refused for exceeding the ceiling has a projection and no rows, and that
    pair is what tells a planner how much to narrow by.
``allocation_level``
    The deepest level the run actually reached. Recorded rather than assumed to
    be customer: a Customer Master carrying no sub-territory mapping allocates
    to sub-territory, which is a real allocation and must be labelled as one
    rather than presented as a customer-level result.
``warning_count``
    Warnings are not errors. A seasonal fallback or a node split evenly for want
    of history is worth reading and does not make the run wrong, so it is
    counted apart from ``error_count`` — and is what distinguishes
    ``COMPLETED_WITH_WARNINGS`` from ``COMPLETED``.
``sales_rows_found``
    Zero is the entire explanation for a ``NO_HISTORY`` run, and keeping it on
    the row lets the run-history list show it without re-querying two financial
    years of sales per line.

**Portability.** ``QUANTITY`` is the ``NUMERIC(18, 4)`` every volume in this
schema uses; the counters are plain integers with integer server defaults, and
no boolean or JSON type is introduced. Verified on SQLite and PostgreSQL.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029_target_adjustments"
down_revision: Union[str, None] = "0028_target_allocation_job"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
FK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
CODE = sa.String(64)
QUANTITY = sa.Numeric(18, 4)


def upgrade() -> None:
    op.create_table(
        "target_adjustment",
        sa.Column("adjustment_id", PK, autoincrement=True, nullable=False),
        sa.Column("version_id", FK, nullable=False),
        sa.Column("level", sa.String(length=24), nullable=False),
        sa.Column("node_code", CODE, nullable=False),
        # NULL adjusts every material at the node; a stated code adjusts one.
        sa.Column("material_code", CODE, nullable=True),
        # Signed and absolute. Negative is ordinary: management moves volume
        # away from a node as often as towards it.
        sa.Column("adjustment_volume", QUANTITY, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("adjusted_by", sa.String(length=64), nullable=True),
        sa.Column("adjusted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["version_id"],
                                ["target_version.version_id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("adjustment_id"),
        # One standing instruction per node and material. A second would leave
        # the engine choosing between two contradictory adjustments.
        sa.UniqueConstraint("version_id", "level", "node_code", "material_code",
                            name="uq_target_adjustment_node"),
    )
    op.create_index("ix_target_adjustment_version", "target_adjustment",
                    ["version_id"])
    op.create_index("ix_target_adjustment_node", "target_adjustment",
                    ["version_id", "level", "node_code"])

    # Nullable where "not counted yet" and 0 are different things, and
    # NOT NULL with a default where zero is the honest starting value.
    op.add_column("target_allocation_job",
                  sa.Column("projected_rows", sa.Integer(), nullable=True))
    op.add_column("target_allocation_job",
                  sa.Column("allocation_level", sa.String(length=24),
                            nullable=True))
    op.add_column("target_allocation_job",
                  sa.Column("warning_count", sa.Integer(), nullable=False,
                            server_default=sa.text("0")))
    op.add_column("target_allocation_job",
                  sa.Column("sales_rows_found", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop the table and the four columns.

    Costs every standing management adjustment, which is not re-derivable — the
    *record* that each was made survives in ``target_audit``, but the standing
    instruction the engine reads does not. The allocations themselves are
    untouched.
    """
    op.drop_column("target_allocation_job", "sales_rows_found")
    op.drop_column("target_allocation_job", "warning_count")
    op.drop_column("target_allocation_job", "allocation_level")
    op.drop_column("target_allocation_job", "projected_rows")

    op.drop_index("ix_target_adjustment_node", table_name="target_adjustment")
    op.drop_index("ix_target_adjustment_version", table_name="target_adjustment")
    op.drop_table("target_adjustment")
