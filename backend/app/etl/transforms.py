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


def derive_credit_invoice(record: dict[str, Any]) -> dict[str, Any]:
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
    credit_days = record.get("credit_days")
    if invoice_date is None or credit_days is None:
        # Both are required fields, so a row reaching here without them has
        # already been rejected; returning nothing keeps this a pure function
        # rather than raising on a row the pipeline is finished with.
        return {}

    derived = credit.derive(
        invoice_date=invoice_date,
        credit_days=int(credit_days),
        invoice_value=_dec(record.get("invoice_value")),
        return_amount=_dec(record.get("return_amount")),
        payment_amount=_dec(record.get("payment_amount")),
        discount_amount=_dec(record.get("discount_amount")),
        adjustment_amount=_dec(record.get("adjustment_amount")),
        stated_due_date=record.get("due_date"),
        stated_balance=_dec(record.get("balance_amount"), None),
    )
    return {
        "due_date": derived.due_date,
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
