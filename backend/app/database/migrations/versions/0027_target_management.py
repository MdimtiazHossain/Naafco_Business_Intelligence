"""Target Management: eight workflow tables, and the two derivation inputs.

Revision ID: 0027_target_management
Revises: 0026_remove_warehouse_marker
Create Date: 2026-08-27

Purely additive. Eight new tables, two new nullable columns on ``dim_material``,
and one seeded configuration table. No existing table is dropped, no column is
removed, no view is rebuilt and no row is deleted or rewritten — every report,
ETL path and reporting view keeps behaving exactly as it did.

**What this is for.** ``fact_target`` holds targets a file *states*: a month, a
territory, a customer, a material and three measures, loaded by the pipeline
like any other transactional dataset. It has nowhere to record a target the
business *builds* — which plan it belongs to, which version, who allocated it,
who approved it, what it replaced and why. These tables are that record.
``fact_target`` stays the one authority on what a target *is*; a version writes
into it only when it is locked.

**The two columns on ``dim_material``.** The module's central calculation is
``quantity = target_volume / conversion_factor`` and
``value = quantity * transfer_price``. Neither input exists anywhere in this
schema today: revision 0022 removed ``dim_product``, which was the only table
that had ever held a pack unit, and the Material Master as it stood states a
company, a group, a brand, a code and a description and nothing else.

So both columns are added here, **nullable, with no server default and no
back-fill**. A default of ``1.0`` would not read as "unknown" — it would read as
"one volume unit per saleable unit", which is a claim about the goods, and
inventing it for 405 existing materials is exactly the guess this system exists
to refuse. Every material starts NULL and acquires a value only when a Material
Master file states one. Until then the derived figures render ``n/a``, which is
the suppress-rather-than-guess invariant applied to a ratio with a missing
divisor.

``conversion_factor`` is ``NUMERIC(18, 6)`` where the money columns are
``NUMERIC(18, 4)``: it is a divisor, and a 5 g sachet of a kilogram-based
material is 0.005. Rounding a divisor is how a rounding error becomes a
multiplication error.

**Portability.** Verified on both dialects before this revision was considered
done, which is what revisions 0016 to 0021 had to be repaired for. Concretely:

* every identity and foreign-key column uses the ``BigInteger``/``Integer``
  variant pair the rest of the schema uses;
* every boolean is created as ``sa.Boolean`` with a ``sa.false()`` / ``sa.true()``
  server default, never an integer literal — PostgreSQL rejects
  ``boolean = integer`` outright while SQLite silently accepts it;
* every unique-constrained text column is a bounded ``String``, never ``Text``,
  because PostgreSQL's btree refuses an index entry over about 2.7 kB;
* the seeded matrix is inserted through a lightweight ``sa.table`` rather than
  raw SQL, so the values are bound and typed by the driver on both sides;
* ``target_version.current_plan_id`` relies on NULLs being distinct in a unique
  constraint, which holds on SQLite and PostgreSQL alike. A partial unique index
  would say it more directly and is PostgreSQL-only.

**On the seeded approval matrix.** ``target_approval_matrix`` is configuration,
and configuration with no rows is not a blank slate — it is a workflow in which
nobody can approve anything. So the default chain is seeded here, the way
revision 0017 seeded a country row into ``map_area_styles``. It is ordinary data
afterwards: an administrator edits it, and this revision never rewrites it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027_target_management"
down_revision: Union[str, None] = "0026_remove_warehouse_marker"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The identity/foreign-key pair used across this schema: a PostgreSQL bigint
#: that SQLite stores as its own rowid-backed integer.
PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
FK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
CODE = sa.String(64)
QUANTITY = sa.Numeric(18, 4)
MONEY = sa.Numeric(18, 4)
FACTOR = sa.Numeric(18, 6)
PERCENT = sa.Numeric(6, 2)


def _timestamps() -> tuple[sa.Column, sa.Column]:
    """``created_at``/``updated_at`` as ``TimestampMixin`` declares them."""
    return (
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )


#: ``(role, level, sequence, edit, approve, reject, revise, limit)``.
#:
#: Restated here rather than imported from ``app.database.models_target``: a
#: migration must keep producing the schema and the seed it produced on the day
#: it was written, and importing application code would let a later edit to that
#: tuple silently change what an old database is upgraded into.
_DEFAULT_MATRIX: tuple[
    tuple[str, str, int | None, bool, bool, bool, bool, float | None], ...
] = (
    ("MANAGEMENT", "company", 1, True, True, True, True, None),
    ("BUSINESS_UNIT_HEAD", "company", 2, True, True, True, True, 20.0),
    ("ZONE_MANAGER", "zone", 3, True, True, True, True, 15.0),
    ("REGIONAL_MANAGER", "region", 4, True, True, True, True, 10.0),
    ("AREA_MANAGER", "area", 5, True, True, True, False, 10.0),
    ("UNIT_MANAGER", "unit", 6, True, True, False, False, 5.0),
    ("TERRITORY_MANAGER", "territory", 7, True, False, False, True, 5.0),
    ("SALES_OFFICER", "sub_territory", 8, False, False, False, True, 0.0),
    ("SUPER_ADMIN", "company", None, True, False, False, False, None),
    ("ADMIN", "company", None, True, False, False, False, None),
)


def upgrade() -> None:
    # ------------------------------------------------- material derivation inputs
    # Nullable, no server default, no back-fill. See this revision's docstring:
    # a default here would be a claim about the goods.
    op.add_column("dim_material",
                  sa.Column("conversion_factor", FACTOR, nullable=True))
    op.add_column("dim_material",
                  sa.Column("transfer_price", MONEY, nullable=True))

    # ------------------------------------------------------------------ the plan
    op.create_table(
        "target_plan",
        sa.Column("plan_id", PK, autoincrement=True, nullable=False),
        sa.Column("plan_code", sa.String(length=32), nullable=False),
        sa.Column("financial_year", sa.String(length=32), nullable=False),
        sa.Column("target_period", sa.String(length=16), nullable=False),
        sa.Column("company_code", CODE, nullable=False),
        sa.Column("bu_code", CODE, nullable=False),
        sa.Column("sales_line_code", CODE, nullable=False),
        sa.Column("basis_financial_years", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("plan_id"),
        sa.UniqueConstraint("plan_code", name="uq_target_plan_code"),
        # One plan per scope. A second target for the same scope is a version,
        # never a second plan nobody can tell apart from the first.
        sa.UniqueConstraint("financial_year", "target_period", "company_code",
                            "bu_code", "sales_line_code",
                            name="uq_target_plan_scope"),
    )
    op.create_index("ix_target_plan_financial_year", "target_plan",
                    ["financial_year"])
    op.create_index("ix_target_plan_status", "target_plan", ["status"])
    op.create_index("ix_target_plan_company", "target_plan", ["company_code"])

    # --------------------------------------------------------------- the version
    op.create_table(
        "target_version",
        sa.Column("version_id", PK, autoincrement=True, nullable=False),
        sa.Column("plan_id", FK, nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        # Holds ``plan_id`` while this version is current, NULL otherwise. NULLs
        # are distinct in a unique constraint on both dialects, so superseded
        # versions coexist freely while only one can claim the plan.
        sa.Column("current_plan_id", FK, nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_batch_id", sa.String(length=36), nullable=True),
        *_timestamps(),
        # RESTRICT: a plan carrying approvals and audit rows is not something a
        # delete should be able to take with it.
        sa.ForeignKeyConstraint(["plan_id"], ["target_plan.plan_id"],
                                ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("version_id"),
        sa.UniqueConstraint("plan_id", "version_no",
                            name="uq_target_version_plan_no"),
        sa.UniqueConstraint("current_plan_id", name="uq_target_version_current"),
    )
    op.create_index("ix_target_version_plan", "target_version", ["plan_id"])
    op.create_index("ix_target_version_status", "target_version", ["status"])

    # ------------------------------------------------------ the typed country row
    op.create_table(
        "target_country_line",
        sa.Column("line_id", PK, autoincrement=True, nullable=False),
        sa.Column("version_id", FK, nullable=False),
        sa.Column("material_code", CODE, nullable=False),
        sa.Column("target_volume", QUANTITY, nullable=False,
                  server_default=sa.text("0")),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["version_id"],
                                ["target_version.version_id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("line_id"),
        sa.UniqueConstraint("version_id", "material_code",
                            name="uq_target_country_line"),
    )
    op.create_index("ix_target_country_line_version", "target_country_line",
                    ["version_id"])
    op.create_index("ix_target_country_line_material", "target_country_line",
                    ["material_code"])

    # ------------------------------------------------------ the generated figures
    op.create_table(
        "target_allocation",
        sa.Column("allocation_id", PK, autoincrement=True, nullable=False),
        sa.Column("version_id", FK, nullable=False),
        sa.Column("level", sa.String(length=24), nullable=False),
        sa.Column("node_code", CODE, nullable=False),
        sa.Column("parent_level", sa.String(length=24), nullable=True),
        sa.Column("parent_code", CODE, nullable=True),
        sa.Column("material_code", CODE, nullable=False),
        sa.Column("target_month", sa.String(length=16), nullable=False),
        sa.Column("system_volume", QUANTITY, nullable=False,
                  server_default=sa.text("0")),
        sa.Column("current_volume", QUANTITY, nullable=False,
                  server_default=sa.text("0")),
        sa.Column("approved_volume", QUANTITY, nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["version_id"],
                                ["target_version.version_id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("allocation_id"),
        # Every row is monthly and there is no period-total row beside it, so a
        # parent total is a sum over its children and cannot disagree with them.
        sa.UniqueConstraint("version_id", "level", "node_code", "material_code",
                            "target_month", name="uq_target_allocation_node"),
    )
    op.create_index("ix_target_allocation_version_level", "target_allocation",
                    ["version_id", "level"])
    op.create_index("ix_target_allocation_parent", "target_allocation",
                    ["version_id", "parent_code"])
    op.create_index("ix_target_allocation_material", "target_allocation",
                    ["version_id", "material_code"])
    op.create_index("ix_target_allocation_month", "target_allocation",
                    ["version_id", "target_month"])
    op.create_index("ix_target_allocation_status", "target_allocation",
                    ["status"])

    # ------------------------------------------------------------- the revisions
    op.create_table(
        "target_revision",
        sa.Column("revision_id", PK, autoincrement=True, nullable=False),
        sa.Column("allocation_id", FK, nullable=False),
        sa.Column("system_volume", QUANTITY, nullable=False),
        sa.Column("requested_volume", QUANTITY, nullable=False),
        sa.Column("approved_volume", QUANTITY, nullable=True),
        # NOT NULL by design: a revision with no stated reason is the thing this
        # table exists to prevent.
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("change_percent", PERCENT, nullable=True),
        sa.Column("escalated_to_role", sa.String(length=32), nullable=True),
        sa.Column("requested_by", sa.String(length=64), nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["allocation_id"],
                                ["target_allocation.allocation_id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("revision_id"),
    )
    op.create_index("ix_target_revision_allocation", "target_revision",
                    ["allocation_id"])
    op.create_index("ix_target_revision_status", "target_revision", ["status"])
    op.create_index("ix_target_revision_requested_by", "target_revision",
                    ["requested_by"])

    # ------------------------------------------------------------ the approvals
    op.create_table(
        "target_approval",
        sa.Column("approval_id", PK, autoincrement=True, nullable=False),
        sa.Column("version_id", FK, nullable=False),
        sa.Column("level", sa.String(length=24), nullable=True),
        sa.Column("node_code", CODE, nullable=True),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("actor_role", sa.String(length=32), nullable=True),
        sa.Column("approval_sequence", sa.Integer(), nullable=True),
        sa.Column("actor", sa.String(length=64), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("acted_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        # No updated_at: append-only. An approver who changes their mind adds a
        # row, and both stay.
        sa.ForeignKeyConstraint(["version_id"],
                                ["target_version.version_id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("approval_id"),
    )
    op.create_index("ix_target_approval_version", "target_approval",
                    ["version_id"])
    op.create_index("ix_target_approval_node", "target_approval",
                    ["version_id", "level", "node_code"])
    op.create_index("ix_target_approval_acted_at", "target_approval",
                    ["acted_at"])

    # ------------------------------------------------------- the approval matrix
    matrix = op.create_table(
        "target_approval_matrix",
        sa.Column("matrix_id", PK, autoincrement=True, nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("hierarchy_level", sa.String(length=24), nullable=False),
        # NULL means the role sits outside the approval chain — not the same as
        # sequence 0, and a real case: the administrator configures the run and
        # never signs off on a number.
        sa.Column("approval_sequence", sa.Integer(), nullable=True),
        sa.Column("can_edit", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("can_approve", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("can_reject", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("can_revise", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        # NULL means unlimited; 0.00 means "may not change a figure at all".
        # Two different settings, kept distinguishable.
        sa.Column("adjustment_limit_percent", PERCENT, nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("matrix_id"),
        sa.UniqueConstraint("role", name="uq_target_approval_matrix_role"),
    )
    op.create_index("ix_target_approval_matrix_sequence",
                    "target_approval_matrix", ["approval_sequence"])
    op.create_index("ix_target_approval_matrix_active",
                    "target_approval_matrix", ["is_active"])

    # Configuration with no rows is not a blank slate — it is a workflow in
    # which nobody can approve anything. Seeded the way 0017 seeded a country
    # row into ``map_area_styles``; ordinary, editable data from here on.
    op.bulk_insert(matrix, [
        {
            "role": role,
            "hierarchy_level": level,
            "approval_sequence": sequence,
            "can_edit": edit,
            "can_approve": approve,
            "can_reject": reject,
            "can_revise": revise,
            "adjustment_limit_percent": limit,
            "is_active": True,
            "updated_by": None,
        }
        for role, level, sequence, edit, approve, reject, revise, limit
        in _DEFAULT_MATRIX
    ])

    # ---------------------------------------------------------------- the audit
    op.create_table(
        "target_audit",
        sa.Column("audit_id", PK, autoincrement=True, nullable=False),
        sa.Column("plan_id", FK, nullable=True),
        sa.Column("version_id", FK, nullable=True),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=True),
        sa.Column("actor_role", sa.String(length=32), nullable=True),
        sa.Column("node_label", sa.String(length=200), nullable=True),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        # SET NULL on both: the audit trail outlives what it describes. Losing
        # the link is acceptable; losing the record of what somebody did is not.
        sa.ForeignKeyConstraint(["plan_id"], ["target_plan.plan_id"],
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["version_id"],
                                ["target_version.version_id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("audit_id"),
    )
    op.create_index("ix_target_audit_version", "target_audit", ["version_id"])
    op.create_index("ix_target_audit_plan", "target_audit", ["plan_id"])
    op.create_index("ix_target_audit_occurred_at", "target_audit",
                    ["occurred_at"])
    op.create_index("ix_target_audit_action", "target_audit", ["action"])


def downgrade() -> None:
    """Drop the eight tables and the two columns.

    Honest about what it costs, unlike most downgrades here. The eight tables
    hold plans, approvals and an audit trail that exist nowhere else — none of
    it is re-derivable, and dropping them destroys it. The two ``dim_material``
    columns hold values a Material Master file supplied, which can be uploaded
    again. Nothing outside this revision references any of it, so the drop is
    structurally safe; it is the *data* that does not come back.

    Children before parents, so no foreign key outlives its target.
    ``target_audit`` goes first because it points at both ``target_plan`` and
    ``target_version``.
    """
    op.drop_index("ix_target_audit_action", table_name="target_audit")
    op.drop_index("ix_target_audit_occurred_at", table_name="target_audit")
    op.drop_index("ix_target_audit_plan", table_name="target_audit")
    op.drop_index("ix_target_audit_version", table_name="target_audit")
    op.drop_table("target_audit")

    op.drop_index("ix_target_approval_matrix_active",
                  table_name="target_approval_matrix")
    op.drop_index("ix_target_approval_matrix_sequence",
                  table_name="target_approval_matrix")
    op.drop_table("target_approval_matrix")

    op.drop_index("ix_target_approval_acted_at", table_name="target_approval")
    op.drop_index("ix_target_approval_node", table_name="target_approval")
    op.drop_index("ix_target_approval_version", table_name="target_approval")
    op.drop_table("target_approval")

    op.drop_index("ix_target_revision_requested_by",
                  table_name="target_revision")
    op.drop_index("ix_target_revision_status", table_name="target_revision")
    op.drop_index("ix_target_revision_allocation", table_name="target_revision")
    op.drop_table("target_revision")

    op.drop_index("ix_target_allocation_status", table_name="target_allocation")
    op.drop_index("ix_target_allocation_month", table_name="target_allocation")
    op.drop_index("ix_target_allocation_material",
                  table_name="target_allocation")
    op.drop_index("ix_target_allocation_parent", table_name="target_allocation")
    op.drop_index("ix_target_allocation_version_level",
                  table_name="target_allocation")
    op.drop_table("target_allocation")

    op.drop_index("ix_target_country_line_material",
                  table_name="target_country_line")
    op.drop_index("ix_target_country_line_version",
                  table_name="target_country_line")
    op.drop_table("target_country_line")

    op.drop_index("ix_target_version_status", table_name="target_version")
    op.drop_index("ix_target_version_plan", table_name="target_version")
    op.drop_table("target_version")

    op.drop_index("ix_target_plan_company", table_name="target_plan")
    op.drop_index("ix_target_plan_status", table_name="target_plan")
    op.drop_index("ix_target_plan_financial_year", table_name="target_plan")
    op.drop_table("target_plan")

    op.drop_column("dim_material", "transfer_price")
    op.drop_column("dim_material", "conversion_factor")
