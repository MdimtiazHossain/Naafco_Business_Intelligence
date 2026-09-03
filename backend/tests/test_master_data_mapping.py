"""Customer sub-territory and product company code.

The specification's two acceptance walkthroughs, plus the part that carries the
most risk: the derivation of the new links from existing data. What is tested
hardest there is what the mapper *refuses* to do — a wrong sub-territory on a
customer is worse than a blank one, because a blank is visibly missing and a
wrong one is invisibly wrong.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.auth.security import hash_password
from app.database.models import DimCompany, DimSubTerritory
from app.database.models_ai import AppUser, Role, UserStatus
from app.database.models_warehouse import DimCustomer
from app.datamgmt import mapping
from app.main import app
from app.security.sections import ALLOW, SectionKey

pytest.importorskip("multipart", reason="python-multipart is required by FastAPI forms")

PASSWORD = "Correct-Horse-9"
WINDOW = "date_from=2026-08-01&date_to=2026-08-31"


@pytest.fixture
def client(agent_engine, users, monkeypatch):
    import app.database.connection as connection

    monkeypatch.setattr(connection, "get_engine", lambda *a, **k: agent_engine)

    with Session(agent_engine) as session:
        session.add(AppUser(username="root", display_name="Administrator",
                            role=Role.SUPER_ADMIN, is_active=True,
                            status=UserStatus.ACTIVE,
                            password_hash=hash_password(PASSWORD)))
        for user in session.query(AppUser).all():
            if user.password_hash is None:
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


def login(client: TestClient, username: str = "root") -> str:
    response = client.post("/api/auth/login",
                           json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ==========================================================================
# Structure and column order (items 1, 5, 9, 13, 30)
# ==========================================================================



def business_entities(engine, **filters) -> list[dict]:
    """Customers and sales force inside a scope, as flat dicts.

    These assertions used to be made through the business map's entity
    endpoint, because the map was the only screen that drew a customer under
    the sub-territory the master assigned it. The map is gone; the resolution
    it displayed is not — Data Management scopes on exactly this function — so
    the tests now call it where it lives rather than through a deleted route.
    The shape mirrors what the endpoint returned, so the assertions below are
    unchanged in meaning.
    """
    from app.org.hierarchy import resolve_business_entities, resolve_org_scope

    # The resolver keys on the level name, not the column: ``sub_territory``,
    # not ``sub_territory_code``. The endpoint these tests used to call did the
    # same stripping on its own query parameters.
    levels = {k.removesuffix("_code"): v for k, v in filters.items()}
    with Session(engine) as session:
        scope = resolve_org_scope(session, levels)
        found = resolve_business_entities(session, scope)
    # Organisational levels first, then the business entities under them — the
    # same two halves the endpoint combined, so a test can still ask whether a
    # territory, its sub-territory and its customers all resolved from one
    # filter.
    rows = [{"type": level, "id": code, "name": code,
             "parent_type": None, "parent_id": None}
            for level, codes in scope.codes.items() for code in codes]
    rows += [{"type": e.type, "id": e.code, "name": e.name,
              "parent_type": e.parent_type, "parent_id": e.parent_code}
             for entities in found.values() for e in entities]
    return rows

def test_customer_has_sub_territory_code_immediately_before_customer_code():
    """The specification is explicit: before, not after, and not at the end."""
    from app.upload.registry import get_upload_type

    columns = [c.target for c in get_upload_type("dim_customer").columns]
    assert columns[0] == "sub_territory_code"
    assert columns[1] == "customer_code"
    assert columns[2] == "customer_name"


def test_the_customer_display_name_is_sub_territory_code():
    from app.upload.registry import get_upload_type

    column = next(c for c in get_upload_type("dim_customer").columns
                  if c.target == "sub_territory_code")
    assert column.name == "Sub Territory Code"


def test_the_material_master_is_seven_identifying_columns_plus_two_inputs():
    """No SKU bridge, no pack size, no unit of measure — and no tenth column.

    The order is the contract, not just the set: revision 0023 put Company Code
    **first**, before the classification and before the material itself, and the
    upload template, the preview, the management table, the edit form and the
    CSV export are all derived from this one tuple. Asserting the list in order
    is what stops a column being added, moved or hidden anywhere without this
    failing first.

    Revision 0027 appended ``conversion_factor`` and ``transfer_price``, the two
    inputs Target Management derives Quantity and Value from. They come last and
    they are **optional**, which is the whole of the difference between them and
    the seven above: a Material Master extract produced before they were asked
    for does not carry them, and requiring them would reject every such file.
    """
    from app.upload.registry import get_upload_type

    columns = [c.target for c in get_upload_type("dim_material").columns]
    assert columns == [
        "company_code",
        "material_group_code", "material_group_name",
        "material_brand_code", "material_brand",
        "material_code", "material_description",
        "conversion_factor", "transfer_price",
    ]


def test_the_two_derivation_inputs_are_the_only_optional_columns():
    """Everything that *identifies* a material is still required.

    The pairing matters: an optional column is one a file may omit, and if that
    ever crept onto the material code or its classification, a row missing it
    would load as a half-identified material rather than being rejected.
    """
    from app.upload.registry import get_upload_type

    optional = [c.target for c in get_upload_type("dim_material").columns
                if not c.required]
    assert optional == ["conversion_factor", "transfer_price"]


def test_every_material_master_column_is_visible_in_the_table():
    """None of the nine is hidden by the narrow-table heuristic.

    ``_master_field`` promotes a column when it is a key, one of the first
    three, or matches a known suffix — everything else starts hidden behind the
    column picker. Moving Material Description to seventh took it out of the
    first-three window and would have hidden the only human-readable column in
    the table, which is why ``_description`` is a promoted suffix.

    The two derivation inputs are promoted by name for a different reason: on
    most deployments they are empty, and they are exactly what a reader needs to
    see is empty — a column hidden by default gives no hint that it exists.
    """
    from app.datamgmt.catalogue import get_master

    entity = get_master("dim_material")
    visible = entity.to_dict()["default_columns"]
    assert visible == [field.name for field in entity.fields]
    assert len(visible) == 9


def test_no_existing_customer_field_was_removed_or_renamed():
    from app.upload.registry import get_upload_type

    customer = {c.target for c in get_upload_type("dim_customer").columns}
    assert {"customer_code", "customer_name", "customer_type", "address",
            "district", "mobile", "status"} <= customer


def test_the_management_table_shows_them_in_that_order(client):
    token = login(client)
    body = client.get("/api/master/dim_customer", headers=auth(token)).json()
    names = [field["name"] for field in body["entity"]["fields"]]
    assert names[:3] == ["sub_territory_code", "customer_code", "customer_name"]


def test_the_material_master_references_no_other_table_by_foreign_key():
    """Its group and brand are its own columns, not codes into other masters."""
    from app.database.models import DimMaterial

    assert DimMaterial.__table__.foreign_keys == set()


def test_the_new_fields_declare_what_they_reference(client):
    """Which is what gives the form a dropdown instead of a free-text box."""
    token = login(client)

    customer = client.get("/api/master/dim_customer", headers=auth(token)).json()
    field = next(f for f in customer["entity"]["fields"]
                 if f["name"] == "sub_territory_code")
    assert field["references"] == "dim_sub_territory"
    assert field["references_column"] == "sub_territory_code"

    # The other half of this test read ``dim_product.company_code``, which
    # referenced ``dim_company`` the same way. That master left in revision
    # 0022; the storage location's plant reference is the surviving example of a
    # declared, non-foreign-key lookup.
    location = client.get("/api/master/dim_storage_location",
                          headers=auth(token)).json()
    field = next(f for f in location["entity"]["fields"]
                 if f["name"] == "plant_code")
    assert field["references"] == "dim_plant"


# ==========================================================================
# Validation (items 4, 8, 12)
# ==========================================================================


@pytest.fixture
def customer(agent_engine):
    with Session(agent_engine) as session:
        session.add(DimCustomer(customer_code="C001", customer_name="ABC Traders"))
        session.commit()
    return "C001"


@pytest.fixture
def traded(agent_engine):
    """A sale that resolves CUST-001 to STR001.

    The shared fixture's rows carry a territory but no sub-territory, so the
    facts alone give the mapper nothing to work from — which is correct, and
    means a test about deriving a sub-territory has to create one.
    """
    from app.etl.pipeline import run_import
    from app.etl.readers import RecordsSourceReader
    from conftest_phase2 import sales_row

    result = run_import(agent_engine, "sales", RecordsSourceReader(
        [sales_row(**{"Invoice No": "INV-ST", "Date": "2026-08-08",
                      "Sub Territory Code": "STR001",
                      "Customer Code": "CUST-001"})],
        source_name="st.csv"), source_system="TEST")
    assert result.rejected_rows == 0, result.error_counts
    return agent_engine


def test_a_valid_sub_territory_is_accepted(client, customer):
    token = login(client)
    response = client.put(f"/api/master/dim_customer/{customer}",
                          headers=auth(token),
                          json={"values": {"sub_territory_code": "STR001"}})
    assert response.status_code == 200, response.text
    assert response.json()["record"]["sub_territory_code"] == "STR001"


def test_an_invalid_sub_territory_is_refused_by_code(client, customer):
    token = login(client)
    response = client.put(f"/api/master/dim_customer/{customer}",
                          headers=auth(token),
                          json={"values": {"sub_territory_code": "ST999"}})
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert any("Invalid Sub-territory Code: ST999" in e["error"] for e in errors)


def test_a_declared_lookup_is_checked_on_create_as_well_as_on_update(client):
    """The reference is validated wherever a record is written, not only on edit.

    Two tests stood here checking ``dim_product.company_code``, which was exactly
    this shape of reference — declared, validated, and deliberately not a
    database foreign key. That master left in revision 0022; the customer's
    sub-territory is the surviving one, and the create path is the half the
    update test above does not cover.
    """
    token = login(client)
    response = client.post("/api/master/dim_customer", headers=auth(token),
                           json={"values": {"customer_code": "C901",
                                            "customer_name": "Nowhere Traders",
                                            "sub_territory_code": "ST999"}})
    assert response.status_code == 422, response.text
    errors = response.json()["detail"]["errors"]
    assert any("Invalid Sub-territory Code: ST999" in e["error"] for e in errors)


def test_the_field_is_optional_so_an_unmapped_record_can_be_saved(client, customer):
    """Blank is a legitimate state — it is what "unmapped" means."""
    token = login(client)
    response = client.put(f"/api/master/dim_customer/{customer}",
                          headers=auth(token),
                          json={"values": {"customer_name": "ABC Traders Ltd"}})
    assert response.status_code == 200


def test_creating_a_customer_with_a_sub_territory(client):
    token = login(client)
    response = client.post("/api/master/dim_customer", headers=auth(token),
                           json={"values": {"sub_territory_code": "STR001",
                                            "customer_code": "C900",
                                            "customer_name": "New Traders"}})
    assert response.status_code == 201, response.text
    assert response.json()["record"]["sub_territory_code"] == "STR001"


# ==========================================================================
# Upload (items 7, 8, 15)
# ==========================================================================


def test_the_customer_template_leads_with_sub_territory_code(client):
    token = login(client)
    body = client.get("/api/data-upload/types", headers=auth(token)).json()
    customer = next(
        entry for category in body["categories"] for entry in category["types"]
        if entry["key"] == "dim_customer"
    )
    assert [c["target"] for c in customer["columns"]][:2] == [
        "sub_territory_code", "customer_code"]


def test_an_upload_maps_the_column_and_validates_it(agent_engine):
    from app.etl.readers import RecordsSourceReader
    from app.upload import master_loader
    from app.upload.registry import get_upload_type

    upload_type = get_upload_type("dim_customer")
    with Session(agent_engine) as session:
        reader = RecordsSourceReader([
            {"Sub Territory Code": "STR001", "Customer Code": "C100",
             "Customer Name": "Good Traders"},
            {"Sub Territory Code": "ST999", "Customer Code": "C101",
             "Customer Name": "Bad Traders"},
        ], source_name="customers.csv")
        result = master_loader.validate(session, upload_type, reader)

    good, bad = result.rows
    assert good.ok
    assert good.values["sub_territory_code"] == "STR001"
    assert not bad.ok
    assert any("Invalid Sub-territory Code: ST999" in issue.message
               for issue in bad.issues)


def test_an_upload_loads_the_mapped_customer(agent_engine):
    from app.etl.readers import RecordsSourceReader
    from app.upload import master_loader
    from app.upload.registry import get_upload_type

    upload_type = get_upload_type("dim_customer")
    with Session(agent_engine) as session:
        reader = RecordsSourceReader(
            [{"Sub Territory Code": "STR001", "Customer Code": "C200",
              "Customer Name": "Loaded Traders"}],
            source_name="customers.csv",
        )
        validation = master_loader.validate(session, upload_type, reader)
        master_loader.load(session, upload_type, validation)
        session.commit()

        record = session.execute(
            select(DimCustomer).where(DimCustomer.customer_code == "C200")
        ).scalar_one()
        assert record.sub_territory_code == "STR001"


def test_a_master_upload_validates_a_declared_lookup(agent_engine):
    """An upload checks a declared reference row by row, naming the bad code.

    This checked a Product Master upload's Company Code before revision 0022
    removed that master. The Storage Location Master's Plant Code is the same
    kind of reference — declared, validated, and not a database foreign key.
    """
    from app.etl.readers import RecordsSourceReader
    from app.upload import master_loader
    from app.upload.registry import get_upload_type

    upload_type = get_upload_type("dim_storage_location")
    with Session(agent_engine) as session:
        reader = RecordsSourceReader(
            [{"Plant Code": "PL999", "Storage Location Code": "SL09",
              "Storage Location Name": "Nowhere"}],
            source_name="locations.csv",
        )
        result = master_loader.validate(session, upload_type, reader)

    assert any("Invalid Plant Code: PL999" in issue.message
               for issue in result.rows[0].issues)


# ==========================================================================
# Deriving the mapping (items 3, 11, 20)
# ==========================================================================


def test_a_customer_trading_in_one_sub_territory_is_mapped(traded, agent_engine):
    """One sub-territory across all transactions is an unambiguous answer."""
    with Session(agent_engine) as session:
        session.add(DimCustomer(customer_code="CUST-001", customer_name="Trader"))
        session.commit()

        report = mapping.map_customers(session, apply=False)

    mapped = {o.code: o.value for o in report.mapped}
    assert mapped.get("CUST-001") == "STR001"


def test_a_customer_trading_in_two_sub_territories_is_left_unmapped(
    traded, agent_engine
):
    """Ambiguity is reported, never resolved by picking one."""
    from app.etl.pipeline import run_import
    from app.etl.readers import RecordsSourceReader
    from conftest_phase2 import sales_row

    with Session(agent_engine) as session:
        session.add(DimSubTerritory(sub_territory_code="STR009",
                                    sub_territory_name="Second",
                                    territory_code="TR001"))
        session.add(DimCustomer(customer_code="CUST-001", customer_name="Trader"))
        session.commit()

    run_import(agent_engine, "sales", RecordsSourceReader(
        [sales_row(**{"Invoice No": "INV-X", "Date": "2026-08-09",
                      "Sub Territory Code": "STR009",
                      "Customer Code": "CUST-001"})],
        source_name="x.csv"), source_system="TEST")

    with Session(agent_engine) as session:
        report = mapping.map_customers(session, apply=False)

    outcome = next(o for o in report.unmapped if o.code == "CUST-001")
    assert outcome.outcome == mapping.UNMAPPED_AMBIGUOUS
    assert set(outcome.candidates) == {"STR001", "STR009"}
    assert "more than one" in outcome.reason


def test_a_customer_with_no_transactions_is_left_unmapped(agent_engine):
    with Session(agent_engine) as session:
        session.add(DimCustomer(customer_code="C-NEW", customer_name="Quiet"))
        session.commit()
        report = mapping.map_customers(session, apply=False)

    outcome = next(o for o in report.unmapped if o.code == "C-NEW")
    assert outcome.outcome == mapping.UNMAPPED_NO_EVIDENCE
    assert outcome.value is None


def test_applying_writes_only_the_unambiguous_mappings(traded, agent_engine):
    with Session(agent_engine) as session:
        session.add(DimCustomer(customer_code="CUST-001", customer_name="Trader"))
        session.add(DimCustomer(customer_code="C-NEW", customer_name="Quiet"))
        session.commit()

        mapping.map_customers(session, apply=True)
        session.commit()

        mapped = session.execute(
            select(DimCustomer).where(DimCustomer.customer_code == "CUST-001")
        ).scalar_one()
        unmapped = session.execute(
            select(DimCustomer).where(DimCustomer.customer_code == "C-NEW")
        ).scalar_one()

    assert mapped.sub_territory_code == "STR001"
    assert unmapped.sub_territory_code is None


def test_an_existing_value_is_never_overwritten(agent_engine):
    with Session(agent_engine) as session:
        session.add(DimCustomer(customer_code="CUST-001", customer_name="Trader",
                                sub_territory_code="STR001"))
        session.commit()

        report = mapping.map_customers(session, apply=True)
        session.commit()

    assert report.already_set == 1
    assert report.mapped_count == 0


def test_a_value_contradicting_the_facts_is_flagged_as_a_conflict(
    traded, agent_engine
):
    """Item 4: do not silently change existing data — flag it."""
    with Session(agent_engine) as session:
        session.add(DimSubTerritory(sub_territory_code="STR050",
                                    sub_territory_name="Elsewhere",
                                    territory_code="TR001"))
        session.add(DimCustomer(customer_code="CUST-001", customer_name="Trader",
                                sub_territory_code="STR050"))
        session.commit()

        report = mapping.map_customers(session, apply=True)
        session.commit()

        record = session.execute(
            select(DimCustomer).where(DimCustomer.customer_code == "CUST-001")
        ).scalar_one()

    assert report.conflict_count == 1
    conflict = report.conflicts[0]
    assert conflict.value == "STR050"
    assert "STR001" in conflict.candidates
    # Flagged, not corrected.
    assert record.sub_territory_code == "STR050"


# ==========================================================================
# Product mapping (item 11) — retired with the master it wrote to
# ==========================================================================


def test_there_is_no_product_to_company_derivation_left():
    """Four tests stood here; the function they exercised is gone.

    ``mapping.map_products`` confirmed a SKU master's free-text
    ``producer_company`` against ``dim_company`` and wrote the result to
    ``dim_product.company_code``. Revision 0022 removed that master, and the
    Material Master states no producing company, so there is no text to confirm
    and none is invented.

    ``normalise_company`` survives and is still tested below: the quality report
    compares company names with it.
    """
    assert not hasattr(mapping, "map_products")
    assert hasattr(mapping, "normalise_company")
    assert "material" in mapping.quality_issues.__doc__.lower()


def test_normalisation_does_not_match_two_different_companies():
    assert mapping.normalise_company("NAAFCO Ltd.") == "naafco"
    assert mapping.normalise_company("NAAFCO") == "naafco"
    # A longer name is a different company, not a suffix variation.
    assert mapping.normalise_company("NAAFCO Agrovet") != "naafco"


# ==========================================================================
# The mapping report (items 21, 22)
# ==========================================================================


def test_the_mapping_report_counts_every_category(traded, client, agent_engine):
    with Session(agent_engine) as session:
        session.add(DimCustomer(customer_code="CUST-001", customer_name="Trader"))
        session.add(DimCustomer(customer_code="C-NEW", customer_name="Quiet"))
        session.commit()

    token = login(client)
    body = client.get("/api/data-quality/master-mapping",
                      headers=auth(token)).json()

    customer = body["customer"]
    assert customer["entity"] == "dim_customer"
    assert customer["total"] == 2
    assert customer["mapped"] == 1
    assert customer["unmapped"] == 1
    assert customer["unmapped_records"][0]["code"] == "C-NEW"
    assert customer["conflicts"] == 0

    # One derived link now, not two: the product half left with its master.
    assert "product" not in body
    assert "quality" in body
    assert "material" in body["quality"]


def test_the_report_writes_nothing(client, agent_engine):
    """It can be refreshed while deciding what to do about what it says."""
    with Session(agent_engine) as session:
        session.add(DimCustomer(customer_code="CUST-001", customer_name="Trader"))
        session.commit()

    token = login(client)
    client.get("/api/data-quality/master-mapping", headers=auth(token))

    with Session(agent_engine) as session:
        record = session.execute(
            select(DimCustomer).where(DimCustomer.customer_code == "CUST-001")
        ).scalar_one()
    assert record.sub_territory_code is None


def test_data_quality_finds_missing_and_invalid_codes(agent_engine):
    with Session(agent_engine) as session:
        session.add(DimCustomer(customer_code="C-BAD", customer_name="Bad",
                                sub_territory_code="ST-NOPE"))
        session.add(DimCustomer(customer_code="C-BLANK", customer_name="Blank"))
        session.commit()

        issues = mapping.quality_issues(session)

    assert "C-BLANK" in issues["customer"]["missing_sub_territory"]
    assert any("C-BAD" in row for row in issues["customer"]["invalid_sub_territory"])


def test_the_report_needs_the_data_quality_section(client):
    admin = login(client)
    user_id = client.get("/api/admin/users", headers=auth(admin),
                         params={"search": "dhaka_rm"}).json()["users"][0]["user_id"]
    client.put(f"/api/admin/users/{user_id}/permissions", headers=auth(admin),
               json={"permissions": {SectionKey.DATA_QUALITY: "DENY"}})

    token = login(client, "dhaka_rm")
    assert client.get("/api/data-quality/master-mapping",
                      headers=auth(token)).status_code == 403


# ==========================================================================
# Map integration (items 16, 17, 18)
# ==========================================================================


def test_the_master_field_places_a_customer_with_no_transactions(client,
                                                                 agent_engine):
    """Invisible to the fact-derived rule; exactly what the field is for."""
    with Session(agent_engine) as session:
        session.add(DimCustomer(customer_code="C-QUIET", customer_name="Quiet",
                                sub_territory_code="STR001"))
        session.commit()

    entities = business_entities(agent_engine, territory_code="TR001")

    customer = next(e for e in entities
                    if e["type"] == "customer" and e["id"] == "C-QUIET")
    assert customer["parent_type"] == "sub_territory"
    assert customer["parent_id"] == "STR001"


def test_a_sub_territory_filter_shows_only_its_own_customers(client,
                                                             agent_engine):
    """Item 17: ST001 shows C001 and C002, and must not show C003."""
    with Session(agent_engine) as session:
        session.add(DimSubTerritory(sub_territory_code="STR002",
                                    sub_territory_name="South",
                                    territory_code="TR001"))
        session.flush()
        session.add(DimCustomer(customer_code="C001", customer_name="ABC",
                                sub_territory_code="STR001"))
        session.add(DimCustomer(customer_code="C002", customer_name="XYZ",
                                sub_territory_code="STR001"))
        session.add(DimCustomer(customer_code="C003", customer_name="Rahman",
                                sub_territory_code="STR002"))
        session.commit()

    entities = business_entities(agent_engine, sub_territory_code="STR001")
    customers = {e["id"] for e in entities if e["type"] == "customer"}

    assert {"C001", "C002"} <= customers
    assert "C003" not in customers


def test_the_master_assignment_overrides_where_the_facts_placed_a_customer(
    client, agent_engine
):
    """Item 18: the master field is authoritative for Sub-Territory → Customer."""
    with Session(agent_engine) as session:
        session.add(DimSubTerritory(sub_territory_code="STR002",
                                    sub_territory_name="South",
                                    territory_code="TR001"))
        session.flush()
        # CUST-001 has transactions resolving to STR001, but the master says
        # it belongs to STR002.
        session.add(DimCustomer(customer_code="CUST-001", customer_name="Trader",
                                sub_territory_code="STR002"))
        session.commit()

    first = business_entities(agent_engine, sub_territory_code="STR001")
    assert "CUST-001" not in {e["id"] for e in first
                              if e["type"] == "customer"}

    second = business_entities(agent_engine, sub_territory_code="STR002")
    assert "CUST-001" in {e["id"] for e in second
                          if e["type"] == "customer"}


def test_an_unassigned_customer_still_falls_back_to_the_facts(traded, client):
    """Nothing that worked before the field existed stops working."""
    entities = business_entities(traded, territory_code="TR001")
    customers = {e["id"] for e in entities if e["type"] == "customer"}
    assert "CUST-001" in customers


def test_the_territory_to_customer_chain_still_resolves(client, agent_engine):
    """Item 16: T001 → ST001 → C001, all present under one territory filter."""
    with Session(agent_engine) as session:
        session.add(DimCustomer(customer_code="C001", customer_name="ABC",
                                sub_territory_code="STR001"))
        session.commit()

    entities = business_entities(agent_engine, territory_code="TR001")
    by_type: dict[str, set[str]] = {}
    for entity in entities:
        by_type.setdefault(entity["type"], set()).add(entity["id"])

    assert "TR001" in by_type["territory"]
    assert "STR001" in by_type["sub_territory"]
    assert "C001" in by_type["customer"]


# ==========================================================================
# Backward compatibility (item 27)
# ==========================================================================


def test_the_reporting_pages_still_work(client):
    """Adding the customer sub-territory field broke none of the report pages.

    The list is the transactional surface as revision 0020 left it — sales,
    material stock and target. Collection and Outstanding are not absent from
    this check by oversight; they no longer exist to check.
    """
    token = login(client)
    for page in ("sales", "stock", "target"):
        response = client.get(f"/api/pages/{page}?{WINDOW}", headers=auth(token))
        assert response.status_code == 200, (page, response.text)


def test_the_transaction_tables_still_work(client):
    token = login(client)
    response = client.get(f"/api/pages/transactions/sales?{WINDOW}",
                          headers=auth(token))
    assert response.status_code == 200


def test_the_material_export_carries_the_master_it_describes(client):
    """The export follows the registry, so it is the six columns and no more."""
    token = login(client)
    response = client.get("/api/master/dim_material/export/csv",
                          headers=auth(token))
    assert response.status_code == 200
    header = response.content.decode("utf-8-sig").splitlines()[0]
    for column in ("Material Code", "Material Description", "Material Group Code",
                   "Material Brand"):
        assert column in header
    assert "SKU" not in header


def test_the_customer_export_leads_with_the_new_column(client, agent_engine):
    with Session(agent_engine) as session:
        session.add(DimCustomer(customer_code="C001", customer_name="ABC",
                                sub_territory_code="STR001"))
        session.commit()

    token = login(client)
    response = client.get("/api/master/dim_customer/export/csv",
                          headers=auth(token))
    header = response.content.decode("utf-8-sig").splitlines()[0]
    assert header.startswith("Sub Territory Code,Customer Code,Customer Name")


def test_searching_and_filtering_the_new_column(client, agent_engine):
    with Session(agent_engine) as session:
        session.add(DimCustomer(customer_code="C001", customer_name="ABC",
                                sub_territory_code="STR001"))
        session.add(DimCustomer(customer_code="C002", customer_name="XYZ"))
        session.commit()

    token = login(client)
    found = client.get("/api/master/dim_customer?search=STR001",
                       headers=auth(token)).json()
    assert {row["customer_code"] for row in found["rows"]} == {"C001"}

    sorted_rows = client.get(
        "/api/master/dim_customer?sort_by=sub_territory_code&sort_dir=desc",
        headers=auth(token))
    assert sorted_rows.status_code == 200


def test_an_edit_to_the_new_field_is_audited(client, customer, agent_engine):
    """Item 25: the existing audit system records it, old value and new."""
    token = login(client)
    client.put(f"/api/master/dim_customer/{customer}", headers=auth(token),
               json={"values": {"sub_territory_code": "STR001"},
                     "reason": "Assigned after survey"})

    body = client.get(f"/api/master/dim_customer/{customer}/history",
                      headers=auth(token)).json()
    entry = body["history"][0]
    change = next(c for c in entry["changes"]
                  if c["field"] == "sub_territory_code")
    assert change["old"] in (None, "")
    assert change["new"] == "STR001"
    assert entry["reason"] == "Assigned after survey"
    assert entry["username"] == "root"


def test_permissions_still_govern_the_new_field(client, customer):
    """Item 26: the existing framework, not a new one."""
    admin = login(client)
    user_id = client.get("/api/admin/users", headers=auth(admin),
                         params={"search": "dhaka_rm"}).json()["users"][0]["user_id"]
    client.put(f"/api/admin/users/{user_id}/permissions", headers=auth(admin),
               json={"permissions": {SectionKey.MASTER_DATA: ALLOW},
                     "actions": {SectionKey.MASTER_DATA: {"EDIT": "DENY"}}})

    token = login(client, "dhaka_rm")
    response = client.put(f"/api/master/dim_customer/{customer}",
                          headers=auth(token),
                          json={"values": {"sub_territory_code": "STR001"}})
    assert response.status_code == 403
