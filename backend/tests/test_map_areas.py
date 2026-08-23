"""The administrative area layer: polygons, stock, filters and styling.

The boundaries used here are **synthetic squares**, not real upazila outlines.
That is deliberate on two counts: a test that depends on a 30 MB published
shapefile is not a test anybody runs, and inventing plausible-looking Bangladesh
geometry to ship would be fabricating geographic data. Squares with known
corners make containment and centroid assertions exact, which real coastlines
never would.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.auth.security import hash_password
from app.database.models_ai import AppUser, Role, UserStatus
from app.database.models_geo import (
    BoundarySource,
    DimDistrict,
    DimDivision,
    DimUpazila,
    MapAreaBoundary,
    MapAreaStyle,
)
from app.database.models_map import GeoPrecision, GeoSource, MapEntityLocation
from app.main import app
from app.map import areas as area_service
from app.map import geometry as geo
from app.map import service as map_service
from app.security.sections import ALLOW, DENY, SectionKey

pytest.importorskip("multipart", reason="python-multipart is required by FastAPI forms")

PASSWORD = "Correct-Horse-9"
WINDOW = "date_from=2026-08-01&date_to=2026-08-31"


def square(west: float, south: float, size: float = 1.0) -> dict:
    """An axis-aligned square, counter-clockwise and closed."""
    east, north = west + size, south + size
    return {
        "type": "Polygon",
        "coordinates": [[
            [west, south], [east, south], [east, north], [west, north],
            [west, south],
        ]],
    }


#: Two neighbouring upazilas in one district, and a third in another district
#: of another division — enough to prove every filter narrows correctly.
AREAS = (
    # code,      name,      district,  division,  west, south
    ("UPZ-TRI", "Trishal", "DIS-MYM", "DIV-DHA", 90.0, 24.0),
    ("UPZ-BHA", "Bhaluka", "DIS-MYM", "DIV-DHA", 91.0, 24.0),
    ("UPZ-KHU", "Khulna Sadar", "DIS-KHU", "DIV-KHU", 89.0, 22.0),
)

DISTRICTS = (("DIS-MYM", "Mymensingh", "DIV-DHA"), ("DIS-KHU", "Khulna", "DIV-KHU"))
DIVISIONS = (("DIV-DHA", "Dhaka"), ("DIV-KHU", "Khulna"))


@pytest.fixture
def geography(agent_engine):
    """Load the synthetic administrative hierarchy and its boundaries."""
    with Session(agent_engine) as session:
        for code, name in DIVISIONS:
            session.add(DimDivision(division_code=code, division_name=name))
        session.flush()
        for code, name, division in DISTRICTS:
            session.add(DimDistrict(district_code=code, district_name=name,
                                    division_code=division))
        session.flush()
        for code, name, district, _division, west, south in AREAS:
            shape = square(west, south)
            latitude, longitude = geo.centroid(shape)
            box = geo.bounds(shape)
            session.add(DimUpazila(upazila_code=code, upazila_name=name,
                                   district_code=district,
                                   latitude=latitude, longitude=longitude))
            session.add(MapAreaBoundary(
                entity_type="upazila", entity_code=code, geometry=shape,
                bbox_north=box.north, bbox_south=box.south,
                bbox_east=box.east, bbox_west=box.west,
                centroid_latitude=latitude, centroid_longitude=longitude,
                point_count=geo.count_points(shape),
                source=BoundarySource.IMPORT, source_file="test.geojson",
            ))
        session.commit()
    return agent_engine


@pytest.fixture
def client(geography, users, monkeypatch):
    import app.database.connection as connection

    monkeypatch.setattr(connection, "get_engine", lambda *a, **k: geography)

    with Session(geography) as session:
        session.add(AppUser(username="root", display_name="Administrator",
                            role=Role.SUPER_ADMIN, is_active=True,
                            status=UserStatus.ACTIVE,
                            password_hash=hash_password(PASSWORD)))
        for user in session.query(AppUser).all():
            if user.password_hash is None:
                user.password_hash = hash_password(PASSWORD)
        map_service.ensure_system_defaults(session)
        session.commit()

    def _session_override():
        session = Session(bind=geography, expire_on_commit=False, future=True)
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


def fetch(client: TestClient, token: str, query: str = "") -> dict:
    response = client.get(f"/api/map/areas?{WINDOW}&{query}", headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def codes(payload: dict) -> set[str]:
    return {feature["id"] for feature in payload["features"]}


# ==========================================================================
# Geometry
# ==========================================================================


def test_centroid_of_a_square_is_its_middle():
    latitude, longitude = geo.centroid(square(90.0, 24.0))
    assert latitude == pytest.approx(24.5)
    assert longitude == pytest.approx(90.5)


def test_centroid_is_area_weighted_not_a_vertex_mean():
    """A dense edge must not drag the centroid towards it."""
    dense = {
        "type": "Polygon",
        "coordinates": [[
            *[[90.0 + i / 100, 24.0] for i in range(101)],   # 101 points
            [91.0, 25.0], [90.0, 25.0], [90.0, 24.0],
        ]],
    }
    latitude, _ = geo.centroid(dense)
    # A vertex mean would sit near 24.05; the true centroid is near 24.33.
    assert latitude > 24.2


def test_a_hole_subtracts_from_the_centroid():
    ring = square(0.0, 0.0, 10.0)["coordinates"][0]
    hole = [[1.0, 1.0], [1.0, 4.0], [4.0, 4.0], [4.0, 1.0], [1.0, 1.0]]
    with_hole = {"type": "Polygon", "coordinates": [ring, hole]}
    latitude, longitude = geo.centroid(with_hole)
    # The hole is in the south-west, so the centroid moves north-east of 5,5.
    assert latitude > 5.0 and longitude > 5.0


def test_point_in_polygon():
    shape = square(90.0, 24.0)
    assert geo.contains(shape, 24.5, 90.5)
    assert not geo.contains(shape, 24.5, 91.5)
    assert not geo.contains(shape, 26.0, 90.5)


def test_a_point_in_a_hole_is_outside():
    ring = square(0.0, 0.0, 10.0)["coordinates"][0]
    hole = [[2.0, 2.0], [2.0, 8.0], [8.0, 8.0], [8.0, 2.0], [2.0, 2.0]]
    with_hole = {"type": "Polygon", "coordinates": [ring, hole]}
    assert geo.contains(with_hole, 1.0, 1.0)
    assert not geo.contains(with_hole, 5.0, 5.0)


def test_simplification_keeps_the_shape_and_drops_the_noise():
    # A straight edge sampled at 200 points plus three corners.
    noisy = {
        "type": "Polygon",
        "coordinates": [[
            *[[90.0 + i / 200, 24.0] for i in range(200)],
            [91.0, 25.0], [90.0, 25.0], [90.0, 24.0],
        ]],
    }
    reduced = geo.simplify(noisy, 0.01)
    assert geo.count_points(reduced) < geo.count_points(noisy) / 4
    # Still a closed ring, and still enclosing the same middle.
    assert reduced["coordinates"][0][0] == reduced["coordinates"][0][-1]
    assert geo.contains(reduced, 24.5, 90.5)


def test_simplification_never_destroys_a_ring():
    reduced = geo.simplify(square(90.0, 24.0), 100.0)
    assert geo.count_points(reduced) >= 4


def test_a_long_near_straight_ring_does_not_blow_the_stack():
    """Recursion on a few thousand collinear points is exactly the crash case."""
    ring = [[90.0 + i / 5000, 24.0 + (i % 2) * 1e-9] for i in range(5000)]
    ring.append([91.0, 25.0])
    ring.append(ring[0])
    reduced = geo.simplify({"type": "Polygon", "coordinates": [ring]}, 0.0001)
    assert geo.count_points(reduced) < len(ring)


def test_a_swapped_coordinate_pair_is_refused():
    """A latitude in the longitude slot is the usual GeoJSON mistake."""
    with pytest.raises(geo.InvalidGeometry) as caught:
        geo.validate({"type": "Polygon",
                      "coordinates": [[[24.0, 200.0], [25.0, 200.0],
                                       [25.0, 201.0], [24.0, 200.0]]]})
    assert "longitude, latitude" in str(caught.value)


def test_a_line_is_not_an_area():
    with pytest.raises(geo.InvalidGeometry):
        geo.validate({"type": "LineString", "coordinates": [[90, 24], [91, 25]]})


def test_bounding_boxes_answer_overlap_without_touching_geometry():
    a = geo.bounds(square(90.0, 24.0))
    b = geo.bounds(square(90.5, 24.5))
    c = geo.bounds(square(95.0, 24.0))
    assert a.intersects(b)
    assert not a.intersects(c)


# ==========================================================================
# The layer (items: polygon rendering, data structure)
# ==========================================================================


def test_areas_are_returned_as_a_geojson_feature_collection(client):
    token = login(client)
    body = fetch(client, token)

    assert body["type"] == "FeatureCollection"
    assert codes(body) == {"UPZ-TRI", "UPZ-BHA", "UPZ-KHU"}
    feature = next(f for f in body["features"] if f["id"] == "UPZ-TRI")
    assert feature["type"] == "Feature"
    assert feature["geometry"]["type"] == "Polygon"
    assert len(feature["geometry"]["coordinates"][0]) >= 4


def test_every_documented_property_is_present(client):
    """The specification's data structure, on every feature."""
    token = login(client)
    feature = next(f for f in fetch(client, token)["features"]
                   if f["id"] == "UPZ-TRI")
    properties = feature["properties"]

    for name in ("upazila_id", "upazila_code", "upazila_name", "district_id",
                 "district_name", "division_id", "division_name",
                 "latitude", "longitude", "stock"):
        assert name in properties, name

    assert properties["upazila_name"] == "Trishal"
    assert properties["district_name"] == "Mymensingh"
    assert properties["division_name"] == "Dhaka"


def test_an_area_is_a_polygon_not_a_point(client):
    token = login(client)
    for feature in fetch(client, token)["features"]:
        assert feature["geometry"]["type"] in ("Polygon", "MultiPolygon")
        assert feature["bbox"] and len(feature["bbox"]) == 4


def test_geometry_can_be_left_out_when_only_properties_are_wanted(client):
    token = login(client)
    body = fetch(client, token, "geometry=false")
    assert all(f["geometry"] is None for f in body["features"])
    # …and the properties still arrive, so a list view costs no geometry.
    assert all(f["properties"]["upazila_name"] for f in body["features"])


# ==========================================================================
# Stock (items: stock visualisation, default value)
# ==========================================================================


def test_stock_defaults_to_the_configured_value(client):
    """Default of 1, from configuration — and labelled as a default."""
    from app.config import get_settings

    token = login(client)
    feature = next(f for f in fetch(client, token)["features"]
                   if f["id"] == "UPZ-TRI")

    assert feature["properties"]["stock"] == get_settings().map_area_default_stock
    assert feature["properties"]["stock"] == 1
    assert feature["properties"]["stock_source"] == "default"


def test_the_default_is_configuration_not_a_constant(client, monkeypatch):
    """Changing the setting changes the answer, which is what "not hardcoded"
    has to mean in practice."""
    import dataclasses

    import app.map.areas as areas_module
    from app.config import get_settings

    changed = dataclasses.replace(get_settings(), map_area_default_stock=7)
    monkeypatch.setattr(areas_module, "get_settings", lambda: changed)

    token = login(client)
    feature = fetch(client, token)["features"][0]
    assert feature["properties"]["stock"] == 7


def test_stock_is_not_attributable_to_an_area_under_the_material_model(
    client, geography
):
    """Not even a placed entity can make stock an area's stock any more.

    The old model held stock per warehouse, and a warehouse had coordinates, so
    the chain warehouse -> point -> upazila produced a real figure. Revision
    0020 removed that dimension outright, and material stock is held per plant,
    storage location and material — none of which states a coordinate, an area,
    or any organisational code to join on.

    So the bridge is gone twice over: there is no warehouse to place, and
    placing something that *can* be placed does not help either, because a stock
    position names nothing that reaches it. Every area falls back to the
    configured default and says ``default`` — which is the point of carrying
    ``stock_source`` at all.
    """
    with Session(geography) as session:
        session.add(MapEntityLocation(
            entity_type="customer", entity_code="CUST-01",
            latitude=24.5, longitude=90.5,          # inside UPZ-TRI
            source=GeoSource.MANUAL, precision=GeoPrecision.EXACT,
        ))
        session.commit()

    token = login(client)
    body = fetch(client, token)
    trishal = next(f for f in body["features"] if f["id"] == "UPZ-TRI")
    bhaluka = next(f for f in body["features"] if f["id"] == "UPZ-BHA")

    for feature in (trishal, bhaluka):
        assert feature["properties"]["stock_source"] == "default"
        assert feature["properties"]["stock"] == 1      # the configured default


def test_the_response_reports_which_default_is_in_use(client):
    token = login(client)
    assert fetch(client, token)["default_stock"] == 1


# ==========================================================================
# Colour and configuration (items: default colour, do not hardcode)
# ==========================================================================


def test_the_style_is_blue_at_the_documented_defaults(client):
    """The colour is the documented constant on every level; the weight is not.

    ``0010`` seeded three levels with one identical style, which was right while
    exactly one was ever on screen. ``0017`` made them drawable together and
    re-weighted them: the same blue everywhere — that is the documented
    requirement, and the upazila stroke keeps it — with broader boundaries drawn
    heavier, and a fill on the deepest level alone so stacked layers tint the
    map once rather than four times.
    """
    token = login(client)
    style = fetch(client, token)["style"]

    assert style["fill_color"].upper() == "#2563EB"
    assert style["stroke_color"].upper() == "#2563EB"
    # Upazila is the default level, and the only one that carries a fill.
    assert style["fill_opacity"] == pytest.approx(0.18)
    assert style["stroke_width"] == pytest.approx(0.75)


@pytest.mark.parametrize(
    "level, stroke_width, fill_opacity",
    [("country", 3.0, 0.0), ("division", 2.0, 0.0), ("district", 1.25, 0.0),
     ("upazila", 0.75, 0.18)],
)
def test_every_level_is_the_same_blue_at_its_own_weight(
        client, level, stroke_width, fill_opacity):
    """Broader boundaries draw heavier, and only the deepest one is filled."""
    token = login(client)
    body = client.get("/api/map/area-styles", headers=auth(token)).json()
    style = body["styles"][level]

    assert style["fill_color"].upper() == "#2563EB"
    assert style["stroke_color"].upper() == "#2563EB"
    assert style["stroke_width"] == pytest.approx(stroke_width)
    assert style["fill_opacity"] == pytest.approx(fill_opacity)


def test_the_style_comes_from_the_database_and_can_be_changed(client, geography):
    token = login(client)
    response = client.put("/api/map/area-styles/upazila", headers=auth(token),
                          json={"fill_color": "#16A34A", "fill_opacity": 0.4,
                                "stroke_color": "#166534", "stroke_width": 3})
    assert response.status_code == 200, response.text

    style = fetch(client, token)["style"]
    assert style["fill_color"] == "#16A34A"
    assert style["fill_opacity"] == pytest.approx(0.4)
    assert style["stroke_width"] == pytest.approx(3)
    assert style["is_system_default"] is False


def test_a_named_colour_is_refused(client):
    """"blue" renders differently across the three places this value is used."""
    token = login(client)
    response = client.put("/api/map/area-styles/upazila", headers=auth(token),
                          json={"fill_color": "blue", "fill_opacity": 0.2,
                                "stroke_color": "#2563EB", "stroke_width": 1.5})
    assert response.status_code == 422
    assert "hex colour" in response.json()["detail"]


def test_changing_the_style_needs_map_settings(client):
    admin = login(client)
    user_id = client.get("/api/admin/users", headers=auth(admin),
                         params={"search": "dhaka_rm"}).json()["users"][0]["user_id"]
    client.put(f"/api/admin/users/{user_id}/permissions", headers=auth(admin),
               json={"permissions": {SectionKey.MAP: ALLOW,
                                     SectionKey.MAP_SETTINGS: DENY}})

    token = login(client, "dhaka_rm")
    assert client.put("/api/map/area-styles/upazila", headers=auth(token),
                      json={"fill_color": "#000000", "fill_opacity": 0.2,
                            "stroke_color": "#000000",
                            "stroke_width": 1}).status_code == 403


def test_future_colour_rules_are_declared_and_not_evaluated(client, geography):
    """Architected, not implemented — the same posture as marker conditions."""
    with Session(geography) as session:
        row = session.query(MapAreaStyle).filter_by(entity_type="upazila").one()
        assert row.rules is None
        assert hasattr(row, "rules")

    token = login(client)
    # Nothing in the response varies the colour by value.
    body = fetch(client, token)
    assert "rules" in body["style"]
    assert body["style"]["rules"] is None


# ==========================================================================
# Filters (item: filter integration)
# ==========================================================================


def test_a_district_filter_shows_only_that_districts_upazilas(client):
    token = login(client)
    body = fetch(client, token, "district_code=DIS-MYM")
    assert codes(body) == {"UPZ-TRI", "UPZ-BHA"}


def test_a_division_filter_narrows_to_its_districts(client):
    token = login(client)
    assert codes(fetch(client, token, "division_code=DIV-KHU")) == {"UPZ-KHU"}


def test_an_upazila_filter_selects_the_one(client):
    token = login(client)
    assert codes(fetch(client, token, "upazila_code=UPZ-TRI")) == {"UPZ-TRI"}


def test_filters_intersect_rather_than_widen(client):
    """Khulna Sadar is not in Mymensingh, so the pair selects nothing."""
    token = login(client)
    body = fetch(client, token, "district_code=DIS-MYM&upazila_code=UPZ-KHU")
    assert body["features"] == []


def test_a_territory_filter_shows_the_areas_containing_it(client, geography):
    """The geographic relationship: whichever polygon holds the coordinate."""
    with Session(geography) as session:
        session.add(MapEntityLocation(
            entity_type="territory", entity_code="TR001",
            latitude=24.5, longitude=90.5,          # inside UPZ-TRI
            source=GeoSource.MANUAL, precision=GeoPrecision.EXACT,
        ))
        session.commit()

    token = login(client)
    assert codes(fetch(client, token, "territory_code=TR001")) == {"UPZ-TRI"}


def test_a_territory_with_no_coordinate_is_reported_not_silently_dropped(client):
    token = login(client)
    body = fetch(client, token, "territory_code=TR001")
    assert body["features"] == []
    assert body["unresolved_territories"] == ["TR001"]


def test_the_viewport_excludes_areas_off_screen(client):
    """Performance: an area outside the box is never fetched."""
    token = login(client)
    # A box over Khulna only.
    body = fetch(client, token, "bbox=88.5,21.5,90.0,23.5")
    assert codes(body) == {"UPZ-KHU"}


def test_an_unknown_level_is_refused(client):
    token = login(client)
    response = client.get(f"/api/map/areas?{WINDOW}&level=galaxy",
                          headers=auth(token))
    assert response.status_code == 422
    assert "galaxy" in response.json()["detail"]


# ==========================================================================
# Layer control and legend
# ==========================================================================


def test_the_config_advertises_the_layer_and_its_levels(client):
    token = login(client)
    body = client.get("/api/map/config", headers=auth(token)).json()
    areas = body["administrative_areas"]

    assert areas["layer_key"] == "admin_area"
    assert areas["default_level"] == "upazila"
    # Broadest first. ``country`` joined the list in 0017, which gave the
    # national outline a level of its own to hang a boundary and a style on.
    assert [level["key"] for level in areas["levels"]] == [
        "country", "division", "district", "upazila"]
    assert areas["has_boundaries"] is True
    assert areas["default_stock"] == 1


def test_the_legend_carries_the_configured_colour(client):
    token = login(client)
    client.put("/api/map/area-styles/upazila", headers=auth(token),
               json={"fill_color": "#123456", "fill_opacity": 0.3,
                     "stroke_color": "#123456", "stroke_width": 2})

    legend = client.get("/api/map/legend", headers=auth(token)).json()
    entry = next(e for e in legend["area_entries"]
                 if e["entity_type"] == "upazila")
    assert entry["label"] == "Administrative Area — Upazila"
    assert entry["style"]["fill_color"] == "#123456"


def test_coverage_reports_how_many_areas_have_a_boundary(client):
    token = login(client)
    body = client.get("/api/map/area-styles", headers=auth(token)).json()
    upazila = next(row for row in body["coverage"] if row["level"] == "upazila")
    assert upazila["total"] == 3
    assert upazila["with_boundary"] == 3
    district = next(row for row in body["coverage"] if row["level"] == "district")
    assert district["missing_boundary"] == 2


# ==========================================================================
# Click detail (item: click behaviour)
# ==========================================================================


def test_the_detail_endpoint_answers_the_popup(client):
    token = login(client)
    body = client.get(f"/api/map/areas/upazila/UPZ-TRI?{WINDOW}",
                      headers=auth(token)).json()

    assert body["properties"]["upazila_name"] == "Trishal"
    assert body["properties"]["district_name"] == "Mymensingh"
    assert body["properties"]["division_name"] == "Dhaka"
    assert body["properties"]["stock"] == 1
    assert body["style"]["fill_color"].upper() == "#2563EB"


def test_optional_metrics_appear_only_when_the_data_supports_them(client,
                                                                  geography):
    """A metric that cannot be computed is absent, never reported as zero.

    There are no optional metrics left to report. ``warehouse_count`` was the
    one, and it worked only because a warehouse was placed on the map and so
    could be located inside a boundary; revision 0020 removed the dimension, and
    a plant and a storage location — which is where stock sits now — are named
    by the material masters and placed nowhere.

    Placing something that *can* be placed does not bring a count back either:
    a customer has no district and nothing counts it, so the honest answer stays
    "absent". That is the property worth pinning — an area that cannot support a
    figure must not carry a zero, which would read as "none here" rather than
    "not known".
    """
    token = login(client)
    before = client.get(f"/api/map/areas/upazila/UPZ-TRI?{WINDOW}",
                        headers=auth(token)).json()
    assert "warehouse_count" not in before["properties"]

    with Session(geography) as session:
        session.add(MapEntityLocation(
            entity_type="customer", entity_code="CUST-09",
            latitude=24.5, longitude=90.5,
            source=GeoSource.MANUAL, precision=GeoPrecision.EXACT,
        ))
        session.commit()

    after = client.get(f"/api/map/areas/upazila/UPZ-TRI?{WINDOW}",
                       headers=auth(token)).json()
    assert "warehouse_count" not in after["properties"]
    # Nor does placing a customer invent a per-entity count of its own.
    # (`point_count` is a different thing and stays: it counts what is placed
    # inside the boundary, which is measured rather than attributed.)
    assert "customer_count" not in after["properties"]


def test_an_unknown_area_is_a_404(client):
    token = login(client)
    assert client.get(f"/api/map/areas/upazila/NOPE?{WINDOW}",
                      headers=auth(token)).status_code == 404


# ==========================================================================
# Security and performance
# ==========================================================================


def test_the_map_section_is_required(client):
    admin = login(client)
    user_id = client.get("/api/admin/users", headers=auth(admin),
                         params={"search": "dhaka_rm"}).json()["users"][0]["user_id"]
    client.put(f"/api/admin/users/{user_id}/permissions", headers=auth(admin),
               json={"permissions": {SectionKey.MAP: DENY}})

    token = login(client, "dhaka_rm")
    assert client.get(f"/api/map/areas?{WINDOW}",
                      headers=auth(token)).status_code == 403


def test_an_anonymous_caller_is_refused(client):
    assert client.get(f"/api/map/areas?{WINDOW}").status_code == 401


def test_an_unchanged_layer_answers_304(client):
    """Boundaries change only on import, so a browser should keep what it has."""
    token = login(client)
    first = client.get(f"/api/map/areas?{WINDOW}", headers=auth(token))
    etag = first.headers["etag"]
    assert etag

    again = client.get(f"/api/map/areas?{WINDOW}",
                       headers={**auth(token), "If-None-Match": etag})
    assert again.status_code == 304


def test_the_etag_changes_when_a_boundary_changes(client, geography):
    token = login(client)
    etag = client.get(f"/api/map/areas?{WINDOW}", headers=auth(token)).headers["etag"]

    with Session(geography) as session:
        row = session.query(MapAreaBoundary).filter_by(entity_code="UPZ-TRI").one()
        session.delete(row)
        session.commit()

    after = client.get(f"/api/map/areas?{WINDOW}",
                       headers={**auth(token), "If-None-Match": etag})
    assert after.status_code == 200


def test_the_cache_header_keeps_the_payload_private(client):
    """The response depends on the caller's scope, so no shared cache may hold it."""
    token = login(client)
    response = client.get(f"/api/map/areas?{WINDOW}", headers=auth(token))
    assert "private" in response.headers["cache-control"]


def test_the_layer_says_so_when_no_boundary_has_been_imported(client, geography):
    with Session(geography) as session:
        session.query(MapAreaBoundary).delete()
        session.commit()

    token = login(client)
    body = fetch(client, token)
    assert body["boundaries_loaded"] is False
    assert body["features"] == []


def test_area_geometry_can_be_simplified_further_on_request(client):
    token = login(client)
    detailed = fetch(client, token)
    coarse = fetch(client, token, "simplify=0.5")

    def vertices(payload: dict) -> int:
        return sum(len(f["geometry"]["coordinates"][0]) for f in payload["features"])

    assert vertices(coarse) <= vertices(detailed)


# ==========================================================================
# The two hierarchies stay separate
# ==========================================================================


def test_administrative_areas_do_not_disturb_the_organisational_layers(client):
    """The existing territory → customer behaviour must be untouched."""
    token = login(client)
    entities = client.get(f"/api/map/entities?{WINDOW}&territory_code=TR001",
                          headers=auth(token))
    assert entities.status_code == 200
    body = entities.json()
    assert {"territory"} <= {e["type"] for e in body["entities"]}


def test_the_admin_dimensions_are_managed_like_any_other_master_data(client):
    """Division, district and upazila appear in the Data Management catalogue."""
    token = login(client)
    body = client.get("/api/data-management/catalogue", headers=auth(token)).json()
    master = {
        entity["key"]
        for group in body["groups"] if group["key"] == "MASTER"
        for entity in group["entities"]
    }
    assert {"dim_division", "dim_district", "dim_upazila"} <= master
