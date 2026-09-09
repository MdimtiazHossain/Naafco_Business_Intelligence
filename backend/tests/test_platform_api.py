"""Phase 4 backend tests: auth, RBAC, pages, filters, admin, exports, WhatsApp."""

from __future__ import annotations

import io
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.auth.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    validate_password_strength,
    verify_password,
)
from app.database.models_ai import AppUser, AuditLog, Role
from app.main import app
from conftest_phase3 import TODAY

pytest.importorskip("multipart", reason="python-multipart is required by FastAPI forms")

PASSWORD = "Correct-Horse-9"

#: Explicit window covering the seeded facts. Tests must not depend on the wall
#: clock: "this month" resolves differently on every run, so a value assertion
#: against a named period would pass or fail by the calendar.
WINDOW = "?date_from=2026-08-01&date_to=2026-08-31"


@pytest.fixture
def platform(agent_engine, users, monkeypatch):
    """A client plus passwords, an admin and a WhatsApp-linked user."""
    import app.ai.agent as agent_module

    original_init = agent_module.BusinessIntelligenceAgent.__init__

    def pinned_init(self, session, user, llm=None, today=None):
        original_init(self, session, user, llm=llm, today=today or TODAY)

    monkeypatch.setattr(agent_module.BusinessIntelligenceAgent, "__init__", pinned_init)

    with Session(agent_engine) as session:
        for user in session.query(AppUser).all():
            user.password_hash = hash_password(PASSWORD)
            user.email = f"{user.username}@example.com"
        ceo = session.query(AppUser).filter_by(username="ceo").one()
        ceo.phone_number = "+8801700000001"
        session.add(AppUser(username="root", display_name="Administrator",
                            role=Role.SUPER_ADMIN, is_active=True,
                            password_hash=hash_password(PASSWORD)))
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


def login(client: TestClient, username: str, password: str = PASSWORD) -> str:
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def card(client: TestClient, token: str, name: str, window: str = WINDOW) -> dict:
    """One dashboard card.

    The dashboard is a frame plus a request per card, fetched in parallel by the
    browser — see ``DASHBOARD_SECTIONS``. Tests read a card the same way the
    page does rather than reaching into one big response that no longer exists.
    """
    response = client.get(f"/api/dashboard/section/{name}{window}",
                          headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()["section"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# Passwords and tokens
# --------------------------------------------------------------------------


def test_password_hashing_round_trip() -> None:
    stored = hash_password(PASSWORD)
    assert PASSWORD not in stored            # never stored in clear
    assert verify_password(PASSWORD, stored)
    assert not verify_password("wrong", stored)
    assert not verify_password(PASSWORD, None)
    assert not verify_password(PASSWORD, "garbage")


def test_two_hashes_of_one_password_differ() -> None:
    assert hash_password(PASSWORD) != hash_password(PASSWORD)   # per-user salt


def test_weak_passwords_are_rejected() -> None:
    assert validate_password_strength("short")
    assert validate_password_strength("12345678")
    assert validate_password_strength("password")
    assert validate_password_strength(PASSWORD) == []


def test_token_round_trip_and_tampering() -> None:
    token, expires_in = create_access_token("ceo", 1, Role.MANAGEMENT)
    assert expires_in > 0
    payload = decode_access_token(token)
    assert payload.username == "ceo" and payload.user_id == 1
    assert decode_access_token(token[:-3] + "aaa") is None
    assert decode_access_token("not-a-token") is None


def test_expired_token_is_refused() -> None:
    token, _ = create_access_token("ceo", 1, Role.MANAGEMENT, expires_minutes=-1)
    assert decode_access_token(token) is None


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------


def test_login_returns_a_token_and_profile(platform: TestClient) -> None:
    response = platform.post("/api/auth/login",
                             json={"username": "ceo", "password": PASSWORD})
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["role"] == Role.MANAGEMENT
    assert "password" not in str(body).lower() or "password_hash" not in str(body)


def test_login_works_with_an_email_address(platform: TestClient) -> None:
    response = platform.post("/api/auth/login",
                             json={"username": "ceo@example.com",
                                   "password": PASSWORD})
    assert response.status_code == 200


def test_wrong_password_and_unknown_user_are_indistinguishable(platform) -> None:
    wrong = platform.post("/api/auth/login",
                          json={"username": "ceo", "password": "nope"})
    unknown = platform.post("/api/auth/login",
                            json={"username": "ghost", "password": "nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]


def test_failed_login_is_audited(platform: TestClient, agent_engine) -> None:
    platform.post("/api/auth/login", json={"username": "ceo", "password": "nope"})
    with Session(agent_engine) as session:
        entries = session.query(AuditLog).filter_by(action="LOGIN_FAILED").all()
    assert entries
    assert entries[0].success is False
    assert "password" not in str(entries[0].detail or {}).lower()


def test_protected_endpoint_requires_a_token(platform: TestClient) -> None:
    assert platform.get("/api/dashboard", headers={"X-User": ""}).status_code == 401


def test_bearer_token_authenticates(platform: TestClient) -> None:
    token = login(platform, "ceo")
    response = platform.get("/api/auth/me", headers=auth(token))
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "ceo"
    assert body["scope_description"] == "all regions"
    assert "password_hash" not in body


def test_logout_is_audited(platform: TestClient) -> None:
    token = login(platform, "ceo")
    assert platform.post("/api/auth/logout", headers=auth(token)).status_code == 200


def test_change_password_requires_the_current_one(platform: TestClient) -> None:
    token = login(platform, "ceo")
    bad = platform.post("/api/auth/password", headers=auth(token),
                        json={"current_password": "wrong", "new_password": "NewPass-12"})
    assert bad.status_code == 400

    good = platform.post("/api/auth/password", headers=auth(token),
                         json={"current_password": PASSWORD,
                               "new_password": "NewPass-12"})
    assert good.status_code == 200
    assert login(platform, "ceo", "NewPass-12")


def test_preferences_persist(platform: TestClient) -> None:
    token = login(platform, "ceo")
    response = platform.patch("/api/auth/preferences", headers=auth(token),
                              json={"preferred_language": "bn", "theme": "dark"})
    assert response.status_code == 200
    body = platform.get("/api/auth/me", headers=auth(token)).json()
    assert body["preferred_language"] == "bn"
    assert body["theme"] == "dark"


# --------------------------------------------------------------------------
# Dashboard and pages
# --------------------------------------------------------------------------


def test_dashboard_returns_kpis_and_charts(platform: TestClient) -> None:
    token = login(platform, "ceo")
    window = "?date_from=2026-08-01&date_to=2026-08-31"
    body = platform.get("/api/dashboard" + window, headers=auth(token)).json()

    keys = {kpi["key"] for kpi in body["kpis"]}
    assert {"total_sales", "target", "achievement", "unrestricted_stock",
            "expiring_soon_stock", "expired_stock"} <= keys
    # Receivables left the platform in revision 0020. There is no collection,
    # outstanding or overdue figure to report, and a dashboard that offered one
    # would have to invent it.
    assert keys.isdisjoint({"collection", "outstanding", "overdue"})
    # No Sales Volume card, and no query behind one. Volume is still reported by
    # the Sales page, by the Sales Vol column of the brand table and by the
    # agent's own volume tool — only the executive card is gone.
    assert "sales_volume" not in keys
    assert "sales_volume" not in body
    sales = next(k for k in body["kpis"] if k["key"] == "total_sales")
    assert sales["value"] == pytest.approx(1_800_000)
    assert sales["previous_value"] is not None
    assert sales["growth_percent"] is not None
    # The frame names its cards rather than carrying them: each is its own
    # request now, because eight aggregates in one response made the page wait
    # nine seconds before anything appeared.
    assert body["sections"] == ["sales_trend", "monthly_performance",
                                "region_overview", "territory_sales",
                                "brand_sales", "top_brands"]
    assert card(platform, token, "sales_trend", window)["rows"]
    # One card where there were two. ``region_performance`` and
    # ``target_achievement`` were merged into ``region_overview``, because the
    # first drew a region's net sales beside a card already carrying it as
    # ``actual_sales``. The old names are asserted gone rather than left
    # unmentioned: a name that outlives what it named is what this codebase
    # keeps tripping over.
    assert card(platform, token, "region_overview", window)["rows"]
    assert "region_performance" not in body["sections"]
    assert "target_achievement" not in body["sections"]


def test_the_monthly_card_draws_months_whatever_period_is_chosen(
    platform: TestClient,
) -> None:
    """The defect this section exists to fix.

    It used to draw ``sales_trend``, which follows the reader's period — so on
    the default "This month" the trend is charted by day and the card drew one
    bar per *date*, with no target and no earlier year, under a title promising
    months. Its window is now its own: the financial year the period ends in.
    """
    token = login(platform, "ceo")
    # A single day, which the shared section charts by day.
    window = "?date_from=2026-08-15&date_to=2026-08-15"

    daily = card(platform, token, "sales_trend", window)["rows"]
    assert daily and "date" in daily[0], "the line chart still follows the period"

    monthly = card(platform, token, "monthly_performance", window)
    labels = [row["label"] for row in monthly["rows"]]
    assert labels == [f"{m} 2026" for m in
                      ("Jul", "Aug", "Sep", "Oct", "Nov", "Dec")] +                      [f"{m} 2027" for m in
                      ("Jan", "Feb", "Mar", "Apr", "May", "Jun")]
    assert any("FY 2026-27" in note for note in monthly["notes"])
    # And it carries what the daily section cannot: a plan, and a year to
    # measure against.
    assert "target_amount" in monthly["rows"][0]
    assert "achievement_percent" in monthly["rows"][0]


def test_the_monthly_card_keeps_the_percentages_off_the_series_list(
    platform: TestClient,
) -> None:
    """``chart.series`` is what the *line* chart turns into lines on a taka axis."""
    token = login(platform, "ceo")
    trend = card(platform, token, "monthly_performance",
                 "?date_from=2026-07-01&date_to=2026-10-31")
    months = {row["label"]: row for row in trend["rows"]}
    assert ["Jul 2026", "Aug 2026", "Sep 2026", "Oct 2026"] == list(months)[:4]

    # August is the only month the seeded targets cover.
    assert months["Aug 2026"]["achievement_percent"] == pytest.approx(60.0)
    assert months["Jul 2026"]["target_amount"] is None
    assert months["Jul 2026"]["achievement_percent"] is None, "0% would be a lie"

    # Nothing was sold in the earlier years, so those series are dropped and
    # the growth column goes with them rather than arriving full of nulls.
    keys = {line["key"] for line in trend["chart"]["series"]}
    assert "net_sales_minus_1" not in keys
    assert all("growth_percent" not in row for row in trend["rows"])
    assert any("no sales in this window" in note for note in trend["notes"])

    # The list the line chart draws carries money and nothing else.
    assert keys.isdisjoint({"achievement_percent", "growth_percent"})


def test_the_country_card_says_when_it_is_not_the_country(
    platform: TestClient,
) -> None:
    """Narrowed and labelled, never hidden.

    Every tool is scoped, so a regional manager's "Monthly Performance"
    is their region's months. Withholding the card would deny them the one view
    of their own year; leaving the country label over a partial figure is the
    unexplained number this platform does not put on a screen.
    """
    window = "?date_from=2026-07-01&date_to=2026-10-31"
    national = card(platform, login(platform, "ceo"),
                    "monthly_performance", window)
    scoped = card(platform, login(platform, "dhaka_rm"),
                  "monthly_performance", window)

    assert not any("not the whole country" in note for note in national["notes"])
    assert any("not the whole country" in note for note in scoped["notes"])
    # And the card is still there, with figures in it.
    assert scoped["rows"]


def test_region_overview_grows_against_the_window_the_kpi_uses(
    platform: TestClient,
) -> None:
    """The card's bars, its growth line and the headline answer to one window.

    A custom range from 1 to 31 August compares against the whole of July, which
    is the window the Total Sales KPI beside it grows against. Reading the
    last-period figure from a second call would be two reads of one view, and
    two reads of one number is how two cards on one screen come to disagree.
    """
    token = login(platform, "ceo")
    window = "?date_from=2026-08-01&date_to=2026-08-31"
    body = platform.get("/api/dashboard" + window, headers=auth(token)).json()
    region = card(platform, token, "region_overview", window)

    totals = region["values"]
    assert totals["compare_from"] == "2026-07-01"
    assert totals["compare_to"] == "2026-07-31"
    assert totals["previous"] == pytest.approx(2_400_000)

    kpi = next(k for k in body["kpis"] if k["key"] == "total_sales")
    assert totals["previous"] == pytest.approx(kpi["previous_value"])
    assert totals["growth_percent"] == pytest.approx(kpi["growth_percent"])

    rows = {row["code"]: row for row in region["rows"]}
    # Every bar the card draws comes from this one row: target, actual and last
    # period's actual, with the two percentage lines beside them.
    for key in ("target_amount", "actual_sales", "previous_sales",
                "achievement_percent", "growth_percent"):
        assert key in rows["REG001"], key
    assert rows["REG001"]["previous_sales"] == pytest.approx(2_000_000)
    assert rows["REG001"]["growth_percent"] == pytest.approx(-25.0)


def test_the_ranked_cards_are_ranked_by_what_was_sold(
    platform: TestClient,
) -> None:
    """A card headed "Sales" ranked by achievement is a different twenty.

    ``target_vs_actual`` ranks by achievement, which is right for the tool's
    own question — who is meeting their target — and wrong for these two: it
    would list whoever came closest to a small target and call them the top
    sellers.
    """
    token = login(platform, "ceo")
    window = "?date_from=2026-07-01&date_to=2026-10-31"
    ranked = {name: card(platform, token, name, window)
              for name in ("territory_sales", "brand_sales")}

    for section in ("territory_sales", "brand_sales"):
        rows = ranked[section]["rows"]
        assert rows, section
        sold = [row["actual_sales"] for row in rows]
        assert sold == sorted(sold, reverse=True), f"{section} is not ranked by sales"
        # Each carries the plan and the outcome; an earlier year only where one
        # was recorded, which this fixture has none of.
        assert "target_amount" in rows[0] and "actual_sales" in rows[0]

    # And the two group the same measures differently, so they are two answers
    # rather than one repeated.
    assert ranked["territory_sales"]["values"]["group_by"] == "territory"
    assert ranked["brand_sales"]["values"]["group_by"] == "material_brand"


def test_an_unknown_card_is_refused_and_says_which_exist(
    platform: TestClient,
) -> None:
    """The registry is the list, so a name it does not hold is not served.

    Named in the refusal rather than answered with an empty section: a caller
    asking for a card this build does not draw has either a typo or a stale
    idea of the page, and both are answered by saying what there is.
    """
    token = login(platform, "ceo")
    response = platform.get("/api/dashboard/section/region_performance" + WINDOW,
                            headers=auth(token))
    assert response.status_code == 404
    assert "region_overview" in response.json()["detail"]


def test_every_named_card_can_actually_be_fetched(platform: TestClient) -> None:
    """A name in the frame's list and no route behind it is the stale-name
    failure this codebase keeps meeting, one HTTP round trip further out."""
    token = login(platform, "ceo")
    body = platform.get("/api/dashboard" + WINDOW, headers=auth(token)).json()
    assert body["sections"], "the frame names no cards at all"
    for name in body["sections"]:
        assert card(platform, token, name) is not None, name


def test_no_card_pays_for_a_distinct_invoice_count(
    platform: TestClient, agent_engine
) -> None:
    """Asserted in the SQL, because the shape of the answer cannot show it.

    Two of the six cards never surfaced ``invoice_count`` in the first place —
    the achievement family builds its rows from named fields — so a test that
    looked for an absent key would pass for them whether or not they still paid
    for the count. What costs the time is the ``COUNT(DISTINCT invoice_no)``
    that forces the database to sort every row before it can group; measured on
    the deployment, that is 46.72s against 6.09s over three year-windows.

    Driven off the frame's own list rather than a list written here, so a card
    added later is covered on the day it is added — and fails loudly if it
    forgets ``include_invoice_count=False`` rather than quietly costing twice
    what it should.
    """
    from sqlalchemy import event

    token = login(platform, "ceo")
    names = platform.get("/api/dashboard" + WINDOW,
                         headers=auth(token)).json()["sections"]
    assert names, "the frame names no cards at all"

    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, many):
        seen.append(statement)

    event.listen(agent_engine, "after_cursor_execute", record)
    try:
        for name in names:
            card(platform, token, name)
    finally:
        event.remove(agent_engine, "after_cursor_execute", record)

    assert seen, "no SQL was observed, so this test proved nothing"
    guilty = [statement for statement in seen
              if "count(distinct" in statement.lower()
              and "invoice_no" in statement.lower()]
    assert not guilty, (
        f"{len(guilty)} of {len(seen)} statements still count distinct "
        f"invoices; first was: {' '.join(guilty[0].split())[:200]}"
    )


def test_the_customers_page_still_states_its_invoice_count(
    platform: TestClient,
) -> None:
    """The one reader the dashboard's saving must not have taken it from.

    ``include_invoice_count`` defaults on precisely so this column survives a
    change made for a different screen, and this is the assertion that keeps
    that promise honest — the column had no test at all before, which is how it
    would have gone missing without anybody noticing.
    """
    token = login(platform, "ceo")
    body = platform.get("/api/pages/customers" + WINDOW,
                        headers=auth(token)).json()

    rows = body["customers"]["rows"]
    assert rows, "no customers to check"
    assert all("invoice_count" in row for row in rows)
    # Present *and* real: a column of zeroes would satisfy the key check while
    # meaning the count had stopped being computed.
    assert any(row["invoice_count"] for row in rows)


def test_dashboard_is_scoped_by_role(platform: TestClient) -> None:
    everything = platform.get("/api/dashboard" + WINDOW, headers=auth(login(platform, "ceo")))
    dhaka = platform.get("/api/dashboard" + WINDOW, headers=auth(login(platform, "dhaka_rm")))
    total = next(k for k in everything.json()["kpis"] if k["key"] == "total_sales")
    scoped = next(k for k in dhaka.json()["kpis"] if k["key"] == "total_sales")
    assert scoped["value"] < total["value"]


@pytest.mark.parametrize(
    # The transactional surface is sales, material stock and target, plus the
    # two master-oriented pages. Collection and Outstanding were removed in
    # revision 0020 and are asserted gone below rather than left listed here.
    "page", ["sales", "stock", "target", "materials", "customers"],
)
def test_every_page_endpoint_responds(platform: TestClient, page: str) -> None:
    token = login(platform, "ceo")
    response = platform.get(f"/api/pages/{page}?date_from=2026-08-01&date_to=2026-08-31", headers=auth(token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert "period" in body and "filters" in body


@pytest.mark.parametrize("page", ["collection", "outstanding"])
def test_a_removed_page_is_not_served(platform: TestClient, page: str) -> None:
    """A question about receivables is answered "not tracked", not with a blank.

    The route is gone rather than returning an empty table, which is what makes
    the absence legible: an empty report reads as "nothing this month".
    """
    token = login(platform, "ceo")
    response = platform.get(f"/api/pages/{page}?date_from=2026-08-01&date_to=2026-08-31",
                            headers=auth(token))
    assert response.status_code == 404


def test_sales_page_carries_the_expected_sections(platform: TestClient) -> None:
    token = login(platform, "ceo")
    body = platform.get("/api/pages/sales?date_from=2026-08-01&date_to=2026-08-31", headers=auth(token)).json()
    assert body["summary"]["value"] == pytest.approx(1_800_000)
    assert body["growth"]["values"]["growth_percent"] is not None
    for section in ("daily_trend", "target_vs_actual", "region_performance",
                    "brand_performance", "customer_performance"):
        assert section in body
    # The general breakdown is brand-wise; SKU ranking moved to /pages/products.
    assert "product_performance" not in body


def test_stock_page_uses_phase_3_calculations(platform: TestClient) -> None:
    token = login(platform, "ceo")
    body = platform.get("/api/pages/stock" + WINDOW, headers=auth(token)).json()
    for section in ("summary", "by_plant", "by_storage_location",
                    "by_material_group", "expiry", "expiring"):
        assert section in body, section
    # The four categories stay four numbers, with the total beside them.
    values = body["summary"]["values"]
    assert values["unrestricted_stock"] == pytest.approx(815)
    assert values["total_stock"] == pytest.approx(900)
    # Every expiry bucket is present even when it holds nothing.
    assert {row["code"] for row in body["expiry"]["rows"]} == {
        "EXPIRED", "EXPIRING_SOON", "VALID", "NO_EXPIRY"}


def test_stock_page_says_the_period_does_not_apply(platform: TestClient) -> None:
    """Stock has no posting date, so a different window must give the same figures.

    The page states that in a note rather than silently ignoring the filter —
    a number that quietly disregards the period the user chose is a lie of
    omission.
    """
    token = login(platform, "ceo")
    august = platform.get("/api/pages/stock" + WINDOW, headers=auth(token)).json()
    january = platform.get(
        "/api/pages/stock?date_from=2026-01-01&date_to=2026-01-31",
        headers=auth(token)).json()
    assert (august["summary"]["values"]["total_stock"]
            == january["summary"]["values"]["total_stock"])
    assert any("no posting date" in note.lower() or "snapshot" in note.lower()
               for note in august["summary"]["notes"])


def test_performance_page_drills_down(platform: TestClient) -> None:
    token = login(platform, "ceo")
    regions = platform.get("/api/pages/performance?level=region&date_from=2026-08-01&date_to=2026-08-31",
                           headers=auth(token)).json()
    assert regions["next_level"] == "area"
    assert {r["code"] for r in regions["performance"]["rows"]} == {"REG001", "REG002"}

    areas = platform.get("/api/pages/performance?level=area&region_code=REG001&date_from=2026-08-01&date_to=2026-08-31",
                         headers=auth(token)).json()
    assert areas["level"] == "area"
    assert {r["code"] for r in areas["performance"]["rows"]} == {"AR001"}


def test_drill_down_respects_rbac(platform: TestClient) -> None:
    token = login(platform, "dhaka_rm")
    response = platform.get("/api/pages/performance?level=area&region_code=REG002&date_from=2026-08-01&date_to=2026-08-31",
                            headers=auth(token))
    assert response.status_code == 403
    assert "permission" in response.json()["detail"].lower()


def test_unknown_performance_level_is_a_404(platform: TestClient) -> None:
    token = login(platform, "ceo")
    assert platform.get("/api/pages/performance?level=galaxy",
                        headers=auth(token)).status_code == 404


# --------------------------------------------------------------------------
# Transaction table
# --------------------------------------------------------------------------


def test_transactions_are_paginated_server_side(platform: TestClient) -> None:
    token = login(platform, "ceo")
    body = platform.get("/api/pages/transactions/sales?page=1&page_size=2&date_from=2026-08-01&date_to=2026-08-31",
                        headers=auth(token)).json()
    assert len(body["rows"]) <= 2
    assert body["total"] >= 3
    assert body["total_pages"] >= 2
    assert "invoice_no" in body["columns"]


def test_transaction_search_and_sort(platform: TestClient) -> None:
    token = login(platform, "ceo")
    body = platform.get(
        "/api/pages/transactions/sales?search=INV-D1&sort_by=net_sales&sort_dir=desc"
        "&date_from=2026-08-01&date_to=2026-08-31",
        headers=auth(token)).json()
    assert body["rows"]
    assert all("INV-D1" in str(r["invoice_no"]) for r in body["rows"])


def test_transaction_sort_column_is_whitelisted(platform: TestClient) -> None:
    token = login(platform, "ceo")
    response = platform.get(
        "/api/pages/transactions/sales?sort_by=(SELECT+1)", headers=auth(token))
    assert response.status_code == 422


def test_transactions_are_scoped(platform: TestClient) -> None:
    everything = platform.get("/api/pages/transactions/sales?page_size=200&date_from=2026-08-01&date_to=2026-08-31",
                              headers=auth(login(platform, "ceo"))).json()
    dhaka = platform.get("/api/pages/transactions/sales?page_size=200&date_from=2026-08-01&date_to=2026-08-31",
                         headers=auth(login(platform, "dhaka_rm"))).json()
    assert dhaka["total"] < everything["total"]


# --------------------------------------------------------------------------
# Cascading filters and search
# --------------------------------------------------------------------------


def test_filter_levels_describe_the_hierarchy(platform: TestClient) -> None:
    token = login(platform, "ceo")
    body = platform.get("/api/master-data/levels", headers=auth(token)).json()
    levels = [level["level"] for level in body["levels"]]
    assert levels[0] == "company_code" and levels[-1] == "sub_territory_code"
    region = next(l for l in body["levels"] if l["level"] == "region_code")
    assert region["parent"] == "zone_code"


def test_filter_options_cascade_by_parent(platform: TestClient) -> None:
    token = login(platform, "ceo")
    all_areas = platform.get("/api/master-data/options/area_code",
                             headers=auth(token)).json()
    assert {o["code"] for o in all_areas["options"]} == {"AR001", "AR002"}

    under_dhaka = platform.get(
        "/api/master-data/options/area_code?parent_code=REG001",
        headers=auth(token)).json()
    assert {o["code"] for o in under_dhaka["options"]} == {"AR001"}


def test_filter_options_are_permission_scoped(platform: TestClient) -> None:
    token = login(platform, "dhaka_rm")
    body = platform.get("/api/master-data/options/region_code",
                        headers=auth(token)).json()
    assert {o["code"] for o in body["options"]} == {"REG001"}


def test_filter_options_search(platform: TestClient) -> None:
    token = login(platform, "ceo")
    body = platform.get("/api/master-data/options/region_code?search=khu",
                        headers=auth(token)).json()
    assert {o["code"] for o in body["options"]} == {"REG002"}


def test_unknown_filter_level_is_a_404(platform: TestClient) -> None:
    token = login(platform, "ceo")
    assert platform.get("/api/master-data/options/planet_code",
                        headers=auth(token)).status_code == 404


def test_global_search_is_scoped(platform: TestClient) -> None:
    ceo = platform.get("/api/master-data/search?q=Khulna",
                       headers=auth(login(platform, "ceo"))).json()
    assert ceo["count"] >= 1

    scoped = platform.get("/api/master-data/search?q=Khulna",
                          headers=auth(login(platform, "dhaka_rm"))).json()
    assert scoped["count"] == 0     # not visible to a Dhaka manager


def test_global_search_finds_materials(platform: TestClient) -> None:
    token = login(platform, "ceo")
    body = platform.get("/api/master-data/search?q=Premium", headers=auth(token)).json()
    assert any(r["entity_type"] == "material" for r in body["results"])


# --------------------------------------------------------------------------
# Alerts and notifications
# --------------------------------------------------------------------------


def test_alerts_carry_severity_and_a_link(platform: TestClient) -> None:
    token = login(platform, "ceo")
    body = platform.get("/api/alerts?date_from=2026-08-01&date_to=2026-08-31", headers=auth(token)).json()
    assert body["alerts"]
    for alert in body["alerts"]:
        assert alert["severity"] in {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
        # No COLLECTION or OUTSTANDING: those alerts had nothing left to fire
        # on once revision 0020 removed the datasets behind them. Listing them
        # here would pass either way — the check is a membership test — and so
        # would quietly suggest a category that can no longer occur.
        assert alert["category"] in {"SALES", "TARGET", "STOCK", "SYSTEM"}
        assert alert["link"].startswith("/")


def test_alerts_can_be_filtered_by_severity(platform: TestClient) -> None:
    token = login(platform, "ceo")
    body = platform.get("/api/alerts?severity=CRITICAL", headers=auth(token)).json()
    assert all(a["severity"] == "CRITICAL" for a in body["alerts"])


def test_notifications_are_per_user(platform: TestClient, agent_engine) -> None:
    from app.database.models_ai import Notification

    token = login(platform, "ceo")
    me = platform.get("/api/auth/me", headers=auth(token)).json()
    with Session(agent_engine) as session:
        session.add(Notification(user_id=me["user_id"], category="STOCK",
                                 severity="CRITICAL", title="SKU002 is out of stock",
                                 link="/stock"))
        session.commit()

    body = platform.get("/api/notifications", headers=auth(token)).json()
    assert body["unread_count"] == 1
    notification_id = body["notifications"][0]["notification_id"]

    other = platform.get("/api/notifications",
                         headers=auth(login(platform, "dhaka_rm"))).json()
    assert other["unread_count"] == 0
    assert platform.post(f"/api/notifications/{notification_id}/read",
                         headers=auth(login(platform, "dhaka_rm"))).status_code == 404

    assert platform.post(f"/api/notifications/{notification_id}/read",
                         headers=auth(token)).status_code == 200
    assert platform.get("/api/notifications",
                        headers=auth(token)).json()["unread_count"] == 0


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------


def test_admin_endpoints_reject_non_admins(platform: TestClient) -> None:
    token = login(platform, "ceo")      # MANAGEMENT is not an admin role
    for path in ("/api/admin/users", "/api/admin/roles", "/api/admin/audit-logs"):
        response = platform.get(path, headers=auth(token))
        assert response.status_code == 403, path
        assert "permission" in response.json()["detail"].lower()


def test_admin_can_list_users(platform: TestClient) -> None:
    token = login(platform, "root")
    body = platform.get("/api/admin/users", headers=auth(token)).json()
    assert body["total"] >= 6
    assert all("password_hash" not in user for user in body["users"])


def test_admin_creates_a_user_with_a_validated_scope(platform: TestClient) -> None:
    token = login(platform, "root")
    response = platform.post("/api/admin/users", headers=auth(token), json={
        "username": "khulna_am", "password": PASSWORD, "role": Role.AREA_MANAGER,
        "display_name": "Khulna Area Manager",
        "data_scope": {"area_code": ["AR002"]},
    })
    assert response.status_code == 201, response.text
    assert response.json()["data_scope"] == {"area_code": ["AR002"]}
    # The new account can sign in and sees only its own area.
    scoped = platform.get("/api/dashboard" + WINDOW,
                          headers=auth(login(platform, "khulna_am"))).json()
    sales = next(k for k in scoped["kpis"] if k["key"] == "total_sales")
    assert sales["value"] == pytest.approx(300_000)


def test_admin_cannot_grant_a_scope_that_does_not_exist(platform: TestClient) -> None:
    token = login(platform, "root")
    response = platform.post("/api/admin/users", headers=auth(token), json={
        "username": "ghost_rm", "password": PASSWORD, "role": Role.REGIONAL_MANAGER,
        "data_scope": {"region_code": ["REG999"]},
    })
    assert response.status_code == 422
    assert "REG999" in response.json()["detail"]


def test_admin_rejects_a_weak_password(platform: TestClient) -> None:
    token = login(platform, "root")
    response = platform.post("/api/admin/users", headers=auth(token), json={
        "username": "weak", "password": "123", "role": Role.VIEWER,
    })
    assert response.status_code == 422


def test_admin_cannot_remove_the_last_administrator(platform: TestClient) -> None:
    token = login(platform, "root")
    me = platform.get("/api/auth/me", headers=auth(token)).json()
    response = platform.patch(f"/api/admin/users/{me['user_id']}", headers=auth(token),
                              json={"role": Role.VIEWER})
    assert response.status_code == 409


def test_audit_log_records_report_views(platform: TestClient) -> None:
    platform.get("/api/dashboard" + WINDOW, headers=auth(login(platform, "ceo")))
    body = platform.get("/api/admin/audit-logs?action=VIEW_REPORT",
                        headers=auth(login(platform, "root"))).json()
    assert body["total"] >= 1
    assert body["logs"][0]["resource"] == "dashboard"


def test_audit_details_never_contain_secrets() -> None:
    from app.auth.audit import sanitize

    cleaned = sanitize({
        "password": "hunter2", "api_key": "sk-123", "DATABASE_URL": "postgres://u:p@h/d",
        "region": "REG001", "nested": {"access_token": "abc", "ok": 1},
    })
    assert cleaned["password"] == "[redacted]"
    assert cleaned["api_key"] == "[redacted]"
    assert cleaned["DATABASE_URL"] == "[redacted]"
    assert cleaned["nested"]["access_token"] == "[redacted]"
    assert cleaned["region"] == "REG001"
    assert cleaned["nested"]["ok"] == 1


def test_audit_details_are_json_storable() -> None:
    """Everything sanitize returns must survive the JSON column it is written to.

    This is not a cosmetic guarantee. Both callers write the result into a JSON
    column, and a value the encoder refuses raised inside ``audit.record``'s
    flush — where the ``except`` that stops auditing from breaking a request
    rolled the *caller's own write* back. The route then committed a clean
    session and answered 201, so the record silently did not exist. A Decimal is
    what SQLAlchemy returns for every NUMERIC column, so this was reachable by
    creating any managed record with a decimal field: a Map Location, a material
    with a conversion factor or transfer price, or an upazila with a coordinate.
    """
    import datetime as dt
    import json
    from decimal import Decimal

    from app.auth.audit import sanitize

    cleaned = sanitize({
        "latitude": Decimal("23.7808"),
        "conversion_factor": Decimal("0.5"),
        "when": dt.datetime(2026, 9, 4, 10, 30),
        "day": dt.date(2026, 9, 4),
        "nested": [Decimal("1.5"), {"transfer_price": Decimal("240")}],
        "already_fine": "text",
    })

    # The point of the test: it can actually be stored.
    json.dumps(cleaned)

    # A figure keeps its value, as a float — the same shape
    # ``ai.queries.normalize_value`` gives every other number leaving the
    # warehouse, so the change log and the reports cannot state one differently.
    assert cleaned["latitude"] == 23.7808
    assert isinstance(cleaned["latitude"], float)
    assert cleaned["conversion_factor"] == 0.5
    assert cleaned["nested"][0] == 1.5
    assert cleaned["nested"][1]["transfer_price"] == 240.0
    assert cleaned["when"] == "2026-09-04T10:30:00"
    assert cleaned["day"] == "2026-09-04"
    assert cleaned["already_fine"] == "text"


def test_an_unstorable_value_becomes_text_rather_than_losing_the_entry() -> None:
    """An unrecognised type is recorded approximately, never dropped."""
    import json

    from app.auth.audit import sanitize

    class Odd:
        def __str__(self) -> str:
            return "odd-value"

    cleaned = sanitize({"thing": Odd()})
    json.dumps(cleaned)
    assert cleaned["thing"] == "odd-value"


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


def test_export_includes_report_metadata(platform: TestClient) -> None:
    token = login(platform, "ceo")
    rows = platform.get("/api/pages/sales" + WINDOW, headers=auth(token)).json()[
        "region_performance"]["rows"]
    response = platform.post("/api/reports/export", headers=auth(token), json={
        "format": "csv", "title": "Region Sales", "report_name": "Region Sales",
        "date_range": "01 Aug 2026 – 15 Aug 2026",
        "filters": {"region_codes": ["REG001"]}, "rows": rows,
    })
    assert response.status_code == 200
    text = response.content.decode("utf-8")
    assert "# Company" in text
    assert "Region Sales" in text
    assert "Generated By" in text
    assert "REG001" in text


def test_pdf_export_includes_kpis(platform: TestClient) -> None:
    token = login(platform, "ceo")
    response = platform.post("/api/reports/export", headers=auth(token), json={
        "format": "pdf", "title": "Sales", "report_name": "Sales Report",
        "kpis": [["Net Sales", "৳18.00 L"], ["Achievement", "60.0%"]],
        "rows": [{"label": "Dhaka", "net_sales": 1500000}],
    })
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_export_is_audited(platform: TestClient, agent_engine) -> None:
    token = login(platform, "ceo")
    platform.post("/api/reports/export", headers=auth(token),
                  json={"format": "csv", "title": "Sales", "rows": []})
    with Session(agent_engine) as session:
        assert session.query(AuditLog).filter_by(action="EXPORT").count() >= 1


# --------------------------------------------------------------------------
# WhatsApp
# --------------------------------------------------------------------------


def test_webhook_verification_requires_the_right_token(platform, monkeypatch) -> None:
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "secret-verify")
    ok = platform.get("/api/integrations/whatsapp/webhook",
                      params={"hub.mode": "subscribe",
                              "hub.verify_token": "secret-verify",
                              "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"

    bad = platform.get("/api/integrations/whatsapp/webhook",
                       params={"hub.mode": "subscribe",
                               "hub.verify_token": "wrong", "hub.challenge": "12345"})
    assert bad.status_code == 403


def test_webhook_verification_is_refused_without_a_configured_token(platform,
                                                                    monkeypatch) -> None:
    monkeypatch.delenv("WHATSAPP_VERIFY_TOKEN", raising=False)
    response = platform.get("/api/integrations/whatsapp/webhook",
                            params={"hub.mode": "subscribe",
                                    "hub.verify_token": "anything",
                                    "hub.challenge": "1"})
    assert response.status_code == 403


def _whatsapp_payload(number: str, text: str) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "value": {
                    "contacts": [{"wa_id": number,
                                  "profile": {"name": "Managing Director"}}],
                    "messages": [{"id": "wamid.1", "from": number, "type": "text",
                                  "timestamp": "1770000000",
                                  "text": {"body": text}}],
                }
            }]
        }],
    }


def test_known_number_gets_an_answer(platform: TestClient) -> None:
    response = platform.post(
        "/api/integrations/whatsapp/webhook",
        json=_whatsapp_payload("8801700000001", "এই মাসের sales কত?"),
    )
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["authorized"] is True
    assert result["intent"] == "SALES_SUMMARY"
    # No provider is configured in tests, so nothing is claimed to be delivered.
    assert result["delivered"] is False


def test_unknown_number_is_refused_without_business_data(platform: TestClient) -> None:
    response = platform.post(
        "/api/integrations/whatsapp/webhook",
        json=_whatsapp_payload("8801999999999", "এই মাসের sales কত?"),
    )
    result = response.json()["results"][0]
    assert result["authorized"] is False


def test_webhook_ignores_non_text_messages(platform: TestClient) -> None:
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "1", "from": "8801700000001", "type": "image"}]}}]}]}
    body = platform.post("/api/integrations/whatsapp/webhook", json=payload).json()
    assert body["handled"] == 0


def test_webhook_signature_is_enforced_when_configured(platform, monkeypatch) -> None:
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "app-secret")
    response = platform.post(
        "/api/integrations/whatsapp/webhook",
        json=_whatsapp_payload("8801700000001", "sales"),
        headers={"X-Hub-Signature-256": "sha256=wrong"},
    )
    assert response.status_code == 403


def test_whatsapp_reply_formatting() -> None:
    from app.integrations.whatsapp import format_reply

    text = format_reply(
        "📊 Sales\n\n**Net Sales:** ৳18.00 L\n\n| Name | Net Sales |\n|---|---:|\n"
        "| Dhaka | ৳15.00 L |",
        rows=[{"label": "Dhaka", "net_sales": 1500000},
              {"label": "Khulna", "net_sales": 300000}],
    )
    assert "*Net Sales:*" in text          # WhatsApp bold, not markdown bold
    assert "|" not in text                  # the table is gone
    assert "1. Dhaka — ৳15.00 L" in text
    assert "2. Khulna — ৳3.00 L" in text


def test_long_whatsapp_reply_is_summarised() -> None:
    from app.integrations.whatsapp import LARGE_REPORT_REPLY, format_reply

    assert LARGE_REPORT_REPLY in format_reply("x" * 6000)


def test_whatsapp_status_is_admin_only_and_leaks_no_token(platform,
                                                          monkeypatch) -> None:
    monkeypatch.setenv("WHATSAPP_API_TOKEN", "super-secret-token")
    assert platform.get("/api/integrations/whatsapp/status",
                        headers=auth(login(platform, "ceo"))).status_code == 403

    body = platform.get("/api/integrations/whatsapp/status",
                        headers=auth(login(platform, "root"))).json()
    assert "super-secret-token" not in str(body)
    assert body["linked_users"] >= 1


def test_whatsapp_simulation_sends_nothing(platform: TestClient) -> None:
    token = login(platform, "ceo")
    body = platform.post("/api/integrations/whatsapp/simulate", headers=auth(token),
                         json={"text": "এই মাসের sales কত?"}).json()
    assert body["authorized"] is True
    assert body["delivered"] is False
    assert "no message was sent" in body["note"].lower()


# --------------------------------------------------------------------------
# Health and period options
# --------------------------------------------------------------------------


def test_health_reports_version_and_database(platform: TestClient) -> None:
    body = platform.get("/health").json()
    assert body["status"] == "ok"
    assert body["database"] in {"connected", "unavailable"}
    assert body["version"]


def test_period_options_come_from_the_backend(platform: TestClient) -> None:
    token = login(platform, "ceo")
    body = platform.get("/api/period-options", headers=auth(token)).json()
    values = {option["value"] for option in body["options"]}
    assert {"TODAY", "THIS_MONTH", "LAST_MONTH", "YTD"} <= values
    assert body["financial_year_start_month"] == 7
    assert body["current_financial_year"].startswith("FY ")
