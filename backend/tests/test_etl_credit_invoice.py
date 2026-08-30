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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models_warehouse import (
    BATCH_COMPLETED,
    DimDate,
    EtlRejectedRecord,
    FactCreditInvoice,
)
from app.etl import credit
from app.etl.pipeline import run_import
from app.etl.readers import RecordsSourceReader
from conftest_phase2 import credit_invoice_row


def do_import(engine, records, **kwargs):
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
        **{"Invoice Value": 100000, "Return": 20000, "Payment": -25000,
           "Discount": -5000, "Adjustment": -1000})])

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
        **{"Invoice Value": 100000, "Payment": -150000})])
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
        **{"Invoice Value": 100000, "Payment": -25000, "Balance": 60000})])

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
    """The business key is company + invoice number, so a re-upload corrects."""
    do_import(seeded_engine, [credit_invoice_row(**{"Payment": -25000})])
    result = do_import(seeded_engine, [credit_invoice_row(**{"Payment": -40000})])

    assert result.inserted_rows == 0
    with Session(seeded_engine) as session:
        assert session.execute(
            select(func.count()).select_from(FactCreditInvoice)).scalar_one() == 1
    (row,) = invoices(seeded_engine)
    assert Decimal(str(row.payment_amount)) == Decimal("-40000")
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
    result = do_import(seeded_engine, [
        credit_invoice_row(**{"Company": "C001", "Invoice No": "0001"}),
        credit_invoice_row(**{"Company": "C002", "Invoice No": "0001"}),
    ])
    assert result.inserted_rows == 2
