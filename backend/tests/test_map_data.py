"""The Multi-Level Geo Business Map: coordinates, aggregation, scope, drill-down.

The seeded warehouse has two regions (Dhaka REG001, Khulna REG002) with sales in
both, so scope enforcement and drill-down are testable against real facts rather
than fixtures invented for the map.
"""

from __future__ import annotations

import csv
import io
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.auth.security import hash_password
from app.config import get_settings
from app.database.models_ai import AppUser, AuditAction, AuditLog, Role, UserStatus
from app.database.models_map import GeoPrecision, GeoSource, MapEntityLocation
from app.main import app
from app.map import geo, resolver, service
from app.map.data import DRILL_PATH, METRIC_BY_KEY
from app.security.sections import ALLOW, DENY, SectionKey

pytest.importorskip("multipart", reason="python-multipart is required by FastAPI forms")

PASSWORD = "Correct-Horse-9"
WINDOW = "date_from=2026-08-01&date_to=2026-08-31"

#: Real positions, so a wrong projection would be visible rather than plausible.
DHAKA = (23.7808, 90.4008)
KHULNA = (22.8456, 89.5403)


@pytest.fixture
def map_client(agent_engine, users, monkeypatch):
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
        service.ensure_system_defaults(session)
        session.commit()
    resolver.invalidate_cache()

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


def place(client: TestClient, token: str, *locations, derive: bool = True):
    return client.put("/api/map/locations", headers=auth(token), json={
        "locations": [
            {"entity_type": e, "entity_code": c, "latitude": lat, "longitude": lon}
            for e, c, lat, lon in locations
        ],
        "derive_parents": derive,
    })


def place_regions(client: TestClient, token: str):
    return place(client, token,
                 ("region", "REG001", *DHAKA), ("region", "REG002", *KHULNA))


# ==========================================================================
# Configuration and levels
# ==========================================================================


def test_map_config_reports_the_basemap_and_coverage(map_client):
    token = login(map_client)
    body = map_client.get("/api/map/config", headers=auth(token)).json()

    assert body["basemap"]["engine"] == "maplibre-gl"
    assert body["basemap"]["style_url"].startswith("http")
    assert body["basemap"]["style_url_dark"].startswith("http")
    assert body["basemap"]["attribution"]

    assert body["has_locations"] is False
    assert [level["key"] for level in body["levels"]] == list(DRILL_PATH)
    metrics = {metric["key"] for metric in body["metrics"]}
    assert metrics >= {"net_sales", "quantity", "target_amount"}
    # Collection and Outstanding went with their datasets in revision 0020, and
    # stock has no geography to plot against under the material model — neither
    # has a measure set to aggregate or a view to read.
    assert metrics.isdisjoint(
        {"collection_amount", "outstanding_amount", "overdue_amount", "stock"})

    coverage = {row["entity_type"]: row for row in body["coverage"]}
    assert coverage["region"]["total"] >= 2
    assert coverage["region"]["placed"] == 0


def test_the_map_needs_no_api_key_of_any_kind(map_client):
    """The reason the map moved to MapLibre and OpenFreeMap.

    Nothing key-shaped may reach the browser from this endpoint. The previous
    implementation served a Google Maps browser key here — public by design, and
    only ever protected by an HTTP-referrer restriction somebody had to remember
    to configure. Neither MapLibre nor OpenFreeMap authenticates, so there is no
    longer a credential to leak, and this asserts that none crept back.
    """
    token = login(map_client)
    body = map_client.get("/api/map/config", headers=auth(token)).json()

    assert "google_maps" not in body
    assert "renderer" not in body
    serialised = json.dumps(body).lower()
    for forbidden in ("api_key", "apikey", "access_token", "map_id"):
        assert forbidden not in serialised, forbidden


def test_the_basemap_style_is_configurable(map_client, monkeypatch):
    """A deployment with no route to OpenFreeMap can point at its own server.

    The style URL is patched on the settings object rather than through the
    environment: ``Settings`` reads ``os.getenv`` in its field defaults, so a
    variable set after import is never seen — which is exactly the trap this
    test would otherwise fall into silently by passing for the wrong reason.
    """
    import dataclasses

    from app.api import routes_map

    patched = dataclasses.replace(
        get_settings(), map_basemap_style_url="https://tiles.internal/styles/house"
    )
    monkeypatch.setattr(routes_map, "get_settings", lambda: patched)

    token = login(map_client)
    body = map_client.get("/api/map/config", headers=auth(token)).json()
    assert body["basemap"]["style_url"] == "https://tiles.internal/styles/house"


def test_levels_form_a_drill_path(map_client):
    token = login(map_client)
    levels = map_client.get("/api/map/levels", headers=auth(token)).json()["levels"]
    assert levels[0]["parent"] is None
    assert levels[-1]["child"] is None
    for index, level in enumerate(levels[:-1]):
        assert level["child"] == levels[index + 1]["key"]


# ==========================================================================
# Coordinates
# ==========================================================================


def test_place_an_entity_and_read_it_back(map_client):
    token = login(map_client)
    response = place(map_client, token, ("region", "REG001", *DHAKA))
    assert response.status_code == 200
    assert response.json()["saved"] == 1

    body = map_client.get("/api/map/locations", headers=auth(token),
                          params={"entity_type": "region"}).json()
    location = body["locations"][0]
    assert location["entity_code"] == "REG001"
    assert (location["latitude"], location["longitude"]) == DHAKA
    assert location["source"] == GeoSource.MANUAL


@pytest.mark.parametrize("latitude,longitude,why", [
    (0, 0, "null island"),
    (95, 90, "latitude out of range"),
    (23, 200, "longitude out of range"),
])
def test_an_impossible_coordinate_is_refused(map_client, latitude, longitude, why):
    token = login(map_client)
    body = place(map_client, token, ("region", "REG001", latitude, longitude)).json()
    assert body["saved"] == 0, why
    assert body["problems"]


def test_placing_territories_derives_every_level_above(map_client, agent_engine):
    """Place the level where an address exists; the rest follows."""
    token = login(map_client)
    response = place(map_client, token, ("territory", "TR001", *DHAKA))
    assert response.status_code == 200
    derived = response.json()["derived"]
    # unit -> area -> region -> zone -> sales line -> bu -> company
    assert {"unit", "area", "region", "zone"} <= set(derived)

    with Session(agent_engine) as session:
        region = session.execute(
            select(MapEntityLocation).where(
                MapEntityLocation.entity_type == "region",
                MapEntityLocation.entity_code == "REG001")
        ).scalar_one()
    assert region.source == GeoSource.DERIVED
    assert region.precision == GeoPrecision.CENTROID
    assert region.derived_from == 1
    assert round(region.latitude, 3) == round(DHAKA[0], 3)


def test_a_hand_placed_coordinate_survives_re_derivation(map_client, agent_engine):
    """A derived centroid must never overwrite a decision someone made."""
    token = login(map_client)
    place(map_client, token, ("territory", "TR001", *DHAKA), derive=False)
    place(map_client, token, ("region", "REG001", 24.0, 91.0), derive=False)

    response = map_client.post("/api/map/locations/derive", headers=auth(token))
    assert response.status_code == 200

    with Session(agent_engine) as session:
        region = session.execute(
            select(MapEntityLocation).where(
                MapEntityLocation.entity_type == "region",
                MapEntityLocation.entity_code == "REG001")
        ).scalar_one()
    assert (region.latitude, region.longitude) == (24.0, 91.0)
    assert region.source == GeoSource.MANUAL


def test_a_coordinate_can_be_removed(map_client):
    token = login(map_client)
    place(map_client, token, ("region", "REG001", *DHAKA), derive=False)
    assert map_client.request(
        "DELETE", "/api/map/locations/region/REG001", headers=auth(token)
    ).status_code == 200
    assert map_client.request(
        "DELETE", "/api/map/locations/region/REG001", headers=auth(token)
    ).status_code == 404


def test_placing_a_location_is_audited(map_client, agent_engine):
    token = login(map_client)
    place(map_client, token, ("region", "REG001", *DHAKA))
    with Session(agent_engine) as session:
        entry = session.execute(
            select(AuditLog).where(AuditLog.action == AuditAction.MAP_LOCATION_UPDATED)
        ).scalars().first()
    assert entry is not None and entry.detail["saved"] == 1


# ==========================================================================
# Map data
# ==========================================================================


def test_map_data_plots_placed_entities_with_their_measures(map_client):
    token = login(map_client)
    place_regions(map_client, token)

    body = map_client.get(f"/api/map/data?{WINDOW}&level=region&metric=net_sales",
                          headers=auth(token)).json()
    assert body["level"] == "region"
    assert body["metric_label"] == "Net Sales"
    codes = {point["code"] for point in body["points"]}
    assert codes == {"REG001", "REG002"}

    dhaka = next(p for p in body["points"] if p["code"] == "REG001")
    assert dhaka["latitude"] == DHAKA[0]
    assert dhaka["value"] > 0
    assert dhaka["measures"]["net_sales"] == dhaka["value"]
    assert body["totals"]["plotted"] == 2
    assert body["totals"]["total_value"] > 0


def test_map_data_carries_the_marker_from_the_designer(map_client):
    token = login(map_client)
    place_regions(map_client, token)
    body = map_client.get(f"/api/map/data?{WINDOW}&level=region",
                          headers=auth(token)).json()
    assert body["marker"]["design_name"] == "Region Default"
    assert body["marker"]["preview_svg"].startswith("<svg")


def test_changing_the_marker_design_changes_the_map(map_client):
    """The map consumes marker configuration; it does not embed it."""
    token = login(map_client)
    place_regions(map_client, token)

    created = map_client.post("/api/map/marker-designs", headers=auth(token), json={
        "name": "Map Region Marker", "entity_type": "region",
        "definition": {"shape": {"kind": "builtin", "builtin": "star",
                                 "fill": "#EAB308"}}}).json()
    map_client.post(f"/api/map/marker-designs/{created['design_id']}/assign",
                    headers=auth(token), json={})

    body = map_client.get(f"/api/map/data?{WINDOW}&level=region",
                          headers=auth(token)).json()
    assert body["marker"]["design_name"] == "Map Region Marker"
    assert "#EAB308" in body["marker"]["preview_svg"]


def test_bounds_enclose_the_plotted_points(map_client):
    token = login(map_client)
    place_regions(map_client, token)
    bounds = map_client.get(f"/api/map/data?{WINDOW}&level=region",
                            headers=auth(token)).json()["bounds"]
    assert bounds["north"] >= DHAKA[0]
    assert bounds["south"] <= KHULNA[0]
    assert bounds["west"] <= KHULNA[1]
    assert bounds["east"] >= DHAKA[1]
    assert KHULNA[0] <= bounds["centre"]["latitude"] <= DHAKA[0]


def test_entities_with_data_but_no_coordinate_are_reported_not_hidden(map_client):
    """A sparse map must say why it is sparse."""
    token = login(map_client)
    place(map_client, token, ("region", "REG001", *DHAKA), derive=False)

    body = map_client.get(f"/api/map/data?{WINDOW}&level=region",
                          headers=auth(token)).json()
    assert [p["code"] for p in body["points"]] == ["REG001"]
    unplaced = {u["code"]: u for u in body["unplaced"]}
    assert "REG002" in unplaced
    assert unplaced["REG002"]["reason"] == "no_coordinate"
    assert unplaced["REG002"]["value"] > 0
    # The unplaced value is still counted, so the total is honest.
    assert body["totals"]["total_value"] > body["points"][0]["value"]


def test_a_map_with_no_coordinates_returns_empty_rather_than_failing(map_client):
    token = login(map_client)
    body = map_client.get(f"/api/map/data?{WINDOW}&level=region",
                          headers=auth(token)).json()
    assert body["points"] == []
    assert body["bounds"] is None
    assert len(body["unplaced"]) >= 2


def test_every_metric_can_be_plotted(map_client):
    token = login(map_client)
    place_regions(map_client, token)
    for metric in METRIC_BY_KEY:
        response = map_client.get(
            f"/api/map/data?{WINDOW}&level=region&metric={metric}",
            headers=auth(token))
        assert response.status_code == 200, metric
        assert response.json()["metric"] == metric


def test_an_unknown_level_or_metric_is_refused(map_client):
    token = login(map_client)
    assert map_client.get(f"/api/map/data?{WINDOW}&level=galaxy",
                          headers=auth(token)).status_code == 422
    assert map_client.get(f"/api/map/data?{WINDOW}&level=region&metric=vibes",
                          headers=auth(token)).status_code == 422


# ==========================================================================
# Drill-down
# ==========================================================================


def test_drilling_from_region_to_area_narrows_the_map(map_client):
    token = login(map_client)
    place(map_client, token, ("territory", "TR001", *DHAKA),
          ("area", "AR002", *KHULNA))

    regions = map_client.get(f"/api/map/data?{WINDOW}&level=region",
                             headers=auth(token)).json()
    assert len(regions["points"]) == 2

    # Clicking Dhaka drills to its areas — the filter is the parent's code.
    areas = map_client.get(
        f"/api/map/data?{WINDOW}&level=area&region_code=REG001",
        headers=auth(token)).json()
    assert {p["code"] for p in areas["points"]} == {"AR001"}


def test_drilling_to_territory_works(map_client):
    token = login(map_client)
    place(map_client, token, ("territory", "TR001", *DHAKA))
    body = map_client.get(
        f"/api/map/data?{WINDOW}&level=territory&area_code=AR001",
        headers=auth(token)).json()
    assert [p["code"] for p in body["points"]] == ["TR001"]


# ==========================================================================
# Data scope
# ==========================================================================


def test_a_scoped_user_sees_only_their_own_region(map_client):
    """The map is a report: the same scope applies."""
    admin = login(map_client)
    place_regions(map_client, admin)

    # dhaka_rm is scoped to REG001.
    token = login(map_client, "dhaka_rm")
    body = map_client.get(f"/api/map/data?{WINDOW}&level=region",
                          headers=auth(token)).json()
    assert [p["code"] for p in body["points"]] == ["REG001"]
    assert body["scope_description"].startswith("region")


def test_a_scoped_user_cannot_plot_another_region_by_asking(map_client):
    admin = login(map_client)
    place_regions(map_client, admin)

    token = login(map_client, "dhaka_rm")
    response = map_client.get(
        f"/api/map/data?{WINDOW}&level=region&region_code=REG002",
        headers=auth(token))
    assert response.status_code == 403
    assert "REG002" in response.json()["detail"]


def test_a_user_with_no_scope_is_refused(map_client):
    token = login(map_client, "no_scope")
    response = map_client.get(f"/api/map/data?{WINDOW}&level=region",
                              headers=auth(token))
    assert response.status_code == 403


def test_management_sees_every_region(map_client):
    admin = login(map_client)
    place_regions(map_client, admin)
    token = login(map_client, "ceo")
    body = map_client.get(f"/api/map/data?{WINDOW}&level=region",
                          headers=auth(token)).json()
    assert {p["code"] for p in body["points"]} == {"REG001", "REG002"}


# ==========================================================================
# Clustering
# ==========================================================================


def test_clustering_can_be_forced_and_conserves_every_point(map_client):
    token = login(map_client)
    place_regions(map_client, token)
    body = map_client.get(
        f"/api/map/data?{WINDOW}&level=region&cluster=true&zoom=12",
        headers=auth(token)).json()
    assert body["clustered"] is True
    assert body["cluster_zoom"] == 12
    # Nothing may be lost or double-counted by clustering.
    assert sum(c["count"] for c in body["clusters"]) == body["totals"]["plotted"]
    assert sum(c["value"] for c in body["clusters"]) == pytest.approx(
        sum(p["value"] for p in body["points"]))
    # At street zoom, Dhaka and Khulna (~120 km apart) are separate.
    assert len(body["clusters"]) == 2


def test_points_far_below_the_threshold_are_not_clustered(map_client):
    token = login(map_client)
    place_regions(map_client, token)
    body = map_client.get(f"/api/map/data?{WINDOW}&level=region",
                          headers=auth(token)).json()
    assert body["clustered"] is False
    assert "clusters" not in body


def test_cluster_cell_size_shrinks_as_the_map_zooms_in():
    """A fixed cell would be wrong at both ends of the zoom range."""
    from app.map.data import cluster_grid

    assert cluster_grid(4) > cluster_grid(10) > cluster_grid(16)
    # Each zoom level halves the cell, matching how slippy maps scale.
    assert cluster_grid(10) == pytest.approx(cluster_grid(11) * 2)
    # Out-of-range zooms are clamped rather than producing absurd cells.
    assert cluster_grid(-5) == cluster_grid(1)
    assert cluster_grid(99) == cluster_grid(20)


def test_clustering_merges_by_screen_distance_not_by_degrees():
    """Two points a few metres apart merge; two 200 km apart do not."""
    from app.map.data import MapPoint, _cluster

    near = [
        MapPoint("A", "A", 23.78, 90.40, 1, {}, "MANUAL", "EXACT"),
        MapPoint("B", "B", 23.781, 90.401, 1, {}, "MANUAL", "EXACT"),
    ]
    far = [
        MapPoint("C", "C", 23.78, 90.40, 1, {}, "MANUAL", "EXACT"),
        MapPoint("D", "D", 25.50, 92.00, 1, {}, "MANUAL", "EXACT"),
    ]
    assert len(_cluster(near, zoom=12)) == 1
    assert len(_cluster(far, zoom=12)) == 2
    # Zoom far enough out and even distant points share a pixel — as they should.
    assert len(_cluster(far, zoom=2)) == 1


# ==========================================================================
# RBAC
# ==========================================================================


def test_the_map_needs_the_map_section(map_client, agent_engine):
    admin = login(map_client)
    with Session(agent_engine) as session:
        user_id = session.execute(
            select(AppUser.user_id).where(AppUser.username == "dhaka_rm")
        ).scalar_one()
    map_client.put(f"/api/admin/users/{user_id}/permissions", headers=auth(admin),
                   json={"permissions": {SectionKey.MAP: DENY}})

    token = login(map_client, "dhaka_rm")
    for path in (f"/api/map/data?{WINDOW}", "/api/map/config", "/api/map/levels"):
        assert map_client.get(path, headers=auth(token)).status_code == 403, path


def test_coordinates_are_managed_under_map_settings_not_the_map(map_client):
    """Seeing the map is not permission to move things on it."""
    token = login(map_client, "dhaka_rm")
    assert map_client.get("/api/map/config", headers=auth(token)).status_code == 200
    assert map_client.get("/api/map/locations", headers=auth(token)).status_code == 403
    assert place(map_client, token, ("region", "REG001", *DHAKA)).status_code == 403


def test_an_anonymous_caller_is_refused(map_client):
    assert map_client.get(f"/api/map/data?{WINDOW}").status_code == 401
    assert map_client.get("/api/map/config").status_code == 401


def test_marker_config_is_readable_by_a_map_viewer_without_the_dashboard(
    map_client, agent_engine,
):
    admin = login(map_client)
    with Session(agent_engine) as session:
        user_id = session.execute(
            select(AppUser.user_id).where(AppUser.username == "dhaka_rm")
        ).scalar_one()
    map_client.put(f"/api/admin/users/{user_id}/permissions", headers=auth(admin),
                   json={"permissions": {SectionKey.DASHBOARD: DENY,
                                         SectionKey.MAP: ALLOW}})
    token = login(map_client, "dhaka_rm")
    assert map_client.get("/api/map/marker-config",
                          headers=auth(token)).status_code == 200
    assert map_client.get("/api/map/legend", headers=auth(token)).status_code == 200


# ==========================================================================
# Coordinates through the Data Upload Center
# ==========================================================================


def csv_bytes(rows: list[list[object]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["Entity Type", "Entity Code", "Latitude", "Longitude", "Label"])
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def upload_locations(client: TestClient, token: str, content: bytes):
    """Upload locations and wait for the validation job to finish.

    Uploading is a background job — the request answers 202 with a job to watch —
    so the shared helpers from the upload suite are reused here rather than this
    file growing its own copy of the wait.
    """
    from test_data_upload import FinishedUpload, _as_outcome, wait_for_job

    response = client.post("/api/data-upload/preview", headers=auth(token),
                           files={"file": ("locations.csv", content, "text/csv")},
                           data={"upload_type": "map_entity_locations",
                                 "import_mode": "UPSERT"})
    if response.status_code != 202:
        return response
    upload_id = response.json()["upload"]["upload_id"]
    return FinishedUpload(202, _as_outcome(wait_for_job(client, token, upload_id)))


def commit_locations(client: TestClient, token: str, upload_id: int):
    """Confirm the import and wait for it."""
    from test_data_upload import FinishedUpload, _as_outcome, wait_for_job

    response = client.post(f"/api/data-upload/{upload_id}/commit",
                           params={"confirm": True}, headers=auth(token))
    if response.status_code != 202:
        return response
    return FinishedUpload(202, _as_outcome(wait_for_job(client, token, upload_id)))


def test_the_upload_centre_offers_a_map_location_type(map_client):
    token = login(map_client)
    body = map_client.get("/api/data-upload/types", headers=auth(token)).json()
    master = next(c for c in body["categories"] if c["key"] == "MASTER")
    location_type = next(t for t in master["types"]
                         if t["key"] == "map_entity_locations")
    assert location_type["business_key"] == ["entity_type", "entity_code"]
    assert "Latitude" in location_type["required_columns"]


def test_coordinates_upload_through_the_validated_pipeline(map_client, agent_engine):
    token = login(map_client)
    content = csv_bytes([
        ["territory", "TR001", 23.7808, 90.4008, "Kazipara"],
        ["region", "REG002", 22.8456, 89.5403, "Khulna"],
    ])
    preview = upload_locations(map_client, token, content).json()
    assert preview["upload"]["status"] == "VALIDATED"
    assert preview["upload"]["totals"]["valid_rows"] == 2

    result = commit_locations(map_client, token,
                              preview["upload"]["upload_id"]).json()
    assert result["upload"]["status"] == "COMPLETED"
    assert result["upload"]["totals"]["inserted_rows"] == 2

    with Session(agent_engine) as session:
        placed = {
            (row.entity_type, row.entity_code): row
            for row in session.execute(select(MapEntityLocation)).scalars()
        }
    assert ("territory", "TR001") in placed
    # The post-load hook derived the levels above the uploaded territory.
    assert ("area", "AR001") in placed
    assert placed[("area", "AR001")].source == GeoSource.DERIVED


def test_an_upload_with_bad_coordinates_reports_row_and_reason(map_client):
    token = login(map_client)
    content = csv_bytes([
        ["territory", "TR001", 23.78, 90.40, ""],          # fine
        ["territory", "TR001", 24.00, 91.00, ""],          # duplicate in file
        ["wormhole", "WH1", 23.0, 90.0, ""],               # unknown entity type
        ["territory", "TR-NOPE", 23.0, 90.0, ""],          # unknown code
        ["region", "REG001", 0, 0, ""],                    # null island
    ])
    body = upload_locations(map_client, token, content).json()
    assert body["upload"]["totals"]["valid_rows"] == 1
    codes = {issue["error_code"] for issue in body["errors"]}
    assert "DUPLICATE_IN_FILE" in codes
    assert "INVALID_TYPE" in codes
    assert "INVALID_PARENT_CODE" in codes
    for issue in body["errors"]:
        assert issue["row"] and issue["suggested_fix"]


def test_the_location_template_downloads(map_client):
    token = login(map_client)
    response = map_client.get(
        "/api/data-upload/types/map_entity_locations/template",
        params={"format": "csv"}, headers=auth(token))
    assert response.status_code == 200
    header = response.content.decode("utf-8-sig").splitlines()[0]
    assert header.startswith("Entity Type,Entity Code,Latitude,Longitude")


# ==========================================================================
# Geometry
# ==========================================================================


def test_mercator_projection_is_normalised_and_ordered():
    from app.map.geo import to_mercator

    for latitude, longitude in (DHAKA, KHULNA, (0, 0), (-33.9, 151.2)):
        x, y = to_mercator(latitude, longitude)
        assert 0 <= x <= 1 and 0 <= y <= 1
    # Northern points sit higher on the screen, so their y is smaller.
    assert to_mercator(*DHAKA)[1] < to_mercator(*KHULNA)[1]


def test_centroid_handles_the_antimeridian():
    """A degree average would give 0; the correct answer is ±180."""
    from app.map.geo import centroid

    latitude, longitude = centroid([(0.0, 179.0), (0.0, -179.0)])
    assert latitude == 0.0
    assert abs(abs(longitude) - 180.0) < 0.001
