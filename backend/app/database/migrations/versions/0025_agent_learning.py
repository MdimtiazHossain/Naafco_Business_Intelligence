"""Agent learning: feedback, mined failure signals, approved aliases and examples.

Revision ID: 0025_agent_learning
Revises: 0024_customer_name_on_views
Create Date: 2026-08-25

Four new tables and nothing else. No existing table is altered, no view is
rebuilt and no row is touched, so every fact table, reporting view and ETL path
keeps working exactly as it did — this revision only adds somewhere to put what
the agent learns.

**Why the agent needs somewhere to learn at all.** Intent classification reads
hand-written keyword dictionaries in ``ai/intent.py`` and entity resolution
matches master-data names in ``ai/entity_resolver.py``. Neither guesses, which
is what makes an answer reproducible — and it is also why a word nobody has
written down is a word the agent cannot understand. Today the only way to teach
it one is to edit Python and deploy.

**Why the tables are shaped around approval rather than around learning.** The
risk here is not that the agent learns too slowly, it is that it learns
something wrong. A mistaken alias does not degrade an answer, it replaces it: the
agent would answer confidently and wrongly for every future question using that
word, which is strictly worse than the "not found" it says today. So every row
here is inert until a person approves it, and ``LearningStatus`` is carried on
both of the tables that are ever read at query time.

The tables, and what each is for:

* ``agent_feedback`` — one reader's verdict on one answer, plus what they say
  they expected. The ground truth; without it there is traffic but no signal.
* ``agent_learning_signal`` — phrases the agent handled badly, deduplicated and
  counted. Mined from ``chat_messages`` and ``chat_tool_calls``, which already
  record the intent, the error code and the row count of every turn, so mining
  reads history rather than requiring new instrumentation.
* ``agent_term_alias`` — approved vocabulary: a phrase and the entity or keyword
  it means.
* ``agent_example`` — approved worked examples: a question and the tool call
  that answered it correctly.

**On ``active_key``, which appears on the two approved tables.** Both need "at
most one ACTIVE row per phrase", and both must let a retired mapping be replaced
by a better one later. A partial unique index expresses that exactly and is
PostgreSQL-only; a plain unique over the natural columns would also forbid the
replacement. So the column holds the natural key while the row is ACTIVE and
NULL in every other state, and carries a plain unique constraint. NULLs are
distinct in a unique constraint on both SQLite and PostgreSQL, so any number of
proposed, rejected and retired rows coexist while at most one active row can
hold the key. The application sets and clears it on the approve and retire
paths.

**Portability.** Nothing here is dialect-specific: the identity columns use the
``BigInteger``/``Integer`` variant pair the rest of the schema uses, JSON is the
same ``JSON``/``JSONB`` variant ``chat_tool_calls`` already stores arguments in,
and every unique-constrained text column is bounded — ``String`` rather than
``Text`` — because PostgreSQL's btree refuses an index entry over about 2.7 kB
while SQLite would have accepted one silently. That asymmetry is exactly the
class of mistake revisions 0016 to 0021 had to be repaired for, so it is
designed out here rather than discovered on the first PostgreSQL run.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025_agent_learning"
down_revision: Union[str, None] = "0024_customer_name_on_views"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The identity/foreign-key pair used across this schema: a PostgreSQL bigint
#: that SQLite stores as its own rowid-backed integer.
PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
FK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSON_TYPE = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)
CODE = sa.String(64)


def upgrade() -> None:
    # ---------------------------------------------------------------- feedback
    op.create_table(
        "agent_feedback",
        sa.Column("feedback_id", PK, autoincrement=True, nullable=False),
        sa.Column("message_id", FK, nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", FK, nullable=False),
        sa.Column("rating", sa.String(length=8), nullable=False),
        sa.Column("expected", sa.Text(), nullable=True),
        sa.Column("injection_flags", JSON_TYPE, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["chat_messages.message_id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("feedback_id"),
        # One verdict per person per answer: a reader may change their mind, but
        # two rows would let one user outvote the rest by clicking twice.
        sa.UniqueConstraint("message_id", "user_id",
                            name="uq_agent_feedback_message_user"),
    )
    op.create_index("ix_agent_feedback_message_id", "agent_feedback",
                    ["message_id"])
    op.create_index("ix_agent_feedback_rating", "agent_feedback", ["rating"])
    op.create_index("ix_agent_feedback_created_at", "agent_feedback",
                    ["created_at"])

    # ------------------------------------------------------------- mined signal
    op.create_table(
        "agent_learning_signal",
        sa.Column("signal_id", PK, autoincrement=True, nullable=False),
        sa.Column("signal_type", sa.String(length=32), nullable=False),
        # Bounded, because it is half of a unique constraint. See the note on
        # portability in this revision's docstring.
        sa.Column("normalized_phrase", sa.String(length=300), nullable=False),
        sa.Column("raw_sample", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.Column("occurrences", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("signal_id"),
        # The dedupe key: one row per (kind of failure, phrase), counted rather
        # than repeated.
        sa.UniqueConstraint("signal_type", "normalized_phrase",
                            name="uq_agent_learning_signal_type_phrase"),
    )
    op.create_index("ix_agent_learning_signal_status", "agent_learning_signal",
                    ["status"])
    op.create_index("ix_agent_learning_signal_occurrences",
                    "agent_learning_signal", ["occurrences"])
    op.create_index("ix_agent_learning_signal_last_seen",
                    "agent_learning_signal", ["last_seen_at"])

    # -------------------------------------------------------------- vocabulary
    op.create_table(
        "agent_term_alias",
        sa.Column("alias_id", PK, autoincrement=True, nullable=False),
        sa.Column("phrase", sa.String(length=200), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.Column("alias_kind", sa.String(length=16), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=True),
        sa.Column("entity_code", CODE, nullable=True),
        sa.Column("target_keyword", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("signal_id", FK, nullable=True),
        sa.Column("active_key", sa.String(length=220), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("approved_by", sa.String(length=64), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        # SET NULL, not CASCADE: an approved alias outlives the complaint that
        # prompted it. Losing the provenance is acceptable; losing a mapping
        # somebody vouched for is not.
        sa.ForeignKeyConstraint(["signal_id"],
                                ["agent_learning_signal.signal_id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("alias_id"),
        sa.UniqueConstraint("active_key", name="uq_agent_term_alias_active_key"),
    )
    op.create_index("ix_agent_term_alias_phrase", "agent_term_alias", ["phrase"])
    op.create_index("ix_agent_term_alias_status", "agent_term_alias", ["status"])
    op.create_index("ix_agent_term_alias_kind", "agent_term_alias",
                    ["alias_kind"])

    # ----------------------------------------------------------- worked example
    op.create_table(
        "agent_example",
        sa.Column("example_id", PK, autoincrement=True, nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("normalized_question", sa.String(length=300), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.Column("intent", sa.String(length=48), nullable=True),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("arguments", JSON_TYPE, nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("source_message_id", FK, nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False),
        sa.Column("active_key", sa.String(length=300), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("approved_by", sa.String(length=64), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        # SET NULL for the same reason as above: an approved example is a
        # standalone piece of vocabulary, not an annotation on one conversation.
        sa.ForeignKeyConstraint(["source_message_id"],
                                ["chat_messages.message_id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("example_id"),
        sa.UniqueConstraint("active_key", name="uq_agent_example_active_key"),
    )
    op.create_index("ix_agent_example_normalized", "agent_example",
                    ["normalized_question"])
    op.create_index("ix_agent_example_status", "agent_example", ["status"])
    op.create_index("ix_agent_example_tool_name", "agent_example", ["tool_name"])


def downgrade() -> None:
    """Drop all four.

    Safe in a way most revisions here are not: nothing else references these
    tables, and everything they hold is either re-mineable from
    ``chat_messages`` and ``chat_tool_calls`` — which this revision does not
    touch — or was entered by hand and can be entered again. The agent's own
    behaviour is unchanged by their absence, because every reader consults them
    only after its own resolution has already failed.

    Dropped children-first so the two foreign keys into
    ``agent_learning_signal`` and ``chat_messages`` are gone before their
    targets matter.
    """
    op.drop_index("ix_agent_example_tool_name", table_name="agent_example")
    op.drop_index("ix_agent_example_status", table_name="agent_example")
    op.drop_index("ix_agent_example_normalized", table_name="agent_example")
    op.drop_table("agent_example")

    op.drop_index("ix_agent_term_alias_kind", table_name="agent_term_alias")
    op.drop_index("ix_agent_term_alias_status", table_name="agent_term_alias")
    op.drop_index("ix_agent_term_alias_phrase", table_name="agent_term_alias")
    op.drop_table("agent_term_alias")

    op.drop_index("ix_agent_learning_signal_last_seen",
                  table_name="agent_learning_signal")
    op.drop_index("ix_agent_learning_signal_occurrences",
                  table_name="agent_learning_signal")
    op.drop_index("ix_agent_learning_signal_status",
                  table_name="agent_learning_signal")
    op.drop_table("agent_learning_signal")

    op.drop_index("ix_agent_feedback_created_at", table_name="agent_feedback")
    op.drop_index("ix_agent_feedback_rating", table_name="agent_feedback")
    op.drop_index("ix_agent_feedback_message_id", table_name="agent_feedback")
    op.drop_table("agent_feedback")
