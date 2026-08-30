"""Credit Control's warehouse layer: the derivations, and the SQL that mirrors them.

Checked against a database built by ``alembic upgrade head`` rather than by
``create_all``, so what is asserted is what a real deployment gets — the three
views in particular, which ``create_all`` does not produce at all.

The test that matters most here is
:func:`test_view_and_python_agree_on_every_boundary`. Revision 0031 freezes the
aging and status rules as literal SQL, because a migration has to keep producing
the same schema forever; :mod:`app.etl.credit` holds the same rules in Python,
because the ETL goes on being edited. Two copies of one rule drift, and the drift
would be silent — the view bucketing an invoice one way and the loader another,
each looking right in isolation. So rather than trusting the copies to match,
every boundary day is walked through both.
"""

from __future__ import annotations

import os
import shutil
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect as sa_inspect, text
from sqlalchemy.exc import IntegrityError

from app.etl import credit

BACKEND_DIR = Path(__file__).resolve().parents[1]

#: Every day either implementation treats specially, plus one either side of it.
#: Boundaries are where a bucketing rule goes wrong, and an off-by-one inside a
#: bucket is invisible from any other test.
BOUNDARY_DAYS: tuple[int, ...] = (
    0, 1, 2, 29, 30, 31, 32, 59, 60, 61, 89, 90, 91, 92,
    119, 120, 121, 179, 180, 181, 182, 364, 365, 366, 367, 500, 1000,
)


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option(
        "script_location", str(BACKEND_DIR / "app" / "database" / "migrations")
    )
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _migrate(path: Path, revision: str = "head", *, down: bool = False) -> None:
    """Migrate the database at ``path`` — and *only* the one at ``path``.

    Setting the environment variable is the whole mechanism, and forgetting it is
    the trap. ``alembic.ini``'s ``sqlalchemy.url`` is **ignored**: ``env.py``
    resolves its own target as ``os.getenv("DIRECT_URL") or
    get_settings().database_url``, and that property reads ``DATABASE_URL`` from
    the environment at access time. So a migration run without either variable
    set does not fail — it succeeds, against whatever this machine's untracked
    ``.env`` names, which on a developer box is ``data/dev.db``.

    That is why the target is asserted rather than assumed. Getting it wrong is
    silent in the worst way: Alembic reports a perfectly successful upgrade, of
    the wrong database, and the test then fails for some unrelated-looking reason
    somewhere else entirely.
    """
    url = f"sqlite:///{path}"
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        from app.config import get_settings
        from app.database.connection import assert_migration_safe

        assert get_settings().database_url == url, (
            "the migration would not have gone where this test intends"
        )
        assert_migration_safe(url)
        if down:
            command.downgrade(_alembic_config(url), revision)
        else:
            command.upgrade(_alembic_config(url), revision)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


@pytest.fixture(scope="module")
def _migrated_template(tmp_path_factory) -> Path:
    """Build the schema once for the whole module, and hand back the file.

    ``upgrade head`` per test means thirty-one revisions per test, and this
    module has forty of them — minutes of migration to exercise seconds of SQL.
    Each test copies this file instead, which is a few kilobytes and gives
    exactly the same isolation: every test still starts from an empty warehouse
    that a real deployment would recognise.
    """
    path = tmp_path_factory.mktemp("credit-template") / "template.db"
    _migrate(path)
    assert path.exists(), "the template was migrated somewhere other than here"
    return path


@pytest.fixture()
def migrated_engine(_migrated_template, tmp_path):
    """A throwaway SQLite database at head, with foreign keys enforced.

    ``PRAGMA foreign_keys=ON`` matters: without it SQLite accepts a foreign key
    pointing at nothing, and the date-dimension assertions below would pass
    vacuously.
    """
    database = tmp_path / "credit.db"
    shutil.copyfile(_migrated_template, database)
    url = f"sqlite:///{database}"

    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    try:
        yield engine
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Building invoices
# ---------------------------------------------------------------------------


def _date_id(value: date) -> int:
    return value.year * 10000 + value.month * 100 + value.day


def _ensure_date(conn, value: date) -> int:
    """Insert one ``dim_date`` row if absent, and return its id.

    Only the columns the views read carry meaningful values; the rest are filled
    with something structurally valid. This is not a substitute for
    ``build_dim_date`` — it is the minimum that lets a foreign key resolve.
    """
    date_id = _date_id(value)
    if conn.execute(
        text("SELECT date_id FROM dim_date WHERE date_id = :i"), {"i": date_id}
    ).scalar():
        return date_id
    quarter = (value.month - 1) // 3 + 1
    conn.execute(
        text(
            "INSERT INTO dim_date (date_id, full_date, day, month, month_name, "
            "month_number, quarter, quarter_name, year, week, week_name, "
            "financial_year, financial_month, financial_quarter, is_month_end, "
            "is_quarter_end, is_year_end, is_financial_year_end) VALUES "
            "(:i, :d, :day, :m, :mn, :m, :q, :qn, :y, :w, :wn, :fy, :fm, :fq, "
            "0, 0, 0, 0)"
        ),
        {
            "i": date_id, "d": value.isoformat(), "day": value.day,
            "m": value.month, "mn": value.strftime("%b"),
            "q": quarter, "qn": f"Q{quarter}", "y": value.year,
            "w": value.isocalendar()[1], "wn": f"W{value.isocalendar()[1]}",
            "fy": f"{value.year}-{str(value.year + 1)[2:]}",
            "fm": value.month, "fq": quarter,
        },
    )
    return date_id


def _batch(conn) -> int:
    # The row counters are NOT NULL with a *Python-side* default, so the ORM
    # fills them and a raw INSERT has to do it itself.
    conn.execute(
        text(
            "INSERT INTO etl_import_batches (batch_uuid, source_type, source_system, "
            "data_type, load_mode, status, total_rows, successful_rows, failed_rows, "
            "duplicate_rows, inserted_rows, updated_rows) VALUES (:u, 'EXCEL', "
            "'TEST', 'credit_invoice', 'INCREMENTAL', 'COMPLETED', 0, 0, 0, 0, 0, 0)"
        ),
        {"u": str(uuid.uuid4())},
    )
    return conn.execute(text("SELECT max(batch_id) FROM etl_import_batches")).scalar()


def _insert_invoice(
    conn,
    batch_id: int,
    *,
    invoice_no: str,
    invoice_date: date,
    credit_days: int,
    company_code: str = "1000",
    customer_code: str = "C-1042",
    invoice_value: Decimal = Decimal("100000"),
    return_amount: Decimal = Decimal("0"),
    payment_amount: Decimal = Decimal("0"),
    discount_amount: Decimal = Decimal("0"),
    adjustment_amount: Decimal = Decimal("0"),
    is_void: bool = False,
) -> credit.Derived:
    """Load one invoice exactly the way the ETL will: derive, then store.

    The point of routing through :func:`credit.derive` here rather than writing
    the numbers by hand is that the fixture cannot disagree with the loader about
    what a balance is. A test that hand-computed its own balance would keep
    passing after the derivation broke.
    """
    derived = credit.derive(
        invoice_date=invoice_date,
        credit_days=credit_days,
        invoice_value=invoice_value,
        return_amount=return_amount,
        payment_amount=payment_amount,
        discount_amount=discount_amount,
        adjustment_amount=adjustment_amount,
    )
    invoice_date_id = _ensure_date(conn, invoice_date)
    due_date_id = _ensure_date(conn, derived.due_date)
    conn.execute(
        text(
            "INSERT INTO fact_credit_invoice (company_code, invoice_no, "
            "customer_code, invoice_date_id, due_date_id, credit_days, "
            "invoice_value, return_amount, payment_amount, discount_amount, "
            "adjustment_amount, net_invoice_amount, balance_amount, "
            "data_quality_flag, payment_mode, source_system, business_key, "
            "import_batch_id, is_void) VALUES (:company, :no, :cust, :inv_d, "
            ":due_d, :days, :value, :ret, :pay, :disc, :adj, :net, :bal, "
            ":flag, 'CREDIT', 'TEST', :bk, :batch, :void)"
        ),
        {
            "company": company_code, "no": invoice_no, "cust": customer_code,
            "inv_d": invoice_date_id, "due_d": due_date_id, "days": credit_days,
            "value": float(invoice_value), "ret": float(return_amount),
            "pay": float(payment_amount), "disc": float(discount_amount),
            "adj": float(adjustment_amount),
            "net": float(derived.net_invoice_amount),
            "bal": float(derived.balance_amount),
            "flag": derived.data_quality_flag,
            "bk": f"{company_code}|{invoice_no}", "batch": batch_id,
            "void": 1 if is_void else 0,
        },
    )
    return derived


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_migration_creates_the_tables_and_views(migrated_engine):
    inspector = sa_inspect(migrated_engine)
    assert "fact_credit_invoice" in inspector.get_table_names()
    assert "stg_credit_invoice" in inspector.get_table_names()
    views = set(inspector.get_view_names())
    assert {
        "vw_credit_invoice_detail",
        "vw_customer_credit_exposure",
        "vw_credit_aging",
    } <= views


def test_the_views_are_queryable_when_empty(migrated_engine):
    """An empty warehouse answers with no rows, not with an error.

    The page loads before any Credit Invoice file has been uploaded, and it has
    to render its empty state rather than a 500.
    """
    with migrated_engine.connect() as conn:
        for view in (
            "vw_credit_invoice_detail",
            "vw_customer_credit_exposure",
            "vw_credit_aging",
        ):
            assert conn.execute(text(f"SELECT count(*) FROM {view}")).scalar() == 0


def test_invoice_number_is_unique_within_a_company_not_globally(migrated_engine):
    """Two group companies may each number an invoice 0001; one company may not."""
    today = date.today()
    with migrated_engine.begin() as conn:
        batch = _batch(conn)
        _insert_invoice(conn, batch, invoice_no="0001", company_code="1000",
                        invoice_date=today, credit_days=30)
        _insert_invoice(conn, batch, invoice_no="0001", company_code="2000",
                        invoice_date=today, credit_days=30)

    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as conn:
            batch = conn.execute(
                text("SELECT max(batch_id) FROM etl_import_batches")
            ).scalar()
            _insert_invoice(conn, batch, invoice_no="0001", company_code="1000",
                            invoice_date=today, credit_days=45)


def test_the_fact_stores_no_date_relative_column(migrated_engine):
    """``days_overdue`` and friends must not reappear as stored columns.

    They were stored in the specification this module was built from, and the
    reason they are not stored is easy to undo by accident: somebody adds the
    column because an index would be convenient, and every figure it holds is
    wrong the next morning. The view is where they belong.
    """
    columns = {c["name"] for c in sa_inspect(migrated_engine).get_columns(
        "fact_credit_invoice")}
    assert {"days_overdue", "aging_bucket", "credit_status"}.isdisjoint(columns)
    # ...while the stable derivations *are* stored, because nothing but a new
    # upload can change them.
    assert {"net_invoice_amount", "balance_amount", "due_date_id"} <= columns


# ---------------------------------------------------------------------------
# The derivations
# ---------------------------------------------------------------------------


def test_net_and_balance_follow_the_measured_signs():
    """Payment, discount and adjustment are added; return is subtracted.

    The source posts a payment as a negative number, so subtracting one *added*
    it and an invoice paid in full came out at roughly twice its value. Return
    goes the other way: a returned-goods document arrives negative and increases
    what is owed on the invoice it offsets. Measured over a real 16,614-row file
    against the source's own balance: 54% for the original all-subtract rule,
    93.5% for all-add, 95.0% for this mixture.
    """
    derived = credit.derive(
        invoice_date=date(2026, 3, 12),
        credit_days=90,
        invoice_value=Decimal("4850000"),
        return_amount=Decimal("50000"),
        payment_amount=Decimal("-1200000"),
        discount_amount=Decimal("-50000"),
        adjustment_amount=Decimal("-25000"),
    )
    assert derived.net_invoice_amount == Decimal("4800000")
    assert derived.balance_amount == Decimal("3525000")
    assert derived.due_date == date(2026, 6, 10)
    assert derived.data_quality_flag is None


def test_a_payment_that_settles_an_invoice_clears_it():
    """The regression the whole correction exists for.

    Invoice AI00000002 of the first real file: value 86,970, payment -86,970,
    discount -1,739. The source says -1,739; the old arithmetic said 175,679 —
    an invoice that was paid in full reported as owing twice its value.
    """
    derived = credit.derive(
        invoice_date=date(2026, 1, 1),
        credit_days=0,
        invoice_value=Decimal("86970"),
        payment_amount=Decimal("-86970"),
        discount_amount=Decimal("-1739"),
    )
    assert derived.balance_amount == Decimal("-1739")
    assert credit.credit_status(
        balance_amount=derived.balance_amount, due_date=derived.due_date,
        as_on=date(2026, 8, 29)) == credit.STATUS_CLEARED


def test_a_negative_return_increases_what_is_owed():
    """The one column that goes the other way, pinned so it cannot drift back."""
    derived = credit.derive(
        invoice_date=date(2026, 1, 1), credit_days=30,
        invoice_value=Decimal("22700"), return_amount=Decimal("-13221339.50"),
    )
    assert derived.balance_amount == Decimal("13244039.50")


def test_a_positive_adjustment_still_increases_the_balance():
    """Signed means signed, in both directions.

    A credit note arrives negative and reduces what is owed; a debit note
    arrives positive and increases it. One column, one rule, no second sign
    convention to remember.
    """
    derived = credit.derive(
        invoice_date=date(2026, 1, 1), credit_days=30,
        invoice_value=Decimal("1000"), adjustment_amount=Decimal("250"),
    )
    assert derived.balance_amount == Decimal("1250")


def test_a_negative_balance_is_kept_and_flagged():
    """Over-adjustment is reported, never floored to zero."""
    derived = credit.derive(
        invoice_date=date(2026, 1, 1),
        credit_days=30,
        invoice_value=Decimal("100000"),
        payment_amount=Decimal("-150000"),
    )
    assert derived.balance_amount == Decimal("-50000")
    assert derived.data_quality_flag == credit.FLAG_BALANCE_NEGATIVE


def test_a_stated_due_date_wins_and_the_disagreement_is_still_flagged():
    """The stated date is the fact; the terms column is what is missing.

    This was the other way round. The reasoning then was that a source computing
    a due date from terms it had not sent us should not move a reported figure —
    but 11,791 rows of the first real file state ``credit_days`` of 0 alongside a
    real due date, so deriving handed back the invoice date and made every one of
    them look immediately overdue.
    """
    derived = credit.derive(
        invoice_date=date(2026, 1, 1),
        credit_days=30,
        invoice_value=Decimal("1000"),
        stated_due_date=date(2026, 3, 1),
    )
    assert derived.due_date == date(2026, 3, 1)
    assert derived.data_quality_flag == credit.FLAG_DUE_DATE_MISMATCH


def test_a_due_date_is_derived_only_when_the_file_states_none():
    derived = credit.derive(
        invoice_date=date(2026, 1, 1), credit_days=30,
        invoice_value=Decimal("1000"),
    )
    assert derived.due_date == date(2026, 1, 31)
    assert derived.data_quality_flag is None


def test_zero_credit_days_is_an_ordinary_term_not_an_anomaly():
    """Three quarters of the first real file carried it.

    Flagging that many rows would teach everyone to ignore the flag, which costs
    more than it catches.
    """
    derived = credit.derive(
        invoice_date=date(2026, 1, 1), credit_days=0,
        invoice_value=Decimal("1000"), stated_due_date=date(2026, 2, 15),
    )
    assert credit.FLAG_CREDIT_DAYS_UNEXPECTED not in (derived.data_quality_flag or "")
    assert derived.due_date == date(2026, 2, 15)


def test_a_stated_balance_that_disagrees_is_flagged():
    derived = credit.derive(
        invoice_date=date(2026, 1, 1),
        credit_days=30,
        invoice_value=Decimal("1000"),
        stated_balance=Decimal("900"),
    )
    assert derived.data_quality_flag == credit.FLAG_BALANCE_MISMATCH


def test_a_rounding_difference_is_not_a_mismatch():
    """A paisa apart is two systems rounding, not a contradiction."""
    derived = credit.derive(
        invoice_date=date(2026, 1, 1),
        credit_days=30,
        invoice_value=Decimal("1000.00"),
        stated_balance=Decimal("1000.01"),
    )
    assert derived.data_quality_flag is None


def test_unexpected_credit_terms_are_flagged_not_rejected():
    derived = credit.derive(
        invoice_date=date(2026, 1, 1),
        credit_days=60,
        invoice_value=Decimal("1000"),
    )
    assert derived.data_quality_flag == credit.FLAG_CREDIT_DAYS_UNEXPECTED
    assert derived.due_date == date(2026, 3, 2)


def test_several_faults_are_all_reported():
    """A row can be wrong twice, and a reviewer needs to see both."""
    derived = credit.derive(
        invoice_date=date(2026, 1, 1),
        credit_days=60,
        invoice_value=Decimal("100"),
        payment_amount=Decimal("-500"),
    )
    flags = set(derived.data_quality_flag.split("|"))
    assert flags == {credit.FLAG_BALANCE_NEGATIVE, credit.FLAG_CREDIT_DAYS_UNEXPECTED}


def test_a_cleared_invoice_has_no_aging_bucket():
    """Aging measures money still owed; a settled row is not late, it is done."""
    due = date(2020, 1, 1)
    assert credit.aging_bucket(
        balance_amount=Decimal("0"), due_date=due, as_on=date(2026, 8, 29)
    ) is None
    assert credit.credit_status(
        balance_amount=Decimal("0"), due_date=due, as_on=date(2026, 8, 29)
    ) == credit.STATUS_CLEARED


def test_a_negative_balance_reads_as_cleared_and_ages_nowhere():
    due = date(2020, 1, 1)
    assert credit.credit_status(
        balance_amount=Decimal("-1"), due_date=due, as_on=date(2026, 8, 29)
    ) == credit.STATUS_CLEARED
    assert credit.aging_bucket(
        balance_amount=Decimal("-1"), due_date=due, as_on=date(2026, 8, 29)
    ) is None


def test_the_buckets_tile_the_whole_range_without_overlap():
    """Every day from 1 to 1000 lands in exactly one bucket.

    A gap would silently drop an invoice out of the aging report; an overlap
    would let the first matching rule win and make the later bucket unreachable.
    Neither shows up in a spot check of a few days.
    """
    seen = []
    for day in range(1, 1001):
        matches = [
            code for code, lower, upper in credit.OVERDUE_BUCKETS
            if day >= lower and (upper is None or day <= upper)
        ]
        assert len(matches) == 1, f"{day} days matched {matches}"
        seen.append(matches[0])
    assert set(seen) == {code for code, _l, _u in credit.OVERDUE_BUCKETS}


# ---------------------------------------------------------------------------
# The SQL and the Python must not drift apart
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("overdue_by", BOUNDARY_DAYS)
def test_view_and_python_agree_on_every_boundary(migrated_engine, overdue_by):
    """The frozen SQL in 0031 and the live Python in ``etl.credit`` must match.

    One invoice is loaded whose due date is exactly ``overdue_by`` days before
    today, and the view's ``days_overdue``, ``credit_status`` and
    ``aging_bucket`` are compared against what the module computes for the same
    day. This is the only thing standing between the two copies and a silent
    divergence.
    """
    today = date.today()
    due = today - timedelta(days=overdue_by)
    credit_days = 30
    invoice_date = due - timedelta(days=credit_days)

    with migrated_engine.begin() as conn:
        batch = _batch(conn)
        derived = _insert_invoice(
            conn, batch, invoice_no=f"INV-{overdue_by}",
            invoice_date=invoice_date, credit_days=credit_days,
            invoice_value=Decimal("100000"),
        )

    with migrated_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT days_overdue, credit_status, aging_bucket "
            "FROM vw_credit_invoice_detail WHERE invoice_no = :no"
        ), {"no": f"INV-{overdue_by}"}).one()

    assert row.days_overdue == credit.days_overdue(due_date=due, as_on=today)
    assert row.credit_status == credit.credit_status(
        balance_amount=derived.balance_amount, due_date=due, as_on=today)
    assert row.aging_bucket == credit.aging_bucket(
        balance_amount=derived.balance_amount, due_date=due, as_on=today)


def test_the_view_reports_every_bucket_the_module_declares(migrated_engine):
    """Load one invoice into each bucket and confirm the SQL can name them all.

    A ``CASE`` arm nobody reaches is a bucket the aging chart would never show,
    and the per-boundary test above would not catch a *missing* arm if its days
    fell through to a neighbour.
    """
    today = date.today()
    with migrated_engine.begin() as conn:
        batch = _batch(conn)
        # One invoice per overdue bucket, plus one still inside its terms.
        for index, (code, lower, _upper) in enumerate(credit.OVERDUE_BUCKETS):
            due = today - timedelta(days=lower)
            _insert_invoice(
                conn, batch, invoice_no=f"B{index}",
                invoice_date=due - timedelta(days=30), credit_days=30,
            )
        future_due = today + timedelta(days=10)
        _insert_invoice(
            conn, batch, invoice_no="NYD",
            invoice_date=future_due - timedelta(days=30), credit_days=30,
        )

    with migrated_engine.connect() as conn:
        buckets = {
            r.aging_bucket for r in conn.execute(text(
                "SELECT aging_bucket FROM vw_credit_aging")).all()
        }
    assert buckets == set(credit.AGING_BUCKETS)


def test_cleared_and_voided_invoices_leave_the_aging_view(migrated_engine):
    """Two different exclusions, both of which must actually take effect.

    A cleared invoice is excluded because it owes nothing; a voided one because
    every reporting view in this schema filters ``is_void``. Either leaking would
    overstate outstanding money.
    """
    today = date.today()
    overdue_invoice_date = today - timedelta(days=100)
    with migrated_engine.begin() as conn:
        batch = _batch(conn)
        _insert_invoice(conn, batch, invoice_no="OPEN",
                        invoice_date=overdue_invoice_date, credit_days=30)
        _insert_invoice(conn, batch, invoice_no="PAID",
                        invoice_date=overdue_invoice_date, credit_days=30,
                        invoice_value=Decimal("100000"),
                        payment_amount=Decimal("-100000"))
        _insert_invoice(conn, batch, invoice_no="VOID",
                        invoice_date=overdue_invoice_date, credit_days=30,
                        is_void=True)

    with migrated_engine.connect() as conn:
        detail = {r.invoice_no for r in conn.execute(text(
            "SELECT invoice_no FROM vw_credit_invoice_detail")).all()}
        aged = conn.execute(text(
            "SELECT SUM(invoice_count) FROM vw_credit_aging")).scalar()
        statuses = dict(conn.execute(text(
            "SELECT invoice_no, credit_status FROM vw_credit_invoice_detail")).all())

    assert detail == {"OPEN", "PAID"}, "a voided invoice must leave every view"
    assert aged == 1, "only the open invoice ages"
    assert statuses["PAID"] == credit.STATUS_CLEARED


def test_customer_exposure_totals_only_positive_balances(migrated_engine):
    """An over-adjusted invoice must not reduce the portfolio it is not part of.

    Letting its negative balance net off against real debt would understate
    outstanding money by the size of a data-quality problem, which is the one
    direction a receivables figure must never be wrong in.
    """
    today = date.today()
    invoice_date = today - timedelta(days=100)
    with migrated_engine.begin() as conn:
        batch = _batch(conn)
        _insert_invoice(conn, batch, invoice_no="OWED", invoice_date=invoice_date,
                        credit_days=30, invoice_value=Decimal("100000"))
        _insert_invoice(conn, batch, invoice_no="OVERPAID", invoice_date=invoice_date,
                        credit_days=30, invoice_value=Decimal("100000"),
                        payment_amount=Decimal("-150000"))

    with migrated_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT invoice_count, outstanding_amount, overdue_amount, "
            "overdue_invoice_count, cleared_invoice_count "
            "FROM vw_customer_credit_exposure")).one()

    assert row.invoice_count == 2
    assert Decimal(str(row.outstanding_amount)) == Decimal("100000")
    assert Decimal(str(row.overdue_amount)) == Decimal("100000")
    assert row.overdue_invoice_count == 1
    assert row.cleared_invoice_count == 1


def test_customer_exposure_keeps_company_in_the_grain(migrated_engine):
    """One customer trading with two companies has two sets of books.

    Collapsing them would make a company-filtered report unable to answer for
    either.
    """
    today = date.today()
    invoice_date = today - timedelta(days=100)
    with migrated_engine.begin() as conn:
        batch = _batch(conn)
        _insert_invoice(conn, batch, invoice_no="A", company_code="1000",
                        invoice_date=invoice_date, credit_days=30)
        _insert_invoice(conn, batch, invoice_no="B", company_code="2000",
                        invoice_date=invoice_date, credit_days=30)

    with migrated_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT company_code, invoice_count FROM vw_customer_credit_exposure "
            "ORDER BY company_code")).all()

    assert [(r.company_code, r.invoice_count) for r in rows] == [("1000", 1), ("2000", 1)]


def test_an_invoice_survives_a_customer_the_master_has_not_received(migrated_engine):
    """A LEFT JOIN, so an unmapped customer keeps its figures and shows its code.

    The alternative — the invoice vanishing from the total — is how a
    receivables report quietly under-reports what is owed.
    """
    today = date.today()
    with migrated_engine.begin() as conn:
        batch = _batch(conn)
        _insert_invoice(conn, batch, invoice_no="ORPHAN", customer_code="C-9999",
                        invoice_date=today - timedelta(days=100), credit_days=30)

    with migrated_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT customer_code, customer_name, balance_amount "
            "FROM vw_credit_invoice_detail WHERE invoice_no = 'ORPHAN'")).one()

    assert row.customer_code == "C-9999"
    assert row.customer_name is None
    assert Decimal(str(row.balance_amount)) == Decimal("100000")


def test_downgrade_removes_the_module(migrated_engine, tmp_path):
    database = tmp_path / "credit.db"
    _migrate(database, "0030_target_revision_node", down=True)

    inspector = sa_inspect(migrated_engine)
    tables = set(inspector.get_table_names())
    assert "fact_credit_invoice" not in tables
    assert "stg_credit_invoice" not in tables
    assert not {
        "vw_credit_invoice_detail",
        "vw_customer_credit_exposure",
        "vw_credit_aging",
    } & set(inspector.get_view_names())
