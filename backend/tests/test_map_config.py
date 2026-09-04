"""What the map can draw, before it draws anything.

Step 3 of the rebuild: the two sections that guard the map, the basemap
catalogue read from configuration, the style defaults a layer may override,
the view modes a level can honour, and the one endpoint that publishes all of
it to the page.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.auth.security import hash_password
from app.config import get_settings
from app.database.models_admin import UserSectionPermission
from app.database.models_ai import AppUser, Role
from app.database.models_map import LayerViewMode
from app.main import app
from app.map import basemaps, designs, geo, levels, metrics, styles
from app.security.sections import DENY, SECTION_BY_KEY, Action, SectionKey

PASSWORD = "Correct-Horse-9"

OPENFREEMAP_LIGHT = "https://tiles.openfreemap.org/styles/positron"
OPENFREEMAP_DARK = "https://tiles.openfreemap.org/styles/dark"
RASTER = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
GLYPHS = "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf"


def configured(**overrides) -> object:
    """Settings with the basemap fields pinned, whatever this machine's env says."""
    base = replace(
        get_settings(),
        map_style_url=OPENFREEMAP_LIGHT,
        map_style_url_dark=OPENFREEMAP_DARK,
        map_style_url_satellite="",
        map_attribution="",
        map_attribution_satellite="",
        map_glyphs_url=GLYPHS,
    )
    return replace(base, **overrides)


# ==========================================================================
# Sections
# ==========================================================================


def test_every_role_may_open_the_map_and_only_administrators_may_compose_it():
    map_section = SECTION_BY_KEY[SectionKey.MAP]
    settings_section = SECTION_BY_KEY[SectionKey.MAP_SETTINGS]

    for role in Role.ALL:
        assert map_section.role_default(role) is True, role
        assert settings_section.role_default(role) is (role in Role.ADMIN_ROLES), role

    # Opening the map is reporting: nothing on it can be created or deleted.
    assert map_section.supports(Action.VIEW)
    assert not map_section.supports(Action.CREATE)
    assert not map_section.supports(Action.DELETE)

    # Composing it is not, and deleting a design defaults to administrators.
    for action in (Action.VIEW, Action.CREATE, Action.EDIT, Action.DELETE):
        assert settings_section.supports(action), action
    assert settings_section.action_default(Action.DELETE, Role.SUPER_ADMIN) is True
    assert settings_section.action_default(Action.DELETE, Role.REGIONAL_MANAGER) is False
    # A manager *granted* the section may edit designs; only the grant is gated.
    assert settings_section.action_default(Action.EDIT, Role.REGIONAL_MANAGER) is True
    assert settings_section.action_default(Action.EDIT, Role.VIEWER) is False


def test_the_map_sections_are_not_locked_to_a_role():
    # Unlike Administration, either can be granted to any role: a marketing
    # lead may own how the map reads without becoming an administrator.
    for key in (SectionKey.MAP, SectionKey.MAP_SETTINGS):
        assert SECTION_BY_KEY[key].locked_to_roles is None


# ==========================================================================
# Basemaps
# ==========================================================================


def test_the_standard_basemap_is_openfreemap_and_satellite_is_absent_until_configured():
    catalogue = basemaps.catalogue(configured())
    assert [basemap.key for basemap in catalogue] == ["standard"]
    standard = catalogue[0]
    assert standard.style_url == OPENFREEMAP_LIGHT
    assert standard.style_url_dark == OPENFREEMAP_DARK
    assert standard.kind == basemaps.KIND_STYLE
    assert standard.label == "Map"
    # OpenFreeMap credits itself through its TileJSON; nothing is restated here.
    assert standard.attribution is None


def test_a_configured_satellite_provider_is_offered_with_its_own_credit():
    catalogue = basemaps.catalogue(configured(
        map_style_url_satellite="https://example.test/satellite/{z}/{x}/{y}.jpg",
        map_attribution_satellite="© Example Imagery",
    ))
    assert [basemap.key for basemap in catalogue] == ["standard", "satellite"]
    satellite = catalogue[1]
    assert satellite.kind == basemaps.KIND_RASTER
    assert satellite.style_url_dark is None
    assert satellite.attribution == "© Example Imagery"
    assert satellite.to_dict()["label"] == "Satellite"
    # A raster basemap has no glyphs of its own; the configured source rides
    # along so its labels can be drawn.
    assert satellite.to_dict()["glyphs_url"] == GLYPHS


def test_a_raster_tile_template_is_recognised_and_a_style_document_is_not():
    assert basemaps.classify(RASTER) == basemaps.KIND_RASTER
    assert basemaps.classify(OPENFREEMAP_LIGHT) == basemaps.KIND_STYLE
    # A template missing one of the three placeholders cannot be tiled.
    assert basemaps.classify("https://example.test/{z}/{x}.png") == basemaps.KIND_STYLE


def test_a_dark_url_of_a_different_kind_is_set_aside_not_mixed(monkeypatch):
    # The module logger is watched directly rather than through ``caplog``:
    # the application configures logging on import, and whether a module
    # logger created before that survives it depends on import order.
    warnings: list[str] = []
    monkeypatch.setattr(basemaps.logger, "warning",
                        lambda message, *args: warnings.append(message % args))
    catalogue = basemaps.catalogue(configured(map_style_url_dark=RASTER))
    standard = catalogue[0]
    assert standard.kind == basemaps.KIND_STYLE
    assert standard.style_url_dark is None
    assert warnings and "MAP_STYLE_URL_DARK" in warnings[0]


def test_an_added_attribution_travels_with_the_standard_basemap():
    catalogue = basemaps.catalogue(configured(map_attribution="© My Tiles"))
    assert catalogue[0].attribution == "© My Tiles"


def test_a_design_naming_an_unconfigured_basemap_falls_back_and_says_so():
    settings = configured()
    basemap, note = basemaps.resolve("satellite", settings)
    assert basemap.key == "standard"
    assert note is not None and "satellite" in note

    basemap, note = basemaps.resolve("standard", settings)
    assert basemap.key == "standard" and note is None

    # A design that names nothing gets the default without a complaint.
    basemap, note = basemaps.resolve(None, settings)
    assert basemap.key == "standard" and note is None

    with_satellite = configured(map_style_url_satellite=RASTER)
    basemap, note = basemaps.resolve("satellite", with_satellite)
    assert basemap.key == "satellite" and note is None


def test_the_first_frame_comes_from_settings():
    view = basemaps.default_view(configured(
        map_default_latitude=23.5, map_default_longitude=90.25,
        map_default_zoom=7.0,
    ))
    assert view == {"latitude": 23.5, "longitude": 90.25, "zoom": 7.0}


# ==========================================================================
# Styles
# ==========================================================================


def test_achievement_bands_follow_the_configured_thresholds():
    default = styles.default_style()
    assert default["thresholds"] == [90.0, 70.0, 50.0]
    assert [band["key"] for band in default["bands"]] == list(styles.BAND_KEYS)
    assert [band["label"] for band in default["bands"]] == [
        "90% and above", "70% – 90%", "50% – 70%", "Below 50%",
    ]
    assert default["bands"][0]["min"] == 90.0 and default["bands"][0]["max"] is None
    assert default["bands"][3]["min"] is None and default["bands"][3]["max"] == 50.0
    for band in default["bands"]:
        assert band["color"] == styles.BAND_COLORS[band["key"]]


def test_absent_is_drawn_neutral_never_critical():
    default = styles.default_style()
    assert default["no_data_color"] not in default["band_colors"].values()
    assert default["no_data_color"] == default["diverging"]["neutral"]


def test_style_overrides_merge_over_the_defaults_and_relabel_the_bands():
    style = styles.effective_style({
        "thresholds": [95, 80, 60],
        "band_colors": {"good": "#008000"},
        "radius": [3, 30],
        "no_data_color": "#CCCCCC",
    })
    assert style["thresholds"] == [95.0, 80.0, 60.0]
    assert [band["label"] for band in style["bands"]] == [
        "95% and above", "80% – 95%", "60% – 80%", "Below 60%",
    ]
    assert style["bands"][0]["color"] == "#008000"
    # An overridden band keeps its neighbours' defaults.
    assert style["bands"][1]["color"] == styles.BAND_COLORS["medium"]
    assert style["radius"] == [3.0, 30.0]
    assert style["no_data_color"] == "#cccccc"
    # Untouched fields are the defaults, and the defaults are not mutated.
    assert style["sequential"] == list(styles.SEQUENTIAL_RAMP)
    assert styles.default_style()["thresholds"] == [90.0, 70.0, 50.0]


def test_no_overrides_is_the_default_style():
    assert styles.effective_style(None) == styles.default_style()
    assert styles.effective_style({}) == styles.default_style()


@pytest.mark.parametrize("overrides, names", [
    ({"thresholds": [50, 70, 90]}, "descending"),
    ({"thresholds": [90, 70]}, "three"),
    ({"thresholds": [90, "70", 50]}, "number"),
    ({"thresholds": [90, 70, -5]}, "negative"),
    ({"band_colors": {"excellent": "#000000"}}, "excellent"),
    ({"band_colors": {"good": "green"}}, "band_colors.good"),
    ({"no_data_color": "#12345"}, "no_data_color"),
    ({"sequential": ["#000000"]}, "two"),
    ({"diverging": {"sideways": "#000000"}}, "sideways"),
    ({"radius": [10, 5]}, "radius"),
    ({"radius": [0, 5]}, "radius"),
    ({"marker_shape": "star"}, "marker_shape"),
])
def test_a_bad_override_is_refused_by_name(overrides, names):
    with pytest.raises(ValueError) as refused:
        styles.effective_style(overrides)
    assert names in str(refused.value)


def test_color_mode_follows_the_metric_not_the_layer():
    assert styles.color_mode(metrics.get_metric("achievement")) == styles.MODE_BANDS
    assert styles.color_mode(metrics.get_metric("growth")) == styles.MODE_DIVERGING
    assert styles.color_mode(metrics.get_metric("shortfall")) == styles.MODE_DIVERGING
    assert styles.color_mode(metrics.get_metric("net_sales")) == styles.MODE_SEQUENTIAL
    assert styles.color_mode(metrics.get_metric("customer_count")) == styles.MODE_SEQUENTIAL


# ==========================================================================
# View modes
# ==========================================================================


def test_no_level_has_a_boundary_source_today_so_point_is_the_only_mode_offered():
    for level in levels.MAP_LEVELS:
        assert level.boundary_available is False, level.key
        assert levels.view_modes_for(level) == (LayerViewMode.POINT,), level.key
        assert level.to_dict()["view_modes"] == [LayerViewMode.POINT]
    assert [mode["key"] for mode in levels.VIEW_MODES] == list(LayerViewMode.ALL)


def test_a_level_with_a_boundary_source_is_offered_every_mode():
    outlined = replace(levels.get_level("region"), boundary_source="some_table")
    assert outlined.boundary_available is True
    assert levels.view_modes_for(outlined) == tuple(LayerViewMode.ALL)


# ==========================================================================
# GET /api/map/config
# ==========================================================================


@pytest.fixture
def client(agent_engine, users):
    with Session(agent_engine) as session:
        for user in session.query(AppUser).all():
            user.password_hash = hash_password(PASSWORD)
        denied = AppUser(username="no_map", display_name="No Map", role=Role.VIEWER,
                         is_active=True, password_hash=hash_password(PASSWORD))
        session.add(denied)
        session.flush()
        session.add(UserSectionPermission(
            user_id=denied.user_id, section_key=SectionKey.MAP, access=DENY,
            updated_by="test",
        ))
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


def login(client: TestClient, username: str) -> dict[str, str]:
    response = client.post("/api/auth/login",
                           json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_config_needs_a_signed_in_user(client):
    assert client.get("/api/map/config").status_code == 401


def test_config_is_refused_to_a_user_denied_the_map(client):
    response = client.get("/api/map/config", headers=login(client, "no_map"))
    assert response.status_code == 403


def test_config_publishes_everything_the_page_needs(client):
    response = client.get("/api/map/config", headers=login(client, "ceo"))
    assert response.status_code == 200, response.text
    body = response.json()

    settings = get_settings()
    assert body["basemaps"][0]["key"] == "standard"
    assert body["basemaps"][0]["style_url"] == settings.map_style_url
    assert body["default_basemap"] == "standard"
    assert body["view"] == basemaps.default_view(settings)

    assert [level["key"] for level in body["levels"]] == list(levels.LEVEL_KEYS)
    assert body["promoted_levels"] == list(levels.PROMOTED_LEVELS)
    sub_territory = next(l for l in body["levels"] if l["key"] == "sub_territory")
    assert sub_territory["promoted"] is True
    assert sub_territory["boundary_available"] is False
    assert sub_territory["view_modes"] == ["point"]

    assert [mode["key"] for mode in body["view_modes"]] == ["point", "boundary", "both"]
    assert [metric["key"] for metric in body["metrics"]] == list(metrics.METRIC_KEYS)
    assert body["defaults"] == {"metric": "net_sales", "color_metric": "achievement",
                                "size_metric": "net_sales",
                                "tooltip_fields": list(designs.DEFAULT_TOOLTIP_FIELDS)}
    assert body["style"] == styles.default_style()
    assert [entry["entity_type"] for entry in body["coverage"]] == list(levels.LEVEL_KEYS)


def test_config_reports_coverage_from_the_database(client, agent_engine):
    with Session(agent_engine) as session:
        geo.upsert_location(session, entity_type="region", entity_code="REG001",
                            latitude=23.78, longitude=90.40)
        session.commit()
    body = client.get("/api/map/config", headers=login(client, "ceo")).json()
    region = next(entry for entry in body["coverage"] if entry["entity_type"] == "region")
    assert region["placed"] == 1
    assert region["missing"] == region["total"] - 1


def test_config_offers_satellite_only_when_configured(client, monkeypatch):
    import app.api.routes_map as routes_map

    monkeypatch.setattr(routes_map, "get_settings",
                        lambda: configured(map_style_url_satellite=RASTER))
    body = client.get("/api/map/config", headers=login(client, "ceo")).json()
    assert [basemap["key"] for basemap in body["basemaps"]] == ["standard", "satellite"]
    assert body["basemaps"][1]["kind"] == "raster"
