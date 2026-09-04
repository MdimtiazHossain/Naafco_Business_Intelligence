"""The map's data: one tool, every level, the same numbers as every report.

The seeded warehouse has two regions (Dhaka REG001, Khulna REG002) with sales
in both, the current and the previous month, and a target on each region's
territory — so aggregation, growth, achievement, scope and drill-down are
testable against real facts rather than fixtures invented for the map.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from app.ai.exceptions import PermissionDeniedError
from app.ai.permission_filter import PermissionFilter, UserContext
from app.ai.schemas import GroupBy, Intent, ScopeFilters
from app.ai.tools import MAP_ROW_FIELDS, REGISTRY, ToolContext, execute_tool, tools_for_intent
from app.ai import queries as q
from app.map import data, geo, levels, metrics
from conftest_phase2 import sales_row
from conftest_phase3 import TODAY, _load

WINDOW = {"date_from": "2026-08-01", "date_to": "2026-08-31"}
PREVIOUS = {"compare_from": "2026-07-01", "compare_to": "2026-07-31"}
AUGUST = (dt.date(2026, 8, 1), dt.date(2026, 8, 31))
JULY = (dt.date(2026, 7, 1), dt.date(2026, 7, 31))

DHAKA = (23.7808, 90.4008)
KHULNA = (22.8456, 89.5403)


def context(session: Session, users: dict[str, UserContext],
            username: str = "ceo") -> ToolContext:
    user = users[username]
    return ToolContext(session, PermissionFilter(session, user), user, TODAY)


def rows_for(ctx: ToolContext, group_by: str, **extra) -> list[dict]:
    result = execute_tool(ctx, "get_map_layer",
                          {**WINDOW, "filters": {}, "group_by": group_by, **extra}).result
    assert result is not None
    return result.rows


def by_code(rows: list[dict]) -> dict[str, dict]:
    return {row["code"]: row for row in rows}


# ==========================================================================
# The tool
# ==========================================================================


def test_the_map_tool_is_registered_and_never_offered_to_the_assistant():
    spec = REGISTRY["get_map_layer"]
    assert spec.intents == ()
    for intent in Intent:
        assert "get_map_layer" not in tools_for_intent(intent), intent


def test_every_map_level_aggregates_by_a_real_dimension():
    assert set(data.GROUP_BY_LEVEL) == {level.key for level in levels.MAP_LEVELS}
    for key, group in data.GROUP_BY_LEVEL.items():
        assert group in q.GROUP_COLUMNS, key
    assert data.GROUP_BY_LEVEL["bu"] is GroupBy.BUSINESS_UNIT
    assert data.GROUP_BY_LEVEL["sub_territory"] is GroupBy.SUB_TERRITORY
    assert data.GROUP_BY_LEVEL["customer"] is GroupBy.CUSTOMER


def test_every_declared_metric_is_a_column_of_the_map_row(session, users):
    """The registry says what can be drawn; the tool says what is computed."""
    for metric in metrics.METRICS:
        assert metric.field in MAP_ROW_FIELDS, metric.key
    rows = rows_for(context(session, users), "region", **PREVIOUS)
    for row in rows:
        for name in MAP_ROW_FIELDS:
            assert name in row, name


def test_customer_count_is_meaningless_at_customer_level():
    assert not metrics.get_metric("customer_count").available_at("customer")
    assert metrics.get_metric("customer_count").available_at("sub_territory")
    assert "customer_count" not in {m.key for m in metrics.metrics_for("customer")}
    with pytest.raises(ValueError, match="Unknown map metric"):
        metrics.get_metric("active_customers")


def test_the_region_layer_equals_the_performance_page(session, users):
    """The number on the map is the number on the Sales page, by construction."""
    ctx = context(session, users)
    page = execute_tool(ctx, "get_region_performance",
                        {**WINDOW, "filters": {}, "group_by": "region",
                         "limit": 50}).result
    assert page is not None and page.rows
    map_rows = by_code(rows_for(ctx, "region"))
    for row in page.rows:
        assert map_rows[row["code"]]["net_sales"] == pytest.approx(row["net_sales"])
        assert map_rows[row["code"]]["quantity"] == pytest.approx(row["quantity"])
    assert set(map_rows) == {row["code"] for row in page.rows}


def test_achievement_and_shortfall_read_the_territory_targets(session, users):
    rows = by_code(rows_for(context(session, users), "territory"))
    dhaka = rows["TR001"]
    assert dhaka["net_sales"] == pytest.approx(1_500_000)
    assert dhaka["target_amount"] == pytest.approx(2_000_000)
    assert dhaka["achievement_percent"] == pytest.approx(75.0)
    assert dhaka["shortfall"] == pytest.approx(-500_000), "actual minus target"

    # Khulna's target territory sold nothing: a measured zero, not an absence.
    khulna = rows["TR002"]
    assert khulna["net_sales"] == 0.0
    assert khulna["target_amount"] == pytest.approx(1_000_000)
    assert khulna["achievement_percent"] == 0.0
    assert khulna["shortfall"] == pytest.approx(-1_000_000)

    # Khulna's sales were booked at area level with no territory: they are
    # returned under the unassigned code rather than dropped from the total.
    unassigned = rows[data.UNASSIGNED_CODE]
    assert unassigned["net_sales"] == pytest.approx(300_000)
    assert unassigned["target_amount"] is None


def test_an_entity_without_a_target_has_no_achievement_not_zero(session, users):
    rows = by_code(rows_for(context(session, users), "area"))
    # Targets are stated per territory and roll up to the area through the
    # view, so both areas carry one; a customer never carries a target.
    customer = by_code(rows_for(context(session, users), "customer"))["CUST-001"]
    assert customer["net_sales"] > 0
    assert customer["target_amount"] is None
    assert customer["achievement_percent"] is None
    assert customer["shortfall"] is None
    assert rows["AR001"]["target_amount"] == pytest.approx(2_000_000)


def test_growth_compares_each_entity_with_its_own_previous_period(session, users):
    rows = by_code(rows_for(context(session, users), "region", **PREVIOUS))
    assert rows["REG001"]["previous_net_sales"] == pytest.approx(2_000_000)
    assert rows["REG001"]["growth_percent"] == pytest.approx(-25.0)
    assert rows["REG002"]["previous_net_sales"] == pytest.approx(400_000)
    assert rows["REG002"]["growth_percent"] == pytest.approx(-25.0)

    without = by_code(rows_for(context(session, users), "region"))
    assert without["REG001"]["previous_net_sales"] is None
    assert without["REG001"]["growth_percent"] is None, \
        "no comparison window was asked for, so no growth is invented"


def test_a_comparison_window_must_be_a_pair():
    from app.ai.schemas import MapLayerToolInput

    with pytest.raises(ValueError):
        MapLayerToolInput(**WINDOW, compare_from=dt.date(2026, 7, 1))
    with pytest.raises(ValueError):
        MapLayerToolInput(**WINDOW, compare_from=dt.date(2026, 7, 31),
                          compare_to=dt.date(2026, 7, 1))


def test_customer_count_is_distinct_customers_per_entity(session, users):
    rows = by_code(rows_for(context(session, users), "region"))
    assert rows["REG001"]["customer_count"] == 1
    assert rows["REG002"]["customer_count"] == 1


def test_the_map_is_not_capped_at_five_hundred_entities(agent_engine, session, users):
    """A customer layer that omitted the 501st customer would draw a false gap."""
    _load(agent_engine, "sales", [
        sales_row(**{"Invoice No": f"INV-C{n:04d}", "Date": "2026-08-20",
                     "Customer Code": f"C-{n:04d}", "Quantity": 1,
                     "Gross Sales": 1_000, "Discount": 0, "Cost": 500,
                     "Source Transaction Id": f"SRC-C{n:04d}"})
        for n in range(1, 506)
    ])
    ctx = context(session, users)
    capped, truncated = q.aggregate_by(
        session, q.MAP_SALES_MEASURES, ScopeFilters(), *AUGUST, GroupBy.CUSTOMER,
        limit=10_000,
    )
    assert truncated and len(capped) == q.MAX_ROWS
    rows = rows_for(ctx, "customer")
    # 505 new customers plus CUST-001; the targets name no customer and come
    # back as the unassigned group, which is not an entity.
    assert len([row for row in rows if row["code"] != data.UNASSIGNED_CODE]) == 506
    assert sum(row["net_sales"] for row in rows) == pytest.approx(
        1_500_000 + 300_000 + 505 * 1_000
    )


# ==========================================================================
# Scope
# ==========================================================================


def test_a_scoped_user_sees_only_their_own_region(session, users):
    rows = by_code(rows_for(context(session, users, "dhaka_rm"), "region"))
    assert set(rows) == {"REG001"}
    territories = by_code(rows_for(context(session, users, "khulna_rm"), "territory"))
    assert "TR001" not in territories
    assert "TR002" in territories


def test_a_scoped_user_cannot_draw_another_region_by_asking(session, users):
    ctx = context(session, users, "dhaka_rm")
    with pytest.raises(PermissionDeniedError):
        execute_tool(ctx, "get_map_layer",
                     {**WINDOW, "filters": {"region_codes": ["REG002"]},
                      "group_by": "territory"})


def test_a_user_with_no_scope_is_refused_not_shown_an_empty_map(session, users):
    with pytest.raises(PermissionDeniedError):
        data.layer_data(context(session, users, "no_scope"), "region",
                        date_from=AUGUST[0], date_to=AUGUST[1])


def test_a_level_above_the_scope_is_labelled_partial(session, users):
    ctx = context(session, users, "dhaka_rm")
    zone = data.layer_data(ctx, "zone", date_from=AUGUST[0], date_to=AUGUST[1])
    assert any("cover only your data scope" in note for note in zone.notes)
    region = data.layer_data(ctx, "region", date_from=AUGUST[0], date_to=AUGUST[1])
    assert not any("cover only" in note for note in region.notes)
    # Unrestricted management is never told its figures are partial.
    everyone = data.layer_data(context(session, users), "zone",
                               date_from=AUGUST[0], date_to=AUGUST[1])
    assert not any("cover only" in note for note in everyone.notes)


# ==========================================================================
# Layers
# ==========================================================================


def test_entities_with_data_but_no_coordinate_are_reported_not_hidden(session, users):
    layer = data.layer_data(context(session, users), "region",
                            date_from=AUGUST[0], date_to=AUGUST[1])
    assert layer.features == []
    assert {entry["code"] for entry in layer.unplaced} == {"REG001", "REG002"}
    assert layer.entity_count == 2
    assert layer.bounds is None
    assert any("no coordinate" in note for note in layer.notes)


def test_a_placed_entity_becomes_a_point_feature_with_its_measures(session, users):
    geo.upsert_location(session, entity_type="region", entity_code="REG001",
                        latitude=DHAKA[0], longitude=DHAKA[1])
    session.flush()
    layer = data.layer_data(context(session, users), "region",
                            date_from=AUGUST[0], date_to=AUGUST[1],
                            compare_from=JULY[0], compare_to=JULY[1])
    assert len(layer.features) == 1
    feature = layer.features[0]
    assert feature["type"] == "Feature"
    assert feature["id"] == "region:REG001"
    # GeoJSON is longitude first.
    assert feature["geometry"] == {"type": "Point",
                                   "coordinates": [DHAKA[1], DHAKA[0]]}
    properties = feature["properties"]
    assert properties["code"] == "REG001"
    assert properties["name"] == "Dhaka"
    assert properties["level"] == "region"
    assert properties["parent_level"] == "zone"
    assert properties["parent_code"] == "Z001"
    assert properties["location_source"] == "UPLOAD"
    assert properties["net_sales"] == pytest.approx(1_500_000)
    assert properties["growth_percent"] == pytest.approx(-25.0)
    assert properties["achievement_percent"] == pytest.approx(75.0)
    for metric in metrics.METRICS:
        assert metric.field in properties, metric.key
    assert [entry["code"] for entry in layer.unplaced] == ["REG002"]
    assert layer.bounds["north"] == DHAKA[0]

    payload = layer.to_dict()
    assert payload["features"]["type"] == "FeatureCollection"
    assert payload["placed_count"] == 1 and payload["entity_count"] == 2


def test_the_unassigned_group_is_handed_back_not_drawn(session, users):
    layer = data.layer_data(context(session, users), "territory",
                            date_from=AUGUST[0], date_to=AUGUST[1])
    assert layer.unassigned is not None
    assert layer.unassigned["net_sales"] == pytest.approx(300_000)
    assert data.UNASSIGNED_CODE not in {entry["code"] for entry in layer.unplaced}


def test_a_customer_feature_carries_its_sub_territory(session, users):
    from app.database.models_warehouse import DimCustomer

    # The sales fixture names CUST-001 but the customer master holds no row for
    # it — the dimension is PENDING_SOURCE_DATA and a fact keeps the raw code.
    # The parent comes from the master, so the master has to say it.
    session.add(DimCustomer(customer_code="CUST-001", customer_name="Dhiren",
                            sub_territory_code="STR001"))
    geo.upsert_location(session, entity_type="customer", entity_code="CUST-001",
                        latitude=DHAKA[0], longitude=DHAKA[1])
    session.flush()
    layer = data.layer_data(context(session, users), "customer",
                            date_from=AUGUST[0], date_to=AUGUST[1])
    assert len(layer.features) == 1
    assert layer.features[0]["properties"]["parent_level"] == "sub_territory"
    assert layer.features[0]["properties"]["parent_code"] == "STR001"


def test_every_promoted_level_can_be_drawn_on_the_same_map(session, users):
    """Test B of the specification: five levels, one call, one map."""
    for level_key, code in (("zone", "Z001"), ("region", "REG001"),
                            ("area", "AR001"), ("territory", "TR001"),
                            ("sub_territory", "STR001")):
        geo.upsert_location(session, entity_type=level_key, entity_code=code,
                            latitude=DHAKA[0], longitude=DHAKA[1])
    session.flush()
    result = data.map_data(
        context(session, users),
        ["zone", "region", "area", "territory", "sub_territory", "customer"],
        date_from=AUGUST[0], date_to=AUGUST[1],
    )
    assert [layer.level for layer in result.layers] == [
        "zone", "region", "area", "territory", "sub_territory", "customer",
    ]
    placed = {layer.level: len(layer.features) for layer in result.layers}
    assert placed["zone"] == placed["region"] == placed["area"] == 1
    assert placed["territory"] == 1
    # The seeded sales state a territory but no sub-territory, so that layer
    # has nothing in the period: an honest empty layer, not an error.
    assert placed["sub_territory"] == 0
    assert placed["customer"] == 0 and result.layers[-1].unplaced
    assert not result.empty
    assert result.to_dict()["layers"][0]["level"] == "zone"


def test_an_empty_period_is_an_empty_map(session, users):
    result = data.map_data(context(session, users), ["region"],
                           date_from=dt.date(2020, 1, 1), date_to=dt.date(2020, 1, 31))
    assert result.empty
    assert result.layers[0].entity_count == 0


def test_an_unknown_level_is_refused(session, users):
    with pytest.raises(ValueError, match="Unknown map level"):
        data.layer_data(context(session, users), "wormhole",
                        date_from=AUGUST[0], date_to=AUGUST[1])


def test_filters_narrow_the_map_like_every_report(session, users):
    ctx = context(session, users)
    rows = by_code(rows_for(ctx, "territory"))
    assert "TR001" in rows and "TR002" in rows
    narrowed = execute_tool(ctx, "get_map_layer",
                            {**WINDOW, "filters": {"region_codes": ["REG002"]},
                             "group_by": "territory"}).result
    assert {row["code"] for row in narrowed.rows} == {"TR002", data.UNASSIGNED_CODE}
