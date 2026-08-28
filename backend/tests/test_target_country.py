"""The country target: the one figure typed, and the two derived from it.

The sharpest edge in this module is what happens when the Material Master
states no Conversion Factor or no Transfer Price — which is the state every
deployment starts in, because revision 0027 added both columns nullable with no
back-fill. So most of this file is about *not* producing a number:

* a line with no conversion factor derives neither quantity nor value;
* a **total** over a mixture of derivable and non-derivable lines is suppressed
  entirely, because a partial sum presented as "Country Total" is not a small
  number, it is a wrong one;
* an unreadable volume is refused by name, never coerced to zero.

The rest pins the write path: all-or-nothing validation, per-line auditing of
only what actually changed, and a frozen version refusing to be typed into.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.auth.security import hash_password
from app.database.models import DimMaterial
from app.database.models_ai import AppUser, Role
from app.database.models_target import (
    TargetAudit,
    TargetCountryLine,
    TargetStatus,
)
from app.main import app
from app.targetmgmt import country, plans as plan_service
from app.targetmgmt.errors import VersionFrozen

PASSWORD = "Correct-Horse-9"

SCOPE = {
    "financial_year": "FY 2026-27",
    "target_period": "Q1",
    "company_code": "C001",
    "bu_code": "BU001",
    "sales_line_code": "SL001",
}

#: ``(code, description, conversion factor, transfer price)``.
#:
#: Four materials covering the four states a line can be in: both inputs
#: present, no transfer price, no conversion factor, and neither.
MATERIALS = (
    ("TM-FULL", "Both inputs stated", 0.5, 240.0),
    ("TM-NOPRICE", "No transfer price", 1.0, None),
    ("TM-NOFACTOR", "No conversion factor", None, 320.0),
    ("TM-NEITHER", "Neither input", None, None),
)


@pytest.fixture
def materials(agent_engine):
    """Materials under company C001, so a plan for C001 may target them.

    The shared fixture's materials state no company at all — they predate
    revision 0023 in the same way a real deployment's oldest rows do — so they
    are deliberately *not* reused here: a plan fixes one company, and putting a
    material that names none into it would be the guess this module refuses.
    """
    with Session(agent_engine) as session:
        for code, description, factor, price in MATERIALS:
            session.add(DimMaterial(
                material_code=code, material_description=description,
                material_group_code="MG01", material_group_name="Tea",
                material_brand_code="MB01", material_brand="Example Brand",
                company_code="C001",
                conversion_factor=factor, transfer_price=price,
            ))
        session.commit()
    return MATERIALS


@pytest.fixture
def version(agent_engine, users, materials):
    """A plan and its V1, committed, with no country lines yet."""
    with Session(agent_engine) as session:
        plan = plan_service.create_plan(
            session, users["ceo"], scope=plan_service.PlanScope(**SCOPE))
        session.commit()
        current = plan_service.current_version(session, plan.plan_id)
        return {"plan_id": plan.plan_id, "version_id": current.version_id}


@pytest.fixture
def tm_client(agent_engine, users, materials):
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


def _token(client: TestClient, username: str = "root") -> dict[str, str]:
    response = client.post("/api/auth/login",
                           json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _set(session, users, version, entries):
    version_row = plan_service.get_version(session, version["version_id"])
    plan = plan_service.get_plan(session, version["plan_id"])
    return country.set_lines(session, users["ceo"], version=version_row,
                             plan=plan, entries=entries)


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def test_quantity_is_volume_over_the_conversion_factor() -> None:
    quantity, value = country.derive(1000, 0.5, 240)
    assert quantity == 2000
    assert value == 480_000


def test_a_missing_conversion_factor_derives_nothing() -> None:
    """Value depends on quantity, so losing the divisor loses both."""
    assert country.derive(1000, None, 320) == (None, None)


def test_a_zero_conversion_factor_is_treated_as_missing() -> None:
    """A pack containing none of the goods is an unfilled cell, not a fact.

    And the alternative is a division by zero, which would be an infinity
    presented as a target.
    """
    assert country.derive(1000, 0, 320) == (None, None)


def test_a_missing_transfer_price_still_derives_a_quantity() -> None:
    """Quantity needs only the factor. Value is what cannot be computed."""
    quantity, value = country.derive(1000, 0.5, None)
    assert quantity == 2000
    assert value is None


def test_a_zero_volume_derives_a_zero_quantity() -> None:
    """Zero is a real instruction — "we are not selling this here" — not absence."""
    assert country.derive(0, 0.5, 240) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_a_line_carries_its_material_and_its_derived_figures(
        agent_engine, users, version) -> None:
    with Session(agent_engine) as session:
        _set(session, users, version, [("TM-FULL", 1000)])
        session.commit()
        line = country.list_lines(session, version["version_id"])[0]

    assert line.material_code == "TM-FULL"
    assert line.material_description == "Both inputs stated"
    assert line.target_volume == 1000
    assert line.quantity == 2000
    assert line.value == 480_000
    assert line.missing == []


def test_a_line_without_a_factor_names_what_is_missing(
        agent_engine, users, version) -> None:
    """`missing` rather than two nulls: the reader has to know which column."""
    with Session(agent_engine) as session:
        _set(session, users, version, [("TM-NOFACTOR", 500)])
        session.commit()
        line = country.list_lines(session, version["version_id"])[0]

    assert line.quantity is None
    assert line.value is None
    assert line.missing == ["conversion_factor"]
    assert line.to_dict()["missing"] == ["conversion_factor"]


def test_a_line_without_a_price_names_only_the_price(
        agent_engine, users, version) -> None:
    with Session(agent_engine) as session:
        _set(session, users, version, [("TM-NOPRICE", 500)])
        session.commit()
        line = country.list_lines(session, version["version_id"])[0]

    assert line.quantity == 500
    assert line.value is None
    assert line.missing == ["transfer_price"]


# ---------------------------------------------------------------------------
# Totals — the suppression rule
# ---------------------------------------------------------------------------


def test_a_complete_target_totals_all_three_measures(agent_engine, users,
                                                     version) -> None:
    with Session(agent_engine) as session:
        _set(session, users, version, [("TM-FULL", 1000)])
        session.commit()
        summary = country.totals(country.list_lines(session, version["version_id"]))

    assert summary["target_volume"] == 1000
    assert summary["quantity"] == 2000
    assert summary["value"] == 480_000
    assert summary["missing_conversion_factor"] == []


def test_one_underivable_line_suppresses_the_whole_total(agent_engine, users,
                                                         version) -> None:
    """The rule this module exists to get right.

    Volume totals 1,500 and is real — it is what was typed. Quantity would total
    2,000 from the one derivable line, and presenting that as the country total
    would be wrong by however much TM-NOFACTOR is worth.
    """
    with Session(agent_engine) as session:
        _set(session, users, version,
             [("TM-FULL", 1000), ("TM-NOFACTOR", 500)])
        session.commit()
        summary = country.totals(country.list_lines(session, version["version_id"]))

    assert summary["target_volume"] == 1500
    assert summary["quantity"] is None
    assert summary["value"] is None
    assert summary["derivable_count"] == 1
    assert summary["line_count"] == 2
    assert summary["missing_conversion_factor"] == ["TM-NOFACTOR"]


def test_a_missing_price_alone_also_suppresses_the_value_total(
        agent_engine, users, version) -> None:
    """A quantity total is no more sayable than a value one here.

    TM-NOPRICE derives a quantity but no value, so it is not a complete line —
    and ``totals`` suppresses both rather than publishing one total that is
    complete beside another that is not, which nothing on the screen could
    distinguish.
    """
    with Session(agent_engine) as session:
        _set(session, users, version,
             [("TM-FULL", 1000), ("TM-NOPRICE", 500)])
        session.commit()
        summary = country.totals(country.list_lines(session, version["version_id"]))

    assert summary["quantity"] is None
    assert summary["value"] is None
    assert summary["missing_transfer_price"] == ["TM-NOPRICE"]


def test_an_empty_target_totals_nothing_rather_than_zero(agent_engine,
                                                         version) -> None:
    """No lines is not a country target of zero units worth zero taka."""
    with Session(agent_engine) as session:
        summary = country.totals(country.list_lines(session, version["version_id"]))
    assert summary["line_count"] == 0
    assert summary["quantity"] is None
    assert summary["value"] is None


def test_the_notes_name_the_materials_and_the_fix(agent_engine, users,
                                                  version) -> None:
    with Session(agent_engine) as session:
        _set(session, users, version,
             [("TM-FULL", 1000), ("TM-NOFACTOR", 500)])
        session.commit()
        summary = country.totals(country.list_lines(session, version["version_id"]))

    notes = " ".join(country.notes(summary))
    assert "TM-NOFACTOR" in notes
    assert "Conversion Factor" in notes
    assert "Material Master" in notes
    # And it says why nothing is defaulted, because that is the question a
    # reader asks next.
    assert "1.0" in notes


def test_a_complete_target_needs_no_explanation(agent_engine, users,
                                                version) -> None:
    with Session(agent_engine) as session:
        _set(session, users, version, [("TM-FULL", 1000)])
        session.commit()
        summary = country.totals(country.list_lines(session, version["version_id"]))
    assert country.notes(summary) == []


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def test_setting_a_volume_creates_then_updates(agent_engine, users,
                                               version) -> None:
    with Session(agent_engine) as session:
        first = _set(session, users, version, [("TM-FULL", 1000)])
        assert first == {"created": 1, "updated": 0}
        second = _set(session, users, version, [("TM-FULL", 1500)])
        assert second == {"created": 0, "updated": 1}
        session.commit()
        lines = country.list_lines(session, version["version_id"])
    assert len(lines) == 1
    assert lines[0].target_volume == 1500


def test_resaving_an_unchanged_grid_writes_nothing(agent_engine, users,
                                                   version) -> None:
    """Re-saving is not a revision of every number on the page."""
    with Session(agent_engine) as session:
        _set(session, users, version, [("TM-FULL", 1000)])
        session.commit()
        again = _set(session, users, version, [("TM-FULL", 1000)])
        session.commit()
        audits = session.execute(
            select(TargetAudit).where(
                TargetAudit.action == "COUNTRY_TARGET_EDITED")
        ).scalars().all()

    assert again == {"created": 0, "updated": 0}
    assert len(audits) == 1


def test_zero_is_accepted_as_a_real_target(agent_engine, users,
                                           version) -> None:
    with Session(agent_engine) as session:
        _set(session, users, version, [("TM-FULL", 0)])
        session.commit()
        lines = country.list_lines(session, version["version_id"])
    assert lines[0].target_volume == 0
    assert lines[0].quantity == 0


def test_a_negative_volume_is_refused(agent_engine, users, version) -> None:
    with Session(agent_engine) as session:
        with pytest.raises(country.InvalidVolume) as caught:
            _set(session, users, version, [("TM-FULL", -400)])
    # The negative message, not the unreadable one: the number *was* understood.
    assert "zero or more" in caught.value.user_message


def test_an_unreadable_volume_says_so_rather_than_calling_it_negative(
        agent_engine, users, version) -> None:
    """Two failures, two messages.

    Telling somebody who typed ``12,5OO`` that it "must be zero or more" is true
    of a value nobody managed to read, and sends them to look at the wrong thing.
    """
    with Session(agent_engine) as session:
        with pytest.raises(country.InvalidVolume) as caught:
            _set(session, users, version, [("TM-FULL", "12,5OO")])
    assert "not a number" in caught.value.user_message
    assert "zero or more" not in caught.value.user_message


@pytest.mark.parametrize("raw", ["12,5OO", "abc", "", "  ", None, "1e", "NaN"])
def test_an_unreadable_volume_is_refused_never_coerced(agent_engine, users,
                                                       version, raw) -> None:
    """The no-invented-data invariant, applied to one cell.

    ``12,5OO`` has a letter O in it. ``float`` would raise, ``parseFloat`` in a
    browser would read it as 12, and a lenient reader might call it 0. All three
    are inventions; the only honest answer is a refusal naming the material.
    """
    with Session(agent_engine) as session:
        with pytest.raises(country.InvalidVolume):
            _set(session, users, version, [("TM-FULL", raw)])


def test_a_comma_grouped_number_is_read_not_refused(agent_engine, users,
                                                    version) -> None:
    """``12,500`` is a number a person legitimately types; ``12,5OO`` is not."""
    with Session(agent_engine) as session:
        _set(session, users, version, [("TM-FULL", "12,500")])
        session.commit()
        lines = country.list_lines(session, version["version_id"])
    assert lines[0].target_volume == 12500


def test_an_unknown_material_is_refused_by_name(agent_engine, users,
                                                version) -> None:
    with Session(agent_engine) as session:
        with pytest.raises(country.UnknownMaterial) as caught:
            _set(session, users, version, [("NO-SUCH-MATERIAL", 100)])
    assert "NO-SUCH-MATERIAL" in caught.value.user_message


def test_a_material_of_another_company_is_refused(agent_engine, users,
                                                  version) -> None:
    with Session(agent_engine) as session:
        session.add(DimMaterial(
            material_code="TM-OTHERCO", material_description="Elsewhere",
            material_group_code="MG01", material_group_name="Tea",
            material_brand_code="MB01", material_brand="Example Brand",
            company_code="C002", conversion_factor=1.0, transfer_price=10.0,
        ))
        session.flush()
        with pytest.raises(country.MaterialOutsideCompany):
            _set(session, users, version, [("TM-OTHERCO", 100)])


def test_a_material_naming_no_company_is_refused_and_says_so(
        agent_engine, users, version) -> None:
    """The legacy rows. Refused, because putting one in C001's plan asserts it
    belongs to C001 — which is exactly the guess the nullable column exists to
    avoid."""
    with Session(agent_engine) as session:
        with pytest.raises(country.MaterialOutsideCompany) as caught:
            _set(session, users, version, [("SKU001", 100)])
    assert "names no company" in caught.value.user_message


def test_one_bad_line_writes_none_of_them(agent_engine, users,
                                          version) -> None:
    """All-or-nothing, the shape the ETL gives an import."""
    with Session(agent_engine) as session:
        with pytest.raises(country.UnknownMaterial):
            _set(session, users, version,
                 [("TM-FULL", 1000), ("NO-SUCH-MATERIAL", 500)])
        session.rollback()

    with Session(agent_engine) as session:
        assert session.execute(select(TargetCountryLine)).scalars().all() == []


@pytest.mark.parametrize("status", [TargetStatus.APPROVED, TargetStatus.LOCKED])
def test_a_frozen_version_refuses_a_country_edit(agent_engine, users, version,
                                                 status) -> None:
    with Session(agent_engine) as session:
        row = plan_service.get_version(session, version["version_id"])
        row.status = status
        session.flush()
        with pytest.raises(VersionFrozen):
            _set(session, users, version, [("TM-FULL", 1000)])


def test_each_changed_line_is_audited_with_both_values(agent_engine, users,
                                                       version) -> None:
    with Session(agent_engine) as session:
        _set(session, users, version, [("TM-FULL", 1000)])
        session.commit()
        _set(session, users, version, [("TM-FULL", 1500)])
        session.commit()
        entries = session.execute(
            select(TargetAudit)
            .where(TargetAudit.action == "COUNTRY_TARGET_EDITED")
            .order_by(TargetAudit.audit_id)
        ).scalars().all()

    assert [(e.old_value, e.new_value) for e in entries] == [
        (None, "1000"), ("1000", "1500"),
    ]
    assert entries[0].node_label == "Country · TM-FULL"


# ---------------------------------------------------------------------------
# The material picker
# ---------------------------------------------------------------------------


def test_only_the_plans_own_company_may_be_targeted(agent_engine, users,
                                                    version) -> None:
    with Session(agent_engine) as session:
        session.add(DimMaterial(
            material_code="TM-OTHERCO", material_description="Elsewhere",
            material_group_code="MG01", material_group_name="Tea",
            material_brand_code="MB01", material_brand="Example Brand",
            company_code="C002",
        ))
        session.commit()
        plan = plan_service.get_plan(session, version["plan_id"])
        codes = {
            row["material_code"]
            for row in country.available_materials(session, plan,
                                                   version["version_id"])
        }

    assert codes == {"TM-FULL", "TM-NOPRICE", "TM-NOFACTOR", "TM-NEITHER"}


def test_a_material_already_on_the_target_is_not_offered_again(
        agent_engine, users, version) -> None:
    with Session(agent_engine) as session:
        _set(session, users, version, [("TM-FULL", 1000)])
        session.commit()
        plan = plan_service.get_plan(session, version["plan_id"])
        codes = {
            row["material_code"]
            for row in country.available_materials(session, plan,
                                                   version["version_id"])
        }
    assert "TM-FULL" not in codes


def test_a_retired_material_is_not_offered(agent_engine, users,
                                           version) -> None:
    """Retiring removes a record from selection, not from history."""
    with Session(agent_engine) as session:
        material = session.execute(
            select(DimMaterial).where(DimMaterial.material_code == "TM-FULL")
        ).scalar_one()
        material.is_deleted = True
        session.commit()
        plan = plan_service.get_plan(session, version["plan_id"])
        codes = {
            row["material_code"]
            for row in country.available_materials(session, plan,
                                                   version["version_id"])
        }
    assert "TM-FULL" not in codes


def test_a_line_naming_a_retired_material_still_shows(agent_engine, users,
                                                      version) -> None:
    """It is history now, and a total it is part of must not silently lose it."""
    with Session(agent_engine) as session:
        _set(session, users, version, [("TM-FULL", 1000)])
        material = session.execute(
            select(DimMaterial).where(DimMaterial.material_code == "TM-FULL")
        ).scalar_one()
        material.is_deleted = True
        session.commit()
        lines = country.list_lines(session, version["version_id"])

    assert [line.material_code for line in lines] == ["TM-FULL"]
    assert lines[0].quantity == 2000


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


def test_the_endpoint_returns_lines_totals_and_notes(tm_client: TestClient,
                                                     version) -> None:
    headers = _token(tm_client)
    version_id = version["version_id"]
    written = tm_client.put(
        f"/api/target-management/versions/{version_id}/country-target",
        json={"lines": [{"material_code": "TM-FULL", "target_volume": "1000"},
                        {"material_code": "TM-NOFACTOR", "target_volume": "500"}]},
        headers=headers)
    assert written.status_code == 200, written.text

    body = tm_client.get(
        f"/api/target-management/versions/{version_id}/country-target",
        headers=headers).json()
    assert body["totals"]["target_volume"] == 1500
    assert body["totals"]["quantity"] is None
    assert body["editable"] is True
    assert any("TM-NOFACTOR" in note for note in body["notes"])


def test_the_endpoint_refuses_an_unreadable_volume(tm_client: TestClient,
                                                   version) -> None:
    headers = _token(tm_client)
    response = tm_client.put(
        f"/api/target-management/versions/{version['version_id']}/country-target",
        json={"lines": [{"material_code": "TM-FULL", "target_volume": "12,5OO"}]},
        headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "TARGET_VOLUME_INVALID"


def test_the_endpoint_refuses_a_write_to_a_frozen_version(
        tm_client: TestClient, agent_engine, version) -> None:
    with Session(agent_engine) as session:
        row = plan_service.get_version(session, version["version_id"])
        row.status = TargetStatus.LOCKED
        session.commit()

    headers = _token(tm_client)
    response = tm_client.put(
        f"/api/target-management/versions/{version['version_id']}/country-target",
        json={"lines": [{"material_code": "TM-FULL", "target_volume": "1000"}]},
        headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "TARGET_VERSION_FROZEN"


def test_a_frozen_version_reports_itself_uneditable(tm_client: TestClient,
                                                    agent_engine,
                                                    version) -> None:
    with Session(agent_engine) as session:
        row = plan_service.get_version(session, version["version_id"])
        row.status = TargetStatus.APPROVED
        session.commit()

    headers = _token(tm_client)
    body = tm_client.get(
        f"/api/target-management/versions/{version['version_id']}/country-target",
        headers=headers).json()
    assert body["editable"] is False


def test_available_materials_are_served_from_the_master(tm_client: TestClient,
                                                        version) -> None:
    headers = _token(tm_client)
    body = tm_client.get(
        f"/api/target-management/versions/{version['version_id']}"
        f"/available-materials", headers=headers).json()
    codes = {row["material_code"] for row in body["materials"]}
    assert codes == {"TM-FULL", "TM-NOPRICE", "TM-NOFACTOR", "TM-NEITHER"}
    # And each carries its derivation inputs, so the grid can show n/a for a
    # material the moment it is added rather than after a round trip.
    by_code = {row["material_code"]: row for row in body["materials"]}
    assert by_code["TM-NOFACTOR"]["conversion_factor"] is None
    assert by_code["TM-FULL"]["conversion_factor"] == 0.5
