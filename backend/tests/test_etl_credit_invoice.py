"""Loading credit invoices through the ETL: derivations, dates and rejections.

The warehouse layer is tested in ``test_credit_control.py``; this file is about
the pipeline actually putting a file into it. Three things here are specific to
this dataset and were not true of any earlier one, which is why they are pinned
rather than assumed:

* the fact carries **four** date columns and no ``date_id``, so the single
  reporting date every other dataset resolves is not enough;
* the due date is **read where the file states one and derived where it does
  not**, and either way has to exist in ``dim_date`` — including the derived
  case, where no column of the file ever named it;
* the fact has a ``plant_id``, which used to be what told the pipeline it was
  looking at a stock position.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models import DimCompany
from app.database.models_warehouse import (
    BATCH_COMPLETED,
    DimDate,
    EtlRejectedRecord,
    FactCreditInvoice,
)
from app.etl import credit
from app.etl.credit import detect_convention
from app.etl.pipeline import run_import
from app.etl.readers import RecordsSourceReader
from conftest_phase2 import credit_invoice_row
from test_credit_control import derive_from_file


def do_import(engine, records, **kwargs):
    """Load credit invoices, declaring the convention the fixtures are written in.

    Stated rather than defaulted inside the pipeline, which refuses to load this
    dataset without it. ``credit_invoice_row`` posts its deductions as positive
    magnitudes, so UNSIGNED is what these rows actually are; a test about the
    other convention passes it explicitly.
    """
    kwargs.setdefault("deduction_convention", credit.DEDUCTION_UNSIGNED)
    reader = RecordsSourceReader(records, source_name="credit.csv", source_type="CSV")
    return run_import(engine, "credit_invoice", reader, source_system="TEST", **kwargs)


def invoices(engine) -> list[FactCreditInvoice]:
    with Session(engine) as session:
        return list(session.execute(
            select(FactCreditInvoice).order_by(FactCreditInvoice.invoice_no)
        ).scalars())


def rejections(engine, batch_id: int) -> list[EtlRejectedRecord]:
    with Session(engine) as session:
        return list(session.execute(
            select(EtlRejectedRecord).where(EtlRejectedRecord.batch_id == batch_id)
        ).scalars())


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_a_credit_invoice_imports(seeded_engine) -> None:
    result = do_import(seeded_engine, [credit_invoice_row()])
    assert result.status == BATCH_COMPLETED
    assert result.inserted_rows == 1

    (row,) = invoices(seeded_engine)
    assert row.company_code == "C001"
    assert row.invoice_no == "INV-0001"
    assert row.customer_code == "CUST-001"
    assert row.credit_days == 90


def test_the_derivations_are_stored(seeded_engine) -> None:
    """Net and balance are computed by the loader, not read from the file."""
    do_import(seeded_engine, [credit_invoice_row(
        **{"Invoice Value": 100000, "Return": 20000, "Payment": 25000,
           "Discount": 5000, "Adjustment": 1000})])

    (row,) = invoices(seeded_engine)
    assert Decimal(str(row.net_invoice_amount)) == Decimal("80000")
    assert Decimal(str(row.balance_amount)) == Decimal("49000")
    assert row.data_quality_flag is None


def test_the_due_date_is_derived_and_gets_a_dim_date_row(seeded_engine) -> None:
    """The whole reason derivation runs before the dates are collected.

    No column of the file names the due date, so nothing would create its
    ``dim_date`` entry — and the foreign key would fail at the write, after the
    entire file had been read and validated, which is the most expensive moment
    to discover anything.
    """
    do_import(seeded_engine, [credit_invoice_row(
        **{"Invoice Date": "2026-03-12", "Credit Days": 90})])

    (row,) = invoices(seeded_engine)
    assert row.due_date_id == 20260610, "invoice date + 90 days"

    with Session(seeded_engine) as session:
        due = session.get(DimDate, 20260610)
    assert due is not None and due.full_date == dt.date(2026, 6, 10)


def test_every_date_the_row_states_lands_in_its_own_column(seeded_engine) -> None:
    do_import(seeded_engine, [credit_invoice_row(
        **{"Invoice Date": "2026-03-12", "Credit Days": 30,
           "Last Payment Date": "2026-04-02", "Clearing Date": "2026-04-05",
           "Clearing Doc": "CLR-77"})])

    (row,) = invoices(seeded_engine)
    assert row.invoice_date_id == 20260312
    assert row.due_date_id == 20260411
    assert row.last_payment_date_id == 20260402
    assert row.clearing_date_id == 20260405
    assert row.clearing_document == "CLR-77"


def test_an_absent_settlement_date_stays_null(seeded_engine) -> None:
    """An invoice nothing has been paid against has not been paid on some date.

    A placeholder here would make an open receivable look settled, which is the
    one direction this module must never be wrong in.
    """
    do_import(seeded_engine, [credit_invoice_row()])

    (row,) = invoices(seeded_engine)
    assert row.last_payment_date_id is None
    assert row.clearing_date_id is None


def test_a_plant_is_resolved_without_dragging_in_the_stock_masters(seeded_engine):
    """``fact_credit_invoice`` has a ``plant_id``, and that used to mean stock.

    The pipeline decided a row was a stock position by looking for ``plant_id``
    on the fact, so every credit invoice would have been sent to
    ``resolve_stock_masters`` and rejected for stating no storage location and no
    material — columns a credit invoice has no reason to carry.
    """
    result = do_import(seeded_engine, [credit_invoice_row(**{"Plant": "PL01"})])
    assert result.inserted_rows == 1
    assert rejections(seeded_engine, result.batch_id) == []

    (row,) = invoices(seeded_engine)
    assert row.plant_code == "PL01"


def test_an_unknown_customer_is_a_deferred_mapping_not_a_rejection(seeded_engine):
    """``dim_customer`` is PENDING_SOURCE_DATA, so the code is kept and resolved later."""
    result = do_import(seeded_engine, [credit_invoice_row(
        **{"Customer": "NOT-IN-MASTER"})])
    assert result.inserted_rows == 1

    (row,) = invoices(seeded_engine)
    assert row.customer_code == "NOT-IN-MASTER"
    assert row.customer_id is None


# ---------------------------------------------------------------------------
# Data quality: flagged, not refused
# ---------------------------------------------------------------------------


def test_an_over_adjusted_invoice_is_flagged_and_kept(seeded_engine) -> None:
    result = do_import(seeded_engine, [credit_invoice_row(
        **{"Invoice Value": 100000, "Payment": 150000})])
    assert result.inserted_rows == 1

    (row,) = invoices(seeded_engine)
    assert Decimal(str(row.balance_amount)) == Decimal("-50000")
    assert row.data_quality_flag == credit.FLAG_BALANCE_NEGATIVE


def test_a_stated_due_date_wins_and_lands_in_dim_date(seeded_engine):
    """The stated date is stored, and the terms disagreeing is still recorded.

    Reversed from how this started. The first real file settled it: 11,791 rows
    state credit days of 0 beside a genuine due date, so deriving handed back the
    invoice date and made all of them look immediately overdue.

    The stated date still has to reach ``dim_date`` — it is a foreign key like
    any other, and it is now the one the row is keyed to.
    """
    result = do_import(seeded_engine, [credit_invoice_row(
        **{"Invoice Date": "2026-03-12", "Credit Days": 90,
           "Due Date": "2026-09-01"})])
    assert result.inserted_rows == 1

    (row,) = invoices(seeded_engine)
    assert row.due_date_id == 20260901, "the stated date wins"
    assert row.data_quality_flag == credit.FLAG_DUE_DATE_MISMATCH

    with Session(seeded_engine) as session:
        assert session.get(DimDate, 20260901) is not None


def test_a_disagreeing_balance_is_flagged(seeded_engine) -> None:
    do_import(seeded_engine, [credit_invoice_row(
        **{"Invoice Value": 100000, "Payment": 25000, "Balance": 60000})])

    (row,) = invoices(seeded_engine)
    assert Decimal(str(row.balance_amount)) == Decimal("75000")
    assert row.data_quality_flag == credit.FLAG_BALANCE_MISMATCH


def test_an_unexpected_credit_term_is_flagged_and_kept(seeded_engine) -> None:
    """The seven known terms are what we were told about, not a constraint."""
    result = do_import(seeded_engine, [credit_invoice_row(**{"Credit Days": 60})])
    assert result.inserted_rows == 1

    (row,) = invoices(seeded_engine)
    assert row.credit_days == 60
    assert row.data_quality_flag == credit.FLAG_CREDIT_DAYS_UNEXPECTED


# ---------------------------------------------------------------------------
# Rejection: the things that genuinely cannot be loaded
# ---------------------------------------------------------------------------


def test_a_missing_required_field_is_rejected(seeded_engine) -> None:
    result = do_import(seeded_engine, [credit_invoice_row(**{"Invoice No": ""})])
    assert result.inserted_rows == 0
    assert len(rejections(seeded_engine, result.batch_id)) == 1


def test_an_unreadable_date_is_rejected_not_guessed(seeded_engine) -> None:
    result = do_import(seeded_engine, [credit_invoice_row(
        **{"Invoice Date": "not a date"})])
    assert result.inserted_rows == 0
    assert len(rejections(seeded_engine, result.batch_id)) == 1


def test_a_negative_credit_term_is_rejected(seeded_engine) -> None:
    """Unexpected is flagged; impossible is refused.

    An unrecognised number of credit days may simply be a term nobody told us
    about. A *negative* one cannot be a term at all, and would derive a due date
    before the invoice existed.
    """
    result = do_import(seeded_engine, [credit_invoice_row(**{"Credit Days": -30})])
    assert result.inserted_rows == 0
    assert len(rejections(seeded_engine, result.batch_id)) == 1


# ---------------------------------------------------------------------------
# Identity and re-import
# ---------------------------------------------------------------------------


def test_the_same_invoice_twice_updates_rather_than_duplicates(seeded_engine) -> None:
    """The business key is company + customer + invoice number; a re-upload corrects."""
    do_import(seeded_engine, [credit_invoice_row(**{"Payment": 25000})])
    result = do_import(seeded_engine, [credit_invoice_row(**{"Payment": 40000})])

    assert result.inserted_rows == 0
    with Session(seeded_engine) as session:
        assert session.execute(
            select(func.count()).select_from(FactCreditInvoice)).scalar_one() == 1
    (row,) = invoices(seeded_engine)
    assert Decimal(str(row.payment_amount)) == Decimal("40000")
    assert Decimal(str(row.balance_amount)) == Decimal("60000"), (
        "the balance must be re-derived from the corrected payment"
    )


def test_every_dataset_is_registered_everywhere_a_hand_written_list_names_one():
    """The registries derive themselves; these three lists do not.

    ``CLAUDE.md`` states the rule this pins: a list of dataset names must never
    outlive — or in this case, lag behind — what it names. Adding
    ``credit_invoice`` to ``DATASETS`` gave it a template, validation, a preview
    and an ETL for free, but ``display_group`` raises deliberately on a key it
    does not know, so the upload centre broke until the two maps beside it were
    updated too. A model registry missing an entry fails later and less clearly,
    at the first import.

    Asserted over ``DATASETS`` rather than over a list of names, so the next
    dataset added is covered by this test on the day it is declared.
    """
    from app.database.models_warehouse import (
        FACT_MODEL_BY_DATA_TYPE,
        STAGING_MODEL_BY_DATA_TYPE,
    )
    from app.etl.datasets import DATASETS
    from app.upload.registry import display_group

    for spec in DATASETS:
        assert spec.data_type in STAGING_MODEL_BY_DATA_TYPE, spec.data_type
        assert spec.data_type in FACT_MODEL_BY_DATA_TYPE, spec.data_type
        assert display_group(spec.data_type) == "TRANSACTIONS", spec.data_type
        assert (STAGING_MODEL_BY_DATA_TYPE[spec.data_type].__tablename__
                == spec.staging_table)
        assert FACT_MODEL_BY_DATA_TYPE[spec.data_type].__tablename__ == spec.fact_table


def test_every_declared_date_column_exists_on_the_fact() -> None:
    """A typo in ``date_columns`` would write a column the filter silently drops.

    ``_build_fact_row`` ends by discarding any key the fact table does not have,
    which is what keeps a shared builder safe across four datasets — and which
    would also make a misspelled ``clearing_date_id`` vanish without a word,
    leaving the column NULL on every row.
    """
    from app.database.models_warehouse import FACT_MODEL_BY_DATA_TYPE
    from app.etl.datasets import DATASETS

    for spec in DATASETS:
        columns = set(FACT_MODEL_BY_DATA_TYPE[spec.data_type].__table__.columns.keys())
        declared = {name for name, _column in spec.date_columns}
        for field_name, column in spec.date_columns:
            assert column in columns, f"{spec.data_type}.{column}"
        assert declared <= set(spec.field_map), (
            f"{spec.data_type} maps a date field it does not declare"
        )


def test_two_companies_may_use_the_same_invoice_number(seeded_engine) -> None:
    # A second company has to exist in the master before it can be loaded
    # against. This test used to pass without one, because `CREDIT_INVOICE`
    # declared no `org_levels` at all and so resolved nothing: any company code
    # was accepted and written straight through. Resolving the hierarchy is what
    # turned "C002" from an unremarkable string into a code with no master.
    with Session(seeded_engine) as session:
        session.add(DimCompany(company_code="C002", company_name="Second Co."))
        session.commit()

    result = do_import(seeded_engine, [
        credit_invoice_row(**{"Company": "C001", "Invoice No": "0001"}),
        credit_invoice_row(**{"Company": "C002", "Invoice No": "0001"}),
    ])
    assert result.inserted_rows == 2


# ---------------------------------------------------------------------------
# What the SPL receivables extract settled
#
# Everything below was measured on one real file of 15,576 open items and is
# pinned here so it stops being anybody's recollection. Where a measurement in
# the specification came out differently when checked, the checked figure is the
# one recorded, with the discrepancy named — a number nobody can reproduce is
# worse than no number.
# ---------------------------------------------------------------------------


def test_the_convention_is_declared_per_upload_and_refused_when_absent(seeded_engine):
    """One dataset, two files, opposite signs — so the *load* is told, not the spec.

    The Credit Invoice extract posts payment, discount and adjustment negative
    and adds them; the SPL extract posts them positive and subtracts them. Both
    are ``credit_invoice``. A constant on the dataset spec could only ever have
    been right for one of them, and running either file through the other's rule
    roughly doubles every balance — which is the defect this platform already
    paid for once, when an all-subtract rule reported an invoice paid in full at
    twice its value.

    So the convention is declared for the upload, and a load that is not given
    one **refuses** rather than picking a side. Silence is the dangerous case
    precisely because it looks harmless: a file whose deductions all happen to be
    zero satisfies both rules identically and would be assigned one by coin-toss.
    """
    with pytest.raises(ValueError, match="must be told how its file signs"):
        do_import(seeded_engine, [credit_invoice_row()], deduction_convention=None)

    with pytest.raises(ValueError, match="unknown deduction convention"):
        credit.canonical_deductions(
            "MOSTLY", return_amount=Decimal("0"), payment_amount=Decimal("0"),
            discount_amount=Decimal("0"), adjustment_amount=Decimal("0"))

    # A dataset with no deductions refuses the setting too: one that quietly did
    # nothing would be a setting somebody trusted.
    from app.etl.readers import RecordsSourceReader
    with pytest.raises(ValueError, match="states no deductions"):
        run_import(seeded_engine, "sales",
                   RecordsSourceReader([], source_name="s.csv", source_type="CSV"),
                   source_system="TEST",
                   deduction_convention=credit.DEDUCTION_SIGNED)


def test_the_two_conventions_normalise_to_one_canonical_sign():
    """Both real files, restated by the rule the warehouse actually stores.

    A stored deduction is the amount by which that component **reduced** the
    balance: positive reduced it, negative increased it. Both worked examples
    below are from real rows, and the point of pinning them together is that one
    arithmetic — ``net = invoice - return`` then
    ``balance = net - payment - discount - adjustment`` — now serves both.
    """
    # SPL (UNSIGNED). Deductions are already magnitudes, so nothing moves.
    derived = derive_from_file(
        credit.DEDUCTION_UNSIGNED,
        invoice_date=dt.date(2026, 1, 1), credit_days=30,
        invoice_value=Decimal("47225"), payment_amount=Decimal("34847"))
    assert derived.net_invoice_amount == Decimal("47225")
    assert derived.balance_amount == Decimal("12378"), "that row's own stated od"

    # Credit Invoice (SIGNED). Payment is negated; return is not, because it was
    # already subtracted from the invoice value under that convention too.
    derived = derive_from_file(
        credit.DEDUCTION_SIGNED,
        invoice_date=dt.date(2026, 1, 1), credit_days=30,
        invoice_value=Decimal("100"), return_amount=Decimal("-20"),
        payment_amount=Decimal("-60"))
    assert derived.net_invoice_amount == Decimal("120")
    assert derived.balance_amount == Decimal("60"), (
        "identical to what that file's old net+payment rule produced")


def test_a_debit_posting_stays_negative_under_both_conventions():
    """Why the sign is normalised and the magnitude is not.

    A debit note is a deduction that *increased* what is owed. Under SIGNED it
    arrives positive and under UNSIGNED it arrives negative, and under the
    canonical rule both mean the same thing and are stored the same way:
    negative, because the amount by which it reduced the balance was negative.

    ``abs()`` would have normalised these too, and turned every one of them into
    a payment — overstating what had been collected by twice the posting, in a
    column nobody would think to check.
    """
    signed = credit.canonical_deductions(
        credit.DEDUCTION_SIGNED, return_amount=Decimal("0"),
        payment_amount=Decimal("0"), discount_amount=Decimal("0"),
        adjustment_amount=Decimal("500"))
    unsigned = credit.canonical_deductions(
        credit.DEDUCTION_UNSIGNED, return_amount=Decimal("0"),
        payment_amount=Decimal("0"), discount_amount=Decimal("0"),
        adjustment_amount=Decimal("-500"))
    assert signed[3] == unsigned[3] == Decimal("-500")

    # ...and it goes on increasing the balance, which is what it did.
    derived = derive_from_file(
        credit.DEDUCTION_SIGNED,
        invoice_date=dt.date(2026, 1, 1), credit_days=30,
        invoice_value=Decimal("1000"), adjustment_amount=Decimal("250"))
    assert derived.balance_amount == Decimal("1250")


def test_detection_defaults_the_convention_and_refuses_to_guess():
    """A default a person confirms, never a decision taken for them.

    Detection reads the file's own signs and reports the counts it read them
    off, so the preview can show the answer *and* the evidence. It declines to
    answer in the two cases where answering would be a guess: a file with no
    non-zero deduction anywhere, which satisfies both rules identically, and a
    file whose signs are genuinely mixed, which usually means two extracts have
    been pasted into one sheet.
    """
    assert detect_convention(
        [{"payment_amount": -60}, {"discount_amount": -40}]
    ).convention == credit.DEDUCTION_SIGNED
    assert detect_convention(
        [{"payment_amount": 60}, {"adjustment_amount": 40}]
    ).convention == credit.DEDUCTION_UNSIGNED

    silent = detect_convention([{"payment_amount": 0}, {"payment_amount": None}])
    assert silent.convention is None and not silent.decisive
    assert "carries no evidence" in silent.reason or "states nothing" in silent.reason

    mixed = detect_convention([{"payment_amount": -60}, {"payment_amount": 40}])
    assert mixed.convention is None
    assert "mixed" in mixed.reason

    # One debit note in a large file must not stop it defaulting: refusing there
    # would make detection useless on exactly the files it exists for.
    lopsided = detect_convention(
        [{"payment_amount": -10}] * 199 + [{"payment_amount": 10}])
    assert lopsided.convention == credit.DEDUCTION_SIGNED

    # The evidence is a sentence a person can check the default against.
    assert "199 negative" in lopsided.evidence



def test_the_six_payment_term_codes_map_and_an_unknown_one_does_not():
    """The gap between due date and journal date resolves each code exactly.

    Measured across the extract: NT00 = 0 days, NT45 = 45, NT90 = 90, N150 =
    150, N180 = 180 and NCST = 250. They are a *mapping*, not a derivation, so a
    row stating one of them is not flagged — the code is the term, in the
    source's own vocabulary.

    An unrecognised code returns nothing rather than a default. Zero would be
    the tempting default and is the one value that must not be guessed: it makes
    an invoice due the day it was raised.
    """
    assert credit.PAYMENT_TERM_DAYS == {
        "NT00": 0, "NT45": 45, "NT90": 90, "N150": 150, "N180": 180, "NCST": 250,
    }
    assert credit.term_days("NT45") == 45
    assert credit.term_days("nt45") == 45          # trimmed and folded
    assert credit.term_days("NT99") is None
    assert credit.term_days("") is None
    assert credit.term_days(None) is None


def test_a_blank_payment_term_derives_the_days_and_says_that_it_did(seeded_engine):
    """8,651 of 15,576 rows state no term. None of them is a term of zero.

    Defaulting to zero would make 55% of the file immediately due, so the term
    is read back out of the two dates the row does state and flagged as derived.

    The flag is its own — ``CREDIT_DAYS_DERIVED`` — and it **suppresses**
    ``CREDIT_DAYS_UNEXPECTED`` rather than joining it. The derived gaps take 290
    distinct values, so every one of them is "unexpected" against the known
    terms; raising that flag on 55% of a file is how a flag stops being read.
    """
    row = credit_invoice_row(**{"Invoice Date": "2026-03-01",
                                "Net Due Date": "2026-04-15"})
    row.pop("Credit Days")
    result = do_import(seeded_engine, [row])
    assert result.valid_rows == 1

    invoice = invoices(seeded_engine)[0]
    assert invoice.credit_days == 45
    assert invoice.due_date_id == 20260415
    flags = (invoice.data_quality_flag or "").split("|")
    assert credit.FLAG_CREDIT_DAYS_DERIVED in flags
    assert credit.FLAG_CREDIT_DAYS_UNEXPECTED not in flags


def test_a_stated_term_beats_a_derived_one_and_is_not_flagged(seeded_engine):
    """A code the file states is the term; only a read-back-from-dates one is flagged."""
    row = credit_invoice_row(**{"Invoice Date": "2026-03-01",
                                "Payment Terms": "N150"})
    row.pop("Credit Days")
    assert do_import(seeded_engine, [row]).valid_rows == 1

    invoice = invoices(seeded_engine)[0]
    assert invoice.credit_days == 150
    assert credit.FLAG_CREDIT_DAYS_DERIVED not in (invoice.data_quality_flag or "")


def test_the_plus_one_day_rule_applies_only_where_the_file_states_no_due_date(
        seeded_engine):
    """``effective_due_date`` wins; the rule behind it is a fallback, not a filter.

    Measured over all 15,576 rows, the extract's ``effective_due_date`` equals
    its ``Net Due Date`` on 12,080 and is exactly one day later on 3,496 — every
    one of those being a row whose journal date equals its net due date, which is
    immediate terms falling due the day *after* the document rather than on it.
    Not one row differs by anything else.

    That rule is derived for a file stating only a net due date. Applying it to a
    file that states an effective date as well would move 3,496 stated facts, so
    where the column exists the column wins.
    """
    same_day = {"Invoice Date": "2026-03-12", "Net Due Date": "2026-03-12"}

    # Net due date alone: the rule fires.
    row = credit_invoice_row(**same_day)
    row.pop("Credit Days")
    assert do_import(seeded_engine, [row]).valid_rows == 1
    assert invoices(seeded_engine)[0].due_date_id == 20260313

    # Both stated and disagreeing: the stated one wins untouched.
    row = credit_invoice_row(**{**same_day, "effective_due_date": "2026-03-12",
                                "Invoice No": "INV-0002"})
    row.pop("Credit Days")
    assert do_import(seeded_engine, [row]).valid_rows == 1
    stated = [i for i in invoices(seeded_engine) if i.invoice_no == "INV-0002"][0]
    assert stated.due_date_id == 20260312


def test_a_spreadsheet_error_value_is_rejected_and_the_whole_row_is_kept(seeded_engine):
    """``#N/A`` is a value, not a code, and a row carrying one is not mappable.

    75 rows of the extract carry a literal ``#N/A`` in every hierarchy column —
    bank FDRs, share investments and inter-company loans, ৳23.76 Cr between them,
    21.7% of the book. They are real money and they belong to no sales territory,
    so they are neither invented a hierarchy nor quietly dropped: they are
    rejected under their own error code, with the full original row kept in
    ``etl_rejected_records`` so the figure stays findable.

    Loading ``#N/A`` as a code would be worse than either: ``dim_zone`` would
    grow an entity called ``#N/A`` and every hierarchy report would carry it.
    """
    row = credit_invoice_row(**{"Zone_Code": "#N/A", "Region_Code": "#N/A",
                                "Area_Code": "#N/A", "Unit_Code": "#N/A",
                                "Territory_Code": "#N/A",
                                "SubTerritory_Code": "#N/A"})
    result = do_import(seeded_engine, [row])
    assert result.valid_rows == 0 and result.rejected_rows == 1

    rejected = rejections(seeded_engine, result.batch_id)[0]
    assert rejected.error_code == "SPREADSHEET_ERROR_VALUE"
    # The row is kept whole, not summarised: the money has to remain readable.
    assert str(rejected.raw_data["Invoice Value"]) == "100000"
    assert rejected.raw_data["Customer"] == "CUST-001"


def test_the_business_key_names_the_customer_because_assignments_repeat():
    """285 Assignment values repeat and 268 of them span more than one customer.

    Keyed on ``(company, invoice_no)`` the extract collides on 290 rows, so the
    key states the customer as well. That is not a complete answer and is not
    presented as one: 17 rows remain where a single customer carries the same
    Assignment twice with different dates and different amounts, and those load
    through the pipeline's ``…#2`` repeat numbering rather than being rejected or
    silently merged. See ``test_credit_control`` for why the table constraint
    that used to forbid them was removed instead of widened.
    """
    from app.etl.datasets import CREDIT_INVOICE
    assert CREDIT_INVOICE.business_key_fields == (
        "company_code", "customer_code", "invoice_no")


def test_the_hierarchy_is_resolved_from_the_levels_the_masters_recognise():
    """The file's Zone and Region columns are not the warehouse's zones and regions.

    Measured against the masters over all 15,576 rows: none of the file's 13 zone
    codes and none of its 16 region codes exists in ``dim_zone`` or ``dim_region``,
    while Area (16/16), Unit (20/20), Territory (154/154) and Sub-Territory
    (267/267) all resolve completely. ``Region_Code`` in fact holds the *area*
    code on every row, and three region names carry two codes each — the file's
    hierarchy is shifted a whole level.

    **``unit_code`` joined them, and it was the deployment that proved it.** The
    first load against production rejected 1,205 rows for HIERARCHY_MISMATCH on
    that column alone — not a loader fault but a master that had moved on: three
    new units created and eight territories re-parented onto them, while the
    extract still names the old parent. Measured against that master over the
    15,501 rows carrying a hierarchy, sub-territory resolves 15,501/15,501,
    territory equals its sub-territory's parent 15,501/15,501 and area equals its
    unit's parent 15,501/15,501 — while unit equals its territory's parent on
    only 14,296.

    So the loader resolves the levels the masters still agree with and lets
    ``resolve_org`` derive the rest from the chain, which is what ``fact_target``
    already does. Area stays because it agrees on every row, and a level that
    agrees is a free cross-check of the file against the master. The file's own
    distrusted columns are kept in staging, where a record of what arrived
    belongs.
    """
    from app.etl.datasets import CREDIT_INVOICE
    assert CREDIT_INVOICE.org_levels == (
        "company_code", "area_code", "territory_code", "sub_territory_code")
    for derived in ("zone_code", "region_code", "unit_code"):
        assert derived not in CREDIT_INVOICE.org_levels, derived


# ---------------------------------------------------------------------------
# The scoped restatement
# ---------------------------------------------------------------------------


def _open_keys(engine) -> set[tuple[str, str]]:
    """Company and invoice number of every row still standing.

    Identity rather than ``business_key``: the key spells out the fields it was
    built from and the source system, which is right for the key and unreadable
    in an assertion about which invoices survived a restatement.
    """
    with Session(engine) as session:
        return {
            (row.company_code, row.invoice_no) for row in session.execute(
                select(FactCreditInvoice).where(
                    FactCreditInvoice.is_void == False)  # noqa: E712
            ).scalars()
        }


def test_a_restatement_voids_what_the_file_stops_naming(seeded_engine):
    """A receivables file states a whole book, so a row it drops has been settled.

    This is the behaviour ``targetmgmt.lock`` already has for a re-locked plan,
    and it is here for the same reason: when a source restates something in full,
    an ordinary incremental load leaves every settled invoice standing in the
    warehouse for ever, and the outstanding total drifts upward month after month
    with nothing to show why.
    """
    first = [credit_invoice_row(**{"Invoice No": f"INV-{n}"}) for n in (1, 2, 3)]
    do_import(seeded_engine, first, restatement_scope=["C001"])
    assert _open_keys(seeded_engine) == {
        ("C001", "INV-1"), ("C001", "INV-2"), ("C001", "INV-3")}

    # The next extract of the same book no longer carries INV-2: it was settled.
    second = [credit_invoice_row(**{"Invoice No": f"INV-{n}"}) for n in (1, 3, 4)]
    result = do_import(seeded_engine, second, restatement_scope=["C001"])

    assert result.restatement is not None
    assert result.restatement.voided_rows == 1
    assert result.restatement.restated_rows == 2
    assert result.restatement.new_rows == 1
    assert _open_keys(seeded_engine) == {
        ("C001", "INV-1"), ("C001", "INV-3"), ("C001", "INV-4")}


def test_a_voided_row_is_kept_and_comes_back_if_the_file_names_it_again(seeded_engine):
    """Voided, never deleted — which is what makes a restatement reversible.

    The row, its provenance and its batch survive; every reporting view filters
    ``is_void``, so it leaves the dashboard, the exports and the assistant in the
    same instant without leaving the record. And because the business key is
    still there, a later file naming it again updates the row back into life
    rather than inserting a second one beside it.
    """
    do_import(seeded_engine, [credit_invoice_row(**{"Invoice No": "INV-1"})],
              restatement_scope=["C001"])
    do_import(seeded_engine, [credit_invoice_row(**{"Invoice No": "INV-9"})],
              restatement_scope=["C001"])

    with Session(seeded_engine) as session:
        voided = session.execute(
            select(FactCreditInvoice).where(
                FactCreditInvoice.invoice_no == "INV-1")).scalar_one()
        assert voided.is_void is True
        assert voided.voided_at is not None
        assert voided.import_batch_id is not None, "provenance survives"

    # It reappears in the source, so it comes back — one row, not two.
    do_import(seeded_engine, [credit_invoice_row(**{"Invoice No": "INV-1"})],
              restatement_scope=["C001"])
    with Session(seeded_engine) as session:
        rows = session.execute(
            select(FactCreditInvoice).where(
                FactCreditInvoice.invoice_no == "INV-1")).scalars().all()
    assert len(rows) == 1 and rows[0].is_void is False


def test_a_restatement_cannot_touch_anything_outside_its_declared_scope(seeded_engine):
    """The single most important property here, and the whole of why it is a scope.

    REPLACE empties whatever a file does not mention, globally. A restatement is
    bounded: a file declaring company C001 cannot stand down C002's book however
    little it says about it. On the real data this is the difference between
    restating company 1000's 14,629 rows and silently erasing the 683 rows of
    companies 2000 and 3000 that no receivables extract has ever covered.
    """
    with Session(seeded_engine) as session:
        session.add(DimCompany(company_code="C002", company_name="Second Co."))
        session.commit()

    do_import(seeded_engine, [
        credit_invoice_row(**{"Company": "C001", "Invoice No": "A"}),
        credit_invoice_row(**{"Company": "C002", "Invoice No": "B"}),
    ])

    # A file for C001 alone, naming neither of them.
    result = do_import(seeded_engine, [
        credit_invoice_row(**{"Company": "C001", "Invoice No": "C"})],
        restatement_scope=["C001"])

    assert result.restatement is not None
    assert result.restatement.voided_rows == 1, "only C001's dropped row"
    assert _open_keys(seeded_engine) == {
        ("C001", "C"), ("C002", "B")}, "C002 is untouched"


def test_no_declared_scope_voids_nothing_at_all(seeded_engine):
    """An ordinary upload is still an ordinary upload.

    The restatement is opt-in per load. Without a declared scope nothing is
    stood down, which is what every other dataset does and what a receivables
    file that covers only part of a book must also do.
    """
    do_import(seeded_engine, [credit_invoice_row(**{"Invoice No": "INV-1"})])
    result = do_import(seeded_engine, [credit_invoice_row(**{"Invoice No": "INV-2"})])

    assert result.restatement is None
    assert len(_open_keys(seeded_engine)) == 2


def test_a_rejected_row_stands_its_warehouse_row_down_and_says_so(seeded_engine):
    """The sharpest edge of a restatement, and the reason the preview spells it out.

    The file names the invoice; the loader refuses the row; the warehouse row is
    stood down with nothing put in its place. The book falls by that amount for a
    data-quality reason rather than because anything was settled — which is a
    different statement entirely, and one nobody should discover after the fact.

    The SPL extract does exactly this 75 times, for ৳23.76 Cr of bank FDRs, share
    investments and inter-company loans carrying ``#N/A`` in every hierarchy
    column.
    """
    do_import(seeded_engine, [credit_invoice_row(**{"Invoice No": "INV-1"})],
              restatement_scope=["C001"])

    # The same invoice comes back unusable rather than absent.
    result = do_import(seeded_engine, [credit_invoice_row(
        **{"Invoice No": "INV-1", "Zone_Code": "#N/A", "Region_Code": "#N/A",
           "Area_Code": "#N/A", "Unit_Code": "#N/A", "Territory_Code": "#N/A",
           "SubTerritory_Code": "#N/A"})], restatement_scope=["C001"])

    assert result.rejected_rows == 1
    assert result.restatement is not None
    assert result.restatement.voided_rows == 1
    assert result.restatement.voided_because_rejected == 1, (
        "counted apart from a settlement, because it is not one")
    assert result.restatement.voided_because_rejected_amount > 0
    assert _open_keys(seeded_engine) == set()


def test_a_dataset_that_declares_no_scope_field_refuses_to_be_restated(seeded_engine):
    """Nothing may be voided without a column to bound the claim with."""
    from app.etl.readers import RecordsSourceReader
    with pytest.raises(ValueError, match="cannot be restated"):
        run_import(seeded_engine, "sales",
                   RecordsSourceReader([], source_name="s.csv", source_type="CSV"),
                   source_system="TEST", restatement_scope=["C001"])


def test_the_restatement_is_visible_before_it_is_committed(seeded_engine):
    """A dry run reports the void without performing it.

    This is what makes the declared scope a decision rather than a formality: the
    preview states how many rows are about to be stood down and what they are
    worth, and the rows are still there afterwards.
    """
    do_import(seeded_engine, [credit_invoice_row(**{"Invoice No": f"INV-{n}"})
                              for n in (1, 2)], restatement_scope=["C001"])

    result = do_import(seeded_engine,
                       [credit_invoice_row(**{"Invoice No": "INV-1"})],
                       restatement_scope=["C001"], dry_run=True)

    assert result.restatement is not None and result.restatement.voided_rows == 1
    assert len(_open_keys(seeded_engine)) == 2, "the dry run changed nothing"


def test_a_declared_hold_spares_rows_the_scope_would_otherwise_void(seeded_engine):
    """A restatement may narrow its own claim, and only ever narrow it.

    The case is real and the first deployment produced it: a receivables extract
    for one company omitted an entire customer's thirteen invoices. Read
    mechanically that is thirteen settlements worth ৳2.81 Cr; read by somebody
    who knows the business it is an extract missing a customer. The scope is
    expressed per *company*, so it cannot express a doubt about one *customer* —
    and widening the scope is no answer, because the doubt is narrower than the
    scope rather than broader.

    So the upload declares what it holds out. Like the scope itself it is a
    declaration rather than an inference, and unlike the scope it can only ever
    spare rows: a hold cannot cause anything to be voided that would otherwise
    have survived, which is what makes it safe to accept from a caller.
    """
    do_import(seeded_engine, [
        credit_invoice_row(**{"Invoice No": "KEEP", "Customer": "CUST-001"}),
        credit_invoice_row(**{"Invoice No": "SETTLED", "Customer": "CUST-001"}),
        credit_invoice_row(**{"Invoice No": "DOUBTED", "Customer": "CUST-002"}),
    ], restatement_scope=["C001"])
    assert len(_open_keys(seeded_engine)) == 3

    # The next extract names only KEEP. Without a hold both others would be
    # stood down; CUST-002's absence is the one we do not accept.
    result = do_import(
        seeded_engine, [credit_invoice_row(**{"Invoice No": "KEEP",
                                              "Customer": "CUST-001"})],
        restatement_scope=["C001"],
        restatement_hold=("customer_code", ["CUST-002"]))

    assert result.restatement is not None
    assert result.restatement.voided_rows == 1, "SETTLED only"
    assert result.restatement.held_rows == 1
    assert result.restatement.held_amount > 0
    assert result.restatement.held_field == "customer_code"
    assert _open_keys(seeded_engine) == {("C001", "KEEP"), ("C001", "DOUBTED")}


def test_a_hold_is_reported_rather_than_left_to_be_inferred(seeded_engine):
    """A void smaller than its scope implies must say why it is smaller.

    Otherwise the only signal is a number that looks lower than expected, which
    is exactly the kind of thing nobody checks.
    """
    do_import(seeded_engine, [
        credit_invoice_row(**{"Invoice No": "A", "Customer": "CUST-002"})],
        restatement_scope=["C001"])
    result = do_import(
        seeded_engine, [credit_invoice_row(**{"Invoice No": "B",
                                              "Customer": "CUST-001"})],
        restatement_scope=["C001"],
        restatement_hold=("customer_code", ["CUST-002"]))

    payload = result.restatement.to_dict()
    assert payload["held"] == ["CUST-002"]
    assert payload["held_rows"] == 1
    assert payload["voided_rows"] == 0, "the only candidate was held out"


def test_a_hold_on_a_column_the_fact_lacks_is_refused(seeded_engine):
    """Rows cannot be held out by something no row states."""
    with pytest.raises(ValueError, match="not a column"):
        do_import(seeded_engine, [credit_invoice_row()],
                  restatement_scope=["C001"],
                  restatement_hold=("region_name", ["Dhaka"]))
