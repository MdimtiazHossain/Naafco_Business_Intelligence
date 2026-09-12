"""Credit Control gains the organisational hierarchy the SPL extract states.

Revision ID: 0038_credit_hierarchy
Revises: 0037_demarcation_level_order
Create Date: 2026-09-11

Nine resolved organisational keys and their indexes on ``fact_credit_invoice``,
the columns the SPL receivables extract carries that ``stg_credit_invoice`` had
nowhere to put, and **one constraint removed**. No existing column is altered and
no row is read or written.

That last part is why this is not the purely additive revision it started as, and
why the SQLite view-capture dance from 0020 and 0022 *does* apply. 0031 declared
an invoice unique within its company; the SPL extract falsifies it, widening the
constraint only moves the failure from 290 rows to 17, and a constraint that
forbids what the pipeline's repeat-numbering exists to permit turns a documented
behaviour into an aborted load. ``business_key`` is the grain and already unique.
See ``_OLD_GRAIN`` for the measurement and the cost.

**Why the fact gains surrogate keys and staging gains text.** Staging keeps the
source shape, which for this file includes two columns that are wrong: measured
over all 15,576 rows, ``Region_Code`` holds the *area* code on every one, and
none of the file's 13 zone codes or 16 region codes exists in ``dim_zone`` or
``dim_region`` — while area, unit, territory and sub-territory resolve
completely (16/16, 20/20, 154/154, 267/267). The fact therefore stores what the
master hierarchy derives from the deepest trustworthy level, which is the rule
``fact_target`` already follows, and the file's own two columns stay in staging
where a record of what arrived belongs.

**What this revision does not do.** It adds no view. ``vw_credit_invoice_detail``
still stops at the customer's sub-territory, so the scope refusals in
``reporting.credit`` and ``ai.tools`` remain load-bearing until the view is
rebuilt — removing them before the view carries the hierarchy would serve a
region-scoped caller the whole company's receivables.

``od`` and ``maturity`` reach staging and **no fact column**, deliberately. They
are the source's overdue split frozen at the date the extract was taken, and a
stored overdue figure is wrong the next morning while still looking
authoritative — the same reason ``days_overdue``, ``aging_bucket`` and
``credit_status`` are not columns either. The load compares its own derivation
against them and reports the disagreement.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0038_credit_hierarchy"
down_revision: Union[str, None] = "0037_demarcation_level_order"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The resolved chain, in hierarchy order.
#:
#: Spelled out rather than imported from ``models_warehouse.FullOrgDimensionMixin``
#: for the reason every seed in this chain is spelled out: a migration has to keep
#: producing the same schema after the mixin it was written against has moved on.
#:
#: **Added without a database foreign key, and that is deliberate rather than an
#: oversight.** SQLite cannot ``ALTER TABLE ADD COLUMN`` with a constraint at all;
#: the only way to attach one is Alembic's batch mode, which copies the table and
#: moves it — and SQLite re-validates *every stored view* against the schema while
#: that rewrite is half-done, which is the capture-and-recreate dance revisions
#: 0020 and 0022 had to perform. Paying that on a purely additive revision, with
#: ``vw_credit_invoice_detail`` reading the table being rewritten, buys a
#: constraint on one dialect at the cost of the most fragile operation in this
#: chain. No revision here has ever added a constrained column post-hoc; every
#: ``add_column`` in 0005 through 0037 adds a plain one.
#:
#: What enforces the reference instead is the loader: ``resolve_org`` produces an
#: id only for a code it found in the master, and a code it cannot find becomes a
#: rejected row rather than a dangling key. ``fact_sales`` carries real
#: constraints because it was *created* with them, and that asymmetry is real —
#: rebuilding this fact is what would close it, and rebuilding a fact to add a
#: constraint is not a trade this revision is willing to make.
_ORG_KEYS: tuple[str, ...] = (
    "company_id",
    "business_unit_id",
    "sales_line_id",
    "zone_id",
    "region_id",
    "area_id",
    "unit_id",
    "territory_id",
    "sub_territory_id",
)

#: The text columns staging gains, all nullable: this extract states them and the
#: Credit Invoice extract does not, and one dataset spec serves both.
_STAGING_COLUMNS: tuple[str, ...] = (
    "payment_terms",
    "zone_code",
    "region_code",
    "area_code",
    "unit_code",
    "territory_code",
    "sub_territory_code",
    "net_due_date",
    "source_od",
    "source_maturity",
)

#: ``BIGINT`` on PostgreSQL and ``INTEGER`` on SQLite, matching every other
#: surrogate key in this schema.
FK = sa.BigInteger().with_variant(sa.Integer, "sqlite")

#: The grain constraint 0031 wrote, and which this revision **removes without
#: replacing**.
#:
#: 0031 declared an invoice unique within its company. The SPL extract falsifies
#: that twice over: 285 ``Assignment`` values repeat and 268 span more than one
#: customer, so ``(company, invoice_no)`` collides on 575 rows; and widening it to
#: ``(company, customer, invoice_no)`` — which is where the business key went —
#: still leaves 17 rows where one customer carries the same Assignment twice with
#: different dates and different amounts.
#:
#: So the narrower constraint was tried first and is not what ships. Widening it
#: only moved the failure from 290 rows to 17, and the 17 are not a data-entry
#: slip this platform gets to rule on.
#:
#: **What actually enforces the grain is ``business_key``**, which is unique and
#: which this codebase already calls the grain. The pipeline numbers a repeated
#: key ``…#2`` precisely so a genuine repeat loads rather than being lost, and a
#: table constraint forbidding what that numbering permits does not add safety —
#: it turns a documented, configurable behaviour into a crash. That is what it
#: did here: the load did not reject 17 rows, it aborted after writing none.
#:
#: The cost is real and is named rather than hidden: a *loader* bug that built
#: the key wrongly would no longer be caught by the database. It would still be
#: caught by ``uq_fact_credit_invoice_business_key``, one layer later.
_OLD_GRAIN = "uq_fact_credit_invoice_company_invoice"

#: The three views over the fact, in creation order — the two aggregates read the
#: detail view, so it has to exist first.
_VIEWS: tuple[str, ...] = (
    "vw_credit_invoice_detail",
    "vw_customer_credit_exposure",
    "vw_credit_aging",
)


def _revision_0031():
    """Load 0031 by path so its view bodies are not transcribed a second time.

    The same device 0020 uses to rebuild ``vw_sales_detail`` from the revision
    that authored it. A fourth copy of these bodies is a fourth thing to drift,
    and the rule in this chain is that a view is rebuilt from whichever revision
    last authored it.
    """
    import importlib.util
    from pathlib import Path

    name = "0031_credit_control"
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"Could not load migration {name}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _drop_grain_constraint(dialect: str) -> None:
    """Remove the company+invoice uniqueness. See ``_OLD_GRAIN`` for why.

    PostgreSQL alters the constraint in place, so the views over the table are
    untouched. SQLite cannot alter a constraint at all — Alembic's batch mode
    copies the table and moves it — and SQLite re-validates **every stored view**
    against the schema during that rewrite, so the three views come down first
    and are rebuilt from 0031 afterwards. That is the same dance 0020 and 0022
    perform, and it is why this is the one part of an otherwise additive revision
    that touches anything existing.
    """
    if dialect == "sqlite":
        for view in reversed(_VIEWS):
            op.execute(f"DROP VIEW IF EXISTS {view}")

    with op.batch_alter_table("fact_credit_invoice") as batch:
        batch.drop_constraint(_OLD_GRAIN, type_="unique")

    if dialect == "sqlite":
        revision_0031 = _revision_0031()
        op.execute(
            "CREATE VIEW vw_credit_invoice_detail AS "
            f"{revision_0031._detail_view(dialect)}"
        )
        op.execute(
            "CREATE VIEW vw_customer_credit_exposure AS "
            f"{revision_0031.CUSTOMER_CREDIT_EXPOSURE}"
        )
        op.execute(f"CREATE VIEW vw_credit_aging AS {revision_0031.CREDIT_AGING}")


def upgrade() -> None:
    for column in _STAGING_COLUMNS:
        op.add_column(
            "stg_credit_invoice",
            sa.Column(column, sa.String(64), nullable=True),
        )

    for column in _ORG_KEYS:
        op.add_column("fact_credit_invoice", sa.Column(column, FK, nullable=True))
    # Indexed because every hierarchy report groups or filters on them. Zone and
    # region are indexed like the rest even though the *file's* versions of those
    # two are distrusted: what is indexed here is the master-derived key, which is
    # exactly as trustworthy as the level below it.
    for column in _ORG_KEYS:
        op.create_index(
            f"ix_fact_credit_invoice_{column}", "fact_credit_invoice", [column]
        )

    _drop_grain_constraint(op.get_bind().dialect.name)


def downgrade() -> None:
    """Remove the hierarchy. The invoices themselves are untouched.

    Dropping a resolved key loses nothing that cannot be recomputed: every one of
    them is derived from the master hierarchy above a code the row still carries,
    so a re-import rebuilds them exactly. That is what makes this direction safe
    where most in this chain are not.
    """
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for view in reversed(_VIEWS):
            op.execute(f"DROP VIEW IF EXISTS {view}")
    with op.batch_alter_table("fact_credit_invoice") as batch:
        batch.create_unique_constraint(_OLD_GRAIN, ["company_code", "invoice_no"])
    if dialect == "sqlite":
        revision_0031 = _revision_0031()
        op.execute("CREATE VIEW vw_credit_invoice_detail AS "
                   f"{revision_0031._detail_view(dialect)}")
        op.execute("CREATE VIEW vw_customer_credit_exposure AS "
                   f"{revision_0031.CUSTOMER_CREDIT_EXPOSURE}")
        op.execute(f"CREATE VIEW vw_credit_aging AS {revision_0031.CREDIT_AGING}")

    for column in _ORG_KEYS:
        op.drop_index(f"ix_fact_credit_invoice_{column}", table_name="fact_credit_invoice")
        op.drop_column("fact_credit_invoice", column)
    for column in _STAGING_COLUMNS:
        op.drop_column("stg_credit_invoice", column)
