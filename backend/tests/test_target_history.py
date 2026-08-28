"""Historical analysis: the basis a target is generated from.

The screen's whole job is to say what actually happened, so the tests are mostly
about *not* saying more than the data supports:

* no sales in a year is ``None``, never ``0`` — a measurement of nothing is not
  a measurement of zero, and every ratio built on it is undefined rather than
  infinite;
* a ``SUM`` over a column that is sometimes NULL understates silently, so a
  year that had blank volumes is marked incomplete and says how many;
* a deployment with no sales loaded at all gets an honest empty screen with a
  note, not a basis invented from nothing.

Plus the two things that make the numbers *right*: the basis years come from the
configured calendar rather than a hard-coded list, and the query runs under the
caller's own data scope, so a regional manager's basis is their region's.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.auth.security import hash_password
from app.database.models import DimMaterial
from app.database.models_ai import AppUser, Role
from app.etl.calendar import FinancialYearConfig
from app.main import app
from app.targetmgmt import country, history, plans as plan_service
from conftest_phase2 import sales_row
from conftest_phase3 import _load

PASSWORD = "Correct-Horse-9"

SCOPE = {
    "financial_year": "FY 2026-27",
    "target_period": "FY",
    "company_code": "C001",
    "bu_code": "BU001",
    "sales_line_code": "SL001",
}


@pytest.fixture
def plan(agent_engine, users):
    """A plan for FY 2026-27, so its basis years are FY 2024-25 and FY 2025-26."""
    with Session(agent_engine) as session:
        created = plan_service.create_plan(
            session, users["ceo"], scope=plan_service.PlanScope(**SCOPE))
        session.commit()
        current = plan_service.current_version(session, created.plan_id)
        return {"plan_id": created.plan_id, "version_id": current.version_id}


def _plan_row(session, plan):
    return plan_service.get_plan(session, plan["plan_id"])


# ---------------------------------------------------------------------------
# The basis years
# ---------------------------------------------------------------------------


def test_the_basis_is_the_two_years_before_the_plan(agent_engine, plan) -> None:
    with Session(agent_engine) as session:
        years = history.basis_years(_plan_row(session, plan))
    assert years == ["FY 2024-25", "FY 2025-26"]


def test_the_plans_own_basis_years_win(agent_engine, plan) -> None:
    """"Two years back" is a planning decision, and a plan may state otherwise."""
    with Session(agent_engine) as session:
        row = _plan_row(session, plan)
        row.basis_financial_years = "FY 2022-23, FY 2023-24"
        session.flush()
        assert history.basis_years(row) == ["FY 2022-23", "FY 2023-24"]


def test_the_basis_years_follow_the_configured_calendar(agent_engine,
                                                        plan) -> None:
    """A January start labels its years differently, and this must follow.

    The financial year is configuration, so a hard-coded list of labels would go
    on saying ``FY 2024-25`` to a deployment whose years are plain calendar ones.
    """
    with Session(agent_engine) as session:
        row = _plan_row(session, plan)
        row.financial_year = "FY 2026"
        january = FinancialYearConfig(start_month=1)
        assert history.basis_years(row, january) == ["FY 2024", "FY 2025"]


def test_a_year_window_is_the_financial_year_not_the_calendar_one() -> None:
    july = FinancialYearConfig(start_month=7)
    assert history.year_window("FY 2025-26", july) == (
        dt.date(2025, 7, 1), dt.date(2026, 6, 30),
    )


def test_a_financial_year_with_no_readable_start_is_refused() -> None:
    """Refused rather than defaulted: a silently wrong window would produce a
    basis from the wrong years and look entirely plausible."""
    with pytest.raises(ValueError):
        history.year_window("last year")


# ---------------------------------------------------------------------------
# Derived figures
# ---------------------------------------------------------------------------


def test_growth_is_undefined_against_a_year_with_no_volume() -> None:
    assert history._percent_change(None, 100) is None
    assert history._percent_change(0, 100) is None
    assert history._percent_change(100, None) is None


def test_growth_is_a_percentage_of_the_earlier_year() -> None:
    assert history._percent_change(100, 150) == 50.0
    assert history._percent_change(100, 80) == -20.0


@pytest.mark.parametrize(
    "earliest,latest,growth,expected",
    [
        (100, 110, 10.0, history.Basis.TWO_YEAR_AVERAGE),
        (100, 200, 100.0, history.Basis.GROWTH_WEIGHTED),
        # Exactly at guidance stays on the average: the rule is "above".
        (100, 115, 15.0, history.Basis.TWO_YEAR_AVERAGE),
        (None, 500, None, history.Basis.NEW_MATERIAL),
        (0, 500, None, history.Basis.NEW_MATERIAL),
        (None, None, None, history.Basis.NO_HISTORY),
    ],
)
def test_the_suggested_basis_follows_the_history(earliest, latest, growth,
                                                 expected) -> None:
    assert history._suggest_basis(earliest, latest, growth) == expected


# ---------------------------------------------------------------------------
# Reading real sales
# ---------------------------------------------------------------------------


def test_a_deployment_with_no_sales_says_so_rather_than_inventing_a_basis(
        agent_engine, users, plan) -> None:
    """The state every deployment starts in, and the one this must handle well."""
    with Session(agent_engine) as session:
        result = history.analyse(session, users["ceo"],
                                 plan=_plan_row(session, plan),
                                 version_id=plan["version_id"])

    assert result["rows"] == []
    assert result["totals"]["with_history"] == 0
    assert any("no country target" in note.lower()
               or "no sales history" in note.lower()
               for note in result["notes"])


def test_a_targeted_material_with_no_history_still_appears(agent_engine, users,
                                                           plan) -> None:
    """Exactly what a planner opens this screen to find.

    A material somebody has set a target for, with nothing behind it — invisible
    if the table listed only materials with sales.
    """
    with Session(agent_engine) as session:
        session.add(DimMaterial(
            material_code="TH-001", material_description="Untraded",
            material_group_code="MG01", material_group_name="Tea",
            material_brand_code="MB01", material_brand="Example Brand",
            company_code="C001"))
        session.flush()
        version = plan_service.get_version(session, plan["version_id"])
        country.set_lines(session, users["ceo"], version=version,
                          plan=_plan_row(session, plan),
                          entries=[("TH-001", 5000)])
        session.commit()

        result = history.analyse(session, users["ceo"],
                                 plan=_plan_row(session, plan),
                                 version_id=plan["version_id"])

    row = result["rows"][0]
    assert row["material_code"] == "TH-001"
    assert row["basis"] == history.Basis.NO_HISTORY
    assert row["volumes"] == [None, None]
    assert row["current_target_volume"] == 5000
    # And the growth against a year with no volume is undefined, not -100%.
    assert row["target_growth_percent"] is None


def test_no_sales_in_a_year_is_none_rather_than_zero(agent_engine, users,
                                                     plan) -> None:
    with Session(agent_engine) as session:
        result = history.analyse(session, users["ceo"],
                                 plan=_plan_row(session, plan),
                                 version_id=plan["version_id"])
    for year in result["totals"]["years"]:
        # None, so the screen can say n/a. Zero would claim a measurement.
        assert year["volume"] is None
        assert year["materials_with_volume"] == 0


# ---------------------------------------------------------------------------
# With sales loaded
# ---------------------------------------------------------------------------


@pytest.fixture
def with_sales(agent_engine, users, plan):
    """Two basis years of real sales for one material, loaded through the ETL.

    Loaded rather than inserted, so the figures travel the same validation,
    hierarchy resolution and view the Sales page reads them through — a basis
    built by a different route would not be the same number.
    """
    _load(agent_engine, "sales", [
        # FY 2024-25 (July 2024 – June 2025).
        sales_row(**{"Invoice No": "TH-A1", "Date": "2024-09-10",
                     "Quantity": 100, "Total Volume": 1000,
                     "Gross Sales": 100_000, "Discount": 0, "Cost": 60_000}),
        # FY 2025-26, grown 50% — above guidance, so growth-weighted.
        sales_row(**{"Invoice No": "TH-B1", "Date": "2025-09-10",
                     "Quantity": 150, "Total Volume": 1500,
                     "Gross Sales": 150_000, "Discount": 0, "Cost": 90_000}),
    ])
    return plan


def test_two_years_of_volume_are_read_per_material(agent_engine, users,
                                                   with_sales) -> None:
    with Session(agent_engine) as session:
        result = history.analyse(session, users["ceo"],
                                 plan=_plan_row(session, with_sales),
                                 version_id=with_sales["version_id"])

    assert result["basis_years"] == ["FY 2024-25", "FY 2025-26"]
    row = result["rows"][0]
    assert row["volumes"] == [1000.0, 1500.0]
    assert row["growth_percent"] == pytest.approx(50.0)
    assert row["average_volume"] == pytest.approx(1250.0)
    # The only material with volume, so it is all of the later year.
    assert row["contribution_percent"] == pytest.approx(100.0)
    assert row["basis"] == history.Basis.GROWTH_WEIGHTED


def test_the_year_totals_add_up(agent_engine, users, with_sales) -> None:
    with Session(agent_engine) as session:
        result = history.analyse(session, users["ceo"],
                                 plan=_plan_row(session, with_sales),
                                 version_id=with_sales["version_id"])
    volumes = {year["financial_year"]: year["volume"]
               for year in result["totals"]["years"]}
    assert volumes == {"FY 2024-25": 1000.0, "FY 2025-26": 1500.0}


def test_a_sales_row_with_no_volume_marks_the_year_incomplete(
        agent_engine, users, plan) -> None:
    """The trap this module exists to avoid.

    ``SUM(volume)`` returns 1,000 whether the other row stated 500 or stated
    nothing. Reporting 1,000 as the year's volume in the second case is a total
    that quietly omits part of its own input.
    """
    _load(agent_engine, "sales", [
        sales_row(**{"Invoice No": "TH-C1", "Date": "2025-09-10",
                     "Quantity": 100, "Total Volume": 1000,
                     "Gross Sales": 100_000, "Discount": 0, "Cost": 60_000}),
        sales_row(**{"Invoice No": "TH-C2", "Date": "2025-10-10",
                     "Quantity": 50, "Total Volume": None,
                     "Gross Sales": 50_000, "Discount": 0, "Cost": 30_000}),
    ])

    with Session(agent_engine) as session:
        result = history.analyse(session, users["ceo"],
                                 plan=_plan_row(session, plan),
                                 version_id=plan["version_id"])

    row = result["rows"][0]
    assert row["complete"] is False
    later = row["years"][-1]
    assert later["volume"] == 1000.0
    assert later["rows_without_volume"] == 1
    assert row["material_code"] in result["totals"]["incomplete_materials"]
    assert any("state no volume" in note for note in result["notes"])


def test_the_basis_is_scoped_to_the_caller(agent_engine, users,
                                           with_sales) -> None:
    """A regional manager's basis is their region's history.

    The seeded sale belongs to REG001, so the Dhaka manager sees it and the
    Khulna manager sees nothing — the same scope chain every other report runs
    under, applied here so this figure and the Sales page cannot disagree.
    """
    with Session(agent_engine) as session:
        dhaka = history.analyse(session, users["dhaka_rm"],
                                plan=_plan_row(session, with_sales),
                                version_id=with_sales["version_id"])
        khulna = history.analyse(session, users["khulna_rm"],
                                 plan=_plan_row(session, with_sales),
                                 version_id=with_sales["version_id"])

    assert dhaka["totals"]["with_history"] == 1
    assert khulna["totals"]["with_history"] == 0


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


@pytest.fixture
def tm_client(agent_engine, users):
    with Session(agent_engine) as session:
        for user in session.query(AppUser).all():
            user.password_hash = hash_password(PASSWORD)
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


def test_the_endpoint_returns_the_basis(tm_client: TestClient,
                                        with_sales) -> None:
    login = tm_client.post("/api/auth/login",
                           json={"username": "root", "password": PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = tm_client.get(
        f"/api/target-management/versions/{with_sales['version_id']}/history",
        headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["basis_years"] == ["FY 2024-25", "FY 2025-26"]
    assert body["growth_guidance_percent"] == history.GROWTH_GUIDANCE_PERCENT
    assert body["rows"][0]["volumes"] == [1000.0, 1500.0]


def test_the_endpoint_needs_the_section(tm_client: TestClient,
                                        with_sales) -> None:
    login = tm_client.post("/api/auth/login",
                           json={"username": "ceo", "password": PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = tm_client.get(
        f"/api/target-management/versions/{with_sales['version_id']}/history",
        headers=headers)
    assert response.status_code == 403
