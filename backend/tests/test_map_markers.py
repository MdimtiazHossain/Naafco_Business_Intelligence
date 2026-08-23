"""Marker / Shape Designer: designs, shapes, uploads, assignment and security.

Structured around the specification's acceptance list. The items that need a
live map (open the dashboard map, change a map filter) are covered as far as the
configuration boundary — the map consumes ``/api/map/marker-config`` and
``/api/map/legend``, so those responses are what the tests pin.
"""

from __future__ import annotations

import base64
import io
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.auth.security import hash_password
from app.database.models_ai import AppUser, AuditAction, AuditLog, Role, UserStatus
from app.database.models_map import (
    MapMarkerAssignment,
    MapMarkerDesign,
    MarkerDesignStatus,
    MarkerDesignType,
)
from app.main import app
from app.map import resolver, service
from app.map.entities import ENTITY_TYPE_BY_KEY
from app.map.render import render
from app.map.schemas import parse_definition
from app.map.shapes import SHAPE_KEYS
from app.security.sections import ALLOW, DENY, SectionKey

pytest.importorskip("multipart", reason="python-multipart is required by FastAPI forms")

PASSWORD = "Correct-Horse-9"


@pytest.fixture
def map_client(agent_engine, users, monkeypatch):
    """A client signed in as a super administrator, with defaults seeded."""
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


def create(client: TestClient, token: str, **overrides):
    body = {
        "name": "Test Marker",
        "entity_type": "territory",
        "design_type": MarkerDesignType.BUILTIN_SHAPE,
        "definition": {"shape": {"kind": "builtin", "builtin": "circle"}},
        **overrides,
    }
    return client.post("/api/map/marker-designs", headers=auth(token), json=body)


SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
       '<path d="M2 2h20v20H2Z" fill="#123456"/></svg>')


# ==========================================================================
# Catalogues (acceptance 1, 2)
# ==========================================================================


def test_entity_types_come_from_the_real_hierarchy(map_client):
    token = login(map_client)
    body = map_client.get("/api/map/entity-types", headers=auth(token)).json()
    keys = {e["key"] for e in body["entity_types"]}
    # Every level the data warehouse and the permission scope already use.
    assert {"zone", "region", "area", "unit", "territory", "sub_territory"} <= keys
    assert {"customer", "sales_force"} <= keys
    customer = next(e for e in body["entity_types"] if e["key"] == "customer")
    assert customer["has_source_data"] is False       # still PENDING_SOURCE_DATA


def test_no_warehouse_type_is_offered_to_place(map_client):
    """Revision 0020 removed the dimension, and nothing took its place.

    Stock is located by a plant and a storage location now, and neither master
    records a coordinate — so offering either as a placeable type would give an
    operator a marker they could never put anywhere.
    """
    token = login(map_client)
    body = map_client.get("/api/map/entity-types", headers=auth(token)).json()
    assert "warehouse" not in {e["key"] for e in body["entity_types"]}


def test_every_default_layer_is_a_type_the_map_can_draw():
    """The default layer list and the drawable types cannot drift apart.

    ``GET /api/map/entities`` falls back to ``DEFAULT_LAYERS`` when the caller
    names none, then rejects any layer it does not recognise. A stale name here
    therefore does not degrade to a missing layer — it makes the *default*
    request fail outright, which is what ``warehouse`` did between revision 0020
    and this test.
    """
    from app.map import entity_view, hierarchy

    assert set(entity_view.DEFAULT_LAYERS) <= set(hierarchy.ALL_TYPES)


def test_all_twelve_builtin_shapes_are_offered_and_render(map_client):
    token = login(map_client)
    body = map_client.get("/api/map/shapes", headers=auth(token)).json()
    keys = [s["key"] for s in body["shapes"]]
    assert keys == list(SHAPE_KEYS)
    assert len(keys) == 12
    for shape in body["shapes"]:
        assert shape["sample_path"].startswith("M"), shape["key"]


def test_the_icon_library_covers_every_required_category(map_client):
    token = login(map_client)
    body = map_client.get("/api/map/icons", headers=auth(token)).json()
    categories = {c["key"] for c in body["categories"]}
    assert {"Business", "Warehouse", "Customer", "Person", "Transport",
            "Agriculture", "Office", "Location", "Sales", "Warning",
            "Analytics"} == categories
    assert all(c["icons"] for c in body["categories"])


# ==========================================================================
# 1. Create a circle marker · 4-6. colour, size, border
# ==========================================================================


def test_create_a_circle_marker(map_client, agent_engine):
    token = login(map_client)
    response = create(map_client, token, name="Circle Marker")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Circle Marker"
    assert body["status"] == MarkerDesignStatus.DRAFT
    assert body["version"] == 1
    assert body["preview"]["svg"].startswith("<svg")
    assert body["preview"]["vector_only"] is True


def test_colour_size_and_border_are_stored_and_rendered(map_client):
    token = login(map_client)
    body = create(map_client, token, name="Styled", definition={
        "shape": {"kind": "builtin", "builtin": "hexagon", "fill": "#FF00AA",
                  "stroke": "#00FF00", "stroke_width": 5, "size": 48,
                  "opacity": 0.8, "rotation": 45},
    }).json()
    shape = body["definition"]["shape"]
    assert shape["fill"] == "#FF00AA"
    assert shape["stroke"] == "#00FF00"
    assert shape["stroke_width"] == 5.0
    assert shape["size"] == 48 and shape["rotation"] == 45
    svg = body["preview"]["svg"]
    assert "#FF00AA" in svg and "#00FF00" in svg and "rotate(45)" in svg


def test_an_invalid_colour_is_refused(map_client):
    token = login(map_client)
    response = create(map_client, token, definition={
        "shape": {"kind": "builtin", "builtin": "circle", "fill": "red"}})
    assert response.status_code == 422
    assert "definition is not valid" in response.json()["detail"]


def test_an_unknown_shape_is_refused(map_client):
    token = login(map_client)
    response = create(map_client, token, definition={
        "shape": {"kind": "builtin", "builtin": "unicorn"}})
    assert response.status_code == 422


def test_a_size_outside_the_allowed_range_is_refused(map_client):
    token = login(map_client)
    assert create(map_client, token, definition={
        "shape": {"kind": "builtin", "builtin": "circle", "size": 5000}},
    ).status_code == 422


# ==========================================================================
# 2. Create a custom polygon (Shape Builder)
# ==========================================================================


def test_create_a_custom_polygon(map_client):
    token = login(map_client)
    body = create(map_client, token, name="Custom Triangle",
                  design_type=MarkerDesignType.SHAPE_BUILDER,
                  definition={"shape": {
                      "kind": "polygon",
                      "points": [[0, -20], [18, 10], [-18, 10]],
                      "fill": "#7C3AED",
                  }}).json()
    assert body["design_type"] == MarkerDesignType.SHAPE_BUILDER
    assert body["definition"]["shape"]["points"] == [[0, -20], [18, 10], [-18, 10]]
    assert "M 0 -20 L 18 10 L -18 10 Z" in body["preview"]["svg"]


def test_a_polygon_needs_at_least_three_points(map_client):
    token = login(map_client)
    assert create(map_client, token, definition={
        "shape": {"kind": "polygon", "points": [[0, 0], [5, 5]]}},
    ).status_code == 422


def test_a_polygon_cannot_be_unbounded(map_client):
    """A design must not become a denial of service against the renderer."""
    token = login(map_client)
    many = [[i, i] for i in range(500)]
    assert create(map_client, token, definition={
        "shape": {"kind": "polygon", "points": many}},
    ).status_code == 422

    far = [[0, 0], [10, 10], [99999, 99999]]
    assert create(map_client, token, definition={
        "shape": {"kind": "polygon", "points": far}},
    ).status_code == 422


# ==========================================================================
# 3. Upload SVG · 25. malicious / invalid SVG
# ==========================================================================


def upload(client: TestClient, token: str, content: bytes, name: str = "icon.svg",
           media: str = "image/svg+xml"):
    return client.post("/api/map/marker-assets", headers=auth(token),
                       files={"file": (name, content, media)})


def test_upload_a_clean_svg(map_client):
    token = login(map_client)
    response = upload(map_client, token, SVG.encode())
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["media_type"] == "image/svg+xml"
    assert "#123456" in body["content"]
    assert body["sanitised"]["changed"] is False


@pytest.mark.parametrize("payload,label", [
    (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
     "script element"),
    (b'<?xml version="1.0"?><!DOCTYPE s [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
     b'<svg xmlns="http://www.w3.org/2000/svg"><text>&x;</text></svg>', "XXE entity"),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><body/></foreignObject>'
     b'</svg>', "foreignObject"),
    (b"not xml at all", "not xml"),
    (b'<html><body>hi</body></html>', "not an svg"),
])
def test_a_malicious_or_invalid_svg_is_refused(map_client, payload, label):
    token = login(map_client)
    response = upload(map_client, token, payload)
    assert response.status_code == 415, f"{label} was accepted"
    assert response.json()["detail"]


def test_a_dangerous_attribute_is_stripped_rather_than_stored(map_client):
    """An otherwise-valid drawing keeps its geometry and loses its payload."""
    token = login(map_client)
    hostile = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        'onload="alert(1)"><circle cx="12" cy="12" r="10" fill="#ff0000" '
        'onclick="steal()"/><image href="http://evil.example/x.png"/></svg>'
    ).encode()
    body = upload(map_client, token, hostile).json()
    content = body["content"]
    assert "onload" not in content and "onclick" not in content
    assert "evil.example" not in content
    assert "circle" in content and "#ff0000" in content    # the drawing survives
    assert body["sanitised"]["changed"] is True


def test_an_unsupported_image_type_is_refused(map_client):
    token = login(map_client)
    assert upload(map_client, token, b"MZ\x90\x00", "payload.exe",
                  "application/octet-stream").status_code == 415


def test_a_renamed_binary_is_refused_as_png(map_client):
    token = login(map_client)
    assert upload(map_client, token, b"not a png at all", "fake.png",
                  "image/png").status_code == 415


def test_a_valid_png_is_accepted_and_measured(map_client):
    token = login(map_client)
    # Smallest valid PNG: signature + IHDR declaring 1×1.
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    body = upload(map_client, token, png, "dot.png", "image/png").json()
    assert body["media_type"] == "image/png"
    assert (body["width"], body["height"]) == (1, 1)
    assert body["content"].startswith("data:image/png;base64,")


def test_an_oversized_upload_is_refused(map_client, monkeypatch):
    import app.map.assets as asset_module

    monkeypatch.setattr(asset_module, "max_upload_bytes", lambda: 128)
    token = login(map_client)
    big = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
           + '<path d="M0 0h1v1H0Z"/>' * 50 + '</svg>').encode()
    response = upload(map_client, token, big)
    assert response.status_code == 415
    assert "larger than" in response.json()["detail"]


def test_identical_uploads_are_stored_once(map_client):
    token = login(map_client)
    first = upload(map_client, token, SVG.encode()).json()
    second = upload(map_client, token, SVG.encode()).json()
    assert second["reused"] is True
    assert second["asset_id"] == first["asset_id"]


# ==========================================================================
# 7. Label · 8. Badge
# ==========================================================================


def test_add_a_label_with_a_supported_variable(map_client):
    token = login(map_client)
    body = create(map_client, token, name="Labelled", definition={
        "shape": {"kind": "builtin", "builtin": "circle"},
        "label": {"enabled": True, "template": "{Code}", "position": "below",
                  "font_size": 12, "colour": "#111827"},
    }).json()
    assert body["definition"]["label"]["template"] == "{Code}"
    assert "<text" in body["preview"]["svg"]


def test_an_unknown_label_variable_is_refused(map_client):
    token = login(map_client)
    response = create(map_client, token, definition={
        "label": {"enabled": True, "template": "{Password}"}})
    assert response.status_code == 422
    assert "Unknown label variable" in response.json()["detail"]


def test_a_label_cannot_carry_markup(map_client):
    token = login(map_client)
    assert create(map_client, token, definition={
        "label": {"enabled": True, "template": "<img src=x onerror=alert(1)>"}},
    ).status_code == 422


def test_a_hostile_entity_name_is_escaped_not_executed():
    """The template is administrator-authored; the *value* is warehouse data."""
    definition = parse_definition({"label": {"enabled": True, "template": "{Name}"}})
    svg = render(definition, context={"Name": "</text><script>alert(1)</script>"}).svg
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


def test_add_a_badge(map_client):
    token = login(map_client)
    body = create(map_client, token, name="Badged", definition={
        "shape": {"kind": "builtin", "builtin": "square"},
        "badge": {"enabled": True, "kind": "alert", "position": "top-right",
                  "fill": "#DC2626", "text": "!"},
    }).json()
    assert body["definition"]["badge"]["kind"] == "alert"
    assert "<circle" in body["preview"]["svg"]
    # A decorated marker is no longer a bare vector symbol.
    assert body["preview"]["vector_only"] is False


def test_badge_text_is_bounded(map_client):
    token = login(map_client)
    assert create(map_client, token, definition={
        "badge": {"enabled": True, "text": "much too long"}},
    ).status_code == 422


# ==========================================================================
# 9-10. Save and edit · 28. Versioning
# ==========================================================================


def test_edit_a_design(map_client):
    token = login(map_client)
    created = create(map_client, token, name="Before").json()
    response = map_client.put(
        f"/api/map/marker-designs/{created['design_id']}", headers=auth(token),
        json={"name": "After", "definition": {
            "shape": {"kind": "builtin", "builtin": "star", "fill": "#F59E0B"}}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "After"
    assert body["definition"]["shape"]["builtin"] == "star"


def test_editing_an_active_design_creates_a_version(map_client):
    token = login(map_client)
    created = create(map_client, token, name="Live",
                     status=MarkerDesignStatus.ACTIVE).json()
    design_id = created["design_id"]
    assert created["version"] == 1

    map_client.put(f"/api/map/marker-designs/{design_id}", headers=auth(token),
                   json={"definition": {"shape": {"kind": "builtin",
                                                  "builtin": "diamond"}}})
    updated = map_client.get(f"/api/map/marker-designs/{design_id}",
                             headers=auth(token)).json()
    assert updated["version"] == 2

    history = map_client.get(f"/api/map/marker-designs/{design_id}/versions",
                             headers=auth(token)).json()
    assert history["current_version"] == 2
    assert len(history["versions"]) == 1
    # The superseded configuration is intact and still renderable.
    assert history["versions"][0]["definition"]["shape"]["builtin"] == "circle"
    assert history["versions"][0]["preview_svg"].startswith("<svg")


def test_editing_a_draft_does_not_accumulate_versions(map_client):
    token = login(map_client)
    created = create(map_client, token, name="Draft work").json()
    for shape in ("square", "triangle", "star"):
        map_client.put(f"/api/map/marker-designs/{created['design_id']}",
                       headers=auth(token),
                       json={"definition": {"shape": {"kind": "builtin",
                                                      "builtin": shape}}})
    history = map_client.get(
        f"/api/map/marker-designs/{created['design_id']}/versions",
        headers=auth(token)).json()
    assert history["versions"] == []
    assert history["current_version"] == 1


# ==========================================================================
# 11. Duplicate
# ==========================================================================


def test_duplicate_a_design(map_client):
    token = login(map_client)
    created = create(map_client, token, name="Territory Default",
                     status=MarkerDesignStatus.ACTIVE).json()
    response = map_client.post(
        f"/api/map/marker-designs/{created['design_id']}/duplicate",
        headers=auth(token), json={"name": "Territory Premium"})
    assert response.status_code == 201
    copy = response.json()
    assert copy["name"] == "Territory Premium"
    assert copy["design_id"] != created["design_id"]
    assert copy["definition"] == created["definition"]
    # A copy must not silently start drawing on the map.
    assert copy["status"] == MarkerDesignStatus.DRAFT
    assert copy["is_system_default"] is False


def test_duplicate_without_a_name_gets_a_unique_one(map_client):
    token = login(map_client)
    created = create(map_client, token, name="Base").json()
    first = map_client.post(
        f"/api/map/marker-designs/{created['design_id']}/duplicate",
        headers=auth(token), json={}).json()
    second = map_client.post(
        f"/api/map/marker-designs/{created['design_id']}/duplicate",
        headers=auth(token), json={}).json()
    assert first["name"] == "Base (copy)"
    assert second["name"] == "Base (copy 2)"


def test_modifying_a_duplicate_leaves_the_original_alone(map_client):
    token = login(map_client)
    original = create(map_client, token, name="Original").json()
    copy = map_client.post(
        f"/api/map/marker-designs/{original['design_id']}/duplicate",
        headers=auth(token), json={"name": "Variant"}).json()

    map_client.put(f"/api/map/marker-designs/{copy['design_id']}",
                   headers=auth(token),
                   json={"definition": {"shape": {"kind": "builtin",
                                                  "builtin": "cross",
                                                  "fill": "#000000"}}})
    unchanged = map_client.get(f"/api/map/marker-designs/{original['design_id']}",
                               headers=auth(token)).json()
    assert unchanged["definition"]["shape"]["builtin"] == "circle"


# ==========================================================================
# 12-13. Assign · 20. priority
# ==========================================================================


def test_assign_a_design_to_territory(map_client):
    token = login(map_client)
    created = create(map_client, token, name="Territory Premium",
                     entity_type="territory").json()
    response = map_client.post(
        f"/api/map/marker-designs/{created['design_id']}/assign",
        headers=auth(token), json={})
    assert response.status_code == 200
    assert response.json()["entity_type"] == "territory"
    assert response.json()["entity_code"] is None
    # Assigning a draft promotes it, otherwise the assignment would be invisible.
    assert response.json()["design"]["status"] == MarkerDesignStatus.ACTIVE

    config = map_client.get("/api/map/marker-config", headers=auth(token)).json()
    assert config["markers"]["territory"]["design_id"] == created["design_id"]
    assert config["markers"]["territory"]["source"] == "entity_type"


def test_assign_a_design_to_customer(map_client):
    token = login(map_client)
    created = create(map_client, token, name="Customer Store",
                     entity_type="customer", definition={
                         "shape": {"kind": "builtin", "builtin": "pin"},
                         "icon": {"enabled": True, "source": "library",
                                  "name": "store"}}).json()
    map_client.post(f"/api/map/marker-designs/{created['design_id']}/assign",
                    headers=auth(token), json={})
    config = map_client.get("/api/map/marker-config", headers=auth(token)).json()
    assert config["markers"]["customer"]["design_name"] == "Customer Store"


def test_a_specific_entity_assignment_beats_the_entity_type(map_client):
    """Priority: specific entity → entity type → system default."""
    token = login(map_client)
    general = create(map_client, token, name="Regular Customer",
                     entity_type="customer").json()
    premium = create(map_client, token, name="Premium Customer",
                     entity_type="customer", definition={
                         "shape": {"kind": "builtin", "builtin": "star",
                                   "fill": "#F59E0B"}}).json()

    map_client.post(f"/api/map/marker-designs/{general['design_id']}/assign",
                    headers=auth(token), json={})
    map_client.post(f"/api/map/marker-designs/{premium['design_id']}/assign",
                    headers=auth(token), json={"entity_code": "CUST-001"})

    general_marker = map_client.get("/api/map/marker-config",
                                    headers=auth(token)).json()["markers"]["customer"]
    assert general_marker["design_name"] == "Regular Customer"
    assert general_marker["source"] == "entity_type"

    specific = map_client.get("/api/map/marker-config/customer/CUST-001",
                              headers=auth(token)).json()
    assert specific["design_name"] == "Premium Customer"
    assert specific["source"] == "entity"

    other = map_client.get("/api/map/marker-config/customer/CUST-999",
                           headers=auth(token)).json()
    assert other["design_name"] == "Regular Customer"


def test_with_nothing_assigned_the_system_default_is_used(map_client):
    token = login(map_client)
    config = map_client.get("/api/map/marker-config", headers=auth(token)).json()
    region = config["markers"]["region"]
    assert region["source"] == "system_default"
    assert region["design_name"] == "Region Default"


def test_reassigning_replaces_rather_than_stacking(map_client, agent_engine):
    token = login(map_client)
    first = create(map_client, token, name="First", entity_type="zone").json()
    second = create(map_client, token, name="Second", entity_type="zone").json()
    for design in (first, second):
        map_client.post(f"/api/map/marker-designs/{design['design_id']}/assign",
                        headers=auth(token), json={})

    with Session(agent_engine) as session:
        count = session.execute(
            select(func.count()).select_from(MapMarkerAssignment)
            .where(MapMarkerAssignment.entity_type == "zone")
        ).scalar_one()
    assert count == 1
    config = map_client.get("/api/map/marker-config", headers=auth(token)).json()
    assert config["markers"]["zone"]["design_name"] == "Second"


# ==========================================================================
# 14-15. Activate / deactivate
# ==========================================================================


def test_activate_and_deactivate_a_design(map_client):
    token = login(map_client)
    created = create(map_client, token, name="Toggle", entity_type="area").json()
    design_id = created["design_id"]

    activated = map_client.post(f"/api/map/marker-designs/{design_id}/activate",
                                headers=auth(token))
    assert activated.json()["status"] == MarkerDesignStatus.ACTIVE

    map_client.post(f"/api/map/marker-designs/{design_id}/assign",
                    headers=auth(token), json={})
    config = map_client.get("/api/map/marker-config", headers=auth(token)).json()
    assert config["markers"]["area"]["design_name"] == "Toggle"

    deactivated = map_client.post(f"/api/map/marker-designs/{design_id}/deactivate",
                                  headers=auth(token))
    assert deactivated.json()["status"] == MarkerDesignStatus.INACTIVE

    # Deactivating falls back without the assignment having to be removed.
    config = map_client.get("/api/map/marker-config", headers=auth(token)).json()
    assert config["markers"]["area"]["source"] == "system_default"


def test_the_system_default_cannot_be_deactivated_or_deleted(map_client, agent_engine):
    token = login(map_client)
    with Session(agent_engine) as session:
        default_id = session.execute(
            select(MapMarkerDesign.design_id)
            .where(MapMarkerDesign.entity_type == "region",
                   MapMarkerDesign.is_system_default.is_(True))
        ).scalar_one()

    assert map_client.post(f"/api/map/marker-designs/{default_id}/deactivate",
                           headers=auth(token)).status_code == 409
    assert map_client.request("DELETE", f"/api/map/marker-designs/{default_id}",
                              headers=auth(token),
                              params={"confirm": True}).status_code == 409


# ==========================================================================
# 17. Delete protection
# ==========================================================================


def test_a_design_in_use_needs_confirmation_before_deletion(map_client):
    token = login(map_client)
    created = create(map_client, token, name="In Use", entity_type="unit").json()
    design_id = created["design_id"]
    map_client.post(f"/api/map/marker-designs/{design_id}/assign",
                    headers=auth(token), json={})

    refused = map_client.request("DELETE", f"/api/map/marker-designs/{design_id}",
                                 headers=auth(token))
    assert refused.status_code == 409
    assert "in use" in refused.json()["detail"]

    accepted = map_client.request("DELETE", f"/api/map/marker-designs/{design_id}",
                                  headers=auth(token), params={"confirm": True})
    assert accepted.status_code == 200
    # The entity falls back rather than losing its marker entirely.
    config = map_client.get("/api/map/marker-config", headers=auth(token)).json()
    assert config["markers"]["unit"]["source"] == "system_default"


def test_an_unused_design_deletes_without_confirmation(map_client):
    token = login(map_client)
    created = create(map_client, token, name="Unused").json()
    response = map_client.request("DELETE",
                                  f"/api/map/marker-designs/{created['design_id']}",
                                  headers=auth(token))
    assert response.status_code == 200


# ==========================================================================
# 16. Reset to system default
# ==========================================================================


def test_reset_to_default_removes_the_assignment_but_keeps_the_design(map_client):
    token = login(map_client)
    created = create(map_client, token, name="Custom Zone", entity_type="zone").json()
    map_client.post(f"/api/map/marker-designs/{created['design_id']}/assign",
                    headers=auth(token), json={})

    unconfirmed = map_client.post("/api/map/assignments/zone/reset",
                                  headers=auth(token))
    assert unconfirmed.status_code == 400

    response = map_client.post("/api/map/assignments/zone/reset",
                               headers=auth(token), params={"confirm": True})
    assert response.status_code == 200
    assert response.json()["assignments_removed"] == 1

    config = map_client.get("/api/map/marker-config", headers=auth(token)).json()
    assert config["markers"]["zone"]["source"] == "system_default"
    # The work itself survives a reset.
    assert map_client.get(f"/api/map/marker-designs/{created['design_id']}",
                          headers=auth(token)).status_code == 200


# ==========================================================================
# 17-18. Persistence
# ==========================================================================


def test_configuration_persists_across_clients(map_client, agent_engine):
    """Standing in for "refresh the browser": a fresh client, same database."""
    token = login(map_client)
    created = create(map_client, token, name="Persistent", entity_type="territory",
                     definition={"shape": {"kind": "builtin", "builtin": "flag",
                                           "fill": "#0EA5E9"}}).json()
    map_client.post(f"/api/map/marker-designs/{created['design_id']}/assign",
                    headers=auth(token), json={})

    with TestClient(app) as fresh:
        fresh_token = login(fresh)
        config = fresh.get("/api/map/marker-config",
                           headers=auth(fresh_token)).json()
    assert config["markers"]["territory"]["design_name"] == "Persistent"
    assert config["markers"]["territory"]["design_id"] == created["design_id"]


# ==========================================================================
# 19-20. Map integration · MapLibre marker payloads
# ==========================================================================


def test_marker_config_serves_every_entity_type(map_client):
    token = login(map_client)
    config = map_client.get("/api/map/marker-config", headers=auth(token)).json()
    assert set(config["markers"]) == set(ENTITY_TYPE_BY_KEY)
    for entity, marker in config["markers"].items():
        assert marker["preview_svg"].startswith("<svg"), entity
        assert marker["size"]["width"] > 0


def test_a_marker_is_delivered_as_svg_art_maplibre_can_register(map_client):
    """One payload shape, because one engine draws the map.

    MapLibre takes an image through ``addImage``, so the SVG, its size and its
    anchor are everything a marker layer needs. The Google Maps adapter that
    used to emit a second, ``google.maps.Symbol``-shaped payload is gone with the
    renderer that consumed it.
    """
    token = login(map_client)
    created = create(map_client, token, name="Plain", entity_type="region",
                     definition={"shape": {"kind": "builtin", "builtin": "pin",
                                           "fill": "#DC2626", "size": 40}}).json()
    map_client.post(f"/api/map/marker-designs/{created['design_id']}/assign",
                    headers=auth(token), json={})

    config = map_client.get("/api/map/marker-config", headers=auth(token)).json()
    entry = config["markers"]["region"]
    marker = entry["marker"]

    assert config["renderer"] == "generic"
    assert marker["kind"] == "svg"
    assert marker["data_uri"].startswith("data:image/svg+xml;base64,")
    decoded = base64.b64decode(marker["data_uri"].split(",", 1)[1]).decode()
    assert decoded.startswith("<svg") and "<script" not in decoded
    assert marker["size"]["width"] > 0 and marker["size"]["height"] > 0
    # A pin is anchored at its tip, not its centre — which is what MapLibre's
    # `icon-anchor` has to agree with for the point to sit on its coordinate.
    assert marker["anchor"]["y"] > marker["anchor"]["x"]
    assert entry["preview_svg"] == marker["svg"]


def test_a_decorated_marker_is_still_one_image(map_client):
    """A label, badge or icon cannot be a bare path, and needs no second format.

    The whole marker — shape, artwork and text — is rasterised into one SVG, so
    a decorated marker costs MapLibre exactly one registered image and no DOM
    node, which is what keeps thousands of them affordable.
    """
    token = login(map_client)
    # A customer, not a warehouse: that entity type went with the dimension in
    # revision 0020. The *icon* library still offers a warehouse glyph, and
    # using it here is the point — an icon is artwork an operator may put on any
    # marker, and it never implied a dimension of its own.
    created = create(map_client, token, name="Rich", entity_type="customer",
                     definition={
                         "shape": {"kind": "builtin", "builtin": "square"},
                         "icon": {"enabled": True, "source": "library",
                                  "name": "warehouse"},
                         "label": {"enabled": True, "template": "{Code}"}}).json()
    map_client.post(f"/api/map/marker-designs/{created['design_id']}/assign",
                    headers=auth(token), json={})

    config = map_client.get("/api/map/marker-config", headers=auth(token)).json()
    marker = config["markers"]["customer"]["marker"]
    assert marker["kind"] == "svg"
    assert marker["vector_only"] is False
    decoded = base64.b64decode(marker["data_uri"].split(",", 1)[1]).decode()
    assert decoded.startswith("<svg") and "<script" not in decoded
    assert marker["anchor"]["x"] > 0


def test_the_renderer_registry_holds_exactly_one_adapter():
    """Two adapters meant two payloads to keep in step. There is now one.

    Guarded rather than left implicit: re-introducing a second adapter is a
    decision that should show up as a failing test, because it also
    re-introduces a ``renderer`` parameter on every endpoint that resolves a
    marker.
    """
    from app.map import adapters

    assert adapters.ADAPTERS == (adapters.GENERIC_SVG,)
    assert not hasattr(adapters, "GOOGLE_MAPS")
    assert adapters.get_adapter(None).key == adapters.GENERIC_SVG


def test_an_unknown_renderer_is_refused():
    from app.map import adapters

    with pytest.raises(ValueError, match="papyrus"):
        adapters.get_adapter("papyrus")


# ==========================================================================
# 22-23. Legend
# ==========================================================================


def test_the_legend_uses_the_configured_markers_and_follows_changes(map_client):
    token = login(map_client)
    before = map_client.get("/api/map/legend", headers=auth(token)).json()
    territory = next(e for e in before["entries"] if e["entity_type"] == "territory")
    assert territory["design_name"] == "Territory Default"
    assert territory["preview_svg"].startswith("<svg")

    created = create(map_client, token, name="Legend Marker",
                     entity_type="territory",
                     definition={"shape": {"kind": "builtin", "builtin": "star",
                                           "fill": "#EAB308"}}).json()
    map_client.post(f"/api/map/marker-designs/{created['design_id']}/assign",
                    headers=auth(token), json={})

    after = map_client.get("/api/map/legend", headers=auth(token)).json()
    entry = next(e for e in after["entries"] if e["entity_type"] == "territory")
    assert entry["design_name"] == "Legend Marker"
    assert "#EAB308" in entry["preview_svg"]


# ==========================================================================
# 24. Caching
# ==========================================================================


def test_saving_a_design_invalidates_the_cache(map_client):
    token = login(map_client)
    first = map_client.get("/api/map/marker-config", headers=auth(token)).json()
    generation = first["generation"]

    created = create(map_client, token, name="Cache Buster",
                     entity_type="territory").json()
    map_client.post(f"/api/map/marker-designs/{created['design_id']}/assign",
                    headers=auth(token), json={})

    second = map_client.get("/api/map/marker-config", headers=auth(token)).json()
    assert second["generation"] > generation
    assert second["markers"]["territory"]["design_name"] == "Cache Buster"


def test_repeated_reads_are_served_from_cache(map_client):
    token = login(map_client)
    one = map_client.get("/api/map/marker-config", headers=auth(token)).json()
    two = map_client.get("/api/map/marker-config", headers=auth(token)).json()
    assert one["generation"] == two["generation"]
    assert one["markers"] == two["markers"]


# ==========================================================================
# 26. Performance — many markers
# ==========================================================================


def test_many_entity_markers_resolve_from_one_config_call(map_client, agent_engine):
    """A map with thousands of pins loads one configuration, not one per pin."""
    token = login(map_client)
    created = create(map_client, token, name="Bulk", entity_type="customer").json()
    map_client.post(f"/api/map/marker-designs/{created['design_id']}/assign",
                    headers=auth(token), json={})

    config = map_client.get("/api/map/marker-config", headers=auth(token)).json()
    marker = config["markers"]["customer"]
    # One payload describes every customer pin; the map reuses it per point.
    assert marker["design_name"] == "Bulk"
    assert "marker" in marker

    with Session(agent_engine) as session:
        resolver.invalidate_cache()
        for index in range(200):
            resolved = resolver.resolve(session, "customer", f"CUST-{index:04d}")
            assert resolved.design_name == "Bulk"


# ==========================================================================
# 27. Audit log
# ==========================================================================


def test_every_important_change_is_audited(map_client, agent_engine):
    token = login(map_client)
    created = create(map_client, token, name="Audited", entity_type="area").json()
    design_id = created["design_id"]
    map_client.put(f"/api/map/marker-designs/{design_id}", headers=auth(token),
                   json={"name": "Audited v2"})
    map_client.post(f"/api/map/marker-designs/{design_id}/duplicate",
                    headers=auth(token), json={})
    map_client.post(f"/api/map/marker-designs/{design_id}/assign",
                    headers=auth(token), json={})
    map_client.post(f"/api/map/marker-designs/{design_id}/deactivate",
                    headers=auth(token))
    map_client.post(f"/api/map/marker-designs/{design_id}/activate",
                    headers=auth(token))
    map_client.request("DELETE", f"/api/map/marker-designs/{design_id}",
                       headers=auth(token), params={"confirm": True})

    with Session(agent_engine) as session:
        actions = {
            row.action for row in session.execute(
                select(AuditLog).where(AuditLog.username == "root")
            ).scalars()
        }
    assert {
        AuditAction.MARKER_DESIGN_CREATED, AuditAction.MARKER_DESIGN_UPDATED,
        AuditAction.MARKER_DESIGN_DUPLICATED, AuditAction.MARKER_DESIGN_ASSIGNED,
        AuditAction.MARKER_DESIGN_DEACTIVATED, AuditAction.MARKER_DESIGN_ACTIVATED,
        AuditAction.MARKER_DESIGN_DELETED,
    } <= actions


def test_an_update_records_the_old_and_new_configuration(map_client, agent_engine):
    token = login(map_client)
    created = create(map_client, token, name="Tracked",
                     status=MarkerDesignStatus.ACTIVE).json()
    map_client.put(f"/api/map/marker-designs/{created['design_id']}",
                   headers=auth(token),
                   json={"definition": {"shape": {"kind": "builtin",
                                                  "builtin": "hexagon"}}})
    with Session(agent_engine) as session:
        entry = session.execute(
            select(AuditLog).where(AuditLog.action == AuditAction.MARKER_DESIGN_UPDATED)
        ).scalars().first()
    assert entry.detail["old"]["shape"]["builtin"] == "circle"
    assert entry.detail["new"]["shape"]["builtin"] == "hexagon"


# ==========================================================================
# 24. Unauthorized user (RBAC)
# ==========================================================================


DESIGNER_PATHS = [
    "/api/map/marker-designs",
    "/api/map/entity-types",
    "/api/map/shapes",
    "/api/map/icons",
    "/api/map/designer-options",
    "/api/map/assignments",
    "/api/map/marker-assets",
]


@pytest.mark.parametrize("path", DESIGNER_PATHS)
def test_a_user_without_map_settings_is_refused(map_client, path):
    token = login(map_client, "dhaka_rm")
    assert map_client.get(path, headers=auth(token)).status_code == 403


def test_an_unauthorised_user_cannot_write(map_client):
    token = login(map_client, "dhaka_rm")
    assert create(map_client, token).status_code == 403
    assert map_client.post("/api/map/marker-assets", headers=auth(token),
                           files={"file": ("a.svg", SVG.encode(), "image/svg+xml")}
                           ).status_code == 403


def test_an_anonymous_caller_is_refused(map_client):
    assert map_client.get("/api/map/marker-designs").status_code == 401
    assert map_client.get("/api/map/marker-config").status_code == 401


def test_map_settings_can_be_granted_without_administration(map_client, agent_engine):
    """The permission is its own thing, not a synonym for administrator."""
    admin_token = login(map_client)
    with Session(agent_engine) as session:
        user_id = session.execute(
            select(AppUser.user_id).where(AppUser.username == "dhaka_rm")
        ).scalar_one()
    map_client.put(f"/api/admin/users/{user_id}/permissions",
                   headers=auth(admin_token),
                   json={"permissions": {SectionKey.MAP_SETTINGS: ALLOW}})

    token = login(map_client, "dhaka_rm")
    assert map_client.get("/api/map/marker-designs",
                          headers=auth(token)).status_code == 200
    # …and administration stays out of reach.
    assert map_client.get("/api/admin/users", headers=auth(token)).status_code == 403


def test_map_settings_can_be_denied_to_an_administrator(map_client, agent_engine):
    admin_token = login(map_client)
    with Session(agent_engine) as session:
        user_id = session.execute(
            select(AppUser.user_id).where(AppUser.username == "root")
        ).scalar_one()
    map_client.put(f"/api/admin/users/{user_id}/permissions",
                   headers=auth(admin_token),
                   json={"permissions": {SectionKey.MAP_SETTINGS: DENY}})
    assert map_client.get("/api/map/marker-designs",
                          headers=auth(admin_token)).status_code == 403


def test_viewers_can_read_the_marker_config_without_map_settings(map_client):
    """Everyone who sees the map needs its markers; nobody needs the designer."""
    token = login(map_client, "dhaka_rm")
    assert map_client.get("/api/map/marker-config",
                          headers=auth(token)).status_code == 200
    assert map_client.get("/api/map/legend", headers=auth(token)).status_code == 200
    assert map_client.get("/api/map/marker-designs",
                          headers=auth(token)).status_code == 403


# ==========================================================================
# 30. Export / import
# ==========================================================================


def test_export_and_import_round_trip(map_client):
    token = login(map_client)
    create(map_client, token, name="Exportable", entity_type="territory",
           definition={"shape": {"kind": "builtin", "builtin": "star",
                                 "fill": "#111111"}})

    export = map_client.get("/api/map/marker-designs-export", headers=auth(token))
    assert export.status_code == 200
    assert "attachment" in export.headers["content-disposition"]
    payload = json.loads(export.content)
    assert payload["format"] == "marker-designs/v1"
    names = {d["name"] for d in payload["designs"]}
    assert "Exportable" in names
    # No secret ever travels in a configuration export.
    body = export.content.decode().lower()
    assert "password" not in body and "token" not in body and "api_key" not in body

    imported = map_client.post("/api/map/marker-designs-import", headers=auth(token),
                               json={"designs": payload["designs"]})
    assert imported.status_code == 200
    # Existing UUIDs are skipped rather than duplicated.
    assert imported.json()["created"] == 0
    assert imported.json()["skipped"] >= 1


def test_import_revalidates_untrusted_definitions(map_client):
    token = login(map_client)
    response = map_client.post("/api/map/marker-designs-import", headers=auth(token),
                               json={"designs": [
                                   {"name": "Hostile", "entity_type": "territory",
                                    "definition": {"label": {
                                        "enabled": True,
                                        "template": "<script>alert(1)</script>"}}},
                                   {"name": "Bad entity", "entity_type": "wormhole",
                                    "definition": {}},
                                   {"name": "Fine", "entity_type": "region",
                                    "definition": {}},
                               ]})
    assert response.status_code == 200
    body = response.json()
    assert body["created"] == 1
    assert body["skipped"] == 2
    assert len(body["problems"]) == 2


# ==========================================================================
# Preview
# ==========================================================================


def test_preview_renders_without_saving(map_client, agent_engine):
    token = login(map_client)
    with Session(agent_engine) as session:
        before = session.execute(
            select(func.count()).select_from(MapMarkerDesign)
        ).scalar_one()

    response = map_client.post("/api/map/marker-designs/preview", headers=auth(token),
                               json={"definition": {
                                   "shape": {"kind": "builtin", "builtin": "star",
                                             "fill": "#123456"}},
                                   "context": {"Code": "T009"}})
    assert response.status_code == 200
    body = response.json()
    assert "#123456" in body["svg"]
    assert body["marker"]["kind"] == "svg"

    with Session(agent_engine) as session:
        after = session.execute(
            select(func.count()).select_from(MapMarkerDesign)
        ).scalar_one()
    assert after == before


def test_preview_rejects_an_invalid_definition(map_client):
    token = login(map_client)
    assert map_client.post("/api/map/marker-designs/preview", headers=auth(token),
                           json={"definition": {"shape": {"kind": "builtin",
                                                          "builtin": "nope"}}}
                           ).status_code == 422


def test_an_uploaded_asset_can_back_a_design(map_client):
    token = login(map_client)
    asset = upload(map_client, token, SVG.encode()).json()
    created = create(map_client, token, name="Uploaded",
                     design_type=MarkerDesignType.CUSTOM_IMAGE,
                     asset_id=asset["asset_id"],
                     definition={"shape": {"kind": "image", "size": 40}}).json()
    assert created["asset_id"] == asset["asset_id"]
    assert "#123456" in created["preview"]["svg"]


def test_a_design_cannot_reference_a_missing_asset(map_client):
    token = login(map_client)
    assert create(map_client, token, asset_id=999999).status_code == 422


# ==========================================================================
# Library listing
# ==========================================================================


def test_the_library_lists_designs_with_previews_and_usage(map_client):
    token = login(map_client)
    created = create(map_client, token, name="Listed", entity_type="territory").json()
    map_client.post(f"/api/map/marker-designs/{created['design_id']}/assign",
                    headers=auth(token), json={})

    body = map_client.get("/api/map/marker-designs", headers=auth(token),
                          params={"entity_type": "territory"}).json()
    listed = next(d for d in body["designs"] if d["design_id"] == created["design_id"])
    assert listed["in_use"] is True
    assert listed["preview"]["svg"].startswith("<svg")
    assert all(d["entity_type"] == "territory" for d in body["designs"])


def test_the_library_can_be_searched_and_filtered_by_status(map_client):
    token = login(map_client)
    create(map_client, token, name="Findable Alpha", status=MarkerDesignStatus.ACTIVE)
    create(map_client, token, name="Other Beta")

    found = map_client.get("/api/map/marker-designs", headers=auth(token),
                           params={"search": "Findable"}).json()
    assert [d["name"] for d in found["designs"]] == ["Findable Alpha"]

    drafts = map_client.get("/api/map/marker-designs", headers=auth(token),
                            params={"status": "DRAFT"}).json()
    assert all(d["status"] == "DRAFT" for d in drafts["designs"])
