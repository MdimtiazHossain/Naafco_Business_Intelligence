"""The Credit Control endpoints: permission, scope, and the figures they report.

The arithmetic is pinned in ``test_credit_control.py`` and the loading in
``test_etl_credit_invoice.py``; this file is about what the four endpoints
publish, and about the two things that are specific to reading receivables:

* **``as_on_date`` genuinely moves the figures.** Overdue is a function of a
  date, so an endpoint that accepted the parameter and ignored it would look
  correct in every test that used the default.
* **A scope this view cannot express is refused, never dropped.** That is the
  one failure here that would be silent and serious.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.auth.security import hash_password
from app.database.models_ai import AppUser, Role
from app.etl import credit
from app.etl.pipeline import run_import
from app.etl.readers import RecordsSourceReader
from app.main import app
from app.reporting.credit import ScopeNotHonourable, assert_scope_is_honourable
from app.reporting.service import ReportFilters

PASSWORD = "TestPass123!"
BASE = "/api/reports/credit-control"

#: Fixed so the tests describe one arrangement of invoices rather than whatever
#: today happens to make of them.
AS_ON = dt.date(2026, 8, 29)


def _invoice(no: str, *, invoice_date: str, credit_days: int, value: int,
             payment: int = 0, customer: str = "CUST-001",
             company: str = "C001") -> dict:
    return {
        "Company": company, "Invoice No": no, "Customer": customer,
        "Plant": "PL01", "Invoice Date": invoice_date,
        "Credit Days": credit_days, "Invoice Value": value,
        "Return": 0, "Payment": payment, "Discount": 0, "Adjustment": 0,
        "Payment Mode": "CREDIT",
    }


#: Five invoices arranged around AS_ON so every status and several buckets are
#: represented, and the expected figures can be stated in the tests by hand.
#:
#:   OPEN-NYD    due 2026-09-10  -> not yet due, 100000 outstanding
#:   OPEN-30     due 2026-08-11  ->  18 days overdue,  50000 outstanding
#:   OPEN-90     due 2026-06-01  ->  89 days overdue,  70000 outstanding
#:   OPEN-OLD    due 2026-01-05  -> 236 days overdue,  30000 outstanding
#:   PAID        due 2026-07-01  -> cleared, nothing outstanding
INVOICES = [
    _invoice("OPEN-NYD", invoice_date="2026-08-11", credit_days=30, value=100000),
    _invoice("OPEN-30", invoice_date="2026-07-12", credit_days=30, value=50000),
    _invoice("OPEN-90", invoice_date="2026-03-03", credit_days=90, value=70000),
    _invoice("OPEN-OLD", invoice_date="2025-10-07", credit_days=90, value=30000,
             customer="CUST-002"),
    _invoice("PAID", invoice_date="2026-06-01", credit_days=30, value=40000,
             payment=-40000),
]


@pytest.fixture
def credit_api(agent_engine, users):
    """A client, with the five invoices loaded and passwords set."""
    reader = RecordsSourceReader(INVOICES, source_name="credit.csv", source_type="CSV")
    run_import(agent_engine, "credit_invoice", reader, source_system="TEST")

    with Session(agent_engine) as session:
        for user in session.query(AppUser).all():
            user.password_hash = hash_password(PASSWORD)
        session.commit()

    def _session_override():
        session = Session(bind=agent_engine, expire_on_commit=False, future=True)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _session_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def token(client: TestClient, username: str) -> dict[str, str]:
    response = client.post("/api/auth/login",
                           json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def get(client: TestClient, path: str, username: str = "ceo", **params):
    params.setdefault("as_on_date", AS_ON.isoformat())
    query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    return client.get(f"{path}?{query}", headers=token(client, username))


# ---------------------------------------------------------------------------
# Permission
# ---------------------------------------------------------------------------


def test_the_section_is_required(credit_api) -> None:
    """A regional manager does not hold Credit Control by default.

    Credit exposure is the basis for stopping a customer's supply, so it does not
    ride along with the reporting sections every role gets.
    """
    assert get(credit_api, BASE, username="dhaka_rm").status_code == 403


def test_management_holds_it(credit_api) -> None:
    assert get(credit_api, BASE).status_code == 200


def test_an_unauthenticated_request_is_refused(credit_api) -> None:
    assert credit_api.get(BASE).status_code == 401


def test_a_scope_this_view_cannot_express_is_refused_not_dropped() -> None:
    """The one failure here that would be silent and serious.

    ``_apply_filters`` ignores a filter naming a column the view lacks, so a
    region-scoped caller would have their scope dropped without comment and be
    served the whole company's receivables. Refusing names the levels instead.
    """
    class _Scoped:
        is_unrestricted = False
        data_scope = {"region_code": ["REG001"]}

    with pytest.raises(ScopeNotHonourable) as raised:
        assert_scope_is_honourable(_Scoped(), ReportFilters())
    assert "region_code" in str(raised.value)


def test_an_unrestricted_caller_has_no_scope_to_lose() -> None:
    class _Unrestricted:
        is_unrestricted = True
        data_scope = {"region_code": ["REG001"]}

    assert_scope_is_honourable(_Unrestricted(), ReportFilters())


def test_a_scope_the_view_can_express_is_allowed() -> None:
    """Company and customer are real columns on the view, so they are honoured."""
    class _ByCompany:
        is_unrestricted = False
        data_scope = {"company_code": ["C001"]}

    assert_scope_is_honourable(_ByCompany(), ReportFilters())


# ---------------------------------------------------------------------------
# The page bundle
# ---------------------------------------------------------------------------


def test_the_kpis_add_up(credit_api) -> None:
    body = get(credit_api, BASE).json()
    metrics = body["metrics"]

    assert metrics["invoice_count"] == 5
    assert metrics["total_invoice_amount"] == 290000.0
    assert metrics["payment_amount"] == 40000.0
    # Four open invoices; the paid one contributes nothing.
    assert metrics["outstanding_amount"] == 250000.0
    assert metrics["open_invoice_count"] == 4
    # Three of the four open invoices are past their due date on AS_ON.
    assert metrics["overdue_amount"] == 150000.0
    assert metrics["overdue_invoice_count"] == 3


def test_the_returns_total_is_reported_signed(credit_api) -> None:
    """The one figure that explains a net larger than a gross.

    Returns are posted negative by this source *and* subtracted, so they push
    the net figure above the invoice total — 1.59 Cr against 1.41 Cr on the first
    real file. The card carries this number so the difference is accounted for
    rather than mysterious, which means it must stay signed: flipped to a
    magnitude it would hide the very thing it is there to explain.
    """
    metrics = get(credit_api, BASE).json()["metrics"]
    assert "return_amount" in metrics

    # The identity the card relies on: net − gross is exactly minus the returns.
    difference = metrics["net_invoice_amount"] - metrics["total_invoice_amount"]
    assert difference == pytest.approx(-metrics["return_amount"])


def test_a_negative_return_pushes_net_above_gross(credit_api) -> None:
    """Reproduced from the real file, so the arithmetic is pinned end to end."""
    reader = RecordsSourceReader(
        [_invoice("RET-1", invoice_date="2026-08-01", credit_days=30, value=22700)
         | {"Return": -13221339.50}],
        source_name="returns.csv", source_type="CSV")
    from app.api.deps import get_session
    session = next(app.dependency_overrides[get_session]())
    run_import(session.get_bind(), "credit_invoice", reader, source_system="TEST")

    metrics = get(credit_api, BASE).json()["metrics"]
    assert metrics["return_amount"] < 0
    assert metrics["net_invoice_amount"] > metrics["total_invoice_amount"]


def test_a_share_is_suppressed_rather_than_shown_as_zero(credit_api) -> None:
    """A portfolio with nothing outstanding has no overdue *proportion*."""
    body = get(credit_api, BASE, company_code="NOSUCH").json()
    assert body["metrics"]["outstanding_amount"] in (0, 0.0, None)
    assert body["metrics"]["overdue_share_percent"] is None


def test_every_aging_bucket_is_reported_in_order(credit_api) -> None:
    """All eight, zeros included: an empty bucket is a fact, not a gap."""
    body = get(credit_api, BASE).json()
    buckets = [row["bucket"] for row in body["aging"]]
    assert buckets == list(credit.AGING_BUCKETS)

    by_bucket = {row["bucket"]: row for row in body["aging"]}
    assert by_bucket["NOT_YET_DUE"]["invoice_count"] == 1
    assert by_bucket["1-30"]["invoice_count"] == 1
    assert by_bucket["61-90"]["invoice_count"] == 1
    assert by_bucket["181-365"]["invoice_count"] == 1
    # Reported as zero rather than omitted.
    assert by_bucket["91-120"]["invoice_count"] == 0
    assert by_bucket["91-120"]["outstanding_amount"] == 0.0


def test_a_cleared_invoice_ages_nowhere(credit_api) -> None:
    body = get(credit_api, BASE).json()
    assert sum(row["invoice_count"] for row in body["aging"]) == 4, (
        "the four open invoices age; the paid one does not"
    )


def test_the_status_split_names_all_three(credit_api) -> None:
    body = get(credit_api, BASE).json()
    by_status = {row["status"]: row for row in body["status"]}
    assert set(by_status) == set(credit.CREDIT_STATUSES)
    assert by_status["CLEARED"]["invoice_count"] == 1
    assert by_status["OVER_DUE"]["invoice_count"] == 3
    assert by_status["NOT_YET_DUE"]["invoice_count"] == 1


def test_top_overdue_customers_are_ranked(credit_api) -> None:
    body = get(credit_api, BASE).json()
    rows = body["top_overdue_customers"]
    assert [row["customer_code"] for row in rows] == ["CUST-001", "CUST-002"]
    assert rows[0]["overdue_amount"] == 120000.0


def test_the_outstanding_trend_says_it_has_no_source(credit_api) -> None:
    """Not approximated, and not silently omitted.

    The source states one aggregate payment and one last payment date, so what
    was outstanding at a past month end is unrecorded. A chart of assumed history
    is indistinguishable on screen from a measured one.
    """
    trend = get(credit_api, BASE).json()["outstanding_trend"]
    assert trend["state"] == "NOT_AVAILABLE"
    assert trend["points"] == []
    assert "payment-transaction extract" in trend["reason"]


# ---------------------------------------------------------------------------
# as_on_date
# ---------------------------------------------------------------------------


def test_as_on_date_moves_the_overdue_figures(credit_api) -> None:
    """The parameter has to do something, or accepting it is a lie.

    On 2026-06-15 two invoices have passed their due date — OPEN-90 fell due on
    2026-06-01 and OPEN-OLD on 2026-01-05 — while by AS_ON a third has. An
    endpoint that read the view's own CURRENT_DATE columns would report the same
    number for both.
    """
    early = get(credit_api, BASE, as_on_date="2026-06-15").json()["metrics"]
    late = get(credit_api, BASE).json()["metrics"]

    assert early["overdue_invoice_count"] == 2
    assert late["overdue_invoice_count"] == 3
    assert early["overdue_amount"] < late["overdue_amount"]


def test_as_on_date_moves_an_invoice_between_buckets(credit_api) -> None:
    def bucket_of(as_on: str) -> str:
        rows = get(credit_api, f"{BASE}/invoices", as_on_date=as_on).json()["rows"]
        return next(r["aging_bucket"] for r in rows if r["invoice_no"] == "OPEN-90")

    assert bucket_of("2026-06-15") == "1-30"
    assert bucket_of(AS_ON.isoformat()) == "61-90"


def test_due_soon_uses_the_configured_horizon(credit_api) -> None:
    """Due Soon is the near edge of not-yet-due, and the window is a setting."""
    narrow = get(credit_api, BASE, due_soon_days=3).json()["metrics"]
    wide = get(credit_api, BASE, due_soon_days=30).json()["metrics"]

    assert narrow["due_soon_invoice_count"] == 0
    assert wide["due_soon_invoice_count"] == 1
    assert wide["due_soon_amount"] == 100000.0


def test_due_soon_and_overdue_do_not_double_count(credit_api) -> None:
    """An overdue invoice is overdue, not also due soon."""
    metrics = get(credit_api, BASE, due_soon_days=365).json()["metrics"]
    assert metrics["due_soon_invoice_count"] == 1, "only the not-yet-due one"
    assert metrics["overdue_invoice_count"] == 3


# ---------------------------------------------------------------------------
# The tables
# ---------------------------------------------------------------------------


def test_the_invoice_table_pages(credit_api) -> None:
    body = get(credit_api, f"{BASE}/invoices", page=1, page_size=2).json()
    assert body["total"] == 5
    assert body["total_pages"] == 3
    assert len(body["rows"]) == 2

    last = get(credit_api, f"{BASE}/invoices", page=3, page_size=2).json()
    assert len(last["rows"]) == 1


def test_the_invoice_table_sorts_on_a_whitelisted_column(credit_api) -> None:
    rows = get(credit_api, f"{BASE}/invoices",
               sort_by="balance_amount", sort_dir="desc").json()["rows"]
    balances = [float(r["balance_amount"]) for r in rows]
    assert balances == sorted(balances, reverse=True)


def test_an_unknown_sort_column_falls_back_rather_than_interpolating(credit_api):
    response = get(credit_api, f"{BASE}/invoices", sort_by="1;DROP TABLE x")
    assert response.status_code == 200
    assert len(response.json()["rows"]) == 5


def test_search_narrows_the_invoice_table(credit_api) -> None:
    body = get(credit_api, f"{BASE}/invoices", search="OPEN-90").json()
    assert body["total"] == 1
    assert body["rows"][0]["invoice_no"] == "OPEN-90"


def test_the_customer_table_totals_exposure(credit_api) -> None:
    body = get(credit_api, f"{BASE}/customers").json()
    by_customer = {row["customer_code"]: row for row in body["rows"]}

    assert by_customer["CUST-001"]["invoice_count"] == 4
    assert by_customer["CUST-001"]["outstanding_amount"] == 220000.0
    assert by_customer["CUST-002"]["outstanding_amount"] == 30000.0
    assert body["portfolio_outstanding"] == 250000.0


def test_credit_exposure_is_a_share_of_the_whole_portfolio(credit_api) -> None:
    """Not of the page: a customer's exposure does not change with pagination."""
    body = get(credit_api, f"{BASE}/customers").json()
    by_customer = {row["customer_code"]: row for row in body["rows"]}
    assert by_customer["CUST-002"]["credit_exposure_percent"] == 12.0
    assert sum(r["credit_exposure_percent"] for r in body["rows"]) == 100.0


# ---------------------------------------------------------------------------
# The detail panel
# ---------------------------------------------------------------------------


def test_one_invoice_comes_back_with_its_derived_state(credit_api) -> None:
    body = get(credit_api, f"{BASE}/invoices/C001/OPEN-90").json()
    invoice = body["invoice"]
    assert invoice["invoice_no"] == "OPEN-90"
    assert invoice["credit_status"] == credit.STATUS_OVER_DUE
    assert invoice["days_overdue"] == 89
    assert invoice["aging_bucket"] == "61-90"


def test_a_missing_invoice_is_404(credit_api) -> None:
    assert get(credit_api, f"{BASE}/invoices/C001/NOPE").status_code == 404


def test_an_invoice_is_addressed_by_company_and_number(credit_api) -> None:
    """The number alone is unique only within its company."""
    assert get(credit_api, f"{BASE}/invoices/C999/OPEN-90").status_code == 404


def test_the_payment_timeline_says_what_it_cannot_show(credit_api) -> None:
    """One event for one aggregate payment, never a reconstructed schedule.

    This invoice was paid in full and the file states no Last Payment Date, so
    the event is reported *undated* rather than dropped: the Financial Summary
    beside it shows the money, and a timeline that omitted it would contradict
    the panel it sits in.
    """
    body = get(credit_api, f"{BASE}/invoices/C001/PAID").json()
    events = body["payment_events"]
    assert len(events) == 1, "one aggregate payment, not a reconstructed schedule"
    assert events[0]["amount"] == 40000.0
    assert events[0]["date"] is None
    assert "no last payment date" in events[0]["note"]
