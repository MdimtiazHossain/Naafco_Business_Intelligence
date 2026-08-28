"""Target Management: the plan a target belongs to, and how it travels.

``fact_target`` holds the *result* — a month, a territory, a customer, a
material and three measures. It says nothing about where that number came from,
who agreed to it or what it replaced, because a target arriving through the
normal ETL has no such story: a file states it and the pipeline loads it.

These tables hold the story for a target the business *builds* rather than
receives. Eight tables, one migration, all additive; ``fact_target`` is not
touched and every report reading it keeps working unchanged.

**Volume is the only figure entered, and the only figure stored.** A country
target states a Target Volume per material. Quantity is ``volume /
conversion_factor`` and Value is ``quantity * transfer_price``, both read from
``dim_material`` at the moment a report is drawn — the same reason
``customer_name`` lives on a view rather than on a fact. A corrected transfer
price therefore corrects every unlocked report at once, and cannot silently
rewrite an approved one, because **locking a version writes it into
``fact_target``**, where ``target_quantity`` and ``target_amount`` freeze what
was agreed. Neither derived figure is stored here, and no column should be added
for one.

A material whose master states no conversion factor or transfer price yields no
quantity and no value. It renders ``n/a``: a derived figure with a missing
divisor is not zero, and the whole point of deriving it is that nobody typed it.

**The chain, and what is final at each link.**

``target_plan``
    The scope: financial year, period, company, business unit, sales line. One
    plan per scope — a second target for the same scope is a *version*, never a
    second plan.
``target_version``
    What changed and who said so. An approved version is never edited: a change
    creates the next version and travels through approval again. At most one
    version per plan is current, enforced by the database rather than by
    convention — see :attr:`TargetVersion.current_plan_id`.
``target_country_line``
    The typed numbers: one Target Volume per material, for one version.
``target_allocation``
    The generated numbers: one row per node, material and month, all the way
    down to customer. Every row is monthly, with no period-total row beside it,
    so a node's total is a sum and can never disagree with its own parts.
``target_revision``
    A request to change one allocated figure. Keeps the system value, the
    requested value and the approved value as three separate columns, because
    "what the engine said" and "what we agreed" are different questions and a
    revision that overwrote the first could not answer either.
``target_approval``
    Who acted on what, and when. Append-only.
``target_approval_matrix``
    Configuration: which role approves at which level, in what order, within
    what adjustment limit.
``target_audit``
    Every action, with its old and new value and its reason. Append-only, and
    written by the backend at the moment of the action — never by the client.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import CODE, SURROGATE_PK, Base, TimestampMixin
from .models_warehouse import FK_TYPE, JSON_TYPE, QUANTITY

#: An adjustment limit is a percentage with two decimals: ``10.00`` means ±10%.
PERCENT = Numeric(6, 2)


class TargetStatus:
    """The nine states a plan version moves through.

    The order is the order of the workflow, and it is one-way apart from
    ``REJECTED``, which returns a version to its submitter. ``LOCKED`` is
    terminal: a locked version is changed only by creating the next one.
    """

    DRAFT = "DRAFT"
    ALLOCATION_IN_PROGRESS = "ALLOCATION_IN_PROGRESS"
    ALLOCATED = "ALLOCATED"
    UNDER_REVIEW = "UNDER_REVIEW"
    PARTIALLY_APPROVED = "PARTIALLY_APPROVED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    LOCKED = "LOCKED"
    REVISED = "REVISED"

    ALL = (DRAFT, ALLOCATION_IN_PROGRESS, ALLOCATED, UNDER_REVIEW,
           PARTIALLY_APPROVED, APPROVED, REJECTED, LOCKED, REVISED)
    #: States in which the country lines may still be typed into.
    EDITABLE = (DRAFT, ALLOCATED, REJECTED)
    #: States in which the version is read-only for everyone.
    FROZEN = (APPROVED, LOCKED)


class TargetLevel:
    """The nodes a country target is allocated across.

    Company is the root — the design calls it "Country", and for a single-company
    plan the two are the same node. Business unit and sales line are absent on
    purpose: the plan already fixes both, so every node beneath it shares them
    and a level that never varies is not a level.

    Customer is the leaf, and is the one level that is not an organisational
    dimension. It sits below sub-territory because that is what
    ``dim_customer.sub_territory_code`` states.
    """

    COMPANY = "company"
    ZONE = "zone"
    REGION = "region"
    AREA = "area"
    UNIT = "unit"
    TERRITORY = "territory"
    SUB_TERRITORY = "sub_territory"
    CUSTOMER = "customer"

    #: Top-down. Index is depth, so a parent is always the entry before its child.
    ORDERED = (COMPANY, ZONE, REGION, AREA, UNIT, TERRITORY, SUB_TERRITORY,
               CUSTOMER)
    ALL = ORDERED

    #: level -> the ``LEVEL_BINDINGS`` code field it filters on. Customer maps to
    #: ``customer_code``, which is a fact column rather than an org level.
    CODE_FIELD = {
        COMPANY: "company_code",
        ZONE: "zone_code",
        REGION: "region_code",
        AREA: "area_code",
        UNIT: "unit_code",
        TERRITORY: "territory_code",
        SUB_TERRITORY: "sub_territory_code",
        CUSTOMER: "customer_code",
    }

    @classmethod
    def parent_of(cls, level: str) -> str | None:
        """The level immediately above, or ``None`` for the root."""
        index = cls.ORDERED.index(level)
        return cls.ORDERED[index - 1] if index else None


class RevisionStatus:
    """A revision request's outcome.

    ``ESCALATED`` is not a synonym for pending: it records that the request
    exceeded the requester's adjustment limit and was routed *past* the approver
    it would normally have gone to. Losing that distinction would make the
    adjustment limit unauditable.
    """

    PENDING = "PENDING"
    ESCALATED = "ESCALATED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"

    ALL = (PENDING, ESCALATED, APPROVED, REJECTED)
    OPEN = (PENDING, ESCALATED)


class ApprovalAction:
    """What an approver did. Stored, never inferred from a status."""

    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    LOCKED = "LOCKED"

    ALL = (SUBMITTED, APPROVED, REJECTED, REVISION_REQUESTED, LOCKED)


class TargetPlan(Base, TimestampMixin):
    """One planning scope: a financial year, a period and an org slice.

    The five scope columns are all required and together are unique. That is the
    module's central promise made structural: there is exactly one plan per
    scope, so "the FY 2026-27 Q1 target for SL001" names one thing, and a
    revised figure is a new *version* of it rather than a second plan nobody can
    tell apart from the first.

    ``target_period`` is a label — ``FY`` or ``Q1``..``Q4`` — not a date range.
    Which months it covers is derived from ``financial_year`` and the configured
    ``FINANCIAL_YEAR_START_MONTH`` through :mod:`app.etl.calendar`, because the
    financial year is configuration and storing its consequences here would put
    a second copy of it somewhere it could drift.
    """

    __tablename__ = "target_plan"

    plan_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                         autoincrement=True)
    #: Human-facing identifier, e.g. ``TP-2026-001``. Generated on creation and
    #: never reused; it is what an audit row and an approval notice name.
    plan_code: Mapped[str] = mapped_column(String(32), nullable=False,
                                           unique=True)
    #: ``FY 2026-27`` — the label
    #: :meth:`app.etl.calendar.FinancialYearConfig.label` produces, so a plan and
    #: a ``dim_date`` row spell the year identically.
    financial_year: Mapped[str] = mapped_column(String(32), nullable=False)
    #: ``FY`` for the whole year, or ``Q1``..``Q4``.
    target_period: Mapped[str] = mapped_column(String(16), nullable=False)

    company_code: Mapped[str] = mapped_column(CODE, nullable=False)
    bu_code: Mapped[str] = mapped_column(CODE, nullable=False)
    sales_line_code: Mapped[str] = mapped_column(CODE, nullable=False)

    #: The financial years whose actual sales the allocation engine reads.
    #: Stated on the plan rather than assumed, because "two years back" is a
    #: planning decision and a plan built on different years must say so.
    basis_financial_years: Mapped[str | None] = mapped_column(String(128))

    status: Mapped[str] = mapped_column(String(32), nullable=False,
                                        default=TargetStatus.DRAFT)
    created_by: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("financial_year", "target_period", "company_code",
                         "bu_code", "sales_line_code",
                         name="uq_target_plan_scope"),
        Index("ix_target_plan_financial_year", "financial_year"),
        Index("ix_target_plan_status", "status"),
        Index("ix_target_plan_company", "company_code"),
    )


class TargetVersion(Base, TimestampMixin):
    """One revision of a plan's numbers, and its place in the workflow.

    **At most one version per plan is current, and the database says so.**
    :attr:`current_plan_id` holds this version's ``plan_id`` while the version is
    current and NULL otherwise, under a plain unique constraint. NULLs are
    distinct in a unique constraint on both SQLite and PostgreSQL, so any number
    of superseded versions coexist while only one can claim the plan. This is
    the same device ``agent_term_alias.active_key`` uses in revision 0025, and
    for the same reason: a partial unique index would express it more directly
    and is PostgreSQL-only.

    Making it structural matters here more than it does there. "An approved
    target is never overwritten" is the promise this module exists to keep, and
    two rows both believing they are the current V3 is exactly how a report and
    an approval would come to disagree about a number.
    """

    __tablename__ = "target_version"

    version_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                            autoincrement=True)
    #: RESTRICT, not CASCADE: a plan whose versions carry approvals and audit
    #: rows is not something a delete should be able to take with it.
    plan_id: Mapped[int] = mapped_column(
        FK_TYPE, ForeignKey("target_plan.plan_id", ondelete="RESTRICT"),
        nullable=False,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False,
                                        default=TargetStatus.DRAFT)
    #: This version's ``plan_id`` while current, NULL otherwise. See the class
    #: docstring — it is a uniqueness device, not a second foreign key to read.
    current_plan_id: Mapped[int | None] = mapped_column(FK_TYPE)
    #: Why this version exists. Required by the API on every version after the
    #: first; nullable here because V1 has no predecessor to explain.
    reason: Mapped[str | None] = mapped_column(Text)

    created_by: Mapped[str | None] = mapped_column(String(64))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The import batch the lock wrote into ``fact_target`` under, so the rows a
    #: version produced can be found — and rolled back — as one batch.
    locked_batch_id: Mapped[str | None] = mapped_column(String(36))

    __table_args__ = (
        UniqueConstraint("plan_id", "version_no",
                         name="uq_target_version_plan_no"),
        UniqueConstraint("current_plan_id", name="uq_target_version_current"),
        Index("ix_target_version_plan", "plan_id"),
        Index("ix_target_version_status", "status"),
    )


class TargetCountryLine(Base, TimestampMixin):
    """The typed country target: one Target Volume per material, per version.

    The only figure a person enters in this whole module. Quantity and value are
    derived from ``dim_material`` and are deliberately absent — see the module
    docstring.
    """

    __tablename__ = "target_country_line"

    line_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                         autoincrement=True)
    #: CASCADE: a country line has no meaning apart from its version, and a
    #: version that has been approved is never deleted in the first place.
    version_id: Mapped[int] = mapped_column(
        FK_TYPE, ForeignKey("target_version.version_id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Resolved against the Material Master. Not a database foreign key, for the
    #: reason every material-side reference in this schema is not: the master
    #: arrives from its own extract, and a constraint would fail the whole
    #: version on a material it has not received yet. The API checks it and can
    #: name the offending row.
    material_code: Mapped[str] = mapped_column(CODE, nullable=False)
    target_volume: Mapped[float] = mapped_column(QUANTITY, nullable=False,
                                                 default=0)
    updated_by: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("version_id", "material_code",
                         name="uq_target_country_line"),
        Index("ix_target_country_line_version", "version_id"),
        Index("ix_target_country_line_material", "material_code"),
    )


class TargetAllocation(Base, TimestampMixin):
    """One generated figure: a node, a material and a month.

    **Every row is monthly, and there is no period-total row beside it.** A
    node's total for a period is a sum over its months, and a level's total is a
    sum over its children — so a parent cannot disagree with its own parts,
    because it is not stored separately from them. Reconciliation is therefore
    something the module *computes*; a stored balance can go stale, and a stale
    one is worse than none because it looks authoritative.

    Three volumes, all kept:

    ``system_volume``
        What the allocation engine generated. Never overwritten by a revision —
        "what the engine said" stays answerable after every adjustment.
    ``current_volume``
        What stands now: the system figure plus every approved revision.
    ``approved_volume``
        What an approver signed off, set when the node is approved and NULL
        until then. NULL means "not yet approved", which is not the same as a
        target of zero.
    """

    __tablename__ = "target_allocation"

    allocation_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                               autoincrement=True)
    version_id: Mapped[int] = mapped_column(
        FK_TYPE, ForeignKey("target_version.version_id", ondelete="CASCADE"),
        nullable=False,
    )
    #: One of :data:`TargetLevel.ORDERED`.
    level: Mapped[str] = mapped_column(String(24), nullable=False)
    node_code: Mapped[str] = mapped_column(CODE, nullable=False)
    #: The node one level up. NULL only on the company root. Stored rather than
    #: re-derived from the master on every read: an allocation is a record of
    #: how volume was distributed *at generation time*, and a later master
    #: change that moved a territory to another region must not silently
    #: reshape a target that has already been approved.
    parent_level: Mapped[str | None] = mapped_column(String(24))
    parent_code: Mapped[str | None] = mapped_column(CODE)

    material_code: Mapped[str] = mapped_column(CODE, nullable=False)
    #: ``YYYY-MM``, spelled as ``fact_target.target_month`` spells it.
    target_month: Mapped[str] = mapped_column(String(16), nullable=False)

    system_volume: Mapped[float] = mapped_column(QUANTITY, nullable=False,
                                                 default=0)
    current_volume: Mapped[float] = mapped_column(QUANTITY, nullable=False,
                                                  default=0)
    approved_volume: Mapped[float | None] = mapped_column(QUANTITY)
    status: Mapped[str] = mapped_column(String(32), nullable=False,
                                        default=TargetStatus.ALLOCATED)

    __table_args__ = (
        UniqueConstraint("version_id", "level", "node_code", "material_code",
                         "target_month", name="uq_target_allocation_node"),
        # The four questions this table is asked, in the order it is asked
        # them: one level of the tree, the children of one node, one material's
        # whole distribution, and one month of the plan.
        Index("ix_target_allocation_version_level", "version_id", "level"),
        Index("ix_target_allocation_parent", "version_id", "parent_code"),
        Index("ix_target_allocation_material", "version_id", "material_code"),
        Index("ix_target_allocation_month", "version_id", "target_month"),
        Index("ix_target_allocation_status", "status"),
    )


class TargetRevision(Base, TimestampMixin):
    """A request to change one allocated figure, and what became of it.

    Three volumes, deliberately three columns. The design this implements states
    the rule outright: a revision keeps the system value, the requested value
    and the approved value, and requires a reason. Collapsing any two of them
    would make "did we agree to what was asked?" unanswerable.

    ``reason`` is NOT NULL. A revision with no stated reason is the thing this
    table exists to prevent.
    """

    __tablename__ = "target_revision"

    revision_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                             autoincrement=True)
    #: The **anchor row**: the lowest month of the lowest material at the node
    #: this revision questions. A revision is about a node's figure for the
    #: period, which spans every month and material — so the node is named
    #: outright below, and this column exists for its CASCADE. An allocation run
    #: replaces every row it produced, and a pending request to change a figure
    #: that no longer exists is not a request anybody can act on.
    allocation_id: Mapped[int] = mapped_column(
        FK_TYPE,
        ForeignKey("target_allocation.allocation_id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The node, stated rather than reached through the anchor row. "Which
    #: revisions are open on this territory?" is the commonest question asked of
    #: this table, and it should not depend on which row happened to be picked.
    #: Nullable only because revision 0030 added them to an existing table.
    version_id: Mapped[int | None] = mapped_column(FK_TYPE)
    level: Mapped[str | None] = mapped_column(String(24))
    node_code: Mapped[str | None] = mapped_column(CODE)
    #: NULL revises the node's whole figure; a stated code revises one
    #: material's share of it. The same meaning it carries on
    #: :class:`TargetAdjustment`.
    material_code: Mapped[str | None] = mapped_column(CODE)

    system_volume: Mapped[float] = mapped_column(QUANTITY, nullable=False)
    requested_volume: Mapped[float] = mapped_column(QUANTITY, nullable=False)
    approved_volume: Mapped[float | None] = mapped_column(QUANTITY)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(String(24), nullable=False,
                                        default=RevisionStatus.PENDING)
    #: The change as a percentage of ``system_volume``, stored because the
    #: adjustment limit that routed this request was applied to *this* number.
    #: Recomputing it later from a system volume that a re-run has changed would
    #: not reproduce the routing decision it explains.
    change_percent: Mapped[float | None] = mapped_column(PERCENT)
    #: Set when the change exceeded the requester's limit: the role it was
    #: escalated to instead of the approver it would normally have gone to.
    escalated_to_role: Mapped[str | None] = mapped_column(String(32))

    requested_by: Mapped[str | None] = mapped_column(String(64))
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_target_revision_allocation", "allocation_id"),
        Index("ix_target_revision_status", "status"),
        Index("ix_target_revision_requested_by", "requested_by"),
        Index("ix_target_revision_version", "version_id"),
        Index("ix_target_revision_node", "version_id", "level", "node_code"),
    )


class TargetApproval(Base):
    """One act of approval, rejection or submission. Append-only.

    No ``updated_at``: nothing here is ever updated. An approver who changes
    their mind adds a row, and both rows stay — which is the only way the
    sequence a target actually travelled can be read back.
    """

    __tablename__ = "target_approval"

    approval_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                             autoincrement=True)
    version_id: Mapped[int] = mapped_column(
        FK_TYPE, ForeignKey("target_version.version_id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The node acted on. Both NULL when the act covers the whole version — a
    #: submission or a lock.
    level: Mapped[str | None] = mapped_column(String(24))
    node_code: Mapped[str | None] = mapped_column(CODE)

    action: Mapped[str] = mapped_column(String(24), nullable=False)
    #: The approver's role and its place in the matrix *at the time they acted*.
    #: A matrix reordered afterwards must not rewrite the order a past approval
    #: went through.
    actor_role: Mapped[str | None] = mapped_column(String(32))
    approval_sequence: Mapped[int | None] = mapped_column(Integer)
    actor: Mapped[str | None] = mapped_column(String(64))
    comment: Mapped[str | None] = mapped_column(Text)
    acted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_target_approval_version", "version_id"),
        Index("ix_target_approval_node", "version_id", "level", "node_code"),
        Index("ix_target_approval_acted_at", "acted_at"),
    )


class TargetApprovalMatrix(Base, TimestampMixin):
    """Which role approves at which level, in what order, within what limit.

    Configuration, owned by the administrator, and the reason approval is not
    hard-coded against :class:`app.database.models_ai.Role`: the *roles* are
    fixed, but who signs off on a target and in what order is a decision each
    deployment makes for itself.

    ``approval_sequence`` NULL means the role sits outside the approval chain
    entirely. That is not the same as sequence 0, and it is a real case: the
    SFE / MIS administrator configures the run and never signs off on a number.

    ``adjustment_limit_percent`` NULL means unlimited, which only Management
    holds by default. Zero would mean "may not change a figure at all" and is a
    different, valid setting — so the two are kept distinguishable.
    """

    __tablename__ = "target_approval_matrix"

    matrix_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                           autoincrement=True)
    #: One row per role. A role approves at one level; two rows would leave the
    #: sequence ambiguous.
    role: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    #: One of :data:`TargetLevel.ORDERED`.
    hierarchy_level: Mapped[str] = mapped_column(String(24), nullable=False)
    approval_sequence: Mapped[int | None] = mapped_column(Integer)

    can_edit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_approve: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_reject: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_revise: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    adjustment_limit_percent: Mapped[float | None] = mapped_column(PERCENT)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_target_approval_matrix_sequence", "approval_sequence"),
        Index("ix_target_approval_matrix_active", "is_active"),
    )


class TargetAudit(Base):
    """Every action taken on a target, with its old and new value and its reason.

    Append-only, written by the backend at the moment of the action. Nothing in
    the interface can edit or remove a row, and no endpoint offers a write that
    is not a side effect of the action being audited.

    ``old_value`` and ``new_value`` are text because what changed is not always
    a number: a status moves from ``Approved`` to ``Locked``, a version from
    ``V2`` to ``V3``. Typing the column to the commonest case would force the
    others into a lie.

    Both foreign keys are SET NULL. The audit trail outlives what it describes:
    losing the link is acceptable, losing the record of what somebody did is not.
    """

    __tablename__ = "target_audit"

    audit_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                          autoincrement=True)
    plan_id: Mapped[int | None] = mapped_column(
        FK_TYPE, ForeignKey("target_plan.plan_id", ondelete="SET NULL"))
    version_id: Mapped[int | None] = mapped_column(
        FK_TYPE, ForeignKey("target_version.version_id", ondelete="SET NULL"))

    action: Mapped[str] = mapped_column(String(48), nullable=False)
    #: Who acted, as a name and a role. Stored rather than joined to ``users``:
    #: an audit row must stay readable after the account is renamed or removed,
    #: and it must record the role held *then*, not the role held now.
    actor: Mapped[str | None] = mapped_column(String(64))
    actor_role: Mapped[str | None] = mapped_column(String(32))
    #: What was acted on, spelled the way the screen spelled it —
    #: ``CUS0011 · MAT-1001``, ``REG001 · Dhaka``, ``FY 2026-27 V3``.
    node_label: Mapped[str | None] = mapped_column(String(200))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_target_audit_version", "version_id"),
        Index("ix_target_audit_plan", "plan_id"),
        Index("ix_target_audit_occurred_at", "occurred_at"),
        Index("ix_target_audit_action", "action"),
    )


class TargetAdjustment(Base, TimestampMixin):
    """A management instruction to move volume to one node from its siblings.

    **An input to the engine, not an edit of its output.** Stored against the
    version and re-applied on every allocation run, so re-running after loading
    more sales keeps management's decisions rather than silently discarding
    them. That is the difference between this and ``target_revision``, which is
    a *request* travelling through the approval chain after allocation.

    ``adjustment_volume`` is absolute and signed - ``+500``, ``-1200`` - never a
    percentage. A uniform percentage applied to every child and re-normalised is
    a mathematical no-op, so the only form of adjustment that means anything is
    one naming a node and a quantity.

    ``material_code`` is optional: NULL adjusts every material at that node, and
    a stated one adjusts just that material. A specific instruction outranks a
    general one when both exist, so "raise Dhaka by 500, except Glyfon by 800"
    reads the way it is written.
    """

    __tablename__ = "target_adjustment"

    adjustment_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                               autoincrement=True)
    version_id: Mapped[int] = mapped_column(
        FK_TYPE, ForeignKey("target_version.version_id", ondelete="CASCADE"),
        nullable=False,
    )
    #: One of :data:`TargetLevel.ORDERED`, never ``company``: adjusting the root
    #: would be changing the country target, which is a different act with its
    #: own screen and its own approval path.
    level: Mapped[str] = mapped_column(String(24), nullable=False)
    node_code: Mapped[str] = mapped_column(CODE, nullable=False)
    material_code: Mapped[str | None] = mapped_column(CODE)

    adjustment_volume: Mapped[float] = mapped_column(QUANTITY, nullable=False)
    #: NOT NULL. An unexplained adjustment to the number a sales force is
    #: measured on is the thing this column exists to prevent.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    adjusted_by: Mapped[str | None] = mapped_column(String(64))
    adjusted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # One standing instruction per node and material. A second would leave
        # the engine to guess which of two contradictory adjustments to apply.
        UniqueConstraint("version_id", "level", "node_code", "material_code",
                         name="uq_target_adjustment_node"),
        Index("ix_target_adjustment_version", "version_id"),
        Index("ix_target_adjustment_node", "version_id", "level", "node_code"),
    )


#: The default approval chain, seeded by revision 0027 and editable afterwards.
#:
#: ``(role, level, sequence, edit, approve, reject, revise, limit)``.
#:
#: **Sequence 1 is the bottom of the hierarchy, not the top.** A target is
#: allocated downwards and reviewed *upwards*: the sales officer sees the
#: sub-territory figure first and is the one who knows whether it is wrong, and
#: Management signs last, once every level beneath it has. Revision 0027 seeded
#: this list the other way up — Management at 1 — which read as the CEO
#: approving a target before the regional manager had looked at it; revision
#: 0030 corrects it, and only where an administrator has not already reordered
#: the chain themselves.
#:
#: The two administrator roles configure the run and hold **no** sequence: NULL
#: means outside the chain entirely, which is a different thing from being first
#: in it. An administrator who is also a business approver is granted the
#: business role that says so.
DEFAULT_APPROVAL_MATRIX: tuple[
    tuple[str, str, int | None, bool, bool, bool, bool, float | None], ...
] = (
    ("SALES_OFFICER", TargetLevel.SUB_TERRITORY, 1, False, False, False, True, 0.0),
    ("TERRITORY_MANAGER", TargetLevel.TERRITORY, 2, True, False, False, True, 5.0),
    ("UNIT_MANAGER", TargetLevel.UNIT, 3, True, True, False, False, 5.0),
    ("AREA_MANAGER", TargetLevel.AREA, 4, True, True, True, False, 10.0),
    ("REGIONAL_MANAGER", TargetLevel.REGION, 5, True, True, True, True, 10.0),
    ("ZONE_MANAGER", TargetLevel.ZONE, 6, True, True, True, True, 15.0),
    ("BUSINESS_UNIT_HEAD", TargetLevel.COMPANY, 7, True, True, True, True, 20.0),
    ("MANAGEMENT", TargetLevel.COMPANY, 8, True, True, True, True, None),
    ("SUPER_ADMIN", TargetLevel.COMPANY, None, True, False, False, False, None),
    ("ADMIN", TargetLevel.COMPANY, None, True, False, False, False, None),
)


__all__ = [
    "PERCENT",
    "TargetStatus",
    "TargetLevel",
    "RevisionStatus",
    "ApprovalAction",
    "TargetPlan",
    "TargetVersion",
    "TargetCountryLine",
    "TargetAllocation",
    "TargetRevision",
    "TargetApproval",
    "TargetApprovalMatrix",
    "TargetAudit",
    "AllocationJobStatus",
    "TargetAllocationJob",
    "TargetAdjustment",
    "DEFAULT_APPROVAL_MATRIX",
]


class AllocationJobStatus:
    """Where one allocation run has got to.

    Separate from :class:`TargetStatus`, which describes the *version*. The two
    answer different questions and move at different times: a job can fail while
    its version stays exactly where it was, and a version reaches ``ALLOCATED``
    only once a job has completed *and* reconciled.
    """

    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    #: Allocated and reconciled, but something a planner should read happened on
    #: the way - a seasonal fallback, a node split evenly for want of history, a
    #: level shallower than customer. Its own status rather than a flag on
    #: COMPLETED, because a run nobody needs to look at and a run somebody does
    #: are different things to a person scanning a list of twenty.
    COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    #: The engine ran, found no sales history and correctly refused to invent an
    #: allocation. Its own state rather than a failure: nothing went wrong, the
    #: data the engine needs has not been loaded yet, and a planner reading
    #: "Failed" would go looking for a bug that is not there.
    NO_HISTORY = "NO_HISTORY"

    ALL = (QUEUED, PROCESSING, COMPLETED, COMPLETED_WITH_WARNINGS, FAILED,
           CANCELLED, NO_HISTORY)
    #: States in which a job is still expected to move on its own.
    OPEN = (QUEUED, PROCESSING)
    #: Runs that produced a usable allocation.
    SUCCEEDED = (COMPLETED, COMPLETED_WITH_WARNINGS)
    TERMINAL = (COMPLETED, COMPLETED_WITH_WARNINGS, FAILED, CANCELLED,
                NO_HISTORY)


class TargetAllocationJob(Base, TimestampMixin):
    """One run of the allocation engine, and how far it got.

    Durable, unlike the upload centre's in-memory progress, and it can afford to
    be: the expensive half of an allocation is pure *reading*, so progress can be
    written to this row throughout without a second connection blocking against a
    write lock. Only the final persist holds one, and by then the stage is
    already ``FINALIZATION``.

    ``progress_percent`` and ``current_stage`` are therefore the live values a
    poll returns, not a last-known approximation.
    """

    __tablename__ = "target_allocation_job"

    job_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                        autoincrement=True)
    #: The uuid a client polls by. A surrogate integer would work equally well
    #: for lookup, but this is handed to a browser and appears in a URL, and a
    #: guessable sequential id would let one planner watch another's run.
    job_uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    plan_id: Mapped[int] = mapped_column(
        FK_TYPE, ForeignKey("target_plan.plan_id", ondelete="CASCADE"),
        nullable=False,
    )
    version_id: Mapped[int] = mapped_column(
        FK_TYPE, ForeignKey("target_version.version_id", ondelete="CASCADE"),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(String(24), nullable=False,
                                        default=AllocationJobStatus.QUEUED)
    current_stage: Mapped[str | None] = mapped_column(String(32))
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False,
                                                  default=0)
    rows_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: What the run expects to write, known once the tree and the months are
    #: read. NULL until then — a total of 0 would read as "nothing to do".
    total_rows: Mapped[int | None] = mapped_column(Integer)

    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)

    #: The factor settings this run used, kept so a past allocation can be
    #: explained. An allocation whose rules are unknown is a number nobody can
    #: defend, and the settings are the difference between "the engine said so"
    #: and "these three factors at these weights said so".
    settings: Mapped[dict | None] = mapped_column(JSON_TYPE)
    #: The reconciliation verdict, stored so the result survives the process
    #: that computed it.
    result: Mapped[dict | None] = mapped_column(JSON_TYPE)

    #: What the pre-flight projected before the run started, kept beside what
    #: was actually written. A run refused for exceeding the ceiling has a
    #: projection and no rows, which is precisely the pair a planner needs in
    #: order to know how much to narrow by.
    projected_rows: Mapped[int | None] = mapped_column(Integer)
    #: The deepest level this run actually reached. Recorded rather than assumed
    #: to be customer: a Customer Master with no sub-territory mapping allocates
    #: to sub-territory, which is a real allocation and must be labelled as one.
    allocation_level: Mapped[str | None] = mapped_column(String(24))
    #: Warnings are not errors. A seasonal fallback or an evenly-split node is
    #: worth reading and does not make the run wrong, so it is counted
    #: separately from ``error_count``.
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: How many sales rows the basis years held. Zero is the whole explanation
    #: for a ``NO_HISTORY`` run, and keeping it here means the run history can
    #: show it without re-querying two years of sales per line.
    sales_rows_found: Mapped[int | None] = mapped_column(Integer)

    requested_by: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_target_allocation_job_version", "version_id"),
        Index("ix_target_allocation_job_plan", "plan_id"),
        Index("ix_target_allocation_job_status", "status"),
        Index("ix_target_allocation_job_created", "created_at"),
    )
