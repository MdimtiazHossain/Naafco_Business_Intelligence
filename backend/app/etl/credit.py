"""Credit Control derivations: what a receivable is worth, and how late it is.

Every figure on the Credit Control surface that the source does not state is
derived here, and only here. The rule this module exists to enforce is that
"overdue" means one thing platform-wide: an endpoint, a view and the ETL that
each defined it separately would disagree the first time a boundary moved.

**Two kinds of derived figure, kept apart on purpose.**

*Stable* figures depend only on what the file said — net invoice, balance, and
the due date implied by the invoice date and the credit terms. Those are
computed once by the ETL and stored on ``fact_credit_invoice``, because nothing
but a new upload can change them.

*Date-relative* figures — days overdue, aging bucket, credit status — depend on
the day you ask. They are **not stored**. A ``days_overdue`` written at load
time is wrong the following morning, and a stored figure that looks
authoritative and is stale is worse than no figure at all; the same reasoning
keeps Target Management's reconciliation computed rather than stored. So the
reporting views derive them from ``due_date_id`` against the reporting date,
which is what lets ``as_on_date`` be a real request parameter rather than a
label on figures frozen at upload time.

The SQL doing that lives in the revision that authored each view, frozen the way
every other view body in this schema is frozen. The Python below is what the ETL
uses, and ``test_credit_control.py`` asserts the two agree across every boundary
rather than trusting that they do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

#: Settled: nothing is owed. Reached when the balance is zero *or below* — an
#: over-adjusted invoice is cleared, not negatively outstanding.
STATUS_CLEARED = "CLEARED"
#: Open, and the credit period has not expired.
STATUS_NOT_YET_DUE = "NOT_YET_DUE"
#: Open, and the due date has passed.
STATUS_OVER_DUE = "OVER_DUE"

CREDIT_STATUSES: tuple[str, ...] = (STATUS_NOT_YET_DUE, STATUS_OVER_DUE, STATUS_CLEARED)

# ---------------------------------------------------------------------------
# Aging
# ---------------------------------------------------------------------------

#: Open but not yet payable. Its own bucket rather than a zero-day one: money
#: inside its agreed terms is not early-stage debt, and showing it as "0 days
#: overdue" invites reading it as a problem.
BUCKET_NOT_YET_DUE = "NOT_YET_DUE"

#: The overdue buckets, in reporting order, as ``(code, lower, upper)`` day
#: bounds **inclusive of both ends**. ``upper=None`` is the open-ended tail.
#:
#: Eight buckets rather than the six the platform uses elsewhere, which was an
#: explicit decision: a 120-day debt and a 179-day debt are chased by different
#: people, so 91-120 and 181-365 are split out. ``AGING_COLORS`` in the frontend
#: gains two entries to match.
#:
#: The bounds do not overlap. The specification wrote the last two as "181-365"
#: and "365+", which claims 365 twice; the tail therefore starts at 366 and the
#: published label stays cosmetic.
OVERDUE_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("1-30", 1, 30),
    ("31-60", 31, 60),
    ("61-90", 61, 90),
    ("91-120", 91, 120),
    ("121-180", 121, 180),
    ("181-365", 181, 365),
    ("365+", 366, None),
)

#: Every bucket a row may report, in the order a chart draws them.
AGING_BUCKETS: tuple[str, ...] = (BUCKET_NOT_YET_DUE,) + tuple(
    code for code, _lower, _upper in OVERDUE_BUCKETS
)

# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------

#: The balance came out below zero. Kept, never floored: the figure is what the
#: source's own numbers produce, and raising it to zero would hide an
#: over-posted credit note rather than surface it.
FLAG_BALANCE_NEGATIVE = "BALANCE_NEGATIVE"
#: The file stated a due date disagreeing with invoice date + credit days. The
#: derived value wins — see :func:`derive`.
FLAG_DUE_DATE_MISMATCH = "DUE_DATE_MISMATCH"
#: The file stated a balance disagreeing with the one its own columns imply.
FLAG_BALANCE_MISMATCH = "BALANCE_MISMATCH"
#: Credit terms outside the set the business has described. Recorded rather than
#: rejected: the seven known values are what we have been told about, not a
#: guarantee about what the source will send, and refusing a row on that
#: assumption would lose a real invoice.
FLAG_CREDIT_DAYS_UNEXPECTED = "CREDIT_DAYS_UNEXPECTED"

#: The credit terms the business has described, plus the one the data added.
#:
#: ``0`` is here because 11,791 rows of the first real file carried it — it is
#: the ordinary value in this source, not an anomaly, and flagging three
#: quarters of a file teaches everyone to ignore the flag. It means the terms
#: were not stated on that row; the due date the source supplies is what stands.
KNOWN_CREDIT_DAYS: tuple[int, ...] = (0, 30, 45, 90, 150, 180, 190, 250)

#: Money is compared to the paisa and no finer. Two systems rounding differently
#: should not raise a mismatch flag on every row.
TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class Derived:
    """What the ETL computes once per row and stores on the fact."""

    net_invoice_amount: Decimal
    balance_amount: Decimal
    due_date: date
    #: ``None`` when the row is consistent. Several problems concatenate with
    #: ``|`` rather than one silently winning: a row can be wrong twice, and a
    #: reviewer needs to see both.
    data_quality_flag: str | None


def derive(
    *,
    invoice_date: date,
    credit_days: int,
    invoice_value: Decimal,
    return_amount: Decimal = Decimal("0"),
    payment_amount: Decimal = Decimal("0"),
    discount_amount: Decimal = Decimal("0"),
    adjustment_amount: Decimal = Decimal("0"),
    stated_due_date: date | None = None,
    stated_balance: Decimal | None = None,
) -> Derived:
    """Compute the stable figures for one invoice and flag what disagrees.

    **The four deduction columns are signed, and are summed rather than
    subtracted.** This was the other way round, and it was wrong: the source
    posts a payment as a negative number, so subtracting it *added* it and an
    invoice paid in full came out at roughly twice its value. Measured against
    the source's own Balance Amount over a real 16,614-row file, summing the
    signed amounts agrees on 93.5% of rows and subtracting them on 54% — and the
    54% is almost entirely rows where payment is zero and the two agree anyway.
    Payment was never once positive in that file.

    So a *negative* payment, discount or adjustment reduces the balance and a
    positive one increases it, which is what lets a credit note and a receipt
    live in the same column without a second sign rule.

    **A stated due date wins over a derived one.** Also the reverse of what this
    did first. The reasoning then was that a source computing a due date from
    terms it had not sent us should not be able to move a reported figure — but
    the file settles it: 11,791 of those rows state ``credit_days`` of 0 while
    carrying a real due date, so deriving gives back the invoice date and makes
    every one of them look immediately overdue. The stated date is the fact; the
    terms column is the thing that is missing. Deriving is the fallback for a row
    that states no due date at all.

    Both disagreements are still flagged. The figure no longer moves because of
    them, but "the terms do not explain this due date" and "the source's balance
    does not equal its own columns" are exactly what a data-quality review needs
    to see.
    """
    # Return is *subtracted*; the other three are *added*. Not a slip — the
    # source genuinely mixes the two conventions, and it was measured rather
    # than assumed. Over the first real 16,614-row file, agreement with the
    # source's own Balance Amount by sign combination:
    #
    #     value - return + pay + disc + adj   15,776  (95.0%)   <- this
    #     value + return + pay + disc + adj   15,539  (93.5%)
    #
    # A returned-goods document arrives negative and *increases* what is owed on
    # the invoice it offsets, which is why subtracting a negative is right here
    # and wrong three columns to the left.
    #
    # The adjustment sign is **not** determined by this data: flipping it changes
    # the agreement by exactly nothing, because every row it would affect has a
    # zero adjustment. It is written ``+`` to match payment and discount, which
    # is a consistency choice rather than a measurement, and the day a file
    # contains a non-zero adjustment that disagrees is the day to re-measure.
    net = invoice_value - return_amount
    balance = net + payment_amount + discount_amount + adjustment_amount

    derived_due = invoice_date + timedelta(days=credit_days)
    due = stated_due_date if stated_due_date is not None else derived_due

    flags: list[str] = []
    if balance < 0:
        flags.append(FLAG_BALANCE_NEGATIVE)
    if stated_due_date is not None and stated_due_date != derived_due:
        flags.append(FLAG_DUE_DATE_MISMATCH)
    if stated_balance is not None and abs(stated_balance - balance) > TOLERANCE:
        flags.append(FLAG_BALANCE_MISMATCH)
    if credit_days not in KNOWN_CREDIT_DAYS:
        flags.append(FLAG_CREDIT_DAYS_UNEXPECTED)

    return Derived(
        net_invoice_amount=net,
        balance_amount=balance,
        due_date=due,
        data_quality_flag="|".join(flags) if flags else None,
    )


def days_overdue(*, due_date: date, as_on: date) -> int:
    """Days past the due date, floored at zero.

    Floored because "negative days overdue" is not a measurement anybody asks
    for; how long remains is a different question with a different name, and the
    Due Soon figure answers it from the due date directly.
    """
    return max((as_on - due_date).days, 0)


def credit_status(*, balance_amount: Decimal, due_date: date, as_on: date) -> str:
    """Which of the three states one invoice is in on a given day.

    Balance is tested first and inclusively: a settled invoice is Cleared
    whether it was paid early or two years late, so a paid-off row never
    reappears in the overdue figures.
    """
    if balance_amount <= 0:
        return STATUS_CLEARED
    return STATUS_NOT_YET_DUE if as_on <= due_date else STATUS_OVER_DUE


def aging_bucket(*, balance_amount: Decimal, due_date: date, as_on: date) -> str | None:
    """Which aging bucket one invoice falls in, or ``None`` if it falls in none.

    ``None`` for a cleared invoice — aging measures money still owed, and
    including settled rows would report debt already collected. That is what
    lets the aging chart and the outstanding KPI be read against each other:
    both count exactly the open invoices.
    """
    if balance_amount <= 0:
        return None
    overdue = days_overdue(due_date=due_date, as_on=as_on)
    if overdue == 0:
        return BUCKET_NOT_YET_DUE
    for code, lower, upper in OVERDUE_BUCKETS:
        if overdue >= lower and (upper is None or overdue <= upper):
            return code
    raise AssertionError(  # pragma: no cover - the tail is open-ended
        f"{overdue} days overdue matched no bucket; OVERDUE_BUCKETS has a hole."
    )


__all__ = [
    "AGING_BUCKETS",
    "BUCKET_NOT_YET_DUE",
    "CREDIT_STATUSES",
    "Derived",
    "FLAG_BALANCE_MISMATCH",
    "FLAG_BALANCE_NEGATIVE",
    "FLAG_CREDIT_DAYS_UNEXPECTED",
    "FLAG_DUE_DATE_MISMATCH",
    "KNOWN_CREDIT_DAYS",
    "OVERDUE_BUCKETS",
    "STATUS_CLEARED",
    "STATUS_NOT_YET_DUE",
    "STATUS_OVER_DUE",
    "TOLERANCE",
    "aging_bucket",
    "credit_status",
    "days_overdue",
    "derive",
]
