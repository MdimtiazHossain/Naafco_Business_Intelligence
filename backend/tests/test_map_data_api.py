"""What the map draws, over HTTP: one design, its layers, one period, one scope.

Step 5 of the rebuild. The figures are the ones ``test_map_data`` already pins
at the tool level; what these tests add is the endpoint's composition — which
layers a design draws, the reader's toggles and metric, the ranking, the
extents a legend needs, the ancestry card — and the specification's acceptance
checks that a map total equals a report total (§70) and that a hierarchy
filter narrows every layer (§34), both proved against the same page endpoint
a reader would open beside the map.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.auth.security import hash_password
from app.database.models_admin import UserSectionPermission
from app.database.models_ai import AppUser, AuditAction, AuditLog, Role
from app.database.models_warehouse import DimCustomer
from app.main import app
from app.map import geo, styles
from app.security.sections import DENY, SectionKey

PASSWORD = "Correct-Horse-9"
#: The window the seeded facts fall in, and the month before it.
WINDOW = {"date_from": "2026-08-01", "date_to": "2026-08-31"}

DHAKA = (23.7808, 90.4008)
KHULNA = (22.8456, 89.5403)
KAZIPARA = (23.7925, 90.3660)
SEED_LAYERS = ["zone", "region", "area", "unit", "territory", "sub_territory", "customer"]
VISIBLE_SEED_LAYERS = ["zone", "region", "area", "territory", "sub_territory"]


@pytest.fixture
def client(agent_engine, users):
    with Session(agent_engine) as db:
        for user in db.query(AppUser).all():
            user.password_hash = hash_password(PASSWORD)
        db.add(AppUser(username="root", display_name="Administrator",
                       role=Role.SUPER_ADMIN, is_active=True,
                       password_hash=hash_password(PASSWORD)))
        denied = AppUser(username="no_map", display_name="No Map", role=Role.VIEWER,
                         is_active=True, password_hash=hash_password(PASSWORD))
        db.add(denied)
        db.flush()
        db.add(UserSectionPermission(user_id=denied.user_id,
                                     section_key=SectionKey.MAP, access=DENY,
                                     updated_by="test"))
        db.add(DimCustomer(customer_code="CUST-K1", customer_name="Kazipara Store",
                           sub_territory_code="STR001"))
        # Two placed regions and one placed territory; everything else is an
        # entity with data and no coordinate, which the map must report.
        geo.upsert_location(db, entity_type="region", entity_code="REG001",
                            latitude=DHAKA[0], longitude=DHAKA[1])
        geo.upsert_location(db, entity_type="region", entity_code="REG002",
                            latitude=KHULNA[0], longitude=KHULNA[1])
        geo.upsert_location(db, entity_type="territory", entity_code="TR001",
                            latitude=KAZIPARA[0], longitude=KAZIPARA[1])
        db.commit()

    def _session_override():
        db = Session(bind=agent_engine, expire_on_commit=False, future=True)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = _session_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def login(client: TestClient, username: str) -> dict[str, str]:
    response = client.post("/api/auth/login",
                           json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def fetch(client: TestClient, headers: dict[str, str], **params) -> dict:
    query: list[tuple[str, str]] = []
    for key, value in {**WINDOW, **params}.items():
        if isinstance(value, (list, tuple)):
            query.extend((key, item) for item in value)
        else:
            query.append((key, str(value)))
    response = client.get("/api/map/data", params=query, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def layer_of(body: dict, level: str) -> dict:
    return next(layer for layer in body["layers"] if layer["level"] == level)


def codes_in(layer: dict) -> set[str]:
    """Every entity the layer reports, drawn or not."""
    placed = {feature["properties"]["code"] for feature in layer["features"]["features"]}
    return placed | {entry["code"] for entry in layer["unplaced"]}


# ==========================================================================
# Class breaks
# ==========================================================================


def test_class_breaks_are_quantiles_over_the_values_that_exist():
    assert styles.class_breaks([]) == []
    assert styles.class_breaks([None, 5.0]) == []
    assert styles.class_breaks([1, 2]) == [2.0]
    assert styles.class_breaks([1, 1, 1, 2]) == [2.0]
    assert styles.class_breaks(range(1, 9)) == [2.0, 4.0, 6.0]
    # Every break is a value some entity has: nothing is interpolated.
    values = [3, 300, 40, 12_000, 7, 900, 65, 1_500_000, 21]
    breaks = styles.class_breaks(values)
    assert len(breaks) == 3 and all(b in values for b in breaks)
    assert breaks == sorted(breaks)


# ==========================================================================
# The data endpoint
# ==========================================================================


def test_data_draws_the_design_the_map_opens_with(client):
    body = fetch(client, login(client, "ceo"))
    assert body["design"]["name"] == "Business Overview"
    assert body["design"]["is_default"]
    assert body["levels"] == VISIBLE_SEED_LAYERS
    assert body["metric"] == "net_sales"
    assert body["period"]["date_from"] == "2026-08-01"
    assert body["period"]["compare_from"] == "2026-07-01"
    assert body["empty"] is False

    region = layer_of(body, "region")
    assert region["placed_count"] == 2 and region["entity_count"] == 2
    assert region["layer"]["point_level"] == "region"
    assert region["layer"]["effective_metric"] == "net_sales"
    assert region["layer"]["style"]["bands"][0]["label"] == "90% and above"
    by_code = {f["properties"]["code"]: f["properties"]
               for f in region["features"]["features"]}
    assert set(by_code) == {"REG001", "REG002"}
    assert by_code["REG001"]["name"] == "Dhaka"
    assert by_code["REG001"]["net_sales"] > 0
    # The map grows year on year like every other surface, and these fixtures
    # hold nothing in 2025 — so there is no base and therefore no growth. The
    # key is present and empty rather than absent: a region the comparison
    # window does not reach has no growth, never -100%.
    #
    # It used to read the *preceding* period, which had data here and so gave a
    # figure. That is what made the same region come out green on the map and
    # red on the dashboard, and why this assertion had to change rather than the
    # behaviour behind it.
    assert "growth_percent" in by_code["REG001"]
    assert by_code["REG001"]["growth_percent"] is None
    # The window it would have grown against is a year back, not a month back.
    assert body["period"]["growth_from"] == "2025-08-01"

    # Extents and breaks describe the drawn points, for the legend.
    extent = region["extents"]["net_sales"]
    assert extent["min"] <= extent["max"]
    assert extent["max"] == max(p["net_sales"] for p in by_code.values())
    assert region["breaks"]["net_sales"] == [extent["max"]]
    assert "customer_count" in region["extents"]

    ranking = region["ranking"]
    assert ranking["metric"] == "net_sales" and ranking["field"] == "net_sales"
    assert [row["code"] for row in ranking["top"]] == sorted(
        by_code, key=lambda code: by_code[code]["net_sales"], reverse=True)
    assert ranking["bottom"] == [], "two entities: a top two and no bottom"
    assert all(row["placed"] is True for row in ranking["top"])
    assert ranking["ranked_count"] == 2 and ranking["unranked_count"] == 0

    # A level with data and no coordinates is reported, never hidden: the
    # area has sales and nobody has placed it.
    area = layer_of(body, "area")
    assert area["placed_count"] == 0 and area["unplaced"]
    assert any("no coordinate" in note for note in area["notes"])


def test_a_reader_toggles_layers_and_picks_a_metric_without_saving_anything(client):
    headers = login(client, "ceo")
    body = fetch(client, headers, levels=["customer", "region"], metric="achievement")
    assert body["levels"] == ["customer", "region"]
    assert body["metric"] == "achievement"
    assert [layer["level"] for layer in body["layers"]] == ["customer", "region"]

    region = layer_of(body, "region")
    assert region["ranking"]["metric"] == "achievement"
    assert region["ranking"]["field"] == "achievement_percent"
    # An entity with no target has no achievement and is not ranked.
    assert region["ranking"]["ranked_count"] + region["ranking"]["unranked_count"] == 2
    for row in region["ranking"]["top"]:
        assert row["achievement_percent"] is not None

    # The design itself is untouched by a reader's toggles.
    saved = client.get(f"/api/map/designs/{body['design']['design_id']}",
                       headers=headers).json()
    assert [l["point_level"] for l in saved["layers"] if l["is_visible"]] == VISIBLE_SEED_LAYERS


def test_a_metric_with_no_meaning_at_a_level_ranks_that_layer_by_its_own(client):
    body = fetch(client, login(client, "ceo"), levels=["region", "customer"],
                 metric="customer_count")
    assert layer_of(body, "region")["ranking"]["metric"] == "customer_count"
    customer = layer_of(body, "customer")
    assert customer["ranking"]["metric"] == "net_sales"
    assert any("ranked by Sales Amount instead" in note for note in customer["notes"])


def test_data_refuses_what_it_cannot_draw(client):
    headers = login(client, "ceo")
    outside = client.get("/api/map/data", params={**WINDOW, "levels": "sales_force"},
                         headers=headers)
    assert outside.status_code == 422 and "sales_force" in outside.json()["detail"]
    metric = client.get("/api/map/data", params={**WINDOW, "metric": "profit"},
                        headers=headers)
    assert metric.status_code == 422 and "profit" in metric.json()["detail"]
    design = client.get("/api/map/data", params={**WINDOW, "design_id": 999_999},
                        headers=headers)
    assert design.status_code == 404
    period = client.get("/api/map/data", params={"period": "SOMEDAY"}, headers=headers)
    assert period.status_code == 422


def test_map_figures_equal_the_performance_page(client):
    """§70: the map's sub-territory (here: territory) sales are the report's."""
    headers = login(client, "ceo")
    body = fetch(client, headers, levels=["region", "territory"], rank_limit=50)

    for level in ("region", "territory"):
        page = client.get("/api/pages/performance",
                          params={**WINDOW, "level": level, "limit": 50},
                          headers=headers)
        assert page.status_code == 200, page.text
        page_rows = {row["code"]: row for row in page.json()["performance"]["rows"]
                     if row["code"] != "(unassigned)"}
        assert page_rows, level
        layer = layer_of(body, level)
        # The ranking carries every entity with data when the limit is wide
        # enough, drawn or not, so it is the map's full statement of figures.
        map_rows = {row["code"]: row for row in layer["ranking"]["top"]}
        assert set(page_rows) <= set(map_rows), level
        for code, page_row in page_rows.items():
            assert map_rows[code]["net_sales"] == pytest.approx(page_row["net_sales"]), code
            assert map_rows[code]["quantity"] == pytest.approx(page_row["quantity"]), code
        # The map may list what a sales report cannot: an entity with a target
        # and no sale in the period (TR002 here) sold nothing, and is on the
        # map at zero rather than missing — never at an invented figure.
        for code in set(map_rows) - set(page_rows):
            assert map_rows[code]["net_sales"] == 0.0, code
            assert map_rows[code]["target_amount"] is not None, code
        # Achievement agrees with the page's own achievement rows.
        achievement = {row["code"]: row
                       for row in (page.json()["achievement"] or {}).get("rows", [])}
        for code, row in achievement.items():
            if code in map_rows and row.get("achievement_percent") is not None:
                assert map_rows[code]["achievement_percent"] == pytest.approx(
                    row["achievement_percent"]), code
        # And a drawn point carries exactly the figure it was ranked by.
        for feature in layer["features"]["features"]:
            code = feature["properties"]["code"]
            assert feature["properties"]["net_sales"] == pytest.approx(
                map_rows[code]["net_sales"])


def test_a_hierarchy_filter_narrows_every_layer(client):
    """§34: Region = Khulna leaves only Khulna's territories on the map."""
    body = fetch(client, login(client, "ceo"), levels=["region", "territory"],
                 region_code="REG002")
    assert body["filters"] == {"region_codes": ["REG002"]}
    assert codes_in(layer_of(body, "region")) == {"REG002"}
    territories = codes_in(layer_of(body, "territory"))
    assert "TR002" in territories and "TR001" not in territories


def test_a_scoped_user_sees_their_region_and_is_refused_beyond_it(client):
    dhaka = login(client, "dhaka_rm")
    body = fetch(client, dhaka, levels=["zone", "region", "territory"])
    assert codes_in(layer_of(body, "region")) == {"REG001"}
    assert "TR002" not in codes_in(layer_of(body, "territory"))
    # The zone above their scope carries their region's figures, and says so.
    assert any("cover only your data scope" in note
               for note in layer_of(body, "zone")["notes"])

    elsewhere = client.get("/api/map/data",
                           params={**WINDOW, "region_code": "REG002"}, headers=dhaka)
    assert elsewhere.status_code == 403


def test_a_user_with_no_scope_is_refused_not_shown_an_empty_map(client):
    response = client.get("/api/map/data", params=WINDOW,
                          headers=login(client, "no_scope"))
    assert response.status_code == 403


def test_data_needs_the_map_section(client):
    assert client.get("/api/map/data", params=WINDOW).status_code == 401
    assert client.get("/api/map/data", params=WINDOW,
                      headers=login(client, "no_map")).status_code == 403


def test_a_reader_may_not_draw_an_inactive_design_but_a_composer_may(client):
    root = login(client, "root")
    ceo = login(client, "ceo")
    created = client.post("/api/map/designs", headers=root, json={
        "name": "Shelved", "layers": [{"point_level": "region"}],
    }).json()
    design_id = created["design_id"]
    assert client.post(f"/api/map/designs/{design_id}/deactivate",
                       headers=root).status_code == 200
    assert client.get("/api/map/data", params={**WINDOW, "design_id": design_id},
                      headers=ceo).status_code == 404
    body = fetch(client, root, design_id=design_id)
    assert body["design"]["name"] == "Shelved" and body["levels"] == ["region"]


def test_drawing_the_map_is_audited_as_a_report_view(client, agent_engine):
    fetch(client, login(client, "ceo"), levels=["region"])
    with Session(agent_engine) as db:
        entry = db.execute(
            select(AuditLog).where(AuditLog.action == AuditAction.VIEW_REPORT,
                                   AuditLog.resource == "map")
        ).scalars().first()
    assert entry is not None and entry.username == "ceo"
    assert entry.detail["levels"] == ["region"]


# ==========================================================================
# The selected-entity card
# ==========================================================================


def test_an_entity_is_described_with_its_ancestry_and_its_coordinate(client):
    headers = login(client, "ceo")
    sub = client.get("/api/map/entities/sub_territory/STR001", headers=headers)
    assert sub.status_code == 200, sub.text
    body = sub.json()
    assert body["name"] == "Kazipara North" and body["label"] == "Sub-Territory"
    assert [(a["level"], a["name"]) for a in body["ancestors"]] == [
        ("company", "Example Industries Ltd."), ("bu", "Consumer Products"),
        ("sales_line", "General Trade"), ("zone", "Dhaka Zone"),
        ("region", "Dhaka"), ("area", "Mirpur"), ("unit", "Mirpur Unit 1"),
        ("territory", "Kazipara"),
    ]
    assert all(a["known"] for a in body["ancestors"])
    assert body["location"] is None, "nothing has placed the sub-territory"

    region = client.get("/api/map/entities/region/REG001", headers=headers).json()
    assert region["location"]["latitude"] == pytest.approx(DHAKA[0])
    assert region["location"]["source"] == "UPLOAD"
    assert [a["level"] for a in region["ancestors"]] == ["company", "bu", "sales_line", "zone"]

    customer = client.get("/api/map/entities/customer/CUST-K1", headers=headers).json()
    assert customer["name"] == "Kazipara Store"
    assert customer["ancestors"][-1] == {
        "level": "sub_territory", "label": "Sub-Territory", "code": "STR001",
        "name": "Kazipara North", "known": True,
    }
    assert customer["ancestors"][0]["level"] == "company"

    assert client.get("/api/map/entities/region/NOPE", headers=headers).status_code == 404
    assert client.get("/api/map/entities/galaxy/REG001", headers=headers).status_code == 404
    assert client.get("/api/map/entities/region/REG001").status_code == 401
