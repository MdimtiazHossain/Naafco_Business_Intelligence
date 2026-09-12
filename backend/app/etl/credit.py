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

#: The forward horizon: how soon money that is **not yet overdue** falls due,
#: as ``(code, upper)`` day bounds inclusive of the upper end, ``None`` for the
#: open-ended tail. The mirror image of :data:`OVERDUE_BUCKETS`, and deliberately
#: the same shape so the two read as counterparts rather than as two schemes.
#:
#: **This exists because the aging chart answers only half the question.** Aging
#: partitions what is late; it says nothing about what is about to be, and a
#: collections team plans next week rather than last quarter. The SPL extract
#: makes the gap concrete: ৳35.75 Cr of it falls due *after* the snapshot month
#: and so appears in neither ``od`` nor ``maturity`` — a third of the book, in
#: a figure the source itself does not publish.
#:
#: Coarser than the overdue buckets on purpose. Debt that is already late is
#: chased by different people at 120 days and at 179, which is why that scale is
#: split eight ways; debt that has not fallen due yet is *planned* for, and
#: nobody plans differently for day 47 and day 52.
#:
#: Unlike the aging boundaries these are **not** written into any view, so there
#: is only one copy of them and nothing to keep in step. The moment a view needs
#: them, the rule in ``0031`` applies and ``test_view_and_python_agree_on_every
#: _boundary`` gains a sibling.
DUE_BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("0-7", 7),
    ("8-30", 30),
    ("31-60", 60),
    ("61-90", 90),
    ("90+", None),
)

#: Every forward bucket, in the order a chart draws them.
DUE_BUCKET_CODES: tuple[str, ...] = tuple(code for code, _upper in DUE_BUCKETS)

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
#: The term was not stated and was read back out of the row's own two dates.
#:
#: Its own flag rather than silence, because a derived term is weaker evidence
#: than a stated one and a reviewer should be able to tell them apart — and its
#: own flag rather than ``CREDIT_DAYS_UNEXPECTED``, which it would otherwise
#: trip on almost every row: the SPL extract states no term on 8,651 of 15,576
#: rows and their date gaps take **290 distinct values**. Flagging 55% of a file
#: as "unexpected" is how a flag stops being read, which is the same reasoning
#: that put ``0`` into :data:`KNOWN_CREDIT_DAYS`. A derived value is measured,
#: not surprising, so :func:`derive` suppresses the unexpected flag when the
#: term came from the dates.
FLAG_CREDIT_DAYS_DERIVED = "CREDIT_DAYS_DERIVED"

# ---------------------------------------------------------------------------
# Deduction conventions
# ---------------------------------------------------------------------------
#
# **Two real files disagree about the sign of a deduction, and neither is
# wrong.** The convention is therefore *declared for the upload* — defaulted
# from detection, stated in the preview and recorded on the batch — and never
# inferred silently: a file whose deductions all happen to be zero satisfies both
# rules, so it carries no evidence and is refused rather than guessed at.
#
# **And the convention stops here, at the load.** It is applied once by
# :func:`canonical_deductions` and nothing downstream is ever told which file a
# row came from. That is deliberate, and it replaced a working alternative: the
# display layer used to flip the sign according to the declared convention,
# which was correct and fragile in exactly the way
# ``test_view_and_python_agree_on_every_boundary`` exists to warn about — a rule
# that has to be consulted in two places is a rule that will eventually be
# consulted in only one. The warehouse holds one canonical sign, so the page and
# the assistant have nothing left to disagree about.

#: Deductions arrive **negative** and are summed. The Credit Invoice extract.
#:
#: Measured over that file's 16,614 rows against its own Balance Amount:
#: ``value − return + pay + disc + adj`` agrees on 15,776 rows (**95.0%**),
#: against 93.5% for adding the return too and 54.2% for subtracting everything.
#: Payment is never once positive there, so subtracting it *added* it and an
#: invoice paid in full came out at roughly twice its value.
DEDUCTION_SIGNED = "SIGNED"

#: Deductions arrive **positive** and are subtracted. The SPL receivables
#: extract.
#:
#: Measured over that file's 15,576 rows against its own ``maturity + od``:
#: ``rounded_total − rev_rtn − adjustment − collection`` agrees on **11,443 of
#: 11,443** rows due on or before the snapshot date — 100%, at a ±৳0.50
#: tolerance, because ``od`` and ``maturity`` are whole-taka integers in every
#: row of that file and the derived figure carries paisa. At ±৳0.01 the same
#: rule reads 99.70%, and the 34 rows it "fails" on differ by between 3 paisa
#: and 50 paisa. Running this file through :data:`DEDUCTION_SIGNED` roughly
#: doubles every balance.
DEDUCTION_UNSIGNED = "UNSIGNED"

DEDUCTION_CONVENTIONS: tuple[str, ...] = (DEDUCTION_SIGNED, DEDUCTION_UNSIGNED)


#: What a stored deduction means, whatever file it arrived in.
#:
#: **A stored deduction is the amount by which that component reduced the
#: balance.** Positive reduced it; negative increased it. One sentence, one
#: canonical sign, and the reason every reader downstream can be ignorant of
#: which extract a row came from.
#:
#: Negative stays meaningful, which is the whole reason this normalises the sign
#: rather than the magnitude. A debit adjustment — a posting that *increased*
#: what is owed — is stored negative under both conventions and goes on reducing
#: the balance by a negative amount, which is to say increasing it. Normalising
#: with ``abs()`` would have quietly turned every one of them into a payment.
DEDUCTION_CANONICAL_RULE = (
    "a stored deduction is the amount by which that component reduced the "
    "balance: positive reduced it, negative increased it"
)


def canonical_deductions(
    convention: str, *,
    return_amount: Decimal,
    payment_amount: Decimal,
    discount_amount: Decimal,
    adjustment_amount: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """The file's four deduction figures, restated in the canonical sign.

    Returned in the order ``(return, payment, discount, adjustment)``.

    Under :data:`DEDUCTION_UNSIGNED` all four are already what the rule
    describes: the file states positive magnitudes and subtracts every one of
    them, so the canonical value *is* the file's value.

    Under :data:`DEDUCTION_SIGNED` three of the four are negated and the fourth
    is not, and the asymmetry is real rather than an oversight. Payment,
    discount and adjustment arrive negative and are *added* to the balance, so
    the amount by which each reduced it is minus the file's figure. **Return is
    different**: that file subtracts it from the invoice value too, so a
    negative return already increases the net exactly as the canonical rule says
    a negative deduction should, and negating it would invert the one figure
    that was right to begin with.

    Worked, on the two real files:

    * SPL (UNSIGNED), one row: invoice 47,225, return 0, payment 34,847 →
      net 47,225 and balance 47,225 − 34,847 = **12,378**, which is that row's
      own stated ``od`` to the taka.
    * Credit Invoice (SIGNED): invoice 100, return −20, payment −60 → canonical
      return −20 and payment +60, so net = 100 − (−20) = **120** and
      balance = 120 − 60 = **60** — identical to what that file's old
      ``net + payment + discount + adjustment`` rule produced.
    """
    if convention not in DEDUCTION_CONVENTIONS:
        raise ValueError(f"unknown deduction convention {convention!r}; "
                         f"expected one of {DEDUCTION_CONVENTIONS}")
    if convention == DEDUCTION_UNSIGNED:
        return return_amount, payment_amount, discount_amount, adjustment_amount
    return (return_amount, -payment_amount, -discount_amount, -adjustment_amount)

#: The payment-term codes the SPL extract states, and the days each names.
#:
#: All six are already in :data:`KNOWN_CREDIT_DAYS`. The mapping is the file's
#: own vocabulary and is applied only where the file states a code; it is **not**
#: consulted to override a row's dates, because the stated due date wins.
#:
#: Note the codes do not always agree with the row's own date gap — ``N180``
#: carries a tail reaching 242 days and ``NCST`` states 250 on 176 rows and 0 on
#: 23. Those rows keep the code's day count and raise
#: :data:`FLAG_DUE_DATE_MISMATCH`, which already means exactly "the terms do not
#: explain this due date".
PAYMENT_TERM_DAYS: dict[str, int] = {
    "NT00": 0,
    "NT45": 45,
    "NT90": 90,
    "N150": 150,
    "N180": 180,
    "NCST": 250,
}

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
    credit_days_derived: bool = False,
) -> Derived:
    """Compute the stable figures for one invoice and flag what disagrees.

    **The four deduction figures are canonical, not the file's own.** This used
    to take a ``convention`` and branch on it, and the branch has moved one step
    earlier: :func:`canonical_deductions` restates the file's signs at the load
    and everything from here on — this function, the fact table, the views, the
    page, the assistant — works in one sign. There is exactly one arithmetic::

        net     = invoice_value - return_amount
        balance = net - payment_amount - discount_amount - adjustment_amount

    and it is correct for both real extracts because they no longer reach it
    differently. A rule that had to be applied identically in the loader and in
    the display layer was a rule with two chances to be got wrong.

    **A stated due date wins over a derived one.** The reasoning for the reverse
    was that a source computing a due date from terms it had not sent us should
    not move a reported figure — but the files settle it: the Credit Invoice
    extract states ``credit_days`` of 0 on 11,791 rows while carrying a real due
    date, and the SPL extract states no term at all on 8,651. Deriving hands back
    the invoice date and makes every one of them look immediately overdue. The
    stated date is the fact; the terms column is what is missing.

    Both disagreements are still flagged. The figure no longer moves because of
    them, but "the terms do not explain this due date" and "the source's balance
    does not equal its own columns" are exactly what a data-quality review needs
    to see.
    """
    net = invoice_value - return_amount
    balance = net - payment_amount - discount_amount - adjustment_amount

    derived_due = invoice_date + timedelta(days=credit_days)
    due = stated_due_date if stated_due_date is not None else derived_due

    flags: list[str] = []
    if balance < 0:
        flags.append(FLAG_BALANCE_NEGATIVE)
    if stated_due_date is not None and stated_due_date != derived_due:
        flags.append(FLAG_DUE_DATE_MISMATCH)
    if stated_balance is not None and abs(stated_balance - balance) > TOLERANCE:
        flags.append(FLAG_BALANCE_MISMATCH)
    if credit_days_derived:
        # Derived *instead of* unexpected, never both. A term read back out of
        # the row's own dates is measured, and the SPL extract's derived gaps
        # take 290 distinct values — flagging all of them "unexpected" would
        # mark 55% of the file and teach everyone to ignore the flag.
        flags.append(FLAG_CREDIT_DAYS_DERIVED)
    elif credit_days not in KNOWN_CREDIT_DAYS:
        flags.append(FLAG_CREDIT_DAYS_UNEXPECTED)

    return Derived(
        net_invoice_amount=net,
        balance_amount=balance,
        due_date=due,
        data_quality_flag="|".join(flags) if flags else None,
    )


def term_days(payment_terms: str | None) -> int | None:
    """Days named by a stated payment-term code, or ``None`` if it names none."""
    if not payment_terms:
        return None
    return PAYMENT_TERM_DAYS.get(str(payment_terms).strip().upper())


def credit_days_from_dates(*, invoice_date: date, due_date: date) -> int:
    """The term the row's own two dates imply, floored at zero.

    Floored because a due date before its invoice date describes no credit term
    at all; the SPL extract has such rows, and a negative term would derive a due
    date before the invoice existed — which is the one thing
    ``CREDIT_INVOICE``'s ``allow_negative=False`` refuses outright when the term
    is *stated*.
    """
    return max((due_date - invoice_date).days, 0)


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


# ---------------------------------------------------------------------------
# Detecting which convention a file uses
# ---------------------------------------------------------------------------

#: The three columns whose sign actually distinguishes the two conventions.
#:
#: ``return_amount`` is deliberately excluded, and not because it is unreliable.
#: It is subtracted from the invoice value under *both* conventions, so
#: :func:`canonical_deductions` leaves it alone either way — which means its sign
#: is evidence about the file but changes nothing about how the file is read.
#: Counting it would let a column that cannot affect the outcome outvote the
#: three that can.
CONVENTION_EVIDENCE_FIELDS: tuple[str, ...] = (
    "payment_amount", "discount_amount", "adjustment_amount",
)

#: How lopsided the signs must be before detection offers a default.
#:
#: Not 100%, because a real file legitimately carries the odd posting of the
#: opposite sign — a debit note is precisely that, and refusing to default on one
#: debit note in fifteen thousand rows would make detection useless on exactly
#: the files it is for. Not a bare majority either: at 51/49 the majority *is* a
#: guess, and this module's whole purpose is to not make one. Both real extracts
#: come in at 100% — the Credit Invoice file has 6,576 negative payments and not
#: one positive — so the gap between 0.95 and 1.0 is where an unfamiliar file
#: would land, and an unfamiliar file is the one a person should look at.
CONVENTION_EVIDENCE_THRESHOLD = 0.95


@dataclass(frozen=True)
class ConventionEvidence:
    """What the file's own signs say, and whether that is enough to default on.

    ``convention`` is ``None`` when the file carries no usable evidence. That is
    a refusal, not a fallback: a file whose deductions are all zero satisfies
    both rules identically and would be assigned one of them by coin-toss, to be
    discovered months later as a balance off by twice the payment.
    """

    convention: str | None
    negative: int
    positive: int
    zero: int
    reason: str

    @property
    def decisive(self) -> bool:
        return self.convention is not None

    @property
    def evidence(self) -> str:
        """One line a person can check the default against, for the preview."""
        return (f"{self.negative:,} negative, {self.positive:,} positive and "
                f"{self.zero:,} zero values across "
                + ", ".join(CONVENTION_EVIDENCE_FIELDS)
                + f". {self.reason}")


def detect_convention(records) -> ConventionEvidence:
    """Read the file's deduction signs and say which convention they imply.

    The answer is a **default for a person to confirm**, never a decision. It is
    stated in the upload preview with the counts above it, recorded on the batch
    that loads the file, and applied once at the load — see
    :func:`canonical_deductions`.

    *records* is any iterable of dicts keyed by canonical field name; values that
    are ``None`` or unreadable are ignored rather than counted as zero, because
    "no figure here" and "a figure of nothing" are different statements and only
    the second is evidence.
    """
    negative = positive = zero = 0
    for record in records:
        for field in CONVENTION_EVIDENCE_FIELDS:
            value = record.get(field)
            if value is None:
                continue
            try:
                amount = Decimal(str(value).replace(",", "").strip() or "0")
            except (ArithmeticError, ValueError):
                continue
            if amount < 0:
                negative += 1
            elif amount > 0:
                positive += 1
            else:
                zero += 1

    signed_values = negative + positive
    if signed_values == 0:
        return ConventionEvidence(
            None, negative, positive, zero,
            "Every deduction in this file is zero or absent, so the file states "
            "nothing about its own convention: both rules produce identical "
            "figures for it and either could be wrong for the next file loaded "
            "the same way. Declare the convention explicitly.")

    share_negative = negative / signed_values
    share_positive = positive / signed_values
    if share_negative >= CONVENTION_EVIDENCE_THRESHOLD:
        return ConventionEvidence(
            DEDUCTION_SIGNED, negative, positive, zero,
            f"{share_negative:.1%} of the {signed_values:,} non-zero deductions "
            f"are negative, which is the {DEDUCTION_SIGNED} convention: they are "
            "posted as negative figures and added to the balance.")
    if share_positive >= CONVENTION_EVIDENCE_THRESHOLD:
        return ConventionEvidence(
            DEDUCTION_UNSIGNED, negative, positive, zero,
            f"{share_positive:.1%} of the {signed_values:,} non-zero deductions "
            f"are positive, which is the {DEDUCTION_UNSIGNED} convention: they "
            "are posted as magnitudes and subtracted from the balance.")
    return ConventionEvidence(
        None, negative, positive, zero,
        f"The signs are mixed — {share_negative:.1%} negative against "
        f"{share_positive:.1%} positive — so neither convention is more than a "
        "majority opinion about this file. Declare it explicitly, and check "
        "whether two extracts have been combined into one sheet.")


__all__ = [
    "AGING_BUCKETS",
    "DUE_BUCKETS",
    "DUE_BUCKET_CODES",
    "BUCKET_NOT_YET_DUE",
    "CREDIT_STATUSES",
    "Derived",
    "FLAG_BALANCE_MISMATCH",
    "FLAG_BALANCE_NEGATIVE",
    "FLAG_CREDIT_DAYS_DERIVED",
    "FLAG_CREDIT_DAYS_UNEXPECTED",
    "FLAG_DUE_DATE_MISMATCH",
    "CONVENTION_EVIDENCE_FIELDS",
    "CONVENTION_EVIDENCE_THRESHOLD",
    "ConventionEvidence",
    "DEDUCTION_CANONICAL_RULE",
    "DEDUCTION_CONVENTIONS",
    "DEDUCTION_SIGNED",
    "DEDUCTION_UNSIGNED",
    "KNOWN_CREDIT_DAYS",
    "PAYMENT_TERM_DAYS",
    "OVERDUE_BUCKETS",
    "STATUS_CLEARED",
    "STATUS_NOT_YET_DUE",
    "STATUS_OVER_DUE",
    "TOLERANCE",
    "aging_bucket",
    "canonical_deductions",
    "credit_days_from_dates",
    "detect_convention",
    "credit_status",
    "days_overdue",
    "derive",
    "term_days",
]
