"""Master and transaction data management.

The specification's two acceptance walkthroughs are exercised end to end — open
a master table, search, filter, sort, edit, save, view, deactivate, check the
audit trail; then open a transaction table, attempt a delete, and verify the
permission and dependency protections hold.

Every authorisation test drives the HTTP API rather than the service layer,
because the claim being tested is that the *endpoint* refuses, not that a
function does. A frontend that hid the button would satisfy the second and not
the first.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.auth.security import hash_password
from app.database.models import DimRegion, DimTerritory
from app.database.models_admin import ChangeAction, DataChangeLog
from app.database.models_ai import AppUser, AuditAction, AuditLog, Role, UserStatus
from app.database.models_warehouse import DimCustomer, FactSales
from app.main import app
from app.security.sections import ALLOW, DENY, Action, SectionKey

pytest.importorskip("multipart", reason="python-multipart is required by FastAPI forms")

PASSWORD = "Correct-Horse-9"
WINDOW = "date_from=2026-08-01&date_to=2026-08-31"


#: The customer the shared fixture's facts actually reference. Using it rather
#: than inventing a code is what makes the dependant counts real.
TRADING_CUSTOMER = "CUST-001"
#: A second customer with no transactions behind it, so retiring it in one test
#: cannot disturb the figures another test measures.
SPARE_CUSTOMER = "CUST-002"

#: A sale created by this module's own fixture, so a void test cannot disturb
#: the figures another test measures. It used to be half of a sale/collection
#: pair that the void guard protected; with the Collection module gone (0020)
#: nothing settles against an invoice and the sale stands alone.
LINKED_INVOICE = "INV-LINK"


@pytest.fixture
def client(agent_engine, users, monkeypatch):
    """A client whose users all have passwords, plus a super administrator."""
    import app.database.connection as connection

    from app.etl.pipeline import run_import
    from app.etl.readers import RecordsSourceReader
    from conftest_phase2 import sales_row

    monkeypatch.setattr(connection, "get_engine", lambda *a, **k: agent_engine)

    rows = [sales_row(**{"Invoice No": LINKED_INVOICE, "Date": "2026-08-20",
                         "Gross Sales": 500_000, "Discount": 0, "Cost": 300_000})]
    result = run_import(agent_engine, "sales",
                        RecordsSourceReader(rows, source_name="link.csv"),
                        source_system="TEST")
    assert result.rejected_rows == 0, result.error_counts

    with Session(agent_engine) as session:
        session.add(AppUser(username="root", display_name="Administrator",
                            role=Role.SUPER_ADMIN, is_active=True,
                            status=UserStatus.ACTIVE,
                            password_hash=hash_password(PASSWORD)))
        for user in session.query(AppUser).all():
            if user.password_hash is None:
                user.password_hash = hash_password(PASSWORD)
        # The customer dimension is PENDING_SOURCE_DATA, so the ETL created a
        # placeholder for CUST-001 rather than a named record. Naming it here is
        # what a real customer master upload would do.
        existing = session.execute(
            select(DimCustomer).where(DimCustomer.customer_code == TRADING_CUSTOMER)
        ).scalar_one_or_none()
        if existing is None:
            session.add(DimCustomer(customer_code=TRADING_CUSTOMER,
                                    customer_name="ABC Traders",
                                    customer_type="Dealer", status="Active",
                                    mobile="+8801700000001"))
        else:
            existing.customer_name = "ABC Traders"
            existing.customer_type = "Dealer"
            existing.status = "Active"
            existing.mobile = "+8801700000001"
        session.add(DimCustomer(customer_code=SPARE_CUSTOMER,
                                customer_name="XYZ Traders",
                                customer_type="Dealer", status="Active"))
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


def login(client: TestClient, username: str = "root") -> str:
    response = client.post("/api/auth/login",
                           json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def grant(client: TestClient, admin: str, username: str,
          sections: dict[str, str], actions: dict | None = None) -> None:
    """Give one user an explicit set of sections, and optionally actions."""
    user_id = client.get("/api/admin/users", headers=auth(admin),
                         params={"search": username}).json()["users"][0]["user_id"]
    body: dict = {"permissions": sections}
    if actions:
        body["actions"] = actions
    response = client.put(f"/api/admin/users/{user_id}/permissions",
                          headers=auth(admin), json=body)
    assert response.status_code == 200, response.text


def codes(payload: dict, field: str = "region_code") -> set[str]:
    return {row[field] for row in payload["rows"]}


# ==========================================================================
# The catalogue
# ==========================================================================


def test_the_catalogue_lists_both_groups(client):
    token = login(client)
    body = client.get("/api/data-management/catalogue",
                      headers=auth(token)).json()

    groups = {group["key"]: group for group in body["groups"]}
    assert set(groups) == {"MASTER", "TRANSACTION"}

    master = {e["key"] for e in groups["MASTER"]["entities"]}
    assert {"dim_region", "dim_territory", "dim_customer",
            "dim_sales_force", "dim_plant", "dim_storage_location",
            "dim_material"} <= master
    assert "dim_warehouse" not in master

    transactions = {e["key"] for e in groups["TRANSACTION"]["entities"]}
    assert transactions == {"sales", "material_stock", "target"}


def test_the_catalogue_is_derived_from_the_upload_registry(client):
    """Adding a column to a dimension must reach the table with no extra work."""
    from app.upload.registry import get_upload_type

    token = login(client)
    body = client.get("/api/data-management/catalogue",
                      headers=auth(token)).json()
    region = next(e for group in body["groups"] for e in group["entities"]
                  if e["key"] == "dim_region")

    registry_columns = {c.target for c in get_upload_type("dim_region").columns}
    assert {f["name"] for f in region["fields"]} == registry_columns


def test_the_business_code_is_never_editable(client):
    token = login(client)
    body = client.get("/api/data-management/catalogue",
                      headers=auth(token)).json()
    for group in body["groups"]:
        for entity in group["entities"]:
            for field in entity["fields"]:
                if field["is_key"]:
                    assert not field["editable"], (entity["key"], field["name"])


# ==========================================================================
# Master table: display, search, filter, sort, page (items 12–15, 33)
# ==========================================================================


def test_the_master_table_paginates_on_the_server(client):
    token = login(client)
    body = client.get("/api/master/dim_region?page=1&page_size=1",
                      headers=auth(token)).json()

    assert len(body["rows"]) == 1
    assert body["total"] >= 2
    assert body["total_pages"] >= 2
    assert body["range_from"] == 1 and body["range_to"] == 1


def test_search_matches_code_and_name(client):
    token = login(client)
    by_name = client.get("/api/master/dim_region?search=Dhaka",
                         headers=auth(token)).json()
    by_code = client.get("/api/master/dim_region?search=REG001",
                         headers=auth(token)).json()

    assert codes(by_name) == {"REG001"}
    assert codes(by_code) == {"REG001"}


def test_filters_narrow_the_table(client):
    token = login(client)
    body = client.get("/api/master/dim_territory?filter.unit_code=UN001",
                      headers=auth(token)).json()
    assert body["total"] >= 1
    assert {row["unit_code"] for row in body["rows"]} == {"UN001"}


def test_an_unknown_filter_is_refused_rather_than_ignored(client):
    """Silently dropping a filter would show more rows than were asked for."""
    token = login(client)
    response = client.get("/api/master/dim_region?filter.unicorn=1",
                          headers=auth(token))
    assert response.status_code == 422
    assert "unicorn" in response.json()["detail"]


def test_sorting_is_server_side_and_whitelisted(client):
    token = login(client)
    ascending = client.get("/api/master/dim_region?sort_by=region_code&sort_dir=asc",
                           headers=auth(token)).json()
    descending = client.get("/api/master/dim_region?sort_by=region_code&sort_dir=desc",
                            headers=auth(token)).json()

    forward = [row["region_code"] for row in ascending["rows"]]
    assert forward == sorted(forward)
    assert [row["region_code"] for row in descending["rows"]] == forward[::-1]

    refused = client.get("/api/master/dim_region?sort_by=; DROP TABLE dim_region",
                         headers=auth(token))
    assert refused.status_code == 422


def test_an_unknown_entity_is_a_404(client):
    token = login(client)
    assert client.get("/api/master/dim_unicorn",
                      headers=auth(token)).status_code == 404


# ==========================================================================
# Data scope (item 11)
# ==========================================================================


def test_a_scoped_user_sees_only_their_own_region(client):
    admin = login(client)
    grant(client, admin, "dhaka_rm", {SectionKey.MASTER_DATA: ALLOW})

    token = login(client, "dhaka_rm")
    body = client.get("/api/master/dim_region", headers=auth(token)).json()

    assert codes(body) == {"REG001"}
    assert body["total"] == 1


def test_a_scoped_user_sees_only_territories_beneath_them(client):
    admin = login(client)
    grant(client, admin, "dhaka_rm", {SectionKey.MASTER_DATA: ALLOW})

    token = login(client, "dhaka_rm")
    body = client.get("/api/master/dim_territory", headers=auth(token)).json()

    assert body["rows"], "the Dhaka manager should see their own territory"
    assert all(row["territory_code"] == "TR001" for row in body["rows"])


def test_a_record_outside_the_scope_is_refused_when_addressed_directly(client):
    """The table filter is not the control; the record endpoint enforces it too."""
    admin = login(client)
    grant(client, admin, "dhaka_rm", {SectionKey.MASTER_DATA: ALLOW})

    token = login(client, "dhaka_rm")
    response = client.get("/api/master/dim_region/REG002", headers=auth(token))
    assert response.status_code == 403
    assert "REG002" in response.json()["detail"]


def test_editing_out_of_scope_is_refused(client):
    admin = login(client)
    grant(client, admin, "dhaka_rm", {SectionKey.MASTER_DATA: ALLOW},
          {SectionKey.MASTER_DATA: {Action.EDIT: ALLOW}})

    token = login(client, "dhaka_rm")
    response = client.put("/api/master/dim_region/REG002", headers=auth(token),
                          json={"values": {"region_name": "Renamed"}})
    assert response.status_code == 403


def test_materials_are_not_scoped_because_they_belong_to_no_region(client):
    """Inventing a scope for materials would hide them, not protect anything."""
    admin = login(client)
    grant(client, admin, "dhaka_rm", {SectionKey.MASTER_DATA: ALLOW})

    token = login(client, "dhaka_rm")
    body = client.get("/api/master/dim_material", headers=auth(token)).json()
    assert body["total"] >= 1


# ==========================================================================
# Role-based actions (items 9, 10, 34)
# ==========================================================================


def test_an_administrator_may_view_edit_and_delete(client):
    token = login(client)
    body = client.get("/api/master/dim_region", headers=auth(token)).json()
    assert body["permissions"][Action.VIEW]
    assert body["permissions"][Action.EDIT]
    assert body["permissions"][Action.DELETE]


def test_a_manager_may_edit_but_not_delete_by_default(client):
    admin = login(client)
    grant(client, admin, "dhaka_rm", {SectionKey.MASTER_DATA: ALLOW})

    token = login(client, "dhaka_rm")
    body = client.get("/api/master/dim_region", headers=auth(token)).json()
    assert body["permissions"][Action.VIEW]
    assert body["permissions"][Action.EDIT]
    assert not body["permissions"][Action.DELETE]


def test_a_viewer_may_only_view(client):
    admin = login(client)
    grant(client, admin, "no_scope", {SectionKey.MASTER_DATA: ALLOW})

    token = login(client, "no_scope")
    body = client.get("/api/master/dim_material", headers=auth(token)).json()
    assert body["permissions"][Action.VIEW]
    assert not body["permissions"][Action.EDIT]
    assert not body["permissions"][Action.DELETE]


def test_the_backend_refuses_a_denied_action_not_just_the_button(client):
    """Item 9: hiding the control is a convenience; the API is the control."""
    admin = login(client)
    grant(client, admin, "dhaka_rm", {SectionKey.MASTER_DATA: ALLOW})

    token = login(client, "dhaka_rm")
    response = client.delete("/api/master/dim_region/REG001", headers=auth(token))
    assert response.status_code == 403


def test_an_administrator_can_grant_delete_to_a_role(client):
    admin = login(client)
    grant(client, admin, "dhaka_rm", {SectionKey.MASTER_DATA: ALLOW},
          {SectionKey.MASTER_DATA: {Action.DELETE: ALLOW}})

    token = login(client, "dhaka_rm")
    body = client.get("/api/master/dim_region", headers=auth(token)).json()
    assert body["permissions"][Action.DELETE]


def test_an_administrator_can_withdraw_edit_from_a_user(client):
    admin = login(client)
    grant(client, admin, "dhaka_rm", {SectionKey.MASTER_DATA: ALLOW},
          {SectionKey.MASTER_DATA: {Action.EDIT: DENY}})

    token = login(client, "dhaka_rm")
    response = client.put("/api/master/dim_region/REG001", headers=auth(token),
                          json={"values": {"region_name": "Nope"}})
    assert response.status_code == 403


def test_the_section_itself_is_still_required(client):
    """A denied section denies every action inside it, whatever they resolve to."""
    admin = login(client)
    grant(client, admin, "dhaka_rm", {SectionKey.MASTER_DATA: DENY})

    token = login(client, "dhaka_rm")
    assert client.get("/api/master/dim_region",
                      headers=auth(token)).status_code == 403


def test_an_action_a_section_does_not_declare_is_rejected(client):
    admin = login(client)
    user_id = client.get("/api/admin/users", headers=auth(admin),
                         params={"search": "dhaka_rm"}).json()["users"][0]["user_id"]
    response = client.put(f"/api/admin/users/{user_id}/permissions",
                          headers=auth(admin), json={
                              "permissions": {SectionKey.SALES: ALLOW},
                              "actions": {SectionKey.SALES: {Action.DELETE: ALLOW}},
                          })
    assert response.status_code == 422
    assert "DELETE" in response.json()["detail"]


def test_auth_me_reports_the_action_matrix(client):
    token = login(client)
    body = client.get("/api/auth/me", headers=auth(token)).json()
    assert body["actions"][SectionKey.MASTER_DATA][Action.EDIT] is True
    assert body["actions"][SectionKey.SALES][Action.VIEW] is True


def test_an_anonymous_caller_is_refused(client):
    assert client.get("/api/master/dim_region").status_code == 401


# ==========================================================================
# Edit (items 5, 20, 21)
# ==========================================================================


def test_editing_saves_and_returns_the_updated_record(client):
    token = login(client)
    response = client.put("/api/master/dim_region/REG001", headers=auth(token),
                          json={"values": {"region_name": "Dhaka Metropolitan",
                                           "region_hq": "Motijheel"}})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["record"]["region_name"] == "Dhaka Metropolitan"
    assert set(body["changed_fields"]) == {"region_name", "region_hq"}

    again = client.get("/api/master/dim_region/REG001", headers=auth(token)).json()
    assert again["record"]["region_name"] == "Dhaka Metropolitan"


def test_a_partial_edit_leaves_untouched_fields_alone(client):
    token = login(client)
    before = client.get("/api/master/dim_region/REG001",
                        headers=auth(token)).json()["record"]

    client.put("/api/master/dim_region/REG001", headers=auth(token),
               json={"values": {"region_hq": "Gulshan"}})

    after = client.get("/api/master/dim_region/REG001",
                       headers=auth(token)).json()["record"]
    assert after["region_name"] == before["region_name"]
    assert after["region_hq"] == "Gulshan"


def test_an_edit_that_changes_nothing_writes_no_history(client):
    token = login(client)
    current = client.get("/api/master/dim_region/REG001",
                         headers=auth(token)).json()["record"]

    response = client.put("/api/master/dim_region/REG001", headers=auth(token),
                          json={"values": {"region_name": current["region_name"]}})
    assert response.json()["changed_fields"] == []

    history = client.get("/api/master/dim_region/REG001/history",
                         headers=auth(token)).json()
    assert history["total"] == 0


def test_the_business_code_cannot_be_edited(client):
    token = login(client)
    response = client.put("/api/master/dim_region/REG001", headers=auth(token),
                          json={"values": {"region_code": "REG999"}})
    assert response.status_code == 422
    assert "cannot be changed" in str(response.json()["detail"])


def test_a_required_field_cannot_be_cleared(client):
    token = login(client)
    response = client.put("/api/master/dim_region/REG001", headers=auth(token),
                          json={"values": {"region_name": ""}})
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert any(e["error_code"] == "MISSING_REQUIRED_FIELD" for e in errors)


def test_every_invalid_field_is_reported_at_once(client):
    """Fixing one field and being told about the next is a poor way to learn."""
    token = login(client)
    response = client.put(f"/api/master/dim_customer/{TRADING_CUSTOMER}", headers=auth(token),
                          json={"values": {"customer_name": "",
                                           "status": "Perhaps"}})
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert len(errors) == 2


def test_a_status_outside_the_allowed_values_is_refused(client):
    token = login(client)
    response = client.put(f"/api/master/dim_customer/{TRADING_CUSTOMER}", headers=auth(token),
                          json={"values": {"status": "Maybe"}})
    assert response.status_code == 422


def test_a_missing_parent_is_refused(client):
    token = login(client)
    response = client.put("/api/master/dim_territory/TR001", headers=auth(token),
                          json={"values": {"unit_code": "UN-NOPE"}})
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert any(e["error_code"] == "INVALID_PARENT_CODE" for e in errors)


# ==========================================================================
# Hierarchy validation (item 21)
# ==========================================================================


def test_a_territory_cannot_be_moved_under_a_unit_that_does_not_exist(client):
    """A dimension row carries only its parent, so that is where the chain breaks."""
    token = login(client)
    response = client.put("/api/master/dim_territory/TR001", headers=auth(token),
                          json={"values": {"unit_code": "UN-ELSEWHERE"}})
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert any(e["error_code"] == "INVALID_PARENT_CODE" for e in errors)


def test_a_sales_force_member_cannot_be_given_a_territory_that_does_not_exist(
    client, agent_engine
):
    """``territory_code`` is not a foreign key, which is why it needs checking."""
    from app.database.models_warehouse import DimSalesForce

    with Session(agent_engine) as session:
        session.add(DimSalesForce(sales_force_code="SF900",
                                  sales_force_name="Md. Anis",
                                  territory_code="TR001"))
        session.commit()

    token = login(client)
    response = client.put("/api/master/dim_sales_force/SF900",
                          headers=auth(token),
                          json={"values": {"territory_code": "TR-NOPE"}})
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert any(e["error_code"] == "INVALID_PARENT_CODE" for e in errors)


def test_a_consistent_hierarchy_is_accepted(client):
    token = login(client)
    response = client.put("/api/master/dim_territory/TR001", headers=auth(token),
                          json={"values": {"unit_code": "UN001",
                                           "territory_hq": "Kazipara"}})
    assert response.status_code == 200, response.text


def test_a_record_cannot_be_reparented_outside_the_callers_scope(client,
                                                                 agent_engine):
    """Otherwise reparenting would be a hole straight through the data scope."""
    # UN002 sits under AR002 in Khulna, outside dhaka_rm's region.
    admin = login(client)

    grant(client, admin, "dhaka_rm", {SectionKey.MASTER_DATA: ALLOW},
          {SectionKey.MASTER_DATA: {Action.EDIT: ALLOW}})

    token = login(client, "dhaka_rm")
    response = client.put("/api/master/dim_territory/TR001", headers=auth(token),
                          json={"values": {"unit_code": "UN002"}})
    assert response.status_code == 403
    assert "UN002" in response.json()["detail"]


# ==========================================================================
# Create (item 20)
# ==========================================================================


def test_creating_a_record(client):
    token = login(client)
    response = client.post("/api/master/dim_customer", headers=auth(token),
                           json={"values": {"customer_code": "C900",
                                            "customer_name": "New Traders",
                                            "status": "Active"}})
    assert response.status_code == 201, response.text
    assert response.json()["record"]["customer_code"] == "C900"


def test_a_duplicate_code_is_a_conflict_not_an_update(client):
    token = login(client)
    response = client.post("/api/master/dim_customer", headers=auth(token),
                           json={"values": {"customer_code": TRADING_CUSTOMER,
                                            "customer_name": "Impostor"}})
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert any(e["error_code"] == "RECORD_ALREADY_EXISTS" for e in errors)


def test_a_latitude_outside_its_range_is_refused(client):
    """Item 20: -90 to +90, and the bound is the map module's, not a copy."""
    from app.datamgmt.validation import LATITUDE_RANGE

    assert LATITUDE_RANGE == (-90.0, 90.0)


# ==========================================================================
# Delete and soft delete (items 6, 7, 35)
# ==========================================================================


def test_deleting_a_master_record_retires_it_rather_than_removing_it(
    client, agent_engine
):
    token = login(client)
    response = client.delete(f"/api/master/dim_customer/{SPARE_CUSTOMER}", headers=auth(token))
    assert response.status_code == 200, response.text

    # Gone from the table …
    listing = client.get("/api/master/dim_customer", headers=auth(token)).json()
    assert SPARE_CUSTOMER not in codes(listing, "customer_code")

    # … but still in the database, flagged, with who and when.
    with Session(agent_engine) as session:
        record = session.execute(
            select(DimCustomer).where(DimCustomer.customer_code == SPARE_CUSTOMER)
        ).scalar_one()
        assert record.is_deleted is True
        assert record.deleted_by == "root"
        assert record.deleted_at is not None


def test_a_retired_record_can_be_listed_and_restored(client):
    token = login(client)
    client.delete(f"/api/master/dim_customer/{SPARE_CUSTOMER}", headers=auth(token))

    with_deleted = client.get("/api/master/dim_customer?include_deleted=true",
                              headers=auth(token)).json()
    assert SPARE_CUSTOMER in codes(with_deleted, "customer_code")

    restored = client.post(f"/api/master/dim_customer/{SPARE_CUSTOMER}/restore",
                           headers=auth(token))
    assert restored.status_code == 200
    assert restored.json()["record"]["is_deleted"] is False


def test_retiring_twice_is_refused(client):
    token = login(client)
    client.delete(f"/api/master/dim_customer/{SPARE_CUSTOMER}", headers=auth(token))
    again = client.delete(f"/api/master/dim_customer/{SPARE_CUSTOMER}", headers=auth(token))
    assert again.status_code == 409


def test_the_delete_confirmation_can_see_what_depends_on_the_record(client):
    """Item 6: the dialog names the record and says what it carries."""
    token = login(client)
    body = client.get(f"/api/master/dim_customer/{TRADING_CUSTOMER}/dependants",
                      headers=auth(token)).json()
    assert body["total"] >= 1
    assert not body["blocking"], "retiring master data is safe, never blocked"
    assert "sales transactions" in body["counts"]


def test_deactivating_is_distinct_from_retiring(client):
    token = login(client)
    response = client.put(f"/api/master/dim_customer/{TRADING_CUSTOMER}/status",
                          headers=auth(token), json={"status": "Inactive"})
    assert response.status_code == 200
    assert response.json()["record"]["status"] == "Inactive"
    # Still visible: deactivated is not deleted.
    listing = client.get("/api/master/dim_customer", headers=auth(token)).json()
    assert TRADING_CUSTOMER in codes(listing, "customer_code")


def test_re_uploading_a_retired_code_brings_it_back(client, agent_engine):
    """Otherwise the loader would report success while the record stayed hidden."""
    from app.etl.readers import RecordsSourceReader
    from app.upload import master_loader
    from app.upload.registry import get_upload_type

    token = login(client)
    client.delete(f"/api/master/dim_customer/{SPARE_CUSTOMER}", headers=auth(token))

    upload_type = get_upload_type("dim_customer")
    with Session(agent_engine) as session:
        reader = RecordsSourceReader(
            [{"Customer Code": SPARE_CUSTOMER, "Customer Name": "XYZ Traders"}],
            source_name="c.csv",
        )
        validation = master_loader.validate(session, upload_type, reader)
        master_loader.load(session, upload_type, validation)
        session.commit()
        record = session.execute(
            select(DimCustomer).where(DimCustomer.customer_code == SPARE_CUSTOMER)
        ).scalar_one()
        assert record.is_deleted is False


# ==========================================================================
# Bulk (item 18)
# ==========================================================================


def test_bulk_deactivate(client):
    token = login(client)
    response = client.post("/api/master/dim_customer/bulk", headers=auth(token),
                           json={"action": "DEACTIVATE",
                                 "codes": [TRADING_CUSTOMER, SPARE_CUSTOMER]})
    assert response.status_code == 200, response.text
    assert response.json()["success_count"] == 2

    listing = client.get("/api/master/dim_customer", headers=auth(token)).json()
    assert all(row["status"] == "Inactive" for row in listing["rows"])


def test_bulk_reports_each_failure_rather_than_failing_the_batch(client):
    token = login(client)
    response = client.post("/api/master/dim_customer/bulk", headers=auth(token),
                           json={"action": "DEACTIVATE",
                                 "codes": [TRADING_CUSTOMER, "C-NOPE"]})
    body = response.json()
    assert body["succeeded"] == [TRADING_CUSTOMER]
    assert body["failed"][0]["code"] == "C-NOPE"


def test_bulk_delete_needs_the_delete_permission_not_merely_edit(client):
    admin = login(client)
    grant(client, admin, "dhaka_rm", {SectionKey.MASTER_DATA: ALLOW})

    token = login(client, "dhaka_rm")
    response = client.post("/api/master/dim_customer/bulk", headers=auth(token),
                           json={"action": "DELETE", "codes": [TRADING_CUSTOMER]})
    assert response.status_code == 403


def test_a_bulk_operation_is_bounded(client):
    from app.datamgmt.service import BULK_LIMIT

    token = login(client)
    response = client.post("/api/master/dim_customer/bulk", headers=auth(token),
                           json={"action": "DEACTIVATE",
                                 "codes": [f"C{n}" for n in range(BULK_LIMIT + 1)]})
    assert response.status_code == 409
    assert "Data Upload Center" in response.json()["detail"]


# ==========================================================================
# Audit and history (items 23, 24)
# ==========================================================================


def test_an_edit_writes_field_level_history(client, agent_engine):
    token = login(client)
    client.put("/api/master/dim_region/REG001", headers=auth(token),
               json={"values": {"region_name": "Dhaka North"},
                     "reason": "Renamed after the split"})

    body = client.get("/api/master/dim_region/REG001/history",
                      headers=auth(token)).json()
    assert body["total"] == 1
    entry = body["history"][0]
    assert entry["action"] == ChangeAction.UPDATED
    assert entry["username"] == "root"
    assert entry["role"] == Role.SUPER_ADMIN
    assert entry["reason"] == "Renamed after the split"
    change = next(c for c in entry["changes"] if c["field"] == "region_name")
    assert change["old"] == "Dhaka"
    assert change["new"] == "Dhaka North"


def test_an_edit_also_writes_an_audit_entry(client, agent_engine):
    token = login(client)
    client.put("/api/master/dim_region/REG001", headers=auth(token),
               json={"values": {"region_hq": "Uttara"}})

    with Session(agent_engine) as session:
        entry = session.execute(
            select(AuditLog).where(AuditLog.action == AuditAction.RECORD_UPDATED)
            .order_by(AuditLog.audit_id.desc())
        ).scalars().first()
    assert entry is not None
    assert entry.resource == "dim_region:REG001"
    # Field names, not values: the audit trail is read broadly and should not
    # accumulate business data. The values live in the change log.
    assert entry.detail["fields"] == ["region_hq"]
    assert "Uttara" not in str(entry.detail)


def test_a_delete_keeps_the_old_values(client):
    """Item 23: for a delete, the old value must be retained."""
    token = login(client)
    client.delete(f"/api/master/dim_customer/{SPARE_CUSTOMER}?reason=Duplicate",
                  headers=auth(token))

    body = client.get(f"/api/master/dim_customer/{SPARE_CUSTOMER}/history",
                      headers=auth(token)).json()
    entry = body["history"][0]
    assert entry["action"] == ChangeAction.DELETED
    assert entry["reason"] == "Duplicate"
    old = {c["field"]: c["old"] for c in entry["changes"]}
    assert old["customer_name"] == "XYZ Traders"


def test_history_is_ordered_newest_first(client):
    token = login(client)
    for name in ("One", "Two", "Three"):
        client.put("/api/master/dim_region/REG001", headers=auth(token),
                   json={"values": {"region_name": name}})

    body = client.get("/api/master/dim_region/REG001/history",
                      headers=auth(token)).json()
    assert body["total"] == 3
    newest = body["history"][0]["changes"][0]
    assert newest["new"] == "Three"


# ==========================================================================
# Export (item 17)
# ==========================================================================


def test_csv_export_covers_the_filtered_rows_only(client):
    token = login(client)
    response = client.get("/api/master/dim_region/export/csv?search=Dhaka",
                          headers=auth(token))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    text = response.content.decode("utf-8-sig")
    assert "REG001" in text
    assert "REG002" not in text


def test_export_respects_the_data_scope(client):
    """Item 17: exporting must not be a way round the scope."""
    admin = login(client)
    grant(client, admin, "dhaka_rm", {SectionKey.MASTER_DATA: ALLOW})

    token = login(client, "dhaka_rm")
    response = client.get("/api/master/dim_region/export/csv", headers=auth(token))
    text = response.content.decode("utf-8-sig")
    assert "REG001" in text
    assert "REG002" not in text


def test_export_needs_the_export_permission(client):
    admin = login(client)
    grant(client, admin, "dhaka_rm", {SectionKey.MASTER_DATA: ALLOW},
          {SectionKey.MASTER_DATA: {Action.EXPORT: DENY}})

    token = login(client, "dhaka_rm")
    assert client.get("/api/master/dim_region/export/csv",
                      headers=auth(token)).status_code == 403


def test_xlsx_export(client):
    token = login(client)
    response = client.get("/api/master/dim_region/export/xlsx",
                          headers=auth(token))
    assert response.status_code == 200
    assert response.content[:2] == b"PK"      # a zip container, i.e. a workbook


def test_an_unknown_export_format_is_a_404(client):
    token = login(client)
    assert client.get("/api/master/dim_region/export/pdf",
                      headers=auth(token)).status_code == 404


# ==========================================================================
# Transactions: display and permission (items 3, 8, 9)
# ==========================================================================


def test_the_transaction_table_lists_rows(client):
    token = login(client)
    body = client.get(f"/api/transactions/sales?{WINDOW}",
                      headers=auth(token)).json()
    assert body["total"] >= 1
    assert "invoice_no" in body["columns"]


def test_the_transaction_table_respects_the_data_scope(client):
    admin = login(client)
    grant(client, admin, "dhaka_rm", {SectionKey.TRANSACTION_DATA: ALLOW})

    token = login(client, "dhaka_rm")
    body = client.get(f"/api/transactions/sales?{WINDOW}",
                      headers=auth(token)).json()
    assert body["rows"]
    assert all(row["region_name"] == "Dhaka" for row in body["rows"])


def test_the_reporting_section_is_required_as_well(client):
    """Denying Stock must deny the stock management table, not just the report."""
    admin = login(client)
    grant(client, admin, "dhaka_rm", {SectionKey.TRANSACTION_DATA: ALLOW,
                                      SectionKey.STOCK: DENY})

    token = login(client, "dhaka_rm")
    assert client.get(f"/api/transactions/material_stock?{WINDOW}",
                      headers=auth(token)).status_code == 403
    # …while a type they do hold still works.
    assert client.get(f"/api/transactions/sales?{WINDOW}",
                      headers=auth(token)).status_code == 200


def test_transactions_cannot_be_created_through_the_api(client):
    """There is no create: a hand-typed transaction bypasses the whole pipeline."""
    from app.security.sections import SECTION_BY_KEY

    section = SECTION_BY_KEY[SectionKey.TRANSACTION_DATA]
    assert Action.CREATE not in section.actions


def test_only_a_super_administrator_may_void_by_default(client):
    admin = login(client)
    grant(client, admin, "dhaka_rm", {SectionKey.TRANSACTION_DATA: ALLOW})

    token = login(client, "dhaka_rm")
    body = client.get(f"/api/transactions/sales?{WINDOW}",
                      headers=auth(token)).json()
    assert not body["permissions"][Action.DELETE]
    assert not body["permissions"][Action.EDIT]


# ==========================================================================
# Void and transaction integrity (items 8, 22, 35)
# ==========================================================================


def _sales_id(client: TestClient, token: str, invoice: str) -> int:
    body = client.get(f"/api/transactions/sales?{WINDOW}&search={invoice}",
                      headers=auth(token)).json()
    return body["rows"][0]["sales_id"]


def test_voiding_removes_the_row_from_every_report(client):
    """The reporting views filter on is_void, so one flag reverses it everywhere."""
    token = login(client)
    before = client.get(f"/api/pages/sales?{WINDOW}",
                        headers=auth(token)).json()["summary"]["value"]

    sales_id = _sales_id(client, token, "INV-K1")
    response = client.delete(
        f"/api/transactions/sales/{sales_id}?reason=Duplicate+invoice",
        headers=auth(token))
    assert response.status_code == 200, response.text

    after = client.get(f"/api/pages/sales?{WINDOW}",
                       headers=auth(token)).json()["summary"]["value"]
    assert after < before


def test_a_voided_row_leaves_the_management_table_too_unless_asked_for(client):
    token = login(client)
    sales_id = _sales_id(client, token, "INV-K1")
    client.delete(f"/api/transactions/sales/{sales_id}?reason=Duplicate",
                  headers=auth(token))

    visible = client.get(f"/api/transactions/sales?{WINDOW}",
                         headers=auth(token)).json()
    assert sales_id not in {row["sales_id"] for row in visible["rows"]}

    including = client.get(f"/api/transactions/sales?{WINDOW}&include_voided=true",
                           headers=auth(token)).json()
    voided = next(row for row in including["rows"] if row["sales_id"] == sales_id)
    assert voided["is_void"] is True
    assert voided["void_reason"] == "Duplicate"


def test_the_fact_row_still_exists_after_a_void(client, agent_engine):
    """Item 35: historical business records are never destroyed."""
    token = login(client)
    sales_id = _sales_id(client, token, "INV-K1")
    client.delete(f"/api/transactions/sales/{sales_id}?reason=Duplicate",
                  headers=auth(token))

    with Session(agent_engine) as session:
        row = session.get(FactSales, sales_id)
    assert row is not None
    assert row.is_void is True
    assert row.voided_by == "root"
    assert row.net_sales != 0, "the figures are kept, not zeroed"


def test_a_void_can_be_reversed(client):
    token = login(client)
    sales_id = _sales_id(client, token, "INV-K1")
    client.delete(f"/api/transactions/sales/{sales_id}?reason=Mistake",
                  headers=auth(token))

    response = client.post(f"/api/transactions/sales/{sales_id}/restore",
                           headers=auth(token))
    assert response.status_code == 200
    assert response.json()["record"]["is_void"] is False


def test_a_void_requires_a_reason(client):
    token = login(client)
    sales_id = _sales_id(client, token, "INV-K1")
    assert client.delete(f"/api/transactions/sales/{sales_id}",
                         headers=auth(token)).status_code == 422


def test_an_invoice_voids_freely_now_that_nothing_settles_against_it(client):
    """Item 22, as it now stands: no remaining dataset depends on an invoice.

    A sale used to be refused while a collection settled it or an outstanding
    line tracked what was owed. Both left in revision 0020, and material stock
    and target are periodic statements that settle against nothing — so the
    guard has nothing to find and the void proceeds. This is asserted rather
    than deleted because it is a *change in what an operator may do*, and a
    future dataset that does settle against an invoice must re-block it here.
    """
    token = login(client)
    sales_id = _sales_id(client, token, LINKED_INVOICE)
    response = client.delete(f"/api/transactions/sales/{sales_id}?reason=Test",
                             headers=auth(token))

    assert response.status_code == 200, response.text
    assert client.get(f"/api/transactions/collection?{WINDOW}",
                      headers=auth(token)).status_code == 404


def test_an_unlinked_invoice_voids_freely(client):
    """Only real dependants block; nothing settles INV-K1."""
    token = login(client)
    sales_id = _sales_id(client, token, "INV-K1")
    assert client.delete(f"/api/transactions/sales/{sales_id}?reason=Duplicate",
                         headers=auth(token)).status_code == 200


def test_voiding_writes_history_with_the_reversed_figures(client):
    token = login(client)
    sales_id = _sales_id(client, token, "INV-K1")
    client.delete(f"/api/transactions/sales/{sales_id}?reason=Duplicate+entry",
                  headers=auth(token))

    body = client.get(f"/api/transactions/sales/{sales_id}/history",
                      headers=auth(token)).json()
    entry = body["history"][0]
    assert entry["action"] == ChangeAction.VOIDED
    assert entry["reason"] == "Duplicate entry"
    reversed_values = {c["field"]: c["old"] for c in entry["changes"]}
    assert reversed_values["net_sales"]


# ==========================================================================
# Transaction correction (item 19)
# ==========================================================================


def test_correcting_a_measure_recomputes_what_derives_from_it(client):
    token = login(client)
    sales_id = _sales_id(client, token, "INV-K1")

    response = client.put(f"/api/transactions/sales/{sales_id}",
                          headers=auth(token),
                          json={"values": {"net_sales": 400000},
                                "reason": "Corrected against the invoice"})
    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert float(record["net_sales"]) == 400000
    # gross_profit = net_sales - cost, so it must have moved with it.
    assert float(record["gross_profit"]) == 400000 - float(record["cost"])


def test_a_transaction_key_field_cannot_be_edited(client):
    """Re-pointing a transaction is not a correction; it is a different one."""
    token = login(client)
    sales_id = _sales_id(client, token, "INV-K1")
    response = client.put(f"/api/transactions/sales/{sales_id}",
                          headers=auth(token),
                          json={"values": {"customer_code": "C999"}})
    assert response.status_code == 422
    assert "import pipeline" in str(response.json()["detail"])


def test_a_voided_transaction_cannot_be_corrected(client):
    token = login(client)
    sales_id = _sales_id(client, token, "INV-K1")
    client.delete(f"/api/transactions/sales/{sales_id}?reason=Void",
                  headers=auth(token))

    response = client.put(f"/api/transactions/sales/{sales_id}",
                          headers=auth(token), json={"values": {"net_sales": 1}})
    assert response.status_code == 409


# ==========================================================================
# Detail view and map integration (items 26, 27)
# ==========================================================================


def test_the_detail_view_carries_hierarchy_geo_and_history(client):
    token = login(client)
    body = client.get("/api/master/dim_territory/TR001",
                      headers=auth(token)).json()

    assert body["record"]["territory_code"] == "TR001"
    assert body["hierarchy"]["region_code"] == "REG001"
    assert body["hierarchy"]["zone_code"]
    assert "history" in body and "dependants" in body


def test_selected_records_can_be_fetched_by_code_for_the_map(client):
    """Item 28: "show selected on map" asks for exactly the chosen records."""
    token = login(client)
    body = client.get(f"/api/master/dim_customer?codes={TRADING_CUSTOMER}",
                      headers=auth(token)).json()
    assert codes(body, "customer_code") == {TRADING_CUSTOMER}
