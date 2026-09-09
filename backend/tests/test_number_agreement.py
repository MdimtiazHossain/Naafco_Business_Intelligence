"""One number per question, whichever surface asks it.

``CLAUDE.md`` makes a specific claim about this architecture: chat,
``/api/dashboard/*`` and ``/api/pages/*`` all funnel through ``ai/tools.py``,
"which is why a page and the agent cannot disagree about a number". That is the
product's central promise — a regional manager who reads ৳2.68 Cr on the sales
page and asks the assistant the same question must not be told something else —
and until this module existed it was a claim in a document rather than a
property under test.

``/api/reports/*`` is the exception the same paragraph names: it reads the same
warehouse through ``reporting/service.py``, applies scope with
``deps.enforce_report_scope`` instead of ``PermissionFilter``, and totals
**``vw_daily_sales``** where the agent totals ``vw_sales_detail``. Two modules,
two views, one number expected. That is precisely the seam worth a test, because
nothing structural forces the two to agree — only arithmetic does, and only
while both keep summing the same columns over the same rows.

The tests compare *figures*, never implementations: each asks two surfaces the
same business question and insists on one answer.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.ai import queries as q
from app.ai.schemas import ScopeFilters
from app.reporting.service import ReportFilters, sales_report
from conftest_phase3 import TODAY
from test_platform_api import auth, login, platform  # noqa: F401 - fixture reuse

#: The month the agent fixture books its current-period sales into.
WINDOW = "?date_from=2026-08-01&date_to=2026-08-31"
FROM, TO = dt.date(2026, 8, 1), dt.date(2026, 8, 31)


def _net_sales(payload: dict) -> float:
    """The headline net sales from a tool result, however it is wrapped."""
    values = payload.get("values") or {}
    if "net_sales" in values:
        return float(values["net_sales"])
    assert payload.get("value") is not None, payload
    return float(payload["value"])


def test_the_agent_and_the_sales_page_state_one_net_sales(platform: TestClient) -> None:
    """The same question, asked of the assistant and of the page beside it.

    Both reach ``get_sales_summary`` through ``execute_tool``, so this is the
    easy half of the promise — and the half most likely to be broken quietly by
    a page that starts computing something itself.
    """
    token = login(platform, "ceo")

    page = platform.get(f"/api/pages/sales{WINDOW}", headers=auth(token))
    assert page.status_code == 200, page.text

    chat = platform.post("/api/chat",
                         json={"message": "এই মাসের total sales কত?"},
                         headers=auth(token))
    assert chat.status_code == 200, chat.text
    body = chat.json()
    assert body["error_code"] is None, body["answer"]

    assert _net_sales(body["data"]) == _net_sales(page.json()["summary"])


def test_the_agent_and_the_report_layer_state_one_net_sales(
    agent_engine,
) -> None:
    """The seam nothing structural holds together.

    ``reporting/service.py`` totals ``vw_daily_sales`` and ``ai/queries.py``
    totals ``vw_sales_detail``: a rollup and the rows it was rolled up from, in
    two modules that share no code. They agree by arithmetic alone, which is
    exactly the kind of agreement that breaks without anybody noticing — a
    measure added to one list, a filter applied on one side, a view rebuilt from
    an older body.
    """
    with Session(agent_engine, future=True) as session:
        agent = q.aggregate_totals(session, q.SALES_MEASURES, ScopeFilters(), FROM, TO)
        report = sales_report(session, ReportFilters(date_from=FROM, date_to=TO))

    totals = report["metrics"]["total"]
    for measure in ("net_sales", "quantity"):
        assert float(agent[measure]) == pytest.approx(float(totals[measure])), measure

    # Volume is read from the detail view on both sides — the daily rollup is
    # deliberately not its source — so it is compared like the rest, including
    # when neither has one to state. "Both say nothing" is agreement; one saying
    # nothing while the other states a figure is the disagreement worth failing
    # on, and asserting only when a figure exists would let that through.
    agent_volume = agent.get("volume")
    report_volume = report["metrics"]["volume"]["value"]
    assert (agent_volume is None) == (report_volume is None), (
        agent_volume, report_volume)
    if agent_volume is not None:
        assert float(agent_volume) == pytest.approx(float(report_volume))


def test_the_two_paths_agree_under_a_filter_as_well(agent_engine) -> None:
    """Agreement on the whole is not agreement on a part.

    The two modules build their WHERE clauses independently, so a filter is
    where they are most able to diverge while both still look right: an
    unfiltered total can match while a region-filtered one does not.
    """
    with Session(agent_engine, future=True) as session:
        agent = q.aggregate_totals(
            session, q.SALES_MEASURES, ScopeFilters(region_codes=["REG001"]), FROM, TO)
        report = sales_report(
            session, ReportFilters(date_from=FROM, date_to=TO, region_code="REG001"))

    assert float(agent["net_sales"]) == pytest.approx(
        float(report["metrics"]["total"]["net_sales"]))
    # And the filter did something: the narrowed figure is not the whole.
    with Session(agent_engine, future=True) as session:
        everything = q.aggregate_totals(session, q.SALES_MEASURES, ScopeFilters(),
                                        FROM, TO)
    assert float(agent["net_sales"]) < float(everything["net_sales"])


def test_the_dashboard_and_the_agent_state_one_net_sales(platform: TestClient) -> None:
    """The executive card and the assistant answer the same question."""
    token = login(platform, "ceo")

    dashboard = platform.get(f"/api/dashboard{WINDOW}", headers=auth(token))
    assert dashboard.status_code == 200, dashboard.text

    chat = platform.post("/api/chat",
                         json={"message": "এই মাসের total sales কত?"},
                         headers=auth(token))
    assert chat.status_code == 200, chat.text

    # ``get_business_summary`` calls the figure ``sales`` where the sales tools
    # call it ``net_sales``. Two names for one measure is its own small hazard,
    # and the point of this test is that the two names carry one figure.
    #
    # Read off the KPI strip rather than the raw tool result: the frame stopped
    # returning ``summary`` when the cards became a request each, and the
    # headline is the figure a reader is actually shown — which is the stronger
    # thing to hold the assistant to anyway.
    kpis = {kpi["key"]: kpi["value"] for kpi in dashboard.json()["kpis"]}
    assert _net_sales(chat.json()["data"]) == pytest.approx(
        float(kpis["total_sales"]))


def test_a_scoped_reader_is_told_one_number_by_both_surfaces(
    platform: TestClient,
) -> None:
    """Scope is applied by two different mechanisms, and must reach one figure.

    The page narrows through ``PermissionFilter`` and the assistant through the
    same filter on the same tool — but a regional manager is the reader for whom
    a disagreement would be least visible and most damaging, because they have
    no unscoped figure to check either against.
    """
    token = login(platform, "dhaka_rm")

    page = platform.get(f"/api/pages/sales{WINDOW}", headers=auth(token))
    assert page.status_code == 200, page.text

    chat = platform.post("/api/chat",
                         json={"message": "এই মাসের total sales কত?"},
                         headers=auth(token))
    assert chat.status_code == 200, chat.text
    body = chat.json()
    assert body["error_code"] is None, body["answer"]

    scoped = _net_sales(body["data"])
    assert scoped == _net_sales(page.json()["summary"])

    # And the scope bound: the CEO sees more than the regional manager does.
    ceo = platform.get(f"/api/pages/sales{WINDOW}", headers=auth(login(platform, "ceo")))
    assert scoped < _net_sales(ceo.json()["summary"])


def test_the_two_paths_state_one_stock_position(agent_engine) -> None:
    """Stock has no date, so the two sides can only differ on how they total.

    Both read ``vw_material_stock_detail``, and ``total_stock`` is computed in
    that view precisely so nothing downstream can define it twice. This is the
    test that says so.
    """
    from app.reporting.service import stock_report

    with Session(agent_engine, future=True) as session:
        agent = q.material_stock_totals(session, ScopeFilters())
        report = stock_report(session, ReportFilters())

    metrics = report["metrics"]
    for measure in ("unrestricted_stock", "quality_inspection_stock",
                    "blocked_stock", "stock_in_transit", "total_stock"):
        assert float(agent[measure]) == pytest.approx(float(metrics[measure])), measure


def test_the_two_paths_state_one_target_and_one_achievement(agent_engine) -> None:
    """The widest seam in the product, and the one nothing forces closed.

    ``reporting.service.target_report`` reads ``vw_target_vs_actual`` — a view
    that joins target to actual in SQL — while ``queries.target_vs_actual``
    aggregates ``vw_target_detail`` and ``vw_sales_detail`` separately and joins
    them in Python. Two implementations of one figure, sharing no code and not
    even the same join. A target page and the assistant disagreeing about
    achievement is the disagreement a sales force would notice first, because it
    is the number they are measured on.
    """
    from app.reporting.service import target_report

    with Session(agent_engine, future=True) as session:
        _, totals, _ = q.target_vs_actual(session, ScopeFilters(), FROM, TO,
                                          limit=q.MAX_ROWS)
        report = target_report(session, ReportFilters(date_from=FROM, date_to=TO))

    metrics = report["metrics"]
    assert float(totals["target"]) == pytest.approx(float(metrics["target"]))
    assert float(totals["actual"]) == pytest.approx(float(metrics["actual"]))

    # And the ratio built from them, which is what a reader actually quotes.
    assert (totals["achievement_percent"] is None) == (
        metrics["achievement_percent"] is None), (totals, metrics)
    if totals["achievement_percent"] is not None:
        assert float(totals["achievement_percent"]) == pytest.approx(
            float(metrics["achievement_percent"]))


def test_today_is_pinned_so_the_comparison_is_reproducible() -> None:
    """The fixture's "today" is the one the windows above were chosen against."""
    assert TODAY == dt.date(2026, 8, 15)
