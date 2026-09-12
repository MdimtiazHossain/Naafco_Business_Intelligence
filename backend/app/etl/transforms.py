"""Business calculations applied when a staged row becomes a fact row.

Every formula here is also expressed in SQL in the reporting views; this module
is the authority for what is *stored*, the views for what is *aggregated*.

Rules honoured:

* ``net_sales = gross_sales - discount`` — derived only when the source omits it.
* ``gross_profit = net_sales - cost`` — NULL when cost is unknown, never 0.
* The four stock categories are stored exactly as the file states them.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .validation import safe_divide

ZERO = Decimal("0")


def _dec(value: Any, default: Decimal | None = ZERO) -> Decimal | None:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


# No aging helpers. ``aging_bucket`` and ``days_overdue_between`` existed only to
# classify a receivable, and receivables left this platform with the Outstanding
# module (revision 0020). Nothing else measured a date against a due date.

# No stock coverage helpers. Days of cover needs a stock figure and a *rate of
# consumption*, and a rate needs two readings and the time between them. Material
# stock is a dateless position: the source states no posting date, so there is
# nothing to measure the change against. Since revision 0022 stock and sales at
# least name the same material, so the two figures can sit side by side — what
# cannot be derived is the rate, and shelf life replaced coverage as the stock
# risk this platform reports.


def gross_margin_percent(net_sales: Any, gross_profit: Any) -> Decimal | None:
    """``gross_profit / net_sales * 100``; ``None`` when net sales are zero."""
    return safe_divide(_dec(gross_profit, None), _dec(net_sales, None), percent=True)


def achievement_percent(actual: Any, target: Any) -> Decimal | None:
    """``actual / target * 100``; ``None`` when the target is zero or missing."""
    return safe_divide(_dec(actual, None), _dec(target, None), percent=True)


def growth_percent(current: Any, previous: Any) -> Decimal | None:
    """``(current - previous) / previous * 100``; ``None`` when there is no base."""
    current_value, previous_value = _dec(current, None), _dec(previous, None)
    if current_value is None or previous_value is None:
        return None
    return safe_divide(current_value - previous_value, previous_value, percent=True)


# ---------------------------------------------------------------------------
# Fact-row builders
# ---------------------------------------------------------------------------


def build_sales_measures(record: dict[str, Any]) -> dict[str, Any]:
    """The measures of one sales line.

    ``net_sales`` is the required input and ``gross_sales`` the optional one, so
    the derivation runs the other way from the obvious direction: gross is
    reconstructed as ``net + discount`` when the source omits it.

    Net is still derived from ``gross - discount`` if it somehow arrives absent.
    That path is unreachable through the validated pipeline, which rejects the
    row first — it is here for the callers that build a record directly, and
    because a measure silently defaulting to zero is the worse failure.
    """
    discount = _dec(record.get("discount"))
    gross_value = _dec(record.get("gross_sales"), None)
    net_value = _dec(record.get("net_sales"), None)

    if net_value is None:
        net_value = (gross_value if gross_value is not None else ZERO) - discount
    if gross_value is None:
        gross_value = net_value + discount

    cost = _dec(record.get("cost"), None)
    return {
        "quantity": _dec(record.get("quantity")),
        "gross_sales": gross_value,
        "discount": discount,
        "net_sales": net_value,
        "cost": cost,
        "gross_profit": None if cost is None else net_value - cost,
    }


def apply_volume(measures: dict[str, Any], result: Any) -> dict[str, Any]:
    """Fold a :class:`app.etl.volume.VolumeResult` into a measure set.

    One column: the total the file supplied. Only a genuinely absent volume
    writes NULL — a zero volume is a real answer, a line for nothing, and must
    not be how "unknown" is spelled.

    ``volume_unit`` and ``volume_factor`` are deliberately not written. They
    belonged to the derived-volume design, where a figure was reconstructed as
    quantity x pack size and needed both the unit and the factor recorded to be
    interpretable later. An uploaded total needs neither, and writing a unit
    this importer would have had to guess is exactly what the change removed.
    """
    measures["volume"] = result.volume
    return measures


def build_material_stock_measures(record: dict[str, Any]) -> dict[str, Any]:
    """The four stock categories, exactly as the file states them.

    Nothing is derived. The old stock builder computed a closing balance from
    opening + purchases + transfers − sales when the source did not supply one;
    a material stock position has no such arithmetic behind it, because it is a
    reading rather than a movement. Each category stands on its own, and the
    total is computed once in ``vw_material_stock_detail`` so every surface
    agrees on it.

    An absent category defaults to zero rather than NULL: the four together
    describe the whole of a position, so a missing one means "none in that
    state", not "unknown". That is the opposite of the volume rule on sales,
    where an absent figure genuinely is unknown.
    """
    return {
        "unrestricted_stock": _dec(record.get("unrestricted_stock")),
        "quality_inspection_stock": _dec(record.get("quality_inspection_stock")),
        "blocked_stock": _dec(record.get("blocked_stock")),
        "stock_in_transit": _dec(record.get("stock_in_transit")),
        "production_date": record.get("production_date"),
        "shelf_life_expiration_date": record.get("shelf_life_expiration_date"),
    }


def build_target_measures(record: dict[str, Any]) -> dict[str, Any]:
    """Amount, quantity and volume — the three measures a target can set.

    Only ``target_amount`` is required and only it defaults to zero. Quantity
    and volume stay ``None`` when the file does not set them, because "no
    quantity target" and "a quantity target of zero" are different instructions
    and a report has to be able to tell them apart.

    The volume target carries no unit, here or anywhere. It used to inherit the
    pack unit of the SKU it named; the Material Master that replaced that master
    states no unit of measure, so the figure is the number the planner typed and
    is reported as that — the same rule the volume on a sales line already
    follows.

    ``target_month`` and ``financial_year`` are already canonical by the time
    this runs — ``etl.period`` resolved and cross-checked them — so they are
    carried through as they are.
    """
    return {
        "target_amount": _dec(record.get("target_amount")),
        "target_quantity": _dec(record.get("target_quantity"), None),
        "target_volume": _dec(record.get("target_volume"), None),
        "target_month": record.get("target_month"),
        "financial_year": record.get("financial_year"),
    }


@dataclass(frozen=True)
class LoadOptions:
    """What a derivation needs to know about *this load* rather than this row.

    One object rather than a keyword per setting, so a second such setting does
    not churn the signature of every derivation that does not care about it.
    """

    #: How the file being loaded signs a deduction, where the dataset says the
    #: question has an answer. Declared per upload and applied exactly once, by
    #: :func:`app.etl.credit.canonical_deductions`, so nothing downstream of the
    #: load has to know which extract a row came from.
    deduction_convention: str | None = None


def derive_credit_invoice(record: dict[str, Any],
                          options: LoadOptions) -> dict[str, Any]:
    """Compute a credit invoice's stable derivations into the cleaned record.

    This runs *before* the fact row is assembled, and that ordering is the whole
    reason it is a separate step rather than part of the measure builder. Two
    consumers need ``due_date`` as a real date and neither is the builder: the
    pipeline collects every date the row states so ``ensure_dates_exist`` can
    create the ``dim_date`` entries the foreign keys need, and the date-column
    mapping then turns it into ``due_date_id``. A due date computed inside the
    builder would arrive after both.

    The arithmetic itself is not here. :mod:`app.etl.credit` owns it, so the
    loader, the reporting views and any future caller cannot come to different
    answers about what a balance is — see that module for why the date-relative
    figures are deliberately *not* among the derivations.

    The file's own due date and balance are read only to be contradicted: where
    either disagrees with what the row's other columns imply, the derived value
    wins and the disagreement is recorded on ``data_quality_flag``.
    """
    from . import credit

    invoice_date = record.get("invoice_date")
    if invoice_date is None:
        # Required, so a row reaching here without it has already been rejected;
        # returning nothing keeps this a pure function rather than raising on a
        # row the pipeline is finished with.
        return {}

    # --- the due date -----------------------------------------------------
    #
    # The stated one wins (revision 0031). Where the file states none, the SPL
    # extract's own rule is derived rather than guessed: its `effective_due_date`
    # equals `Net Due Date` except on the 3,496 rows whose journal date equals
    # their net due date, where it is one day later — immediate terms, where the
    # money is due the day *after* the document rather than on it. Measured over
    # all 15,576 rows: 12,080 equal, 3,496 exactly +1, and **not one** row
    # differing by anything else.
    stated_due = record.get("due_date")
    net_due = record.get("net_due_date")
    if stated_due is None and net_due is not None:
        stated_due = net_due + dt.timedelta(days=1) if net_due == invoice_date else net_due

    # --- the term ---------------------------------------------------------
    #
    # Three sources, in descending order of directness, and only the last is
    # flagged. A number the file states is the term. A code the file states is
    # the term in the source's own vocabulary — mapped, not derived, so it is
    # not flagged; where that code disagrees with the row's own dates the
    # existing DUE_DATE_MISMATCH says so, which is what that flag already means.
    # Neither stated: read the term back out of the two dates, and flag it,
    # because a derived term is weaker evidence than a stated one.
    credit_days = record.get("credit_days")
    derived_term = False
    if credit_days is None:
        credit_days = credit.term_days(record.get("payment_terms"))
    if credit_days is None:
        reference = stated_due or net_due
        credit_days = (
            credit.credit_days_from_dates(invoice_date=invoice_date, due_date=reference)
            if reference is not None else 0
        )
        derived_term = True

    # --- the signs ---------------------------------------------------------
    #
    # The one place this file's own convention is consulted. From here on the
    # four deductions mean what `credit.DEDUCTION_CANONICAL_RULE` says they mean,
    # and the canonical values are what reach the fact — so the views, the page
    # and the assistant never learn which extract a row came from and cannot come
    # to different conclusions about it.
    #
    # The file's original signs are not lost: staging holds every column as text
    # and each rejected row keeps its whole `raw_data`, with the batch recording
    # the convention they were read under.
    assert options.deduction_convention is not None  # the pipeline refuses first
    return_amount, payment_amount, discount_amount, adjustment_amount = (
        credit.canonical_deductions(
            options.deduction_convention,
            return_amount=_dec(record.get("return_amount")),
            payment_amount=_dec(record.get("payment_amount")),
            discount_amount=_dec(record.get("discount_amount")),
            adjustment_amount=_dec(record.get("adjustment_amount")),
        )
    )

    derived = credit.derive(
        invoice_date=invoice_date,
        credit_days=int(credit_days),
        invoice_value=_dec(record.get("invoice_value")),
        return_amount=return_amount,
        payment_amount=payment_amount,
        discount_amount=discount_amount,
        adjustment_amount=adjustment_amount,
        stated_due_date=stated_due,
        stated_balance=_dec(record.get("balance_amount"), None),
        credit_days_derived=derived_term,
    )
    return {
        "credit_days": int(credit_days),
        "due_date": derived.due_date,
        # The canonical figures replace the file's own on the cleaned record, so
        # the fact stores what the warehouse means rather than what this
        # particular export happened to say.
        "return_amount": return_amount,
        "payment_amount": payment_amount,
        "discount_amount": discount_amount,
        "adjustment_amount": adjustment_amount,
        "net_invoice_amount": derived.net_invoice_amount,
        "balance_amount": derived.balance_amount,
        "data_quality_flag": derived.data_quality_flag,
    }


def build_credit_invoice_measures(record: dict[str, Any]) -> dict[str, Any]:
    """The amounts a credit invoice carries, as stated and as derived.

    Every source amount defaults to zero rather than NULL: an invoice states a
    value and the four deductions describe what has been posted against it, so
    an absent deduction means "none posted", not "unknown". That is the same
    reasoning the four stock categories follow, and the opposite of the sales
    volume rule where an absent figure genuinely is unknown.

    ``net_invoice_amount``, ``balance_amount`` and ``data_quality_flag`` are read
    back from the record rather than recomputed, because
    :func:`derive_credit_invoice` has already put them there. Recomputing would
    be a second implementation of the same arithmetic, which is the one thing
    this module and :mod:`app.etl.credit` exist to avoid between them.
    """
    # Validated as a number so a non-numeric term is rejected with the same
    # message as any other bad figure, but stored as an integer: credit days are
    # whole days, and the column is an INTEGER that will not take a Decimal.
    credit_days = record.get("credit_days")
    return {
        "credit_days": None if credit_days is None else int(credit_days),
        "invoice_value": _dec(record.get("invoice_value")),
        "return_amount": _dec(record.get("return_amount")),
        "payment_amount": _dec(record.get("payment_amount")),
        "discount_amount": _dec(record.get("discount_amount")),
        "adjustment_amount": _dec(record.get("adjustment_amount")),
        "net_invoice_amount": _dec(record.get("net_invoice_amount")),
        "balance_amount": _dec(record.get("balance_amount")),
        "data_quality_flag": record.get("data_quality_flag"),
        "payment_mode": record.get("payment_mode"),
    }


#: A load disagreeing with the source's own overdue split by more than this is a
#: defect rather than rounding. Measured: the derivation reproduces the SPL
#: extract's ``od`` to within 0.6%, and the residue is whole-taka rounding —
#: ``od`` and ``maturity`` are integers in every row of that file while the
#: derived figure carries paisa.
OVERDUE_AGREEMENT_TOLERANCE_PERCENT = Decimal("1.0")


def credit_invoice_load_notes(records: list[dict[str, Any]]) -> list[str]:
    """Compare the load's own overdue split against the source's, then forget it.

    ``od`` and ``maturity`` are the source's answer to "how much is late",
    computed on the day the extract was taken. They are staged, checked here, and
    reach **no fact column**, for the reason ``days_overdue`` is not a column
    either: an overdue figure is a function of the day you ask, and one frozen at
    upload is wrong the next morning while still looking authoritative.

    They are also not the whole book, which is why this compares only the part
    they cover. ``od`` is overdue and ``maturity`` is what matures inside the
    snapshot month; in the SPL extract ৳35.75 Cr falls due after it and appears
    in neither, so a comparison against the full outstanding total would report a
    third of the portfolio as a disagreement.

    **The as-on date is measured, not assumed, and that is what this function had
    wrong.** No column states when the extract was run. The latest journal date
    is only a *lower bound* on it — a book whose last posting is 31 August is
    extracted on the 31st or on some later day — so reading that date as the
    as-on date reported the SPL file as disagreeing by -2.27%, on 172 rows that
    were every one of them due **exactly on** the last journal date. Those rows
    are late if the extract was run the next morning and not late if it was run
    that evening, and the arithmetic cannot tell the two apart from the file.

    So both candidates are computed and the note names the one the source's own
    ``od`` implies. That is a measurement rather than a constant fitted to the
    answer, and it degrades honestly: a derivation that is genuinely wrong
    disagrees at *both* candidates, and the note then reports the stricter one.
    For the SPL extract the day after agrees to ৳1.32 on ৳36.22 Cr with not one
    row differing by more than ৳0.50, which is what a file called
    ``Sep_OD_Maturity`` holding August postings should be expected to say.
    """
    stated = [r for r in records if r.get("source_od") is not None
              or r.get("source_maturity") is not None]
    if not stated:
        return []

    invoice_dates = [r["invoice_date"] for r in records if r.get("invoice_date")]
    if not invoice_dates:
        return []
    last_posting = max(invoice_dates)

    def _overdue_at(as_on: dt.date) -> tuple[Decimal, int]:
        """What this load says is late on ``as_on``, and over how many invoices.

        ``due < as_on`` is this platform's own boundary, not a choice made here:
        ``credit.days_overdue`` is ``(as_on - due).days`` and an invoice due today
        is NOT_YET_DUE at zero days. Both candidates below use it, so what the
        comparison varies is the date and never the rule.
        """
        total, covered = ZERO, 0
        for record in records:
            due = record.get("due_date")
            balance = _dec(record.get("balance_amount"))
            if due is None or balance is None:
                continue
            if due < as_on:
                total += balance
                covered += 1
        return total, covered

    source_od = sum((_dec(r.get("source_od")) for r in stated), ZERO)
    if source_od == ZERO:
        derived, _ = _overdue_at(last_posting)
        return [f"The source states no overdue figure, so this load's own "
                f"{derived:,.0f} as at {last_posting} could not be checked "
                f"against it."]

    # The extract was run on the last posting date or afterwards; one day covers
    # both readings, because a further day moves nothing that these two do not.
    candidates = [last_posting, last_posting + dt.timedelta(days=1)]
    measured = []
    for as_on in candidates:
        derived, covered = _overdue_at(as_on)
        measured.append((abs((derived - source_od) / source_od * 100), as_on,
                         derived, covered))
    _, as_on, derived_od, covered = min(measured, key=lambda m: m[0])

    gap = (derived_od - source_od) / source_od * 100
    agrees = abs(gap) <= OVERDUE_AGREEMENT_TOLERANCE_PERCENT
    verdict = "agrees with" if agrees else "DISAGREES WITH"
    implied = ("" if not agrees else
               f" The file states no as-on date; {as_on} is the one its own "
               f"figures imply, the day "
               + ("of" if as_on == last_posting else "after")
               + " its last posting.")
    return [
        f"Overdue as at {as_on}: this load derives {derived_od:,.0f} across "
        f"{covered:,} invoices and the file states {source_od:,.0f} — "
        f"{gap:+.2f}%, which {verdict} the source.{implied} Neither `od` nor "
        f"`maturity` is stored; both are read only to be checked."
    ]


#: Whole-load checks, keyed by data type. A dataset with no entry has none.
LOAD_NOTES = {
    "credit_invoice": credit_invoice_load_notes,
}


MEASURE_BUILDERS = {
    "sales": build_sales_measures,
    "material_stock": build_material_stock_measures,
    "target": build_target_measures,
    "credit_invoice": build_credit_invoice_measures,
}

#: Derivations that must run on the cleaned record *before* the fact row is
#: built, keyed by data type. A dataset with no entry needs none, which is every
#: dataset but one: credit invoices are the only source here that states terms
#: rather than the date those terms imply.
DERIVATIONS = {
    "credit_invoice": derive_credit_invoice,
}

#: Extra pass-through columns each fact keeps for traceability.
#:
#: ``material_code`` is in all three since revision 0022, and on ``fact_sales``
#: it is ``NOT NULL``: every fact states the item code the file gave it, beside
#: the ``material_id`` the mapper resolved from it. That is what lets a row still
#: say what it claimed after the Material Master is corrected, and it is the same
#: rule the stock fact has always followed.
PASSTHROUGH_COLUMNS = {
    # ``invoice_line_no`` and ``batch_code`` are identity, not measures: they
    # are copied through so the fact row carries what the business key was
    # built from, which is what makes a duplicate report able to name the line.
    "sales": ("invoice_no", "invoice_line_no", "batch_code", "material_code",
              "customer_code", "sales_force_code"),
    # Company, plant, storage location and material are the position's identity,
    # so they travel onto the fact row rather than being reachable only through
    # the resolved master links. Group and brand come too, though they identify
    # nothing: they are what the file stated, kept beside the resolved material
    # so a position still says what it claimed after the Material Master is
    # corrected — which is the only way the two can be compared afterwards.
    "material_stock": ("company_code", "plant_code", "storage_location_code",
                       "material_code", "material_group_code",
                       "material_brand_code"),
    "target": ("material_code", "customer_code", "sales_force_code"),
    # Company and invoice number are the row's identity and what the business
    # key is built from; the customer and plant codes travel beside the master
    # links resolved from them, so an invoice still says who it was raised
    # against after the Customer Master is corrected.
    "credit_invoice": ("company_code", "invoice_no", "customer_code",
                       "plant_code", "clearing_document"),
}

__all__ = [
    "gross_margin_percent",
    "achievement_percent",
    "growth_percent",
    "MEASURE_BUILDERS",
    "DERIVATIONS",
    "PASSTHROUGH_COLUMNS",
]
