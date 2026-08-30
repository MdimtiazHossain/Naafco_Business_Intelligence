"""The assistant's receivables tools, and the refusal that keeps them honest.

Revision 0020 removed receivables and the agent was taught to say so; revision
0031 gave them a source and this is the other half of that reversal. Three
things are pinned here:

* the three tools answer, and their figures agree with the Credit Control page's
  — they read the same view through the same expressions;
* **a scope this view cannot enforce is refused, not answered**, so the agent
  cannot become the way around the endpoint guard;
* collections are still not reported, because no source states them.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.ai.permission_filter import PermissionFilter
from app.ai.schemas import CreditToolInput, Intent, ScopeFilters
from app.ai.tools import CreditScopeRefused, REGISTRY, ToolContext
from app.etl import credit
from app.etl.pipeline import run_import
from app.etl.readers import RecordsSourceReader

TODAY = dt.date(2026, 8, 29)
#: Wide enough to include OPEN-OLD, which was invoiced in October 2025. The
#: date range filters on the *invoice* date; how late a row is comes from
#: ``as_on_date``, and the two are deliberately different questions.
WINDOW = {"date_from": dt.date(2025, 1, 1), "date_to": dt.date(2026, 12, 31)}


def _invoice(no: str, *, invoice_date: str, credit_days: int, value: int,
             payment: int = 0, customer: str = "CUST-001") -> dict:
    return {
        "Company": "C001", "Invoice No": no, "Customer": customer,
        "Plant": "PL01", "Invoice Date": invoice_date,
        "Credit Days": credit_days, "Invoice Value": value,
        "Return": 0, "Payment": payment, "Discount": 0, "Adjustment": 0,
        "Payment Mode": "CREDIT",
    }


#: Four open invoices and one settled, arranged around TODAY.
#:   OPEN-NYD  due 2026-09-10 -> not yet due, 100000 outstanding
#:   OPEN-30   due 2026-08-11 ->  18 days overdue,  50000
#:   OPEN-90   due 2026-06-01 ->  89 days overdue,  70000
#:   OPEN-OLD  due 2026-01-05 -> 236 days overdue,  30000 (second customer)
#:   PAID      cleared (payment is negative: the source deducts by sign)
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
def credit_engine(agent_engine):
    reader = RecordsSourceReader(INVOICES, source_name="credit.csv", source_type="CSV")
    run_import(agent_engine, "credit_invoice", reader, source_system="TEST")
    return agent_engine


def run(engine, users, tool: str, username: str = "ceo", **overrides):
    """Invoke one tool the way the orchestrator does."""
    with Session(engine) as session:
        user = users[username]
        ctx = ToolContext(session=session, permissions=PermissionFilter(session, user),
                          user=user, today=TODAY)
        arguments = CreditToolInput(
            filters=ScopeFilters(), **{"as_on_date": TODAY, **WINDOW, **overrides})
        return REGISTRY[tool].handler(ctx, arguments)


# ---------------------------------------------------------------------------
# The tools answer
# ---------------------------------------------------------------------------


def test_the_tools_are_registered_against_their_intents() -> None:
    assert REGISTRY["get_credit_summary"].intents == (Intent.CREDIT_SUMMARY,)
    assert REGISTRY["get_credit_aging"].intents == (Intent.CREDIT_AGING,)
    assert REGISTRY["get_overdue_customers"].intents == (Intent.CREDIT_OVERDUE,)


def test_the_summary_reports_the_headline_figures(credit_engine, users) -> None:
    result = run(credit_engine, users, "get_credit_summary")

    assert result.values["invoice_count"] == 5
    assert float(result.values["outstanding_amount"]) == 250000.0
    assert result.values["open_invoice_count"] == 4
    assert float(result.values["overdue_amount"]) == 150000.0
    assert result.values["overdue_invoice_count"] == 3
    assert result.value == result.values["outstanding_amount"]


def test_every_credit_answer_states_the_date_it_was_measured_on(
        credit_engine, users) -> None:
    """"Overdue" without a date is not a figure anybody can repeat or act on."""
    for tool in ("get_credit_summary", "get_credit_aging", "get_overdue_customers"):
        result = run(credit_engine, users, tool)
        assert any("2026-08-29" in note for note in result.notes), tool


def test_the_reporting_date_moves_the_answer(credit_engine, users) -> None:
    """A tool that accepted as_on_date and ignored it would look right by default."""
    early = run(credit_engine, users, "get_credit_summary",
                as_on_date=dt.date(2026, 6, 15))
    late = run(credit_engine, users, "get_credit_summary")

    assert early.values["overdue_invoice_count"] == 2
    assert late.values["overdue_invoice_count"] == 3


def test_aging_returns_every_bucket_including_the_empty_ones(
        credit_engine, users) -> None:
    result = run(credit_engine, users, "get_credit_aging")
    buckets = [row["aging_bucket"] for row in result.rows]

    assert buckets == list(credit.AGING_BUCKETS)
    by_bucket = {row["aging_bucket"]: row for row in result.rows}
    assert by_bucket["61-90"]["invoice_count"] == 1
    # Reported as zero rather than omitted: "nothing in 91-120" is an answer.
    assert by_bucket["91-120"]["invoice_count"] == 0


def test_a_cleared_invoice_ages_nowhere(credit_engine, users) -> None:
    result = run(credit_engine, users, "get_credit_aging")
    assert sum(row["invoice_count"] for row in result.rows) == 4


def test_overdue_customers_are_ranked(credit_engine, users) -> None:
    result = run(credit_engine, users, "get_overdue_customers")

    assert [row["customer_code"] for row in result.rows] == ["CUST-001", "CUST-002"]
    assert float(result.rows[0]["overdue_amount"]) == 120000.0
    assert result.rows[0]["oldest_due_date"] is not None


def test_an_empty_warehouse_answers_rather_than_failing(agent_engine, users) -> None:
    """The page and the agent both load before any Credit Invoice file arrives."""
    result = run(agent_engine, users, "get_credit_summary")
    assert result.rows == []
    assert result.notes


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------


def test_a_scope_this_view_cannot_enforce_is_refused(credit_engine, users) -> None:
    """The whole reason these tools have a guard the stock tools do not.

    ``filter_conditions`` skips a filter naming a column the view lacks, and the
    credit view carries no region — so without this a regional manager's scope
    would be dropped in silence and they would be handed the whole company's
    receivables. The stock tools meet the same gap and merely disclose it, which
    is right for stock and wrong for the figure that decides whether a customer
    keeps getting supplied.
    """
    for tool in ("get_credit_summary", "get_credit_aging", "get_overdue_customers"):
        with pytest.raises(CreditScopeRefused):
            run(credit_engine, users, tool, username="dhaka_rm")


def test_the_refusal_names_the_level_it_could_not_apply(credit_engine, users) -> None:
    with pytest.raises(CreditScopeRefused) as raised:
        run(credit_engine, users, "get_credit_summary", username="dhaka_rm")
    assert "region_code" in str(raised.value)


def test_an_unrestricted_caller_is_not_refused(credit_engine, users) -> None:
    assert run(credit_engine, users, "get_credit_summary").values["invoice_count"] == 5


def test_the_agent_cannot_be_a_way_around_the_endpoint(credit_engine, users) -> None:
    """The two surfaces must answer the same question the same way.

    ``/api/reports/credit-control`` refuses a scope it cannot enforce. If the
    tool path merely noted it, the assistant would be a documented route to
    figures the API declines to serve the same person.
    """
    from app.reporting.credit import ScopeNotHonourable, assert_scope_is_honourable
    from app.reporting.service import ReportFilters

    with pytest.raises(ScopeNotHonourable):
        assert_scope_is_honourable(users["dhaka_rm"], ReportFilters())
    with pytest.raises(CreditScopeRefused):
        run(credit_engine, users, "get_credit_summary", username="dhaka_rm")


# ---------------------------------------------------------------------------
# What is still not reported
# ---------------------------------------------------------------------------


def test_there_is_no_collection_tool() -> None:
    """No source states individual payments, so there is no collection figure.

    An invoice's aggregate paid amount is sitting right there and is not a
    collection; a tool named for one would make that substitution look official.
    """
    assert not [name for name in REGISTRY if "collection" in name]
    assert not hasattr(Intent, "COLLECTION")


def test_the_prompt_offers_credit_and_still_refuses_collections() -> None:
    from app.ai.prompts import SYSTEM_PROMPT

    assert "credit" in SYSTEM_PROMPT.lower()
    assert "do NOT report collections" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# The alert
# ---------------------------------------------------------------------------


def test_high_overdue_alerts_on_the_share_not_the_amount(credit_engine, users) -> None:
    """A crore overdue is alarming on a small book and routine on a large one."""
    from app.ai.schemas import AlertToolInput

    with Session(credit_engine) as session:
        user = users["ceo"]
        ctx = ToolContext(session=session, permissions=PermissionFilter(session, user),
                          user=user, today=TODAY)
        result = REGISTRY["get_business_alerts"].handler(
            ctx, AlertToolInput(**WINDOW, overdue_share_percent=25.0))

    overdue = [row for row in result.rows if row["alert_type"] == "HIGH_OVERDUE"]
    assert len(overdue) == 1
    # 150000 of 250000 outstanding is 60%.
    assert overdue[0]["current_value"] == pytest.approx(60.0)


def test_a_high_threshold_raises_no_overdue_alert(credit_engine, users) -> None:
    from app.ai.schemas import AlertToolInput

    with Session(credit_engine) as session:
        user = users["ceo"]
        ctx = ToolContext(session=session, permissions=PermissionFilter(session, user),
                          user=user, today=TODAY)
        result = REGISTRY["get_business_alerts"].handler(
            ctx, AlertToolInput(**WINDOW, overdue_share_percent=90.0))

    assert not [row for row in result.rows if row["alert_type"] == "HIGH_OVERDUE"]


def test_alerts_still_run_for_a_caller_whose_credit_scope_cannot_apply(
        credit_engine, users) -> None:
    """Skipped, not refused — and said so.

    This tool answers four questions at once. Refusing all of them would deny a
    regional manager their stock and achievement alerts in order to protect a
    receivables figure they were never going to be shown.
    """
    from app.ai.schemas import AlertToolInput

    with Session(credit_engine) as session:
        user = users["dhaka_rm"]
        ctx = ToolContext(session=session, permissions=PermissionFilter(session, user),
                          user=user, today=TODAY)
        result = REGISTRY["get_business_alerts"].handler(ctx, AlertToolInput(**WINDOW))

    assert not [row for row in result.rows if row["alert_type"] == "HIGH_OVERDUE"]
    assert any("not checked" in note for note in result.notes)
