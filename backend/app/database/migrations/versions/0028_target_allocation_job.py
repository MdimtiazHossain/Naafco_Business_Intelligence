"""The allocation job: one run of the engine, and how far it got.

Revision ID: 0028_target_allocation_job
Revises: 0027_target_management
Create Date: 2026-08-27

One new table. Nothing existing is altered, dropped or rewritten.

**Why the job is durable where an upload's progress is not.** The upload centre
holds live progress in process memory, because ``etl.pipeline.run_import`` is
one long transaction: a progress row written inside it is invisible to the
polling connection until it commits, and on SQLite a second writer would block
against its write lock for exactly the period the user is waiting through.

An allocation is the other shape. Its expensive half — reading two years of
sales, building the tree, mixing the factors, rounding every split — is **pure
reading**, and holds no write lock at all. Only the final persist opens a write
transaction, and by then the stage is already ``FINALIZATION``. So progress can
be written to this row throughout and read by a poll on another connection,
which makes it survive a process restart and a page reload rather than
degrading to "no detail" the way an in-flight upload does.

**Why the job's status is not the version's status.** They answer different
questions and move at different times. A job that fails leaves its version
exactly where it was; a version reaches ``ALLOCATED`` only once a job has both
completed *and* reconciled. Folding them together would mean a failed run had to
either corrupt the version's state or leave the failure invisible.

``NO_HISTORY`` is a status of its own for the same kind of reason. An engine that
finds no sales in the basis years and refuses to invent an allocation has not
failed — it is correctly waiting for transactional data, and a planner reading
"Failed" would go looking for a bug that is not there.

**Portability.** ``settings`` and ``result`` use the same ``JSON``/``JSONB``
variant pair ``chat_tool_calls`` and the agent-learning tables already store
documents in; ``job_uuid`` is a bounded ``String(36)`` because it carries a
unique constraint, and PostgreSQL's btree refuses an unbounded one. Verified on
SQLite and PostgreSQL before this revision was considered done.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028_target_allocation_job"
down_revision: Union[str, None] = "0027_target_management"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
FK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSON_TYPE = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    op.create_table(
        "target_allocation_job",
        sa.Column("job_id", PK, autoincrement=True, nullable=False),
        # A uuid rather than the surrogate key, because this is what a browser
        # polls by: a sequential id would let one planner watch another's run.
        sa.Column("job_uuid", sa.String(length=36), nullable=False),
        sa.Column("plan_id", FK, nullable=False),
        sa.Column("version_id", FK, nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("current_stage", sa.String(length=32), nullable=True),
        sa.Column("progress_percent", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("rows_processed", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        # NULL until the tree and the months are known. Zero would read as
        # "nothing to do", which is a different thing from "not counted yet".
        sa.Column("total_rows", sa.Integer(), nullable=True),
        sa.Column("error_count", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("settings", JSON_TYPE, nullable=True),
        sa.Column("result", JSON_TYPE, nullable=True),
        sa.Column("requested_by", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        # CASCADE on both: a job is a record of work done *to* a version and has
        # no meaning apart from it. The business trail of what the allocation
        # did lives in ``target_audit``, which is SET NULL and survives.
        sa.ForeignKeyConstraint(["plan_id"], ["target_plan.plan_id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_id"],
                                ["target_version.version_id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id"),
        sa.UniqueConstraint("job_uuid", name="uq_target_allocation_job_uuid"),
    )
    op.create_index("ix_target_allocation_job_version",
                    "target_allocation_job", ["version_id"])
    op.create_index("ix_target_allocation_job_plan",
                    "target_allocation_job", ["plan_id"])
    op.create_index("ix_target_allocation_job_status",
                    "target_allocation_job", ["status"])
    op.create_index("ix_target_allocation_job_created",
                    "target_allocation_job", ["created_at"])


def downgrade() -> None:
    """Drop the table.

    Costs the history of which runs were made and under what settings, which is
    not re-derivable — the allocations themselves survive in
    ``target_allocation``, but the record of the rules that produced them does
    not. Nothing else references this table, so the drop is structurally safe.
    """
    op.drop_index("ix_target_allocation_job_created",
                  table_name="target_allocation_job")
    op.drop_index("ix_target_allocation_job_status",
                  table_name="target_allocation_job")
    op.drop_index("ix_target_allocation_job_plan",
                  table_name="target_allocation_job")
    op.drop_index("ix_target_allocation_job_version",
                  table_name="target_allocation_job")
    op.drop_table("target_allocation_job")
