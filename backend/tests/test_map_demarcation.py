"""The Area Demarcation map: coordinates, shapes, and no figure anywhere.

Step 3 of the second tab. What these tests pin is the half that makes it a
*different* map rather than a view of the first one: that it reads no fact
table, that its design cannot be handed to the endpoints that draw figures (or
the reverse), that a level with nothing placed says so instead of drawing
nothing, and that a shape is validated against the one catalogue rather than
stored and ignored.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.auth.security import hash_password
from app.database.models_admin import UserSectionPermission
from app.database.models_ai import AppUser, Role
from app.database.models_map import DesignPurpose, GeoSource, MapDesign
from app.main import app
from app.map import designs, geo, locations, styles
from app.security.sections import DENY, SectionKey

PASSWORD = "Correct-Horse-9"
DHAKA = (23.7808, 90.4008)
KHULNA = (22.8456, 89.5403)
KAZIPARA = (23.7925, 90.3660)


@pytest.fixture
def client(agent_engine, users):
    with Session(agent_engine) as db:
        for user in db.query(AppUser).all():
            user.password_hash = hash_password(PASSWORD)
        db.add(AppUser(username="root", display_name="Administrator",
                       role=Role.SUPER_ADMIN, is_active=True,
                       password_hash=hash_password(PASSWORD)))
        denied = AppUser(username="no_map", display_name="No Map",
                         role=Role.VIEWER, is_active=True,
                         password_hash=hash_password(PASSWORD))
        db.add(denied)
        db.flush()
        db.add(UserSectionPermission(user_id=denied.user_id,
                                     section_key=SectionKey.MAP, access=DENY,
                                     updated_by="test"))
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


def login(client: TestClient, username: str = "root") -> dict[str, str]:
    response = client.post("/api/auth/login",
                           json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def fetch(client: TestClient, headers: dict[str, str], **params) -> dict:
    query: list[tuple[str, str]] = []
    for key, value in params.items():
        if isinstance(value, (list, tuple)):
            query.extend((key, item) for item in value)
        else:
            query.append((key, str(value)))
    response = client.get("/api/map/locations", params=query, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def layer_of(body: dict, level: str) -> dict:
    return next(layer for layer in body["layers"] if layer["level"] == level)


# ==========================================================================
# The catalogue is declared once
# ==========================================================================


def test_every_shape_travels_with_its_geometry():
    """A key without a path would leave the renderer holding its own catalogue.

    The rule ``0033``'s removal recorded is that the renderer must not be the
    only thing that knows a marker's design. Publishing only the key would
    satisfy it in name: the browser would still need a path per key of its own,
    and a shape the server allowed but the browser had never heard of would
    draw nothing with no error anywhere.
    """
    published = styles.shape_catalogue()
    assert [shape["key"] for shape in published] == list(styles.SHAPE_KEYS)
    for shape in published:
        assert shape["path"].startswith("M"), shape["key"]
        assert shape["label"] and shape["viewbox"] == styles.SHAPE_VIEWBOX


def test_the_seeded_shapes_are_all_in_the_catalogue(agent_engine):
    """The migration restates its shapes; this is what stops the two drifting.

    ``0035`` writes shape keys as literals, deliberately — a migration must
    keep producing the seed it produced on the day it was written. The cost of
    that is a second copy, and this is the check that keeps it honest.
    """
    with Session(agent_engine) as db:
        design = designs.default_design(db, DesignPurpose.DEMARCATION)
        assert design is not None, "0035 seeds a demarcation design"
        for layer in design.layers:
            style = designs.layer_to_dict(layer, design)["style"]
            assert style["shape"] in styles.SHAPE_KEYS, layer.point_level


def test_an_unknown_shape_is_refused_by_name(agent_engine, users):
    """Stored and ignored is the one outcome a style override must never have."""
    from app.ai.permission_filter import UserContext
    from app.map.errors import InvalidLayer

    with Session(agent_engine) as db:
        user = UserContext.from_user(db.execute(select(AppUser)).scalars().first())
        spec = designs.DesignSpec(
            name="Bad Shape", purpose=DesignPurpose.DEMARCATION,
            layers=(designs.LayerSpec(point_level="region",
                                      style_config={"shape": "octagon"}),),
        )
        with pytest.raises(InvalidLayer) as raised:
            designs.create_design(db, user, spec)
    assert "octagon" in str(raised.value)
    # Named per level and per field, so the editor can say which layer to fix
    # rather than "invalid style".
    assert raised.value.details == {"level": "region", "field": "style_config"}
    assert ", ".join(styles.SHAPE_KEYS) in raised.value.user_message


# ==========================================================================
# The two maps stay apart
# ==========================================================================


def test_the_demarcation_design_is_not_offered_to_the_analysis_tab(client):
    headers = login(client)
    analysis = client.get("/api/map/designs", headers=headers).json()
    assert analysis["purpose"] == DesignPurpose.ANALYSIS
    names = [design["name"] for design in analysis["designs"]]
    assert "Area Demarcation" not in names
    assert all(design["purpose"] == DesignPurpose.ANALYSIS
               for design in analysis["designs"])

    demarcation = client.get("/api/map/designs",
                             params={"purpose": DesignPurpose.DEMARCATION},
                             headers=headers).json()
    assert [design["name"] for design in demarcation["designs"]] == ["Area Demarcation"]


def test_each_map_has_its_own_default(client):
    """Two tabs, two designs to open on — not one flag fought over."""
    headers = login(client)
    for purpose in DesignPurpose.ALL:
        body = client.get("/api/map/designs", params={"purpose": purpose},
                          headers=headers).json()
        assert body["default_design_id"] is not None, purpose
        chosen = next(d for d in body["designs"]
                      if d["design_id"] == body["default_design_id"])
        assert chosen["purpose"] == purpose


def test_the_figures_endpoint_refuses_a_demarcation_design(client):
    """Otherwise a composition that named no metric would be drawn by one."""
    headers = login(client)
    demarcation = client.get("/api/map/designs",
                             params={"purpose": DesignPurpose.DEMARCATION},
                             headers=headers).json()["designs"][0]
    response = client.get("/api/map/data", headers=headers, params={
        "date_from": "2026-08-01", "date_to": "2026-08-31",
        "design_id": demarcation["design_id"],
    })
    assert response.status_code == 404, response.text


def test_the_locations_endpoint_refuses_an_analysis_design(client):
    """And the same in reverse, or the tabs would only half separate."""
    headers = login(client)
    analysis = client.get("/api/map/designs", headers=headers).json()["designs"][0]
    response = client.get("/api/map/locations", headers=headers,
                          params={"design_id": analysis["design_id"]})
    assert response.status_code == 404, response.text


# ==========================================================================
# What it draws
# ==========================================================================


def test_it_draws_coordinates_and_states_no_figure(client):
    """The point of the tab: positions, with nothing measured attached."""
    headers = login(client)
    body = fetch(client, headers, levels=["region", "territory"])

    region = layer_of(body, "region")
    assert region["placed"] == 2
    properties = region["features"]["features"][0]["properties"]
    assert set(properties) == {
        "code", "name", "level", "parent_level", "parent_code",
        "source", "precision", "derived_from",
    }
    # Not one measure anywhere in the payload: no period, no metric, no rows.
    assert "period" not in body and "metric" not in body
    for layer in body["layers"]:
        assert "ranking" not in layer and "extents" not in layer
        assert "rows" not in layer and "breaks" not in layer


def test_longitude_comes_first(client):
    """GeoJSON's order, stated once in the module and pinned here."""
    headers = login(client)
    region = layer_of(fetch(client, headers, levels=["region"]), "region")
    for feature in region["features"]["features"]:
        longitude, latitude = feature["geometry"]["coordinates"]
        assert 88 < longitude < 93, feature
        assert 20 < latitude < 27, feature


def test_a_level_with_nothing_placed_says_so(client):
    """A blank map and a map of nothing look identical; only one is a problem."""
    headers = login(client)
    body = fetch(client, headers, levels=["region", "customer"])
    customer = layer_of(body, "customer")
    assert customer["placed"] == 0
    assert customer["notes"], "an empty layer must explain itself"
    note = customer["notes"][0]
    assert "Map Locations" in note or "no customer records" in note.lower()


def test_a_level_missing_some_coordinates_counts_them(client):
    headers = login(client)
    region = layer_of(fetch(client, headers, levels=["region"]), "region")
    assert region["total"] >= region["placed"]
    assert region["missing"] == region["total"] - region["placed"]
    if region["missing"]:
        assert any("cannot be drawn" in note for note in region["notes"])


def test_a_derived_coordinate_is_counted_apart_from_a_placed_one(client,
                                                                 agent_engine):
    """A centroid is a computation, not a place anybody surveyed.

    On a map whose purpose is judging where a boundary falls, that difference
    is the one a reader must not miss, so it travels per feature *and* as a
    count the legend can state.
    """
    with Session(agent_engine) as db:
        geo.derive_parents(db)
        db.commit()
    headers = login(client)
    body = fetch(client, headers, levels=["region", "zone"])
    zone = layer_of(body, "zone")
    assert zone["placed"] > 0, "zone is derived from the placed regions"
    assert zone["derived"] == zone["placed"]
    assert all(feature["properties"]["source"] == GeoSource.DERIVED
               for feature in zone["features"]["features"])

    region = layer_of(body, "region")
    assert region["derived"] == 0, "a placed region is not a centroid"


def test_each_layer_carries_the_configuration_it_is_drawn_with(client):
    """The renderer reads one object per layer and decides nothing itself."""
    headers = login(client)
    body = fetch(client, headers, levels=["region", "territory"])
    for layer in body["layers"]:
        config = layer["layer"]
        assert config["style"]["shape"] in styles.SHAPE_KEYS
        assert config["style"]["point_color"].startswith("#")
        assert isinstance(config["min_zoom"], int)


def test_the_seeded_design_gives_each_level_its_own_shape(client):
    """Telling the levels apart at a glance is the whole requirement."""
    headers = login(client)
    body = fetch(client, headers, levels=["region", "territory", "sub_territory"])
    shapes = {layer["level"]: layer["layer"]["style"]["shape"]
              for layer in body["layers"]}
    assert len(set(shapes.values())) == len(shapes), shapes


def test_an_unknown_level_is_refused_rather_than_silently_dropped(client):
    headers = login(client)
    response = client.get("/api/map/locations", headers=headers,
                          params={"levels": "planet"})
    assert response.status_code == 422
    assert "planet" in response.text


def test_the_map_section_is_required(client):
    response = client.get("/api/map/locations", headers=login(client, "no_map"))
    assert response.status_code == 403


# ==========================================================================
# It reads no fact table
# ==========================================================================


def test_it_answers_the_same_with_no_sales_at_all(client, agent_engine):
    """The tab that must work on a warehouse with no transactions in it.

    ``data/dev.db`` holds 1,139 coordinates and no ``fact_sales`` rows, so the
    analysis map there is correctly empty and this one is not. That is not a
    coincidence to be grateful for — it is the difference between the two, and
    it is what this asserts.
    """
    from app.database.models_warehouse import FactSales

    headers = login(client)
    before = fetch(client, headers, levels=["region"])
    with Session(agent_engine) as db:
        db.execute(FactSales.__table__.delete())
        db.commit()
    after = fetch(client, headers, levels=["region"])
    assert after["layers"] == before["layers"]
    assert layer_of(after, "region")["placed"] == 2


def test_the_module_reports_only_levels_that_have_a_coordinate(client, agent_engine):
    """Derived from the level registry and the data, never a written-down list.

    Takes ``client`` for the coordinates its fixture places, not for HTTP.
    """
    with Session(agent_engine) as db:
        drawable = locations.drawable_levels(db)
    assert "region" in drawable and "territory" in drawable
    assert "customer" not in drawable, "no customer is placed in this fixture"
    # Hierarchy order, from the level registry rather than from the table.
    from app.map.levels import LEVEL_KEYS
    assert drawable == [key for key in LEVEL_KEYS if key in set(drawable)]
