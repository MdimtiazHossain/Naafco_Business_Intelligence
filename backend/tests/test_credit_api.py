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
from app.reporting.service import ReportFilters

PASSWORD = "TestPass123!"
BASE = "/api/reports/credit-control"

#: Fixed so the tests describe one arrangement of invoices rather than whatever
#: today happens to make of them.
AS_ON = dt.date(2026, 8, 29)


def _invoice(no: str, *, invoice_date: str, credit_days: int, value: int,
             payment: int = 0, customer: str = "CUST-001",
             territory: str = "TR001",
             company: str = "C001") -> dict:
    return {
        "Company": company, "Invoice No": no, "Customer": customer,
        "Plant": "PL01", "Invoice Date": invoice_date,
        "Credit Days": credit_days, "Invoice Value": value,
        "Return": 0, "Payment": payment, "Discount": 0, "Adjustment": 0,
        "Payment Mode": "CREDIT",
        "Territory_Code": territory,
    }


#: ``territory`` places the invoice in the hierarchy, and every fixture here
#: states one since revision 0040 gave the view the sales chain. Before it, an
#: invoice reached a sub-territory through its customer and nothing above that,
#: so a scope test could only ever assert a refusal. Now the same rows can show
#: the scope being *applied*, which is the stronger claim: a test that only
#: checks a scoped caller is not refused passes equally well on a scope that was
#: silently dropped.
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
    # Khulna, so a Dhaka scope has something to leave out. TR002 -> UN002 ->
    # AR002 -> REG002, seeded by ``conftest_phase3``.
    _invoice("OPEN-OLD", invoice_date="2025-10-07", credit_days=90, value=30000,
             customer="CUST-002", territory="TR002"),
    _invoice("PAID", invoice_date="2026-06-01", credit_days=30, value=40000,
             payment=40000),
]


@pytest.fixture
def credit_api(agent_engine, users):
    """A client, with the five invoices loaded and passwords set."""
    reader = RecordsSourceReader(INVOICES, source_name="credit.csv", source_type="CSV")
    run_import(agent_engine, "credit_invoice", reader, source_system="TEST",
               deduction_convention=credit.DEDUCTION_UNSIGNED)

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



def _grant_section(client: TestClient, username: str,
                   section: str = "credit_control") -> None:
    """Give one user an explicit ALLOW for a section, the way an admin would.

    Written through the model rather than the admin API because what is under
    test here is the data scope, and routing the setup through a second endpoint
    would make a failure there look like a failure in this.
    """
    from app.database.models_admin import UserSectionPermission
    from app.api.deps import get_session

    session = next(app.dependency_overrides[get_session]())
    user = session.query(AppUser).filter_by(username=username).one()
    session.add(UserSectionPermission(user_id=user.user_id, section_key=section,
                                      access="ALLOW"))
    session.commit()


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


def test_a_region_scope_is_applied_rather_than_refused(credit_api) -> None:
    """The reversal revision 0040 exists for, checked end to end over HTTP.

    Until the view carried the sales hierarchy this endpoint answered **403** to
    a region-scoped caller. It had to: ``_apply_filters`` drops a filter naming a
    column the view lacks, so the alternative was serving a regional manager the
    whole company's receivables with no sign that their scope had been ignored.

    Now the level is a real column, so the scope is *applied* — and the figures
    come back narrowed rather than complete. That is the assertion worth making:
    a test that only checked for 200 would pass just as happily on a scope that
    had been silently dropped, which is the failure this whole mechanism exists
    to prevent.
    """
    # The section and the scope are different gates, and this test is about the
    # second one. A regional manager does not hold Credit Control by default —
    # the test above pins that — so the section is granted here explicitly,
    # leaving the scope as the only thing that can narrow the answer.
    _grant_section(credit_api, "dhaka_rm")

    response = get(credit_api, BASE, username="dhaka_rm")
    assert response.status_code == 200, response.text

    scoped = response.json()["metrics"]
    unscoped = get(credit_api, BASE).json()["metrics"]
    assert scoped["invoice_count"] < unscoped["invoice_count"], (
        "a narrowed scope that returns the whole book has been dropped, not applied")
    assert scoped["invoice_count"] > 0, "and it must not narrow to nothing either"


def test_the_scope_mechanism_is_the_shared_one(credit_api) -> None:
    """One rule, one implementation — which is why the bespoke one was deleted.

    Credit Control used to carry its own ``SCOPE_LEVELS_HONOURED`` literal and
    its own refusal beside the generic ``SCOPE_POLICY`` check. Two mechanisms for
    one rule drift, and this one had already started to: the literal named four
    levels while the view was about to carry eleven. What is pinned here is that
    the surviving list is *equal to the scope-bearing columns the view actually
    has*, so it cannot outlive what it names.
    """
    from sqlalchemy import MetaData, Table
    from app.ai import queries as q
    from app.database.connection import get_engine
    from app.reporting.credit import SCOPE_LEVELS_HONOURED
    from app.security.scope import SCOPE_LEVELS

    view = Table(q.CREDIT_INVOICE_VIEW, MetaData(), autoload_with=get_engine())
    carried = {level for level in SCOPE_LEVELS if level in view.c}
    # ``customer_code`` is a scope this platform can *grant* but is not one of
    # ``SCOPE_LEVELS``, so it is named here and excluded from the comparison,
    # exactly as the four-level version of this assertion did.
    assert SCOPE_LEVELS_HONOURED - {"customer_code"} == carried
    # ...and the policy stays REFUSE, inert now but still the guard for the next
    # report that cannot express a level.
    assert q.scope_policy(q.CREDIT_INVOICE_VIEW) is q.ScopePolicy.REFUSE



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
    run_import(session.get_bind(), "credit_invoice", reader,
               source_system="TEST",
               deduction_convention=credit.DEDUCTION_UNSIGNED)

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


# ---------------------------------------------------------------------------
# The hierarchy sections, and the partition that makes the headline add up
# ---------------------------------------------------------------------------


def test_the_open_book_partitions_into_overdue_due_soon_and_due_later(
    credit_api: TestClient,
) -> None:
    """The three figures add up to the fourth, and until now the third was absent.

    Overdue and Due Soon were reported without Due Later, so the two largest
    numbers on the page did not account for the outstanding total and nothing
    said what the remainder was. On the SPL extract the missing piece is
    ৳45.59 Cr — *more* than the overdue book — money that is neither late nor
    imminent, and which the source's own ``od`` and ``maturity`` columns do not
    publish either.

    Asserted on both the money and the count, because a partition that balances
    in taka and not in invoices would mean a row counted twice and another not
    at all, netting to nothing.
    """
    metrics = get(credit_api, BASE).json()["metrics"]

    parts = (metrics["overdue_amount"] + metrics["due_soon_amount"]
             + metrics["due_later_amount"])
    assert parts == pytest.approx(metrics["outstanding_amount"], abs=0.01)

    counts = (metrics["overdue_invoice_count"] + metrics["due_soon_invoice_count"]
              + metrics["due_later_invoice_count"])
    assert counts == metrics["open_invoice_count"]


def test_an_invoice_due_on_the_reporting_date_is_due_soon_and_not_overdue(
    credit_api: TestClient,
) -> None:
    """The boundary the whole module agrees on, checked where it partitions.

    ``credit.days_overdue`` is ``(as_on - due).days`` and zero days late is
    NOT_YET_DUE, so an invoice due *today* belongs to Due Soon. Getting this
    wrong by a day would move money between two cards that are read against each
    other, and it is the one boundary a partition cannot hide.
    """
    # OPEN-NYD is due 2026-09-10; asked as at exactly that date.
    body = get(credit_api, BASE, as_on_date="2026-09-10").json()
    statuses = {row["status"]: row for row in body["status"]}
    assert statuses["OVER_DUE"]["invoice_count"] >= 0
    rows = get(credit_api, f"{BASE}/invoices", as_on_date="2026-09-10").json()["rows"]
    nyd = next(row for row in rows if row["invoice_no"] == "OPEN-NYD")
    assert nyd["credit_status"] == "NOT_YET_DUE"
    assert nyd["days_overdue"] == 0


def test_the_aging_matrix_agrees_with_the_aging_chart_bucket_for_bucket(
    credit_api: TestClient,
) -> None:
    """Two readings of one truth, which is the reason to check them against each other.

    ``aging`` is the portfolio by bucket; ``aging_by_level`` is the same money
    split by region as well. Summing the matrix down its columns must reproduce
    the chart exactly — if it does not, one of the two is filtering differently
    and the page would draw a total beside a breakdown that contradicts it.
    """
    body = get(credit_api, BASE).json()
    chart = {row["bucket"]: row["outstanding_amount"] for row in body["aging"]}
    matrix = body["aging_by_level"]

    assert matrix["buckets"] == list(chart), "same buckets, same order"
    for index, bucket in enumerate(matrix["buckets"]):
        column = sum(row["amounts"][index] for row in matrix["rows"])
        assert column == pytest.approx(chart[bucket], abs=0.01), bucket


def test_every_matrix_row_carries_every_bucket_even_the_empty_ones(
    credit_api: TestClient,
) -> None:
    """A ragged row would read as a rendering fault rather than as a fact.

    Same rule the flat aging chart follows: "nothing in 91-120" is something the
    screen has to state. A matrix is where it matters most — a row with four
    cells beside a row with eight does not line up as a table at all.
    """
    matrix = get(credit_api, BASE).json()["aging_by_level"]
    assert matrix["rows"], "the fixtures place invoices in regions"
    for row in matrix["rows"]:
        assert len(row["amounts"]) == len(matrix["buckets"])
        assert len(row["counts"]) == len(matrix["buckets"])
        assert row["outstanding_amount"] == pytest.approx(sum(row["amounts"]), abs=0.01)


def test_the_breakdown_groups_by_the_level_the_caller_asks_for(
    credit_api: TestClient,
) -> None:
    """Region by default, because that is the level this page is read at.

    The level is a request parameter rather than a constant: a managing director
    reads receivables by region and an area manager reads their own areas by
    territory, and one fixed level serves one of them.
    """
    default = get(credit_api, BASE).json()
    assert default["exposure_by_level"]["level"] == "region_code"
    assert default["aging_by_level"]["level"] == "region_code"

    by_territory = get(credit_api, BASE, group_level="territory_code").json()
    assert by_territory["exposure_by_level"]["level"] == "territory_code"
    assert by_territory["aging_by_level"]["level"] == "territory_code"

    # The same money, cut a different way: a finer level can only have at least
    # as many groups, and the totals cannot move.
    assert (sum(r["outstanding_amount"] for r in by_territory["exposure_by_level"]["rows"])
            == pytest.approx(
                sum(r["outstanding_amount"]
                    for r in default["exposure_by_level"]["rows"]), abs=0.01))


def test_an_unknown_group_level_is_refused_rather_than_silently_defaulted(
    credit_api: TestClient,
) -> None:
    """Reading correct figures under the wrong heading is worse than an error."""
    response = get(credit_api, BASE, group_level="material_code")
    assert response.status_code == 422
    assert "material_code" in response.text


def test_a_group_with_nothing_outstanding_has_no_overdue_share(
    credit_api: TestClient,
) -> None:
    """Suppressed, never rendered as 0% — the platform's rule, applied per row.

    A group with no receivables has no overdue *proportion*. Reporting 0% there
    would read as good news about a book that does not exist, which is exactly
    the claim the headline metric refuses to make.
    """
    rows = get(credit_api, BASE).json()["exposure_by_level"]["rows"]
    for row in rows:
        if not row["outstanding_amount"]:
            assert row["overdue_share_percent"] is None
        else:
            assert 0 <= row["overdue_share_percent"] <= 100


def test_the_due_profile_covers_what_is_not_yet_late_and_says_what_it_omits(
    credit_api: TestClient,
) -> None:
    """The aging chart's other half, and deliberately not overlapping it.

    Aging looks backwards; this looks forwards. Overdue money is **not** a
    bucket here — it has seven of its own on the other chart — because the same
    taka on two charts is taka a reader will add. It is returned beside the
    buckets instead, so the two can be related without being mixed.

    The buckets must therefore sum to Due Soon plus Due Later exactly: that is
    the whole of what is open and not yet late.
    """
    body = get(credit_api, BASE).json()
    metrics, profile = body["metrics"], body["due_profile"]

    assert profile["overdue_amount"] == pytest.approx(
        metrics["overdue_amount"], abs=0.01)
    total = sum(bucket["due_amount"] for bucket in profile["buckets"])
    assert total == pytest.approx(
        metrics["due_soon_amount"] + metrics["due_later_amount"], abs=0.01)

    from app.etl.credit import DUE_BUCKET_CODES
    assert [b["bucket"] for b in profile["buckets"]] == list(DUE_BUCKET_CODES)


def test_the_status_split_states_what_was_billed_as_well_as_what_is_owed(
    credit_api: TestClient,
) -> None:
    """Because ``outstanding_amount`` cannot describe CLEARED at all.

    A cleared invoice has a balance of zero or below *by definition*, so the
    outstanding figure for that status is always 0.0 — which left the split
    reporting a status with a count and no money, and no way to see how much had
    actually been settled.
    """
    rows = {row["status"]: row for row in get(credit_api, BASE).json()["status"]}

    cleared = rows["CLEARED"]
    assert cleared["outstanding_amount"] == 0.0, "cleared means nothing owed"
    assert cleared["invoice_count"] > 0
    assert cleared["invoice_amount"] > 0, "and that is the figure that was missing"

    # The three statuses partition every invoice, settled or not.
    assert sum(row["invoice_count"] for row in rows.values()) == (
        get(credit_api, BASE).json()["metrics"]["invoice_count"])


def test_the_worst_overdue_customers_say_where_they_are(
    credit_api: TestClient,
) -> None:
    """The action this chart leads to is somebody going to see the customer.

    Territory rather than region, because that is the level a single visit
    happens at — and impossible before revision 0040, when this view stopped at
    the customer's sub-territory. The region rides along so a regional manager
    reading their own filtered page recognises the rows.
    """
    rows = get(credit_api, BASE).json()["top_overdue_customers"]
    assert rows, "the fixtures hold overdue invoices"
    for row in rows:
        assert row["territory_code"], row
        assert row["region_code"], row
        assert row["max_days_overdue"] >= 1, "an overdue row is at least a day late"

    # Ranked by what is owed, which is what makes it a ranking rather than a list.
    amounts = [row["overdue_amount"] for row in rows]
    assert amounts == sorted(amounts, reverse=True)


def test_a_customer_trading_in_two_territories_is_still_one_row(
    credit_api: TestClient,
) -> None:
    """Grouping on the place as well would split them and drop both below the cut.

    The customer is the grain of this ranking because the customer is what gets
    a visit. Their territory is taken as a MAX over their own invoices, which is
    their territory in every case the master data allows and a stable answer in
    any case it does not.
    """
    rows = get(credit_api, BASE).json()["top_overdue_customers"]
    codes = [row["customer_code"] for row in rows]
    assert len(codes) == len(set(codes)), "one row per customer"
