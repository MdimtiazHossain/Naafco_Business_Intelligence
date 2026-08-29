"""Credit Control: the credit invoice fact, its staging table and three views.

Revision ID: 0031_credit_control
Revises: 0030_target_revision_node
Create Date: 2026-08-29

**This reverses a decision, deliberately.** Revision 0020 removed
``fact_outstanding`` and ``fact_collection`` and every view over them, because no
receivables extract was ever produced and reporting on money nothing could
measure was worse than saying the platform did not track it. That reasoning was
right and its premise has changed: the Credit Invoice file exists, states the
invoice, its terms and every amount posted against it, so what is outstanding is
now *read* rather than estimated. 0020's ``downgrade()`` was the closest thing to
a blueprint — ``days_overdue`` and ``aging_bucket`` had this shape there — but
nothing is restored from it, because the grain is different: that table held a
balance somebody else had calculated, and this one holds an invoice.

**One table, not two.** 0020 removed a collection fact as well; this revision
does not bring one back. The source aggregates payments into a single
``payment_amount`` and a Last Payment Date, so there is nothing per-transaction
to put in such a table, and creating one would be an empty promise of a payment
history. If a transaction-level extract ever arrives it is a new table beside
this one.

**Status and aging are computed, never stored.** The fact carries
``due_date_id`` and the two stable money derivations; how late an invoice is
depends on the day you ask, so the views derive it. A ``days_overdue`` frozen at
upload would be wrong the next morning while still looking authoritative — the
same reason Target Management computes reconciliation rather than storing it —
and computing it is what lets ``as_on_date`` become a real request parameter
instead of a label over figures frozen at load time. The three expressions are
written **once** each, in ``_DAYS_OVERDUE`` / ``_CREDIT_STATUS`` /
``_AGING_BUCKET`` below, and every view and endpoint reads them from
``vw_credit_invoice_detail``, so no two surfaces can define "overdue"
differently.

**Frozen SQL, pinned against live Python.** The bucket boundaries appear here as
literal SQL and in :mod:`app.etl.credit` as Python, because a migration must
keep producing the same schema forever while the ETL's rules go on being edited.
Two copies drift, so ``test_credit_control.py`` walks every boundary day and
asserts the view and the function agree rather than trusting that they do.

**No CHECK constraints on the amounts or the credit terms.** Whether a credit
note ever arrives as a negative adjustment has not been established, and the
seven credit terms the business described are what we have been told about
rather than a guarantee about what the source will send. A row that contradicts
either expectation is *flagged* — ``data_quality_flag`` — and kept. Refusing it
would discard a real invoice on the strength of an assumption nobody has
verified.

Purely additive: no table is dropped, no column is removed, no row is read or
written. The SQLite view-capture dance revisions 0020 and 0022 needed does not
apply, because no existing table is rewritten.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031_credit_control"
down_revision: Union[str, None] = "0030_target_revision_node"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Views this revision creates, in creation order: the two aggregates read the
#: detail view, so it has to exist first. Dropped in reverse.
_VIEWS: tuple[str, ...] = (
    "vw_credit_invoice_detail",
    "vw_customer_credit_exposure",
    "vw_credit_aging",
)


def _days_between(dialect: str, later: str, earlier: str) -> str:
    """Whole days from ``earlier`` to ``later``, on either dialect.

    The one expression in this revision that cannot be written portably.
    PostgreSQL subtracts two dates and yields an integer; SQLite has no date
    arithmetic and needs ``julianday``. Both operands are ``DATE`` columns or
    ``CURRENT_DATE``, and SQLite's ``CURRENT_DATE`` is the ``YYYY-MM-DD`` text
    ``julianday`` already accepts, so no casting of the inputs is needed.

    Everything else about the views is identical on both, which is what keeps
    this to a single substitution rather than two hand-maintained view bodies.
    """
    if dialect == "sqlite":
        return f"CAST(julianday({later}) - julianday({earlier}) AS INTEGER)"
    return f"({later} - {earlier})"


def _detail_view(dialect: str) -> str:
    """``vw_credit_invoice_detail`` — one row per live invoice, fully labelled.

    Joins follow the conventions the sales and target views already set: LEFT
    JOIN to the customer **on the code**, because a surrogate key is NULL on any
    fact loaded before its master arrived and an invoice whose customer the
    master lacks must keep its figures and show its code rather than vanish from
    a total. Retired customers are not excluded — ``is_deleted`` retires a record
    from selection, not from history.

    The two date joins that matter are inner: an invoice with no invoice date and
    no due date is rejected long before it reaches the fact, so a row missing
    either is a bug rather than a case to tolerate silently.
    """
    lateness = _days_between(dialect, "CURRENT_DATE", "dd.full_date")
    return f"""
SELECT
    f.credit_invoice_id,
    f.company_code,
    f.invoice_no,
    f.plant_code,
    p.plant_name,
    f.customer_code,
    c.customer_name,
    c.sub_territory_code,
    f.invoice_date_id,
    idt.full_date AS invoice_date,
    f.credit_days,
    f.due_date_id,
    dd.full_date AS due_date,
    f.invoice_value,
    f.return_amount,
    f.net_invoice_amount,
    f.payment_amount,
    f.discount_amount,
    f.adjustment_amount,
    f.balance_amount,
    f.payment_mode,
    f.last_payment_date_id,
    lpd.full_date AS last_payment_date,
    f.clearing_date_id,
    cld.full_date AS clearing_date,
    f.clearing_document,
    f.data_quality_flag,
    CASE WHEN {lateness} > 0 THEN {lateness} ELSE 0 END AS days_overdue,
    CASE
        WHEN f.balance_amount <= 0 THEN 'CLEARED'
        WHEN {lateness} > 0 THEN 'OVER_DUE'
        ELSE 'NOT_YET_DUE'
    END AS credit_status,
    CASE
        WHEN f.balance_amount <= 0 THEN NULL
        WHEN {lateness} <= 0 THEN 'NOT_YET_DUE'
        WHEN {lateness} <= 30 THEN '1-30'
        WHEN {lateness} <= 60 THEN '31-60'
        WHEN {lateness} <= 90 THEN '61-90'
        WHEN {lateness} <= 120 THEN '91-120'
        WHEN {lateness} <= 180 THEN '121-180'
        WHEN {lateness} <= 365 THEN '181-365'
        ELSE '365+'
    END AS aging_bucket,
    f.source_system,
    f.import_batch_id,
    f.is_void
FROM fact_credit_invoice f
JOIN dim_date dd ON dd.date_id = f.due_date_id
JOIN dim_date idt ON idt.date_id = f.invoice_date_id
LEFT JOIN dim_date lpd ON lpd.date_id = f.last_payment_date_id
LEFT JOIN dim_date cld ON cld.date_id = f.clearing_date_id
LEFT JOIN dim_customer c ON c.customer_code = f.customer_code
LEFT JOIN dim_plant p ON p.plant_id = f.plant_id
WHERE f.is_void = FALSE
"""


#: ``vw_customer_credit_exposure`` — one row per customer *per company*.
#:
#: Company stays in the grain rather than being aggregated away. A customer
#: trading with two group companies has two sets of books, and collapsing them
#: would make a company-filtered report unable to answer for either. Summing
#: across companies where no filter is applied is the caller's decision and is
#: arithmetically safe: every measure here is a SUM, a COUNT, a MIN or a MAX.
#:
#: Outstanding counts only *positive* balances. An over-adjusted invoice reads as
#: Cleared, and letting its negative figure reduce the portfolio would understate
#: what is genuinely owed by the size of a data-quality problem.
CUSTOMER_CREDIT_EXPOSURE = """
SELECT
    company_code,
    customer_code,
    customer_name,
    sub_territory_code,
    COUNT(*) AS invoice_count,
    SUM(invoice_value) AS total_invoice_amount,
    SUM(net_invoice_amount) AS net_invoice_amount,
    SUM(payment_amount) AS payment_amount,
    SUM(discount_amount) AS discount_amount,
    SUM(adjustment_amount) AS adjustment_amount,
    SUM(CASE WHEN balance_amount > 0 THEN balance_amount ELSE 0 END)
        AS outstanding_amount,
    SUM(CASE WHEN credit_status = 'OVER_DUE' THEN balance_amount ELSE 0 END)
        AS overdue_amount,
    SUM(CASE WHEN credit_status = 'OVER_DUE' THEN 1 ELSE 0 END)
        AS overdue_invoice_count,
    SUM(CASE WHEN credit_status = 'CLEARED' THEN 1 ELSE 0 END)
        AS cleared_invoice_count,
    MIN(CASE WHEN credit_status = 'OVER_DUE' THEN due_date END) AS oldest_due_date,
    MAX(last_payment_date) AS last_payment_date
FROM vw_credit_invoice_detail
GROUP BY company_code, customer_code, customer_name, sub_territory_code
"""

#: ``vw_credit_aging`` — outstanding money and invoice count per bucket.
#:
#: Only open invoices appear: ``aging_bucket`` is NULL for a cleared row, so the
#: filter below is what keeps this view and the outstanding KPI countable against
#: each other.
#:
#: A bucket holding nothing produces **no row** — a view reports what is there
#: and cannot invent an empty one. The endpoint is what renders all eight in
#: order, filling the absent ones with zero, because "no invoices in 91-120" is a
#: fact the screen must state rather than a gap it should leave blank.
CREDIT_AGING = """
SELECT
    company_code,
    aging_bucket,
    COUNT(*) AS invoice_count,
    SUM(balance_amount) AS outstanding_amount,
    MIN(days_overdue) AS min_days_overdue,
    MAX(days_overdue) AS max_days_overdue
FROM vw_credit_invoice_detail
WHERE aging_bucket IS NOT NULL
GROUP BY company_code, aging_bucket
"""


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # Staging keeps the source shape: every business column TEXT, exactly as it
    # was read, so a value failing numeric or date validation is still visible
    # for diagnosis. The audit columns are spelled out to match ``StagingMixin``
    # on the model rather than imported from it — a migration has to keep
    # producing this schema after the mixin has moved on.
    op.create_table(
        "stg_credit_invoice",
        sa.Column("staging_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("import_batch_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                  sa.ForeignKey("etl_import_batches.batch_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("source_file", sa.Text(), nullable=True),
        sa.Column("source_row_number", sa.Integer(), nullable=True),
        sa.Column("source_system", sa.String(32), nullable=True),
        sa.Column("raw_data", sa.JSON(), nullable=True),
        sa.Column("loaded_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("validation_status", sa.String(16), nullable=False,
                  server_default="PENDING"),
        sa.Column("validation_error", sa.Text(), nullable=True),
        sa.Column("company_code", sa.String(64), nullable=True),
        sa.Column("invoice_no", sa.String(64), nullable=True),
        sa.Column("plant_code", sa.String(64), nullable=True),
        sa.Column("customer_code", sa.String(64), nullable=True),
        sa.Column("invoice_date", sa.String(64), nullable=True),
        sa.Column("credit_days", sa.String(64), nullable=True),
        sa.Column("invoice_value", sa.String(64), nullable=True),
        sa.Column("return_amount", sa.String(64), nullable=True),
        sa.Column("payment_amount", sa.String(64), nullable=True),
        sa.Column("discount_amount", sa.String(64), nullable=True),
        sa.Column("adjustment_amount", sa.String(64), nullable=True),
        sa.Column("payment_mode", sa.String(64), nullable=True),
        sa.Column("last_payment_date", sa.String(64), nullable=True),
        sa.Column("clearing_date", sa.String(64), nullable=True),
        sa.Column("clearing_document", sa.String(64), nullable=True),
        # What the file claimed. Staged only so the derived value has something
        # to be checked against; neither is ever copied to the fact.
        sa.Column("due_date", sa.String(64), nullable=True),
        sa.Column("balance_amount", sa.String(64), nullable=True),
        sa.Column("source_transaction_id", sa.String(128), nullable=True),
    )
    op.create_index("ix_stg_credit_invoice_batch", "stg_credit_invoice",
                    ["import_batch_id", "validation_status"])

    op.create_table(
        "fact_credit_invoice",
        sa.Column("credit_invoice_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                  sa.ForeignKey("dim_customer.customer_id", ondelete="RESTRICT"),
                  nullable=True),
        sa.Column("plant_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                  sa.ForeignKey("dim_plant.plant_id", ondelete="RESTRICT"),
                  nullable=True),
        sa.Column("company_code", sa.String(64), nullable=False),
        sa.Column("invoice_no", sa.String(64), nullable=False),
        sa.Column("plant_code", sa.String(64), nullable=True),
        sa.Column("customer_code", sa.String(64), nullable=False),
        sa.Column("invoice_date_id", sa.Integer(),
                  sa.ForeignKey("dim_date.date_id"), nullable=False),
        sa.Column("due_date_id", sa.Integer(),
                  sa.ForeignKey("dim_date.date_id"), nullable=False),
        sa.Column("last_payment_date_id", sa.Integer(),
                  sa.ForeignKey("dim_date.date_id"), nullable=True),
        sa.Column("clearing_date_id", sa.Integer(),
                  sa.ForeignKey("dim_date.date_id"), nullable=True),
        sa.Column("credit_days", sa.Integer(), nullable=False),
        sa.Column("invoice_value", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("return_amount", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("payment_amount", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("discount_amount", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("adjustment_amount", sa.Numeric(18, 4), nullable=False,
                  server_default="0"),
        sa.Column("payment_mode", sa.String(16), nullable=True),
        sa.Column("clearing_document", sa.String(64), nullable=True),
        sa.Column("net_invoice_amount", sa.Numeric(18, 4), nullable=False,
                  server_default="0"),
        sa.Column("balance_amount", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("data_quality_flag", sa.String(128), nullable=True),
        sa.Column("source_system", sa.String(32), nullable=False),
        sa.Column("source_transaction_id", sa.String(128), nullable=True),
        sa.Column("source_file", sa.Text(), nullable=True),
        sa.Column("source_row_number", sa.Integer(), nullable=True),
        sa.Column("business_key", sa.String(512), nullable=False),
        sa.Column("import_batch_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                  sa.ForeignKey("etl_import_batches.batch_id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("is_void", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column("voided_by", sa.String(64), nullable=True),
        sa.Column("void_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("business_key", name="uq_fact_credit_invoice_business_key"),
        # Unique within the company, not globally: two group companies each
        # numbering their invoices from 1 is ordinary, and a global constraint
        # would reject the whole of the second one's file.
        sa.UniqueConstraint("company_code", "invoice_no",
                            name="uq_fact_credit_invoice_company_invoice"),
    )
    for name, columns in (
        ("ix_fact_credit_invoice_company", ["company_code"]),
        ("ix_fact_credit_invoice_invoice_no", ["invoice_no"]),
        ("ix_fact_credit_invoice_customer", ["customer_code"]),
        ("ix_fact_credit_invoice_customer_id", ["customer_id"]),
        ("ix_fact_credit_invoice_plant", ["plant_code"]),
        ("ix_fact_credit_invoice_plant_id", ["plant_id"]),
        ("ix_fact_credit_invoice_invoice_date", ["invoice_date_id"]),
        # The index the whole module leans on: every aging figure, every overdue
        # KPI and the status filter is a comparison of this column against the
        # reporting date.
        ("ix_fact_credit_invoice_due_date", ["due_date_id"]),
        ("ix_fact_credit_invoice_clearing_doc", ["clearing_document"]),
        ("ix_fact_credit_invoice_quality", ["data_quality_flag"]),
        ("ix_fact_credit_invoice_batch", ["import_batch_id"]),
        ("ix_fact_credit_invoice_source_system", ["source_system"]),
    ):
        op.create_index(name, "fact_credit_invoice", columns)

    for view in reversed(_VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {view}")
    op.execute(f"CREATE VIEW vw_credit_invoice_detail AS {_detail_view(dialect)}")
    op.execute(f"CREATE VIEW vw_customer_credit_exposure AS {CUSTOMER_CREDIT_EXPOSURE}")
    op.execute(f"CREATE VIEW vw_credit_aging AS {CREDIT_AGING}")


def downgrade() -> None:
    """Remove the module. The two tables go with it, and so does their data.

    This is the one direction in which Credit Control loses rows, and it is the
    ordinary meaning of a downgrade: the revision created both tables, so
    reversing it returns the schema to a state in which neither existed. Nothing
    else in the platform reads them, so there is nothing left dangling.
    """
    for view in reversed(_VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {view}")
    op.drop_table("fact_credit_invoice")
    op.drop_table("stg_credit_invoice")
