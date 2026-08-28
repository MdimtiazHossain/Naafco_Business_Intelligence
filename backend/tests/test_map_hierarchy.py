"""Hierarchical map filtering: a filter at any level includes its descendants.

The specification's acceptance test needs a hierarchy with a *second* branch, so
that entities under one territory can be proved absent when another is selected.
The shared fixture has one territory, so this module extends it with a second
branch and its own customers — which is also the only way to demonstrate
exclusion at all: the shipped demo data has every customer trading in every
sub-territory, so nothing there could ever be excluded.

Branch built on top of the seeded hierarchy:

    REG001 Dhaka                          REG002 Khulna
      └── AR001 Mirpur                      └── AR002 Khulna Sadar
            └── UN001                             └── UN002
                  └── TR001  ── STR001                  └── TR002 ── STR003
                             └─ STR002
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.auth.security import hash_password
from app.database.models import DimSubTerritory, DimTerritory, DimUnit
from app.database.models_ai import AppUser, Role, UserStatus
from app.database.models_warehouse import DimSalesForce
from app.main import app
from app.map import service
from app.ai.exceptions import PermissionDeniedError
from app.ai.permission_filter import UserContext
from app.map.hierarchy import (
    ORG_CHAIN,
    apply_data_scope,
    filter_codes,
    resolve_business_entities,
    resolve_org_scope,
)
from conftest_phase2 import sales_row

pytest.importorskip("multipart", reason="python-multipart is required by FastAPI forms")

PASSWORD = "Correct-Horse-9"
WINDOW = "date_from=2026-07-01&date_to=2026-09-30"


@pytest.fixture
def branched(agent_engine):
    """Add a second territory branch under REG002, with its own customers."""
    from app.etl.pipeline import run_import
    from app.etl.readers import RecordsSourceReader

    # UN002 and TR002 are already in place: the phase-3 fixture completes the
    # Khulna branch down to territory so that Khulna can carry a target. What is
    # added here is the sub-territory level the map needs.
    with Session(agent_engine) as session:
        session.add(DimSubTerritory(sub_territory_code="STR002",
                                    sub_territory_name="Kazipara South",
                                    territory_code="TR001"))
        session.add(DimSubTerritory(sub_territory_code="STR003",
                                    sub_territory_name="Daulatpur East",
                                    territory_code="TR002"))
        session.add(DimSalesForce(sales_force_code="SF001",
                                  sales_force_name="Dhaka Officer",
                                  territory_code="TR001"))
        session.add(DimSalesForce(sales_force_code="SF002",
                                  sales_force_name="Khulna Officer",
                                  territory_code="TR002"))
        session.commit()

    # Customers C001/C002 trade only under TR001; C999 only under TR002.
    records = [
        sales_row(**{"Invoice No": "H-1", "Date": "2026-08-05",
                     "Sub Territory Code": "STR001", "Customer Code": "C001",
                     "Warehouse Code": "WH-DHK"}),
        sales_row(**{"Invoice No": "H-2", "Date": "2026-08-06",
                     "Sub Territory Code": "STR002", "Customer Code": "C002",
                     "Warehouse Code": "WH-DHK"}),
        sales_row(**{"Invoice No": "H-3", "Date": "2026-08-07",
                     "Territory Code": "TR002", "Sub Territory Code": "STR003",
                     "Area Code": "AR002", "Unit Code": "UN002",
                     "Region Code": "REG002", "Customer Code": "C999",
                     "Warehouse Code": "WH-KHL"}),
    ]
    result = run_import(agent_engine, "sales",
                        RecordsSourceReader(records, source_name="h.csv"),
                        source_system="HIER")
    assert result.rejected_rows == 0, result.error_counts
    return agent_engine


@pytest.fixture
def client(branched, users, monkeypatch):
    import app.database.connection as connection

    monkeypatch.setattr(connection, "get_engine", lambda *a, **k: branched)

    with Session(branched) as session:
        session.add(AppUser(username="root", display_name="Administrator",
                            role=Role.SUPER_ADMIN, is_active=True,
                            status=UserStatus.ACTIVE,
                            password_hash=hash_password(PASSWORD)))
        for user in session.query(AppUser).all():
            if user.password_hash is None:
                user.password_hash = hash_password(PASSWORD)
        service.ensure_system_defaults(session)
        session.commit()

    def _session_override():
        session = Session(bind=branched, expire_on_commit=False, future=True)
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


def entities(client: TestClient, token: str, query: str = "") -> dict:
    response = client.get(f"/api/map/entities?{WINDOW}&{query}", headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def codes_of(payload: dict, entity_type: str) -> set[str]:
    return {e["id"] for e in payload["entities"] if e["type"] == entity_type}


# ==========================================================================
# The specification's acceptance test (item 26)
# ==========================================================================


def test_territory_filter_returns_the_whole_subtree(client):
    """T001 → itself, its sub-territories, its customers and its sales force."""
    token = login(client)
    body = entities(client, token, "territory_code=TR001")

    assert codes_of(body, "territory") == {"TR001"}
    assert codes_of(body, "sub_territory") == {"STR001", "STR002"}
    assert {"C001", "C002"} <= codes_of(body, "customer")
    assert "SF001" in codes_of(body, "sales_force")


def test_a_customer_of_another_territory_is_excluded(client):
    """C999 trades only under TR002, so selecting TR001 must not return it."""
    token = login(client)
    body = entities(client, token, "territory_code=TR001")

    assert "C999" not in codes_of(body, "customer")
    assert "STR003" not in codes_of(body, "sub_territory")
    assert "TR002" not in codes_of(body, "territory")
    assert "SF002" not in codes_of(body, "sales_force")


def test_the_other_branch_returns_only_its_own(client):
    token = login(client)
    body = entities(client, token, "territory_code=TR002")

    assert codes_of(body, "sub_territory") == {"STR003"}
    assert "C999" in codes_of(body, "customer")
    assert "C001" not in codes_of(body, "customer")
    assert "C002" not in codes_of(body, "customer")


def test_ancestors_are_returned_so_the_selection_can_be_placed(client):
    """Item 9: show the parent chain, but only the selected branch's."""
    token = login(client)
    body = entities(client, token,
                    "territory_code=TR001&layers=region,area,unit,territory")

    assert codes_of(body, "region") == {"REG001"}
    assert codes_of(body, "area") == {"AR001"}
    assert codes_of(body, "unit") == {"UN001"}
    # Not the unrelated branch's ancestors.
    assert "REG002" not in codes_of(body, "region")
    assert "AR002" not in codes_of(body, "area")


# ==========================================================================
# Every level (items 3–8, 25)
# ==========================================================================


def test_no_filter_returns_every_authorised_level(client):
    token = login(client)
    body = entities(client, token)
    counts = body["counts"]
    assert counts["region"] == 2
    assert counts["territory"] == 2
    assert counts["sub_territory"] == 3
    assert {"C001", "C002", "C999"} <= codes_of(body, "customer")


def test_zone_filter_includes_every_descendant(client):
    token = login(client)
    body = entities(client, token, "zone_code=Z001")
    assert body["counts"]["region"] == 2
    assert body["counts"]["territory"] == 2


def test_region_filter_narrows_to_its_own_branch(client):
    token = login(client)
    body = entities(client, token, "region_code=REG001")
    assert codes_of(body, "area") == {"AR001"}
    assert codes_of(body, "territory") == {"TR001"}
    assert codes_of(body, "sub_territory") == {"STR001", "STR002"}
    assert "C999" not in codes_of(body, "customer")


def test_area_filter_narrows_to_its_own_branch(client):
    token = login(client)
    body = entities(client, token, "area_code=AR002")
    assert codes_of(body, "unit") == {"UN002"}
    assert codes_of(body, "territory") == {"TR002"}
    assert "C001" not in codes_of(body, "customer")


def test_unit_filter_narrows_to_its_own_branch(client):
    token = login(client)
    body = entities(client, token, "unit_code=UN001")
    assert codes_of(body, "territory") == {"TR001"}
    assert codes_of(body, "sub_territory") == {"STR001", "STR002"}


def test_sub_territory_filter_returns_only_its_customers(client):
    """Item 7: ST001 → its customers, not its sibling's."""
    token = login(client)
    body = entities(client, token, "sub_territory_code=STR001")

    assert codes_of(body, "sub_territory") == {"STR001"}
    assert "C001" in codes_of(body, "customer")
    assert "C002" not in codes_of(body, "customer")   # C002 trades in STR002
    assert "C999" not in codes_of(body, "customer")


# ==========================================================================
# Multiple filters (item 14)
# ==========================================================================


def test_multiple_filters_intersect(client):
    token = login(client)
    body = entities(client, token, "region_code=REG001&territory_code=TR001")
    assert codes_of(body, "territory") == {"TR001"}
    assert codes_of(body, "sub_territory") == {"STR001", "STR002"}


def test_contradictory_filters_return_nothing_rather_than_widening(client):
    """TR002 is not in REG001, so the intersection is empty — not REG001's tree."""
    token = login(client)
    body = entities(client, token, "region_code=REG001&territory_code=TR002")

    assert body["entities"] == []
    assert body["totals"]["entities"] == 0
    assert all(count == 0 for count in body["counts"].values())


# ==========================================================================
# Filter versus layer visibility (items 10, 27)
# ==========================================================================


def test_layers_change_what_is_drawn_not_what_is_in_scope(client):
    token = login(client)
    full = entities(client, token, "territory_code=TR001")
    hidden = entities(client, token, "territory_code=TR001&layers=territory")

    # Nothing is drawn but the territory …
    assert {e["type"] for e in hidden["entities"]} == {"territory"}
    # … yet the scope, and therefore the counts, are identical.
    assert hidden["counts"]["customer"] == full["counts"]["customer"]
    assert hidden["counts"]["sub_territory"] == full["counts"]["sub_territory"]


def test_turning_a_layer_on_needs_no_refiltering(client):
    token = login(client)
    only_customers = entities(client, token,
                              "territory_code=TR001&layers=customer")
    assert {e["type"] for e in only_customers["entities"]} == {"customer"}
    assert "C999" not in codes_of(only_customers, "customer")


def test_an_unknown_layer_is_refused(client):
    token = login(client)
    response = client.get(f"/api/map/entities?{WINDOW}&layers=unicorn",
                          headers=auth(login(client)))
    assert response.status_code == 422
    assert "unicorn" in response.json()["detail"]


# ==========================================================================
# Counts (item 21)
# ==========================================================================


def test_counts_reflect_the_current_filter(client):
    token = login(client)
    everything = entities(client, token)
    narrowed = entities(client, token, "territory_code=TR001")

    assert narrowed["counts"]["territory"] == 1
    assert narrowed["counts"]["sub_territory"] == 2
    assert everything["counts"]["sub_territory"] == 3
    assert narrowed["counts"]["customer"] < everything["counts"]["customer"]


def test_placed_counts_are_separate_from_scope_counts(client):
    """An entity in scope but without a coordinate is counted, not hidden."""
    token = login(client)
    body = entities(client, token, "territory_code=TR001")
    assert body["counts"]["sub_territory"] == 2
    # No coordinates are loaded in this fixture, so nothing is placed …
    assert body["totals"]["placed"] == 0
    # … and every entity is reported as unplaced rather than dropped.
    assert body["totals"]["unplaced"] == body["totals"]["entities"]


# ==========================================================================
# Business filters keep the hierarchy intact (item 15)
# ==========================================================================


def test_a_product_filter_changes_metrics_not_membership(client):
    """Filtering to a product must not empty a territory of its customers."""
    token = login(client)
    plain = entities(client, token, "territory_code=TR001")
    filtered = entities(client, token, "territory_code=TR001&sku_code=SKU002")

    assert codes_of(filtered, "customer") == codes_of(plain, "customer")
    assert codes_of(filtered, "sub_territory") == codes_of(plain, "sub_territory")


def test_a_narrow_date_window_keeps_the_hierarchy(client):
    token = login(client)
    response = client.get(
        "/api/map/entities?date_from=2030-01-01&date_to=2030-01-02"
        "&territory_code=TR001", headers=auth(token))
    assert response.status_code == 200
    body = response.json()
    # No sales in that window, but the places still exist.
    assert codes_of(body, "sub_territory") == {"STR001", "STR002"}


# ==========================================================================
# RBAC and data scope (item 24)
# ==========================================================================


def test_a_scoped_user_is_narrowed_to_their_own_region(client):
    token = login(client, "dhaka_rm")
    body = entities(client, token)
    assert codes_of(body, "region") == {"REG001"}
    assert "C999" not in codes_of(body, "customer")


def test_a_scoped_user_asking_outside_their_scope_is_refused(client):
    token = login(client, "dhaka_rm")
    response = client.get(f"/api/map/entities?{WINDOW}&region_code=REG002",
                          headers=auth(token))
    assert response.status_code == 403
    assert "REG002" in response.json()["detail"]


def test_a_scoped_user_cannot_reach_another_branch_by_territory(client):
    """The refusal must hold at every level, not just the scoped one."""
    token = login(client, "dhaka_rm")
    response = client.get(f"/api/map/entities?{WINDOW}&territory_code=TR002",
                          headers=auth(token))
    assert response.status_code == 403


def test_the_map_section_is_required(client):
    from app.security.sections import DENY, SectionKey

    admin = login(client)
    user_id = client.get("/api/admin/users", headers=auth(admin),
                         params={"search": "dhaka_rm"}).json()["users"][0]["user_id"]
    client.put(f"/api/admin/users/{user_id}/permissions", headers=auth(admin),
               json={"permissions": {SectionKey.MAP: DENY}})

    token = login(client, "dhaka_rm")
    assert client.get(f"/api/map/entities?{WINDOW}",
                      headers=auth(token)).status_code == 403


def test_an_anonymous_caller_is_refused(client):
    assert client.get(f"/api/map/entities?{WINDOW}").status_code == 401


# ==========================================================================
# Diagnostics (item 22)
# ==========================================================================


def test_diagnostics_report_what_the_filter_resolved_to(client):
    token = login(client)
    body = entities(client, token, "territory_code=TR001&diagnostics=true")
    diagnostics = body["diagnostics"]

    assert diagnostics["selected_level"] == "territory"
    assert diagnostics["selected_code"] == "TR001"
    assert diagnostics["resolved_ancestors"]["region"] == ["REG001"]
    assert diagnostics["resolved_descendants"]["sub_territory"] == ["STR001", "STR002"]
    assert "C001" in diagnostics["resolved_business_codes"]["customer"]


def test_diagnostics_are_absent_unless_asked_for(client):
    token = login(client)
    assert "diagnostics" not in entities(client, token, "territory_code=TR001")


def test_diagnostics_are_refused_in_production(client, monkeypatch):
    """Resolved identifiers are a development aid, not something users see.

    ``Settings.environment`` is a dataclass default read at import, so the
    environment cannot be changed by setting a variable after the fact —
    ``get_settings`` itself is replaced instead, which is what the route calls.
    """
    import dataclasses

    import app.api.routes_map as routes_map
    from app.config import get_settings

    production = dataclasses.replace(get_settings(), environment="production")
    monkeypatch.setattr(routes_map, "get_settings", lambda: production)

    token = login(client)
    body = entities(client, token, "territory_code=TR001&diagnostics=true")
    assert "diagnostics" not in body
    # The request still succeeds — diagnostics are dropped, not an error.
    assert body["entities"]


# ==========================================================================
# Response shape and markers (items 16, 17)
# ==========================================================================


def test_every_entity_carries_its_type_and_parent(client):
    token = login(client)
    body = entities(client, token, "territory_code=TR001")

    for entity in body["entities"]:
        assert entity["type"]
        assert entity["id"]
        assert entity["name"]
        assert "latitude" in entity and "longitude" in entity
        if entity["type"] != "company":
            assert "parent_type" in entity

    customer = next(e for e in body["entities"] if e["type"] == "customer")
    assert customer["parent_type"] in ("sub_territory", "territory")
    assert customer["parent_id"]


def test_a_marker_is_supplied_for_every_drawn_layer(client):
    """Item 17: the Marker Designer decides how each type is drawn."""
    token = login(client)
    body = entities(client, token, "territory_code=TR001")

    for layer in body["layers"]:
        assert layer in body["markers"], layer
        assert body["markers"][layer]["preview_svg"].startswith("<svg")


# ==========================================================================
# Performance (item 23)
# ==========================================================================


def test_resolution_does_not_scale_queries_with_hierarchy_depth(branched):
    """No N+1: the query count is fixed, whatever level is filtered.

    The fact views are reflected once per engine and cached, so the cache is
    warmed first — otherwise the first call would be measured with reflection
    included and the comparison would say nothing about the resolver.
    """
    from sqlalchemy import event

    with Session(branched) as warm:
        resolve_business_entities(warm, resolve_org_scope(warm, {}))

    counts: list[int] = []
    for filters in ({}, {"region": "REG001"}, {"territory": "TR001"},
                    {"sub_territory": "STR001"}):
        with Session(branched) as session:
            issued: list[str] = []
            bind = session.get_bind()

            def record(conn, cursor, statement, *args):  # noqa: ANN001
                issued.append(statement)

            event.listen(bind, "before_cursor_execute", record)
            try:
                scope = resolve_org_scope(session, filters)
                resolve_business_entities(session, scope)
            finally:
                event.remove(bind, "before_cursor_execute", record)
            counts.append(len(issued))

    # One path query, one per business type, three master-name lookups and the
    # sales-force master link — a constant, not a function of depth.
    assert max(counts) <= 10, counts
    assert len(set(counts)) == 1, f"query count varied by depth: {counts}"


def test_one_request_answers_every_level(client):
    """The front end must not have to query level by level."""
    token = login(client)
    body = entities(client, token, "region_code=REG001")
    present = {e["type"] for e in body["entities"]}
    assert {"region", "area", "unit", "territory", "sub_territory",
            "customer"} <= present


# ---------------------------------------------------------------------------
# Several codes at one level
#
# A map filter carried one code per level, so "show me these three territories"
# could not be asked. These pin the widened shape: a bare string still means what
# it always did, a list means "either", and neither can reach past the caller's
# data scope.
# ---------------------------------------------------------------------------


def test_filter_codes_reads_either_spelling():
    """A bare code and a one-element list are the same request."""
    assert filter_codes("TR001") == ["TR001"]
    assert filter_codes(["TR001"]) == ["TR001"]
    assert filter_codes(("TR001", "TR002")) == ["TR001", "TR002"]
    # De-duplicated, order preserved: a repeated parameter is easy to send twice.
    assert filter_codes(["TR002", "TR001", "TR002"]) == ["TR002", "TR001"]
    # A blank is the absence of a filter, not a code that happens to be empty.
    assert filter_codes(None) == []
    assert filter_codes("") == []
    assert filter_codes(["", "  ", "TR001"]) == ["TR001"]
    assert filter_codes([" TR001 "]) == ["TR001"]


def test_a_single_code_resolves_identically_either_way(branched):
    """The one-element case must not have become a different query."""
    with Session(branched) as session:
        plain = resolve_org_scope(session, {"territory": "TR001"})
        listed = resolve_org_scope(session, {"territory": ["TR001"]})

    assert plain.codes == listed.codes
    assert plain.selected_level == listed.selected_level == "territory"
    assert plain.selected_code == listed.selected_code == "TR001"
    assert plain.selected_codes == listed.selected_codes == ("TR001",)


def test_several_codes_at_one_level_mean_either(branched):
    """Two territories resolve to both, not to their intersection."""
    with Session(branched) as session:
        one = resolve_org_scope(session, {"territory": "TR001"})
        two = resolve_org_scope(session, {"territory": "TR002"})
        both = resolve_org_scope(session, {"territory": ["TR001", "TR002"]})

    assert both.of("territory") == {"TR001", "TR002"}
    # The union at the selected level carries its ancestors and descendants up
    # and down with it: TR001 and TR002 sit under different regions.
    assert both.of("region") == one.of("region") | two.of("region")
    assert both.of("sub_territory") == one.of("sub_territory") | two.of("sub_territory")
    assert not both.empty


def test_levels_still_intersect_across_a_multi_code_level(branched):
    """Union within a level, intersection between them — both at once.

    TR002 sits under REG002, so naming it alongside TR001 under a REG001 filter
    must not smuggle it in. A multi-select that widened past another filter would
    show a regional manager a territory outside their region.
    """
    with Session(branched) as session:
        scope = resolve_org_scope(
            session, {"region": "REG001", "territory": ["TR001", "TR002"]},
        )

    assert scope.of("territory") == {"TR001"}
    assert scope.of("region") == {"REG001"}


def test_a_multi_code_selection_reports_no_single_selected_code(branched):
    """`selected_code` describes one selection, so it is null for several."""
    with Session(branched) as session:
        scope = resolve_org_scope(session, {"territory": ["TR001", "TR002"]})

    assert scope.selected_level == "territory"
    assert scope.selected_codes == ("TR001", "TR002")
    assert scope.selected_code is None


def test_a_filter_matching_nothing_is_still_empty(branched):
    """A list of codes that exist nowhere is empty, not unfiltered."""
    with Session(branched) as session:
        scope = resolve_org_scope(session, {"territory": ["NOPE1", "NOPE2"]})

    assert scope.empty
    assert scope.of("territory") == set()


def test_apply_data_scope_returns_a_list_for_either_spelling(branched, users):
    """Whatever a caller sends, downstream reads one shape."""
    with Session(branched) as session:
        plain = apply_data_scope(users["ceo"], session, {"territory": "TR001"})
        listed = apply_data_scope(users["ceo"], session, {"territory": ["TR001"]})
        several = apply_data_scope(
            users["ceo"], session, {"territory": ["TR001", "TR002"]},
        )

    assert plain == {"territory": ["TR001"]}
    assert listed == {"territory": ["TR001"]}
    assert several == {"territory": ["TR001", "TR002"]}


def test_apply_data_scope_refuses_a_list_holding_an_out_of_scope_code(branched, users):
    """A level is refused whole, never quietly reduced to the allowed part.

    Silently dropping TR002 would answer a different question from the one asked
    and give no sign it had done so — the reader would read a two-territory total
    that covered one.
    """
    with Session(branched) as session:
        with pytest.raises(PermissionDeniedError):
            apply_data_scope(
                users["dhaka_rm"], session, {"territory": ["TR001", "TR002"]},
            )


def test_an_unfiltered_request_is_pinned_to_every_code_in_scope(branched, users):
    """A scope of several codes narrows to all of them, not to none.

    A single-valued filter could not express a scope covering two regions, so it
    left the level unset; a list says both. This cannot widen anything — the
    codes come back from the permission filter, so they are the caller's own
    scope by construction.
    """
    two_regions = UserContext(
        user_id=users["dhaka_rm"].user_id,
        username="two_regions",
        role=users["dhaka_rm"].role,
        data_scope={"region_code": ["REG001", "REG002"]},
    )

    with Session(branched) as session:
        merged = apply_data_scope(two_regions, session, {})
        scope = resolve_org_scope(session, merged)

    assert merged == {"region": ["REG001", "REG002"]}
    assert scope.of("region") == {"REG001", "REG002"}


# ==========================================================================
# Business entities carry a metric too
# ==========================================================================


def test_customers_are_scored_like_every_other_level(client):
    """A customer arrives with its own net sales, not with nothing.

    Metrics used to be computed only for the organisational chain, so every
    customer and every sales-force point came back with ``value: null`` — and
    the browser drew that null as a measured zero over each one. The map opens
    on the customer layer, so that was the first figure anybody saw.
    """
    token = login(client)
    payload = entities(client, token, "layers=customer")

    customers = [e for e in payload["entities"] if e["type"] == "customer"]
    assert customers, "the fixture should resolve at least one customer"

    scored = [c for c in customers if c["value"] is not None]
    assert scored, "every customer came back unscored"
    assert any(c["value"] > 0 for c in scored), "no customer carried a real figure"


def test_a_customer_with_no_sales_is_unscored_rather_than_zero(client):
    """The fix must not swap one invented figure for another.

    A customer the window holds no sales for has no net sales to state, and the
    aggregate simply has no row for it. That must stay ``None`` all the way out,
    because a customer who did not trade this month and a customer who traded
    nothing are the same thing only if you already know which — and the map
    does not.
    """
    token = login(client)
    payload = entities(client, token,
                       "layers=customer&date_from=2030-01-01&date_to=2030-01-31")
    customers = [e for e in payload["entities"] if e["type"] == "customer"]
    assert customers, "the customers should still be listed outside the window"
    assert all(c["value"] is None for c in customers)


def test_a_scoped_user_only_ever_sees_their_own_customers_scored(client):
    """Scoring adds a figure; it must not add an entity.

    The aggregation is bounded by the codes the scope already resolved, so a
    regional manager's payload gains net sales for their own customers and
    gains no customer they could not see before.
    """
    root = entities(client, login(client), "layers=customer")
    scoped = entities(client, login(client, "khulna_rm"), "layers=customer")

    everyone = {e["id"] for e in root["entities"] if e["type"] == "customer"}
    theirs = {e["id"] for e in scoped["entities"] if e["type"] == "customer"}
    assert theirs <= everyone
    assert all(e["value"] is None or isinstance(e["value"], (int, float))
               for e in scoped["entities"] if e["type"] == "customer")
