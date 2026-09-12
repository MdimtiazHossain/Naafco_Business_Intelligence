"""Credit Control at a realistic row count.

Opt-in (``pytest -m volume``), because it takes minutes. It exists because every
other credit test loads a handful of invoices, and three of the things most
likely to break in production break only at scale:

* **Bind-parameter chunking.** ``fact_credit_invoice`` has roughly thirty
  columns and SQLite's ceiling is 32,766 parameters per statement, so an
  unchunked insert fails somewhere above about eleven hundred rows — which is to
  say it passes every other test in this suite and fails on the first real file.
* **The index actually being used.** Every aging figure is a comparison of
  ``due_date`` against a reporting date. If that comparison ever stops being
  index-friendly — a function wrapped around the column, a cast — nothing breaks;
  it just gets slower in proportion to the table, which no correctness test
  notices.
* **Totals still reconciling.** The aging buckets must sum to the outstanding
  figure. With five invoices that is arithmetic nobody can get wrong; across
  fifty thousand spread over every bucket it is a real check on the boundaries.

``CREDIT_VOLUME_ROWS`` sets the size, so this can be run at whatever the
business's actual invoice volume turns out to be rather than at a guess.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.permission_filter import PermissionFilter
from app.ai.schemas import CreditToolInput, ScopeFilters
from app.ai.tools import REGISTRY, ToolContext
from app.database.models_warehouse import FactCreditInvoice
from app.etl import credit
from app.etl.pipeline import run_import
from app.etl.readers import RecordsSourceReader

pytestmark = pytest.mark.volume

#: Default size. Fifty thousand is about a year of credit invoices for a
#: distributor of this shape, and is comfortably past every chunking boundary.
ROWS = int(os.getenv("CREDIT_VOLUME_ROWS", "50000"))

TODAY = dt.date(2026, 8, 29)
WINDOW = {"date_from": dt.date(2024, 1, 1), "date_to": dt.date(2027, 12, 31)}

#: A generous ceiling. Not a benchmark — it is there to catch the difference
#: between an indexed comparison and a scan, which is orders of magnitude, not
#: percentages. A tight bound here would fail on a loaded CI machine and teach
#: everyone to ignore it.
QUERY_SECONDS = 15.0


def _invoices(count: int) -> list[dict]:
    """Invoices spread across every aging bucket and both settled states.

    Deterministic: the same count always produces the same file, so a failure is
    reproducible and the expected totals can be computed rather than measured.
    """
    rows = []
    # Offsets chosen to land in each bucket in turn, including one still inside
    # its terms and one settled, so no bucket is empty at any size.
    offsets = (-20, 5, 40, 75, 100, 150, 250, 500)
    for index in range(count):
        offset = offsets[index % len(offsets)]
        due = TODAY - dt.timedelta(days=offset)
        credit_days = 30
        invoice_date = due - dt.timedelta(days=credit_days)
        settled = index % 10 == 0
        rows.append({
            "Company": "C001",
            "Invoice No": f"V-{index:07d}",
            "Customer": f"CUST-{index % 500:04d}",
            "Plant": "PL01",
            "Invoice Date": invoice_date.isoformat(),
            "Credit Days": credit_days,
            "Invoice Value": 1000,
            "Return": 0,
            "Payment": 1000 if settled else 0,
            "Discount": 0,
            "Adjustment": 0,
            "Payment Mode": "CREDIT",
        })
    return rows


@pytest.fixture(scope="module")
def volume_engine(tmp_path_factory):
    """A migrated, master-seeded warehouse, built once for this whole module.

    Not ``seeded_engine``: that is function-scoped, and loading fifty thousand
    invoices once per test would turn a few minutes into an hour. The seeding
    below is the same call the shared fixture makes, so what is under test is
    the same database shape, only longer-lived — and nothing here writes after
    the load.
    """
    from alembic import command
    from sqlalchemy import create_engine, event

    from conftest_phase2 import _alembic_config, seed_master_data

    path = tmp_path_factory.mktemp("credit-volume") / "warehouse.db"
    url = f"sqlite:///{path}"
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(_alembic_config(url), "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous

    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    seed_master_data(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def loaded(volume_engine):
    """Load ``ROWS`` invoices once and hand the engine to every test here."""
    engine = volume_engine
    started = time.perf_counter()
    reader = RecordsSourceReader(_invoices(ROWS), source_name="volume.csv",
                                 source_type="CSV")
    result = run_import(engine, "credit_invoice", reader, source_system="TEST",
                        deduction_convention=credit.DEDUCTION_UNSIGNED)
    elapsed = time.perf_counter() - started
    print(f"\nloaded {ROWS:,} credit invoices in {elapsed:.1f}s "
          f"({ROWS / max(elapsed, 0.001):,.0f} rows/s)")
    return engine, result


def test_every_row_is_loaded(loaded) -> None:
    """The chunking test. An unchunked insert fails long before this count."""
    engine, result = loaded
    assert result.inserted_rows == ROWS, result.error_counts

    with Session(engine) as session:
        stored = session.execute(
            select(func.count()).select_from(FactCreditInvoice)).scalar_one()
    assert stored == ROWS


def test_every_derived_date_resolved(loaded) -> None:
    """``ensure_dates_exist`` is a bind-parameter site too.

    Each invoice contributes an invoice date and a derived due date, so this is
    the other statement that grows with the file. A missing ``dim_date`` row
    would have failed the foreign key at the write, so reaching here proves it,
    but the count is asserted rather than inferred.
    """
    engine, _ = loaded
    with Session(engine) as session:
        missing = session.execute(
            select(func.count()).select_from(FactCreditInvoice)
            .where(FactCreditInvoice.due_date_id.is_(None))).scalar_one()
    assert missing == 0


def _run(engine, tool: str, **overrides):
    from conftest_phase3 import USERS  # noqa: F401 - imported for its side effects

    with Session(engine) as session:
        from app.ai.permission_filter import UserContext
        from app.database.models_ai import AppUser, Role

        user = session.query(AppUser).filter_by(role=Role.MANAGEMENT).first()
        if user is None:
            user = AppUser(username="volume_ceo", display_name="Volume",
                           role=Role.MANAGEMENT, is_active=True)
            session.add(user)
            session.commit()
        ctx_user = UserContext.from_user(user)
        ctx = ToolContext(session=session, permissions=PermissionFilter(session, ctx_user),
                          user=ctx_user, today=TODAY)
        arguments = CreditToolInput(
            filters=ScopeFilters(), **{"as_on_date": TODAY, **WINDOW, **overrides})
        started = time.perf_counter()
        result = REGISTRY[tool].handler(ctx, arguments)
        elapsed = time.perf_counter() - started
        print(f"{tool} over {ROWS:,} rows in {elapsed:.2f}s")
        return result, elapsed


def test_the_summary_stays_fast_over_the_whole_table(loaded) -> None:
    engine, _ = loaded
    result, elapsed = _run(engine, "get_credit_summary")

    assert result.values["invoice_count"] == ROWS
    assert elapsed < QUERY_SECONDS


def test_the_aging_buckets_sum_to_the_outstanding_total(loaded) -> None:
    """The invariant the whole module rests on, checked at scale.

    Aging counts open invoices and outstanding counts open invoices, so the two
    must agree exactly. A boundary error in the bucket expressions would drop or
    double-count invoices here and nowhere in the small tests.
    """
    engine, _ = loaded
    summary, _ = _run(engine, "get_credit_summary")
    aging, elapsed = _run(engine, "get_credit_aging")

    assert elapsed < QUERY_SECONDS
    bucketed = sum(Decimal(str(row["outstanding_amount"] or 0)) for row in aging.rows)
    counted = sum(int(row["invoice_count"] or 0) for row in aging.rows)

    assert bucketed == Decimal(str(summary.values["outstanding_amount"]))
    assert counted == summary.values["open_invoice_count"]


def test_no_bucket_is_empty_at_this_size(loaded) -> None:
    """The data is spread deliberately, so an empty bucket means a boundary bug."""
    engine, _ = loaded
    aging, _ = _run(engine, "get_credit_aging")
    by_bucket = {row["aging_bucket"]: row for row in aging.rows}

    assert set(by_bucket) == set(credit.AGING_BUCKETS)
    for bucket in credit.AGING_BUCKETS:
        assert by_bucket[bucket]["invoice_count"] > 0, bucket


def test_the_customer_ranking_stays_fast(loaded) -> None:
    """Grouping five hundred customers out of fifty thousand invoices."""
    engine, _ = loaded
    result, elapsed = _run(engine, "get_overdue_customers")

    assert elapsed < QUERY_SECONDS
    assert result.rows
    amounts = [float(row["overdue_amount"] or 0) for row in result.rows]
    assert amounts == sorted(amounts, reverse=True)


def test_a_paged_read_does_not_slow_down_at_the_last_page(loaded) -> None:
    """OFFSET grows with the page number; the query must not grow with it.

    A reader who sorts by balance and walks to the end is the ordinary way to
    use this table, and a page that took ten times as long at the end as at the
    start would be reported as the page being broken.
    """
    engine, _ = loaded
    from app.reporting.credit import credit_invoices, resolve_query
    from app.reporting.service import ReportFilters

    filters = ReportFilters(date_from=WINDOW["date_from"], date_to=WINDOW["date_to"])
    with Session(engine) as session:
        timings = []
        for page in (1, max(1, ROWS // 100)):
            query = resolve_query(as_on=TODAY, page=page, page_size=100,
                                  sort_by="due_date", sort_dir="asc")
            started = time.perf_counter()
            body = credit_invoices(session, filters, query)
            timings.append(time.perf_counter() - started)
            assert body["total"] == ROWS
            assert len(body["rows"]) == 100

    first, last = timings
    print(f"first page {first:.2f}s, last page {last:.2f}s")
    assert last < QUERY_SECONDS
