"""One canonical sign for a deduction, and the declarations a load is given.

Revision ID: 0039_deduction_convention
Revises: 0038_credit_hierarchy
Create Date: 2026-09-11

Four columns and one back-fill.

**The columns** record what a load was *told*, on both batch tables.
``etl_import_batches`` and ``upload_batches`` each gain ``deduction_convention``
and ``restatement_scope``: how the file signed its deductions, and what it
claimed to state in full. Both are declarations made per upload rather than
properties of the dataset, because two real receivables extracts disagree about
the first and only a person can be trusted with the second.

**The back-fill** is the part that touches existing rows, and it is the reason
this revision counts before it writes.

Until now a deduction was stored exactly as its file posted it, and the *reading*
was corrected downstream — the display layer negated a payment so a card headed
"Total Payment" would not show a minus. That worked and could not keep working:
it is one rule that has to be applied identically in two places, which is the
shape of defect ``test_view_and_python_agree_on_every_boundary`` exists to warn
about, and the arrival of a second extract with the opposite convention turned
the latent version of that defect into a live one.

So the sign is normalised once, at the load, and the warehouse holds one meaning:

    a stored deduction is the amount by which that component reduced the
    balance — positive reduced it, negative increased it.

Every row already in ``fact_credit_invoice`` was loaded from the Credit Invoice
extract, which posts payment, discount and adjustment **negative** and adds them.
Their canonical value is therefore minus what is stored — and *minus*, not the
magnitude, for the reason set out below. ``return_amount`` is deliberately **not**
touched:
it is subtracted from the invoice value under both conventions, so it already
means what the canonical rule says it means.

``net_invoice_amount`` and ``balance_amount`` are not recomputed, because they do
not change: ``net + (-60)`` and ``net - 60`` are the same number. The back-fill
restates how the three components are *spelled*, not what any invoice is worth,
and the verification below proves that by checking the balance still reconciles.

**It negates, and the first draft took a magnitude instead.** Under SIGNED the
canonical value is *minus* the file's figure: a payment of −27,428 becomes
+27,428, and an adjustment of **+68,570** becomes −68,570 — a debit note, money
that *increased* what is owed. ``ABS`` is the same thing as negation only while
every value is already negative, which is why the first draft of this revision
guarded that they were and refused otherwise.

The deployment then produced five rows with a positive adjustment, and the guard
did its job — but the right answer was never to refuse them. ``ABS`` would have
turned a ৳68,570 charge into a ৳68,570 payment and moved that invoice's balance
by ৳1.37 lakh; negation reproduces the stored balance to the taka. And negation
is what ``etl.credit.canonical_deductions`` does at load time, so the magnitude
version was a back-fill that disagreed with the live rule — the exact drift
between two copies of one rule that this whole change exists to remove.

**Two guards remain, and both are about arithmetic rather than about signs.** A
row whose stored balance does not reconcile under the canonical rule was not
produced by the rule this revision assumes, and a batch that already declares a
convention is one this revision has run against or one loaded after it. Either
aborts the whole revision with nothing written — the ``0016`` / ``0019`` /
``0020`` pattern, for the same reason those use it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039_deduction_convention"
down_revision: Union[str, None] = "0038_credit_hierarchy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The three components whose sign the two conventions disagree about.
#:
#: ``return_amount`` is absent on purpose and not by oversight. Both conventions
#: subtract it from the invoice value, so a negative return already increases the
#: net exactly as the canonical rule says a negative deduction should. Negating
#: it would invert the one figure that was right to begin with.
_SIGNED_COMPONENTS: tuple[str, ...] = (
    "payment_amount", "discount_amount", "adjustment_amount",
)

#: What the Credit Invoice extract's rows were loaded under. Spelled here rather
#: than imported from ``app.etl.credit``: a migration has to keep producing the
#: same result after the module it was written against has moved on.
_SIGNED = "SIGNED"

#: Whole taka. The stored figures are ``NUMERIC(18, 4)`` and the arithmetic is
#: exact, so this is not a tolerance for rounding — it is a tolerance for the
#: paisa-level noise a source file can carry, and it is deliberately tight enough
#: that a genuinely mis-derived row cannot hide inside it.
_BALANCE_TOLERANCE = "1.0"


def _scalar(connection, sql: str) -> int:
    return int(connection.execute(sa.text(sql)).scalar() or 0)


def upgrade() -> None:
    for table in ("etl_import_batches", "upload_batches"):
        op.add_column(table, sa.Column("deduction_convention", sa.String(16),
                                       nullable=True))
        op.add_column(table, sa.Column("restatement_scope", sa.JSON(), nullable=True))

    connection = op.get_bind()

    # Nothing loaded yet is nothing to restate. A fresh database reaches head
    # with no credit rows at all, and the guards below would read a clean
    # database as a clean result, which it is.
    total = _scalar(connection, "SELECT count(*) FROM fact_credit_invoice")
    if total == 0:
        return

    # --- guard 1: every batch must be one this revision has not already seen.
    declared = _scalar(connection, """
        SELECT count(*) FROM etl_import_batches
        WHERE data_type = 'credit_invoice' AND deduction_convention IS NOT NULL
    """)
    if declared:
        raise RuntimeError(
            f"{declared} credit_invoice batch(es) already declare a deduction "
            "convention, so this database has loaded rows under the canonical "
            "rule already. Re-running the back-fill would negate them a second "
            "time and turn every payment back into a charge. Nothing was "
            "changed."
        )

    # --- guard 2: the rows must reconcile under the arithmetic being assumed.
    #
    # Checked as the *old* rule states it, which is the rule that produced them:
    # balance = (invoice - return) + payment + discount + adjustment. A row that
    # fails this was not produced by the convention this revision restates, and
    # negating its components would be restating something else.
    unreconciled = _scalar(connection, f"""
        SELECT count(*) FROM fact_credit_invoice
        WHERE abs(
            (invoice_value - return_amount)
            + payment_amount + discount_amount + adjustment_amount
            - balance_amount
        ) > {_BALANCE_TOLERANCE}
    """)
    if unreconciled:
        raise RuntimeError(
            f"{unreconciled} of {total} credit invoice row(s) do not reconcile "
            "under the SIGNED arithmetic that produced them, so they were not "
            "loaded by the rule this back-fill restates. Nothing was changed."
        )

    # --- the back-fill.
    #
    # Negation, which is the canonical rule itself: the amount by which this
    # component reduced the balance is minus what a SIGNED file posted. It leaves
    # an ordinary payment positive and a debit note negative, and it is what
    # ``etl.credit.canonical_deductions`` applies to every row loaded after this.
    #
    # **The fact only. Staging is deliberately left exactly as it arrived**, and
    # the first draft of this revision negated it too — wrong twice over.
    #
    # Wrong on principle: staging exists to hold the source shape, every business
    # column TEXT, so a value that failed validation is still visible for
    # diagnosis. The pipeline writes it *before* any derivation runs, so a row
    # loaded tomorrow keeps its file's own signs there; rewriting yesterday's
    # would make staging mean one thing for old batches and another for new, and
    # destroy the only record of what the file actually said. The batch's
    # ``deduction_convention``, added above, is what makes those signs readable.
    #
    # And wrong on PostgreSQL, which is how it was caught: those columns are
    # ``character varying``, so ``-payment_amount`` is an undefined operator
    # there. SQLite coerces text to a number and ran it happily — the same
    # dialect trap revisions 0016 through 0021 met with ``is_void = 0``, where
    # the mistake left no trace on the dialect it was written against.
    for column in _SIGNED_COMPONENTS:
        op.execute(f"UPDATE fact_credit_invoice SET {column} = -{column}")

    # --- and record what they were loaded under, which is what the columns are
    # for. Batch-level rather than row-level: the convention is a property of the
    # file, and every row of a batch came from one file.
    op.execute(
        "UPDATE etl_import_batches SET deduction_convention = "
        f"'{_SIGNED}' WHERE data_type = 'credit_invoice'"
    )
    op.execute(
        "UPDATE upload_batches SET deduction_convention = "
        f"'{_SIGNED}' WHERE upload_type = 'credit_invoice'"
    )

    # --- prove it. The balance must still reconcile, now under the canonical
    # arithmetic, and it must do so for every row rather than most of them.
    remaining = _scalar(connection, f"""
        SELECT count(*) FROM fact_credit_invoice
        WHERE abs(
            (invoice_value - return_amount)
            - payment_amount - discount_amount - adjustment_amount
            - balance_amount
        ) > {_BALANCE_TOLERANCE}
    """)
    if remaining:
        raise RuntimeError(
            f"After the back-fill, {remaining} of {total} row(s) no longer "
            "reconcile under the canonical arithmetic. The revision is rolled "
            "back; no balance was changed by it, so the fault is in the "
            "restatement of the components."
        )


def downgrade() -> None:
    """Put the file's own signs back, then drop the columns.

    Reversible exactly, and for the same reason the upgrade is safe: negation is
    its own inverse, and no balance moved in either direction. The convention is
    read back off the batch rather than assumed, so a database that has since
    loaded an UNSIGNED file is not negated into nonsense — those rows were
    already canonical and are left alone.
    """
    connection = op.get_bind()
    signed_batches = connection.execute(sa.text(
        "SELECT batch_id FROM etl_import_batches WHERE data_type = "
        f"'credit_invoice' AND deduction_convention = '{_SIGNED}'"
    )).scalars().all()
    if signed_batches:
        ids = ", ".join(str(int(b)) for b in signed_batches)
        for column in _SIGNED_COMPONENTS:
            op.execute(f"UPDATE fact_credit_invoice SET {column} = -{column} "
                       f"WHERE import_batch_id IN ({ids})")

    for table in ("upload_batches", "etl_import_batches"):
        op.drop_column(table, "restatement_scope")
        op.drop_column(table, "deduction_convention")
