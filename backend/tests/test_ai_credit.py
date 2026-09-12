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
from app.ai.exceptions import ScopeNotEnforceable
from app.ai.tools import REGISTRY, ToolContext
from app.etl import credit
from app.etl.pipeline import run_import
from app.etl.readers import RecordsSourceReader

TODAY = dt.date(2026, 8, 29)
#: Wide enough to include OPEN-OLD, which was invoiced in October 2025. The
#: date range filters on the *invoice* date; how late a row is comes from
#: ``as_on_date``, and the two are deliberately different questions.
WINDOW = {"date_from": dt.date(2025, 1, 1), "date_to": dt.date(2026, 12, 31)}


def _invoice(no: str, *, invoice_date: str, credit_days: int, value: int,
             payment: int = 0, customer: str = "CUST-001",
             territory: str = "TR001") -> dict:
    return {
        "Company": "C001", "Invoice No": no, "Customer": customer,
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
#: Four open invoices and one settled, arranged around TODAY.
#:   OPEN-NYD  due 2026-09-10 -> not yet due, 100000 outstanding
#:   OPEN-30   due 2026-08-11 ->  18 days overdue,  50000
#:   OPEN-90   due 2026-06-01 ->  89 days overdue,  70000
#:   OPEN-OLD  due 2026-01-05 -> 236 days overdue,  30000 (second customer)
#:   PAID      cleared (the declared convention subtracts the payment)
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
def credit_engine(agent_engine):
    reader = RecordsSourceReader(INVOICES, source_name="credit.csv", source_type="CSV")
    run_import(agent_engine, "credit_invoice", reader, source_system="TEST",
               deduction_convention=credit.DEDUCTION_UNSIGNED)
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


def test_every_credit_tool_narrows_a_region_scope_instead_of_refusing(
        credit_engine, users) -> None:
    """The reversal, across all three tools — and why it is a reversal.

    These tools used to raise ``ScopeNotEnforceable`` for a region-scoped caller.
    That was right at the time: ``filter_conditions`` skips a filter naming a
    column the view lacks, the credit view carried no region, so the only
    alternative to refusing was handing a regional manager the whole company's
    receivables with no sign their scope had been ignored.

    Revision 0040 gave the view the sales hierarchy, so the level is a real
    column and the scope is applied. What is asserted is the *narrowing*, not
    merely the absence of an exception: a test that only checked a call succeeds
    would pass just as well on a scope that had been silently dropped, which is
    the exact failure the refusal existed to prevent.
    """
    def outstanding(result) -> float:
        """What the answer says is owed, whatever shape the tool returns it in.

        Counted in money rather than in rows on purpose. ``get_credit_aging``
        renders **all eight** buckets whether or not each holds anything — an
        empty bucket is a fact about the portfolio, not a gap — so its row count
        is eight for every caller and says nothing at all about narrowing.
        """
        if result.values.get("outstanding_amount") is not None:
            return float(result.values["outstanding_amount"])
        return sum(
            float(row.get("outstanding_amount")
                  or row.get("overdue_amount")
                  or row.get("balance_amount") or 0)
            for row in (result.rows or [])
        )

    for tool in ("get_credit_summary", "get_credit_aging", "get_overdue_customers"):
        scoped = outstanding(run(credit_engine, users, tool, username="dhaka_rm"))
        unscoped = outstanding(run(credit_engine, users, tool))
        assert scoped < unscoped, (
            f"{tool} returned the whole book to a region-scoped caller")
        assert scoped > 0, f"{tool} narrowed a real region to nothing"


def test_the_refusal_mechanism_is_still_in_place_and_simply_has_nothing_to_refuse(
        credit_engine, users) -> None:
    """Deleting the *reason* for a refusal is not deleting the guard.

    ``SCOPE_POLICY`` still declares REFUSE for this view, and that matters: it is
    what would catch the next column the view loses, or the next scope level this
    platform learns to grant. What has changed is that the view now carries every
    level a scope can be stated at, so ``unhonourable_levels`` comes back empty
    and the guard passes on its own rather than being switched off.
    """
    from app.ai import queries as q

    assert q.scope_policy(q.CREDIT_INVOICE_VIEW) is q.ScopePolicy.REFUSE
    with Session(credit_engine) as session:
        permissions = PermissionFilter(session, users["dhaka_rm"])
        view = q.view(session, q.CREDIT_INVOICE_VIEW)
        assert permissions.unhonourable_levels(view.c) == ()
        # The same call the tools make, now returning rather than raising.
        assert permissions.assert_scope_is_honourable(
            view, q.scope_policy(q.CREDIT_INVOICE_VIEW), "receivables") == ()



def test_an_unrestricted_caller_is_not_refused(credit_engine, users) -> None:
    assert run(credit_engine, users, "get_credit_summary").values["invoice_count"] == 5


def test_the_agent_answers_a_regional_manager_their_own_region(credit_engine,
                                                               users) -> None:
    """The two surfaces still agree — and what they now agree on is an answer.

    Both used to refuse a region-scoped caller, because the view could not
    express the scope and answering would have meant ignoring it. Revision 0040
    gave the view the hierarchy, so both now narrow instead. The property being
    pinned is unchanged and is the important one: the assistant is not a route to
    figures the API would decline to serve the same person, in either direction.
    """
    scoped = run(credit_engine, users, "get_credit_summary", username="dhaka_rm")
    unscoped = run(credit_engine, users, "get_credit_summary")
    assert scoped.values["invoice_count"] < unscoped.values["invoice_count"], (
        "a scope that changes nothing has been dropped rather than applied")
    assert scoped.values["invoice_count"] > 0


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


def test_a_regional_manager_now_gets_their_own_overdue_alert(
        credit_engine, users) -> None:
    """HIGH_OVERDUE used to be skipped for this caller. Now it is answered.

    The skip was never about the alert being unimportant: this tool answers four
    questions at once, and refusing all of them would have denied a regional
    manager their stock and achievement alerts to protect a receivables figure
    they were never going to be shown. So the section was skipped with a note
    saying it had *not been evaluated* — which is the honest sentence, and a poor
    substitute for the figure.

    With the hierarchy on the view there is nothing left to skip, and the note
    must go with it: a note saying a section was not checked, on a result where it
    was, is worse than no note at all.
    """
    from app.ai.schemas import AlertToolInput

    with Session(credit_engine) as session:
        user = users["dhaka_rm"]
        ctx = ToolContext(session=session, permissions=PermissionFilter(session, user),
                          user=user, today=TODAY)
        result = REGISTRY["get_business_alerts"].handler(ctx, AlertToolInput(**WINDOW))

    assert [row for row in result.rows if row["alert_type"] == "HIGH_OVERDUE"], (
        "the overdue section is evaluated for a scoped caller now")
    assert not any("Overdue receivables were not checked" in note
                   for note in result.notes)



def test_the_summary_states_figures_as_numbers_not_strings(credit_engine, users) -> None:
    """A `Decimal` survives `model_dump(mode="json")` as a **string**.

    Every other query in ``ai.queries`` passes its rows through
    ``normalize_value`` for exactly this reason; ``credit_totals`` alone did not,
    so every receivables figure reached the browser quoted. It JSON-decodes to a
    string, and the dashboard card that leads with a ratio compared and divided
    it as though it were a number. Nothing raised — the card simply drew the
    wrong thing, which is the expensive kind of wrong.
    """
    values = run(credit_engine, users, "get_credit_summary").values
    for key, value in values.items():
        assert not isinstance(value, str), f"{key} came back as a string"
    assert isinstance(values["outstanding_amount"], (int, float))


def test_the_summary_carries_the_shares_and_suppresses_them_at_zero(
    credit_engine, users
) -> None:
    """The assistant could state an overdue amount and not what share it was.

    The page computed ``overdue_share_percent`` and the tool did not, so the one
    dashboard card built on the tool had nothing to lead with. Both surfaces now
    read the same figure — and both suppress it rather than render 0%, because a
    portfolio with nothing outstanding has no overdue *proportion*.
    """
    values = run(credit_engine, users, "get_credit_summary").values
    assert 0 < values["overdue_share_percent"] <= 100
    assert values["payment_rate_percent"] is not None

    # A window with no invoices in it has no ratio to report.
    empty = run(credit_engine, users, "get_credit_summary",
                date_from="2019-01-01", date_to="2019-12-31")
    assert empty.values.get("overdue_share_percent") in (None, {}) or not empty.values
