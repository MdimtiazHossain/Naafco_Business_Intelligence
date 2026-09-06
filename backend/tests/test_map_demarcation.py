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


# ==========================================================================
# What the tab draws is exactly the coordinate table
# ==========================================================================


def test_every_drawable_level_is_visible_on_the_demarcation_design(agent_engine):
    """A hidden level on this map is a hidden *row*.

    On a map of figures a hidden layer is one fewer thing competing for the
    eye. On a map of coordinates it is data the reader is not shown and is not
    told about, which is what ``0036`` exists to correct.
    """
    from app.map.levels import LEVEL_KEYS

    with Session(agent_engine) as db:
        design = designs.default_design(db, DesignPurpose.DEMARCATION)
        drawn = {layer.point_level for layer in design.layers if layer.is_visible}
    assert drawn == set(LEVEL_KEYS), sorted(set(LEVEL_KEYS) - drawn)


def test_the_points_drawn_equal_the_rows_stored(client, agent_engine):
    """The acid test: unfiltered, the map is the table.

    If these two ever disagree the map is the one that is wrong — it is drawing
    a subset of ``map_entity_locations`` and calling it the whole.
    """
    from sqlalchemy import func
    from app.database.models_map import MapEntityLocation

    headers = login(client)
    body = fetch(client, headers)
    drawn = sum(layer["placed"] for layer in body["layers"])
    with Session(agent_engine) as db:
        stored = db.execute(
            select(func.count()).select_from(MapEntityLocation)).scalar_one()
    assert drawn == stored


def test_the_tooltip_can_tell_a_placed_point_from_a_computed_one(client):
    """``source`` travels per feature: the two are the same dot otherwise.

    A centroid and a surveyed position mean different things on a boundary, and
    a reader deciding where a line falls must not have to guess which they are
    looking at.
    """
    headers = login(client)
    region = layer_of(fetch(client, headers, levels=["region"]), "region")
    for feature in region["features"]["features"]:
        assert feature["properties"]["source"] in {"UPLOAD", "MANUAL", "DERIVED"}


# ==========================================================================
# A filter narrows by containment, not by row
# ==========================================================================


def test_a_region_draws_itself_and_everything_under_it(client, agent_engine):
    """The rule this module parts company with every report table over.

    A report ANDs its filters and a row matches only on a level it carries, so
    the flat rule would drop every coordinate that states no region — which is
    all of them except the region's own. A map means containment.
    """
    with Session(agent_engine) as db:
        geo.derive_parents(db)
        db.commit()

    headers = login(client)
    body = fetch(client, headers, region_code="REG001")

    region = layer_of(body, "region")
    assert {f["properties"]["code"] for f in region["features"]["features"]} == {"REG001"}

    # Descendants are drawn...
    territory = layer_of(body, "territory")
    assert territory["placed"] > 0, "the territories inside REG001 are its subtree"

    # ...and ancestors are not. This is the half a flat filter gets wrong in
    # the other direction: REG001's zone is not inside REG001.
    for ancestor in ("zone", "sales_line", "bu", "company"):
        assert layer_of(body, ancestor)["placed"] == 0, ancestor


def test_a_filter_matching_nothing_says_which_selection_emptied_it(client):
    """"No data" is the answer this platform refuses everywhere else."""
    headers = login(client)
    body = fetch(client, headers, region_code="REG002")
    customer = layer_of(body, "customer")
    if customer["placed"] == 0 and customer["available"]:
        assert any("REG002" in note for note in customer["notes"]), customer["notes"]


def test_each_layer_reports_matched_out_of_placed(client, agent_engine):
    """"9 points" and "9 of 94 points" are different findings.

    Without the denominator a reader cannot tell a narrow filter from a level
    nobody has mapped yet, which on a demarcation map is the more useful of the
    two things to know.
    """
    with Session(agent_engine) as db:
        geo.derive_parents(db)
        db.commit()
    headers = login(client)
    narrowed = layer_of(fetch(client, headers, region_code="REG001"), "territory")
    whole = layer_of(fetch(client, headers), "territory")
    assert narrowed["available"] == whole["placed"]
    assert narrowed["placed"] <= narrowed["available"]


# ==========================================================================
# Scope is the ceiling; the filter narrows inside it
# ==========================================================================


def test_a_filter_outside_the_readers_scope_is_refused_by_name(client):
    """403 naming the code, never an empty map.

    The two are indistinguishable on screen and mean opposite things: "there is
    nothing there" against "you may not see that". ``dhaka_rm`` is scoped to
    REG001, so asking for REG002 is asking for somebody else's coordinates.
    """
    headers = login(client, "dhaka_rm")
    response = client.get("/api/map/locations", headers=headers,
                          params={"region_code": "REG002"})
    assert response.status_code == 403, response.text
    assert "REG002" in response.text

    # And the reader's *own* region is allowed. Without this half the test
    # passes on a scope check that refuses everything: the level was being
    # spelled `region` where the permission layer spells it `region_code`, so
    # the ancestor walk found nothing and answered False for every code — a
    # refusal indistinguishable from the correct one, on the map's most
    # ordinary request.
    allowed = client.get("/api/map/locations", headers=headers,
                         params={"region_code": "REG001"})
    assert allowed.status_code == 200, allowed.text


def test_a_level_above_the_selection_says_so_rather_than_counting(client,
                                                                  agent_engine):
    """The containment rule emptied it, and that is a different sentence.

    Selecting a region empties zone, sales line, business unit and company by
    construction. Read through the counts that came out as "none of the 4
    placed zone coordinates is inside region REG001", which describes the data
    and sends a reader looking for a coordinate that is loaded and fine.
    """
    with Session(agent_engine) as db:
        geo.derive_parents(db)
        db.commit()

    body = fetch(client, login(client), region_code="REG001")
    zone = layer_of(body, "zone")
    assert zone["placed"] == 0
    note = " ".join(zone["notes"])
    assert "sits above" in note and "Clear it" in note
    assert "has a coordinate" not in note, (
        "a level the selection excludes is not a level nobody has surveyed"
    )


def test_a_scoped_reader_is_not_told_to_clear_a_filter_they_do_not_have(client,
                                                                       agent_engine):
    """Their role emptied the level, and a role is not something to clear."""
    with Session(agent_engine) as db:
        geo.derive_parents(db)
        db.commit()

    body = fetch(client, login(client, "dhaka_rm"))
    zone = layer_of(body, "zone")
    assert zone["placed"] == 0
    assert not any("Clear it" in note for note in zone["notes"]), (
        "advice a reader cannot take is worse than none"
    )
    # The scope note is what explains it, once, for the whole map.
    assert body["scope_note"]


def test_missing_counts_records_without_a_coordinate_not_ones_filtered_out(
        client, agent_engine):
    """A placed point a filter excluded has not gone missing.

    ``missing`` was ``total - placed``, so narrowing the map reported a data
    problem that grew with the narrowing — on the one screen whose job is
    saying which records still have to be surveyed.
    """
    with Session(agent_engine) as db:
        geo.derive_parents(db)
        db.commit()

    headers = login(client)
    whole = layer_of(fetch(client, headers), "region")
    narrowed = layer_of(fetch(client, headers, region_code="REG001"), "region")

    assert narrowed["placed"] < whole["placed"], "the filter must exclude one"
    assert narrowed["missing"] == whole["missing"], (
        "narrowing the map cannot create records that lack a coordinate"
    )


def test_the_denominator_is_scoped_too(client, agent_engine):
    """A scoped reader is never told a total they may not see.

    ``available`` is the "of how many" in "9 of 94", and it used to be the
    level's own row count — so a regional manager filtering to one of their own
    territories read the *national* figure as their denominator. Scope is the
    outer bound, and it bounds what a reader is told exists exactly as it bounds
    what they are shown: their 94 is their region's 16.
    """
    with Session(agent_engine) as db:
        geo.derive_parents(db)
        db.commit()

    # Region is the level the fixture separates on: two are placed nationally
    # and `dhaka_rm` may see one of them.
    national = layer_of(fetch(client, login(client)), "region")
    scoped = layer_of(fetch(client, login(client, "dhaka_rm")), "region")

    assert national["available"] == 2
    assert scoped["available"] == 1, (
        "the denominator must be the reader's own, not the whole map's"
    )
    # Unfiltered, a reader is shown everything they may see, so the two counts
    # agree — the pair reads "1 of 1" rather than "1 of 2".
    assert scoped["available"] == scoped["placed"]

    # And with a filter inside that scope, the denominator stays the scope's.
    inside = fetch(client, login(client, "dhaka_rm"), region_code="REG001")
    assert layer_of(inside, "region")["available"] == scoped["available"]


def test_a_reader_with_no_scope_counts_nothing_out_of_nothing(client):
    """Not "0 of 94": the denominator is a figure too, and it is not theirs."""
    body = fetch(client, login(client, "no_scope"))
    assert all(layer["available"] == 0 for layer in body["layers"])
    assert all(layer["placed"] == 0 for layer in body["layers"])


def test_a_scoped_reader_sees_their_own_subtree_without_asking(client, agent_engine):
    """Scope is the ceiling, so it applies whether or not a filter is sent."""
    with Session(agent_engine) as db:
        geo.derive_parents(db)
        db.commit()

    everything = fetch(client, login(client))
    scoped = fetch(client, login(client, "dhaka_rm"))

    theirs = layer_of(scoped, "region")
    assert {f["properties"]["code"] for f in theirs["features"]["features"]} == {"REG001"}
    assert sum(l["placed"] for l in scoped["layers"])         < sum(l["placed"] for l in everything["layers"])


def test_a_reader_with_no_scope_is_told_why_rather_than_shown_an_empty_map(client):
    """The third case: not an error, not somebody else's coordinates."""
    body = fetch(client, login(client, "no_scope"))
    assert sum(layer["placed"] for layer in body["layers"]) == 0
    assert body["scope_note"], "an empty map must say whose emptiness it is"
    assert "no data scope" in body["scope_note"].lower()


def test_the_filter_levels_are_derived_from_the_level_registry(client):
    """A hand-written level list is the failure CLAUDE.md opens with."""
    from app.map.levels import MAP_LEVELS
    from app.map.locations import FILTER_LEVELS

    assert FILTER_LEVELS == tuple(f"{level.key}_code" for level in MAP_LEVELS)

    published = client.get("/api/map/config", headers=login(client)).json()
    assert published["location_filters"] == list(FILTER_LEVELS), (
        "the browser is sent this list so it keeps none of its own"
    )

    # The browser draws a control per level, so it needs the list at build time
    # and cannot wait for a request. `LOCATION_FILTERS` in
    # `frontend/src/contexts/FilterContext.tsx` is that copy, and
    # `demarcation.test.tsx` pins it against the fixture below. Spelled here
    # rather than derived, deliberately: it is the notification. A level added
    # to `MAP_LEVELS` fails this line, which names the two files to update — the
    # alternative is a chain that derives itself on one side and silently
    # diverges on the other.
    assert published["location_filters"] == [
        "company_code", "bu_code", "sales_line_code", "zone_code",
        "region_code", "area_code", "unit_code", "territory_code",
        "sub_territory_code", "customer_code", "sales_force_code",
    ], (
        "the map gained or lost a level: update LOCATION_FILTERS in "
        "frontend/src/contexts/FilterContext.tsx and location_filters in "
        "frontend/src/test/mapFixtures.ts to match"
    )
