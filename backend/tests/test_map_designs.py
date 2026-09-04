"""Composing the map: designs, layers and the rules that keep them drawable.

Step 4 of the rebuild. The service is exercised directly against the seeded
warehouse (which carries the migration's one protected design) and then over
HTTP, where the two sections decide who may read and who may compose.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.auth.security import hash_password
from app.database.models_ai import AppUser, AuditAction, AuditLog, Role
from app.database.models_map import MapDesign, MapLayer, MapPointConfiguration
from app.main import app
from app.map import designs, metrics, styles
from app.map.designs import DesignSpec, LayerSpec
from app.map.errors import (
    DesignInactive,
    DesignNameTaken,
    DesignNotFound,
    DesignProtected,
    InvalidDesign,
    InvalidLayer,
)

PASSWORD = "Correct-Horse-9"
SEED_NAME = "Business Overview"


def layer(level: str, **overrides) -> LayerSpec:
    return LayerSpec(point_level=level, **overrides)


def spec(name: str = "Territory Performance", levels=("territory", "sub_territory"),
         **overrides) -> DesignSpec:
    return DesignSpec(name=name, layers=tuple(layer(level) for level in levels),
                      **overrides)


def seed(session: Session) -> MapDesign:
    return session.execute(
        select(MapDesign).where(MapDesign.name == SEED_NAME)
    ).scalar_one()


# ==========================================================================
# Reading
# ==========================================================================


def test_the_seeded_design_is_the_default_the_page_opens_with(session):
    design = designs.default_design(session)
    assert design is not None and design.name == SEED_NAME

    payload = designs.design_to_dict(design)
    assert payload["is_default"] and payload["is_system_default"]
    assert payload["basemap_resolved"]["key"] == "standard"
    assert payload["basemap_note"] is None
    assert [entry["point_level"] for entry in payload["layers"]] == [
        "zone", "region", "area", "unit", "territory", "sub_territory", "customer",
    ]
    for entry in payload["layers"]:
        # Inherited, and shown as such: the stored metric is empty, the
        # effective one is the design's.
        assert entry["metric"] is None
        assert entry["effective_metric"] == "net_sales"
        assert entry["effective_color_metric"] == "achievement"
        assert entry["color_mode"] == styles.MODE_BANDS
        assert entry["view_modes"] == ["point"]
        assert entry["tooltip_inherited"] is True
        assert entry["style"] == styles.default_style()
    territory = next(e for e in payload["layers"] if e["point_level"] == "territory")
    assert territory["tooltip_fields"] == list(designs.DEFAULT_TOOLTIP_FIELDS)
    customer = next(e for e in payload["layers"] if e["point_level"] == "customer")
    # A customer's customer count is one, so the default tooltip drops it there.
    assert "customer_count" not in customer["tooltip_fields"]
    assert customer["is_visible"] is False and customer["cluster_at"] == 200


def test_designs_list_default_first_and_hide_the_inactive(session, users):
    ceo = users["ceo"]
    designs.create_design(session, ceo, spec("Zeta Design"))
    hidden = designs.create_design(session, ceo, spec("Alpha Design", levels=("zone",)))
    designs.set_active(session, ceo, hidden.design_id, False)
    session.flush()

    visible = [d.name for d in designs.list_designs(session)]
    assert visible == [SEED_NAME, "Zeta Design"]
    everything = designs.list_designs(session, include_inactive=True)
    assert [d.name for d in everything] == [SEED_NAME, "Alpha Design", "Zeta Design"]
    assert not next(d for d in everything if d.name == "Alpha Design").is_active


# ==========================================================================
# Validation
# ==========================================================================


@pytest.mark.parametrize("bad_layer, field, words", [
    (layer("moon"), "point_level", "no map level"),
    (layer("customer", metric="customer_count"), "metric", "no meaning"),
    (layer("sub_territory", view_mode="boundary"), "view_mode", "boundary source"),
    (layer("sub_territory", view_mode="both"), "view_mode", "boundary source"),
    (layer("region", view_mode="hexagon"), "view_mode", "not a view mode"),
    (layer("region", style_config={"thresholds": [50, 70, 90]}), "style_config", "thresholds"),
    (layer("region", metric="profit"), "metric", "no metric"),
    (layer("region", color_metric="profit"), "color_metric", "no metric"),
    (layer("region", label_field="colour"), "label_field", "label"),
    (layer("region", tooltip_fields=("code", "profit")), "tooltip_fields", "tooltip"),
    (layer("region", tooltip_fields=()), "tooltip_fields", "at least one"),
    (layer("customer", tooltip_fields=("customer_count",)), "tooltip_fields", "no meaning"),
    (layer("region", min_zoom=30), "min_zoom", "between 0 and 22"),
    (layer("region", cluster_at=0), "cluster_at", "at least 1"),
    (layer("region", layer_name="   "), "layer_name", "blank"),
])
def test_a_layer_the_map_cannot_draw_is_refused_by_level_and_field(
        session, users, bad_layer, field, words):
    with pytest.raises(InvalidLayer) as refused:
        designs.create_design(session, users["ceo"],
                              DesignSpec(name="Broken", layers=(bad_layer,)))
    assert refused.value.details["field"] == field
    assert words in refused.value.user_message


def test_a_design_draws_each_level_once(session, users):
    with pytest.raises(InvalidLayer) as refused:
        designs.create_design(session, users["ceo"],
                              spec(levels=("zone", "region", "zone")))
    assert refused.value.details == {"level": "zone", "field": "point_level"}
    assert "twice" in refused.value.user_message


@pytest.mark.parametrize("bad_spec, words", [
    (spec(basemap="satellite"), "satellite"),
    (spec(default_metric="profit"), "no metric"),
    (spec(levels=()), "at least one layer"),
    (spec(name="   "), "needs a name"),
])
def test_a_design_field_the_map_cannot_honour_is_refused(session, users, bad_spec, words):
    with pytest.raises(InvalidDesign) as refused:
        designs.create_design(session, users["ceo"], bad_spec)
    assert words in refused.value.user_message


def test_a_design_default_no_layer_can_draw_is_refused_naming_the_layer(session, users):
    with pytest.raises(InvalidLayer) as refused:
        designs.create_design(session, users["ceo"], spec(
            default_metric="customer_count", levels=("region", "customer")))
    assert refused.value.details == {"level": "customer", "field": "default_metric"}
    # Given its own metric the customer layer no longer inherits, and the
    # design default is fine for the region above it.
    design = designs.create_design(session, users["ceo"], DesignSpec(
        name="Counts", default_metric="customer_count",
        layers=(layer("region"), layer("customer", metric="net_sales")),
    ))
    payload = designs.design_to_dict(design)
    assert [e["effective_metric"] for e in payload["layers"]] == ["customer_count", "net_sales"]


def test_a_design_name_is_unique_ignoring_case(session, users):
    with pytest.raises(DesignNameTaken):
        designs.create_design(session, users["ceo"], spec(name="business overview"))
    with pytest.raises(DesignNameTaken):
        designs.create_design(session, users["ceo"], spec(name="  Business Overview  "))


# ==========================================================================
# Writing
# ==========================================================================


def test_a_created_design_is_ordered_as_given_and_inherits_where_it_says_nothing(
        session, users):
    design = designs.create_design(session, users["ceo"], DesignSpec(
        name="Field View",
        description="  Territory and below  ",
        layers=(layer("sub_territory"), layer("territory", layer_name="Field Force"),
                layer("zone", is_visible=False)),
    ))
    session.flush()
    assert design.design_id > 1 and design.created_by == "ceo"
    assert design.is_active and not design.is_default and not design.is_system_default
    assert design.description == "Territory and below"

    payload = designs.design_to_dict(design)
    assert [(e["point_level"], e["display_order"], e["layer_name"]) for e in payload["layers"]] == [
        ("sub_territory", 1, "Sub-Territory"), ("territory", 2, "Field Force"),
        ("zone", 3, "Zone"),
    ]
    zone = payload["layers"][2]
    assert zone["is_visible"] is False
    assert zone["effective_size_metric"] == "net_sales" and zone["size_metric"] is None
    assert zone["label_field"] == "name" and zone["show_label"] is False
    assert all(entry["layer_id"] for entry in payload["layers"])


def test_updating_a_design_changes_only_what_was_named(session, users):
    ceo = users["ceo"]
    design = designs.create_design(session, ceo, spec(
        "Before", description="kept", levels=("region", "customer")))

    designs.update_design(session, ceo, design.design_id, {"name": "After"})
    assert design.name == "After" and design.description == "kept"
    assert design.default_metric == "net_sales"

    designs.update_design(session, ceo, design.design_id, {"description": None})
    assert design.description is None

    with pytest.raises(InvalidDesign):
        designs.update_design(session, ceo, design.design_id, {"is_default": True})
    with pytest.raises(DesignNameTaken):
        designs.update_design(session, ceo, design.design_id, {"name": "BUSINESS overview"})
    # Renaming to its own name, differently cased, is not a collision.
    designs.update_design(session, ceo, design.design_id, {"name": "after"})
    assert design.name == "after"

    # The customer layer inherits the design metric, so a default it cannot
    # draw is refused — naming the layer, not the design.
    with pytest.raises(InvalidLayer) as refused:
        designs.update_design(session, ceo, design.design_id,
                              {"default_metric": "customer_count"})
    assert refused.value.details["level"] == "customer"
    assert design.default_metric == "net_sales"


def test_replacing_layers_reorders_keeps_ids_and_drops_the_missing(session, users):
    ceo = users["ceo"]
    design = designs.create_design(session, ceo, spec(levels=("zone", "region", "area")))
    session.flush()
    ids = {l.point_level: l.layer_id for l in design.layers}
    region_config_id = next(l for l in design.layers if l.point_level == "region").point_config.config_id

    designs.replace_layers(session, ceo, design.design_id, [
        layer("area", is_visible=False, cluster_at=50, show_label=True,
              tooltip_fields=("net_sales", "code"),
              style_config={"thresholds": [95, 80, 60]}),
        layer("zone"),
    ])
    payload = designs.design_to_dict(design)
    assert [(e["point_level"], e["display_order"]) for e in payload["layers"]] == [
        ("area", 1), ("zone", 2),
    ]
    assert [e["layer_id"] for e in payload["layers"]] == [ids["area"], ids["zone"]], \
        "a surviving layer keeps its id"
    area = payload["layers"][0]
    assert area["is_visible"] is False and area["cluster_at"] == 50
    assert area["show_label"] is True
    assert area["tooltip_fields"] == ["net_sales", "code"] and area["tooltip_inherited"] is False
    assert area["style_config"] == {"thresholds": [95, 80, 60]}
    assert area["style"]["bands"][0]["label"] == "95% and above"

    assert session.get(MapLayer, ids["region"]) is None
    assert session.get(MapPointConfiguration, region_config_id) is None

    with pytest.raises(InvalidDesign):
        designs.replace_layers(session, ceo, design.design_id, [])
    with pytest.raises(InvalidLayer):
        designs.replace_layers(session, ceo, design.design_id,
                               [layer("zone"), layer("zone")])


def test_duplicating_makes_an_independent_copy(session, users):
    ceo = users["ceo"]
    original = seed(session)

    copy = designs.duplicate_design(session, ceo, original.design_id)
    session.flush()
    assert copy.name == f"{SEED_NAME} Copy"
    assert copy.created_by == "ceo"
    assert copy.is_active and not copy.is_default and not copy.is_system_default
    assert copy.description == original.description
    assert [(l.point_level, l.display_order, l.is_visible, l.cluster_at, l.min_zoom)
            for l in copy.layers] == [
        (l.point_level, l.display_order, l.is_visible, l.cluster_at, l.min_zoom)
        for l in original.layers
    ]
    assert {l.layer_id for l in copy.layers}.isdisjoint({l.layer_id for l in original.layers})

    second = designs.duplicate_design(session, ceo, original.design_id)
    assert second.name == f"{SEED_NAME} Copy 2"
    named = designs.duplicate_design(session, ceo, original.design_id, name="Mine")
    assert named.name == "Mine"

    # Changing the copy leaves the original alone, and the copy is deletable.
    designs.replace_layers(session, ceo, copy.design_id, [layer("territory")])
    session.flush()
    assert len(original.layers) == 7 and len(copy.layers) == 1
    designs.delete_design(session, ceo, copy.design_id)
    assert session.get(MapDesign, copy.design_id) is None


def test_setting_the_default_moves_it_and_needs_an_active_design(session, users):
    ceo = users["ceo"]
    original = seed(session)
    custom = designs.create_design(session, ceo, spec())

    designs.set_default(session, ceo, custom.design_id)
    assert custom.is_default and not original.is_default
    assert designs.default_design(session).design_id == custom.design_id

    # Deactivating the default hands the default back to the system design.
    designs.set_active(session, ceo, custom.design_id, False)
    assert not custom.is_active and not custom.is_default and original.is_default
    assert designs.default_design(session).design_id == original.design_id
    with pytest.raises(DesignInactive):
        designs.set_default(session, ceo, custom.design_id)

    with pytest.raises(DesignProtected):
        designs.set_active(session, ceo, original.design_id, False)
    assert original.is_active

    designs.set_active(session, ceo, custom.design_id, True)
    assert custom.is_active and original.is_default


def test_deleting_a_custom_design_takes_its_layers_and_the_default_falls_back(
        session, users):
    ceo = users["ceo"]
    original = seed(session)
    custom = designs.create_design(session, ceo, spec(levels=("zone", "region")))
    session.flush()
    layer_ids = [l.layer_id for l in custom.layers]
    designs.set_default(session, ceo, custom.design_id)

    result = designs.delete_design(session, ceo, custom.design_id)
    assert result == {
        "deleted_design_id": custom.design_id, "name": "Territory Performance",
        "layers_removed": 2, "default_design_id": original.design_id,
    }
    assert session.get(MapDesign, custom.design_id) is None
    assert all(session.get(MapLayer, layer_id) is None for layer_id in layer_ids)
    assert original.is_default

    with pytest.raises(DesignProtected):
        designs.delete_design(session, ceo, original.design_id)
    with pytest.raises(DesignNotFound):
        designs.delete_design(session, ceo, 999_999)
    with pytest.raises(DesignNotFound):
        designs.get_design(session, 999_999)


# ==========================================================================
# Over HTTP
# ==========================================================================


@pytest.fixture
def client(agent_engine, users):
    with Session(agent_engine) as db:
        for user in db.query(AppUser).all():
            user.password_hash = hash_password(PASSWORD)
        db.add(AppUser(username="root", display_name="Administrator",
                       role=Role.SUPER_ADMIN, is_active=True,
                       password_hash=hash_password(PASSWORD)))
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


def body(levels=("territory", "sub_territory"), **overrides) -> dict:
    return {"name": "HTTP Design",
            "layers": [{"point_level": level} for level in levels], **overrides}


def test_designs_are_readable_by_anyone_who_may_open_the_map(client):
    headers = login(client, "ceo")
    response = client.get("/api/map/designs", headers=headers)
    assert response.status_code == 200, response.text
    listing = response.json()
    assert [d["name"] for d in listing["designs"]] == [SEED_NAME]
    default_id = listing["default_design_id"]
    assert default_id == listing["designs"][0]["design_id"]
    assert len(listing["designs"][0]["layers"]) == 7

    one = client.get(f"/api/map/designs/{default_id}", headers=headers)
    assert one.status_code == 200 and one.json()["name"] == SEED_NAME


def test_composing_needs_the_map_settings_section(client):
    ceo = login(client, "ceo")   # MANAGEMENT: opens the map, does not compose it
    seed_id = client.get("/api/map/designs", headers=ceo).json()["default_design_id"]
    assert client.post("/api/map/designs", json=body(), headers=ceo).status_code == 403
    assert client.put(f"/api/map/designs/{seed_id}/layers",
                      json={"layers": [{"point_level": "zone"}]},
                      headers=ceo).status_code == 403
    assert client.post(f"/api/map/designs/{seed_id}/duplicate", headers=ceo).status_code == 403
    assert client.delete(f"/api/map/designs/{seed_id}", headers=ceo).status_code == 403
    assert client.get("/api/map/designs", params={"include_inactive": "true"},
                      headers=ceo).status_code == 403


def test_the_full_lifecycle_over_http(client, agent_engine):
    root = login(client, "root")
    seed_id = client.get("/api/map/designs", headers=root).json()["default_design_id"]

    created = client.post("/api/map/designs", headers=root, json=body(
        description="Two levels", layers=[
            {"point_level": "territory", "min_zoom": 6},
            {"point_level": "sub_territory", "cluster_at": 300, "show_label": True},
        ]))
    assert created.status_code == 201, created.text
    design = created.json()
    design_id = design["design_id"]
    assert design["created_by"] == "root" and design["is_active"]
    assert [l["point_level"] for l in design["layers"]] == ["territory", "sub_territory"]
    assert design["layers"][1]["cluster_at"] == 300

    renamed = client.put(f"/api/map/designs/{design_id}", headers=root,
                         json={"name": "Renamed", "description": None})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Renamed"
    assert renamed.json()["description"] is None

    layered = client.put(f"/api/map/designs/{design_id}/layers", headers=root, json={
        "layers": [{"point_level": "customer", "is_visible": False},
                   {"point_level": "territory"}],
    })
    assert layered.status_code == 200, layered.text
    assert [(l["point_level"], l["is_visible"]) for l in layered.json()["layers"]] == [
        ("customer", False), ("territory", True),
    ]

    copied = client.post(f"/api/map/designs/{design_id}/duplicate", headers=root)
    assert copied.status_code == 201, copied.text
    assert copied.json()["name"] == "Renamed Copy"
    assert [l["point_level"] for l in copied.json()["layers"]] == ["customer", "territory"]

    made_default = client.post(f"/api/map/designs/{design_id}/default", headers=root)
    assert made_default.status_code == 200 and made_default.json()["is_default"]
    assert client.get("/api/map/designs", headers=root).json()["default_design_id"] == design_id

    off = client.post(f"/api/map/designs/{design_id}/deactivate", headers=root)
    assert off.status_code == 200 and not off.json()["is_active"]
    assert not off.json()["is_default"]
    assert client.get("/api/map/designs", headers=root).json()["default_design_id"] == seed_id

    on = client.post(f"/api/map/designs/{design_id}/activate", headers=root)
    assert on.status_code == 200 and on.json()["is_active"]

    deleted = client.delete(f"/api/map/designs/{design_id}", headers=root)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted_design_id"] == design_id
    assert deleted.json()["default_design_id"] == seed_id
    assert client.get(f"/api/map/designs/{design_id}", headers=root).status_code == 404

    with Session(agent_engine) as db:
        actions = [
            (entry.action, entry.username, entry.resource)
            for entry in db.execute(
                select(AuditLog).where(AuditLog.action.like("MAP_%"))
                .order_by(AuditLog.audit_id)
            ).scalars().all()
        ]
    assert [action for action, _, _ in actions] == [
        AuditAction.MAP_DESIGN_CREATED, AuditAction.MAP_DESIGN_UPDATED,
        AuditAction.MAP_LAYERS_UPDATED, AuditAction.MAP_DESIGN_DUPLICATED,
        AuditAction.MAP_DESIGN_DEFAULT_SET, AuditAction.MAP_DESIGN_DEACTIVATED,
        AuditAction.MAP_DESIGN_ACTIVATED, AuditAction.MAP_DESIGN_DELETED,
    ]
    assert {username for _, username, _ in actions} == {"root"}
    assert actions[0][2] == f"map_design:{design_id}"


def test_refusals_are_named_over_http(client):
    root = login(client, "root")
    seed_id = client.get("/api/map/designs", headers=root).json()["default_design_id"]

    boundary = client.post("/api/map/designs", headers=root, json=body(
        layers=[{"point_level": "sub_territory", "view_mode": "boundary"}]))
    assert boundary.status_code == 409, boundary.text
    assert boundary.json()["detail"]["error_code"] == "MAP_LAYER_INVALID"
    assert "boundary source" in boundary.json()["detail"]["message"]

    taken = client.post("/api/map/designs", headers=root, json=body(name="business overview"))
    assert taken.status_code == 409
    assert taken.json()["detail"]["error_code"] == "MAP_DESIGN_NAME_TAKEN"

    unknown_field = client.post("/api/map/designs", headers=root,
                                json={**body(), "colour": "blue"})
    assert unknown_field.status_code == 422
    empty = client.put(f"/api/map/designs/{seed_id}/layers", headers=root,
                       json={"layers": []})
    assert empty.status_code == 422

    missing = client.get("/api/map/designs/999999", headers=root)
    assert missing.status_code == 404
    assert missing.json()["detail"]["error_code"] == "MAP_DESIGN_NOT_FOUND"

    protected = client.delete(f"/api/map/designs/{seed_id}", headers=root)
    assert protected.status_code == 409
    assert protected.json()["detail"]["error_code"] == "MAP_DESIGN_PROTECTED"
    assert client.get(f"/api/map/designs/{seed_id}", headers=root).status_code == 200


def test_readers_cannot_see_an_inactive_design_but_settings_holders_can(client):
    root = login(client, "root")
    ceo = login(client, "ceo")
    design_id = client.post("/api/map/designs", headers=root, json=body()).json()["design_id"]
    assert client.post(f"/api/map/designs/{design_id}/deactivate",
                       headers=root).status_code == 200

    assert [d["design_id"] for d in client.get("/api/map/designs", headers=ceo).json()["designs"]] == [
        client.get("/api/map/designs", headers=ceo).json()["default_design_id"],
    ]
    assert client.get(f"/api/map/designs/{design_id}", headers=ceo).status_code == 404

    everything = client.get("/api/map/designs", params={"include_inactive": "true"},
                            headers=root).json()["designs"]
    assert design_id in [d["design_id"] for d in everything]
    assert client.get(f"/api/map/designs/{design_id}", headers=root).status_code == 200
