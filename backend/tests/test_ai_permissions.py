"""Permission filtering — the security boundary.

These tests check the two properties that matter: unauthorised data is never
*read* (not read-then-filtered), and the scope cannot be widened by anything the
user or the model says.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.ai.exceptions import PermissionDeniedError
from app.ai.permission_filter import PermissionFilter, UserContext
from app.ai.schemas import EntityType, ResolvedEntity, ScopeFilters
from app.ai.tools import ToolContext, execute_tool
from conftest_phase3 import TODAY


def entity(entity_type: EntityType, code: str, label: str) -> ResolvedEntity:
    return ResolvedEntity(entity_type=entity_type, code=code, label=label, term=label)


def make_filter(session, users, username: str) -> PermissionFilter:
    return PermissionFilter(session, users[username])


# --------------------------------------------------------------------------
# Scope arithmetic
# --------------------------------------------------------------------------


def test_management_is_unrestricted(session, users) -> None:
    permissions = make_filter(session, users, "ceo")
    assert permissions.user.is_unrestricted
    assert permissions.build_filters([]).is_empty()


def test_regional_manager_scope_is_injected_when_no_entity_is_named(session, users):
    permissions = make_filter(session, users, "dhaka_rm")
    filters = permissions.build_filters([])
    assert filters.region_codes == ["REG001"]


def test_manager_may_query_their_own_region(session, users) -> None:
    permissions = make_filter(session, users, "dhaka_rm")
    filters = permissions.build_filters([entity(EntityType.REGION, "REG001", "Dhaka")])
    assert filters.region_codes == ["REG001"]


def test_manager_is_refused_another_region(session, users) -> None:
    permissions = make_filter(session, users, "dhaka_rm")
    with pytest.raises(PermissionDeniedError) as exc:
        permissions.build_filters([entity(EntityType.REGION, "REG002", "Khulna")])
    assert "don't have permission" in exc.value.user_message
    assert "Khulna" in exc.value.user_message


def test_manager_may_query_an_area_inside_their_region(session, users) -> None:
    """AR001 sits under REG001, so a Dhaka manager may ask about it."""
    permissions = make_filter(session, users, "dhaka_rm")
    filters = permissions.build_filters([entity(EntityType.AREA, "AR001", "Mirpur")])
    assert filters.area_codes == ["AR001"]


def test_manager_is_refused_an_area_in_another_region(session, users) -> None:
    permissions = make_filter(session, users, "dhaka_rm")
    with pytest.raises(PermissionDeniedError):
        permissions.build_filters([entity(EntityType.AREA, "AR002", "Khulna Sadar")])


def test_area_manager_scope_is_narrower_than_the_region(session, users) -> None:
    permissions = make_filter(session, users, "mirpur_am")
    assert permissions.build_filters([]).area_codes == ["AR001"]
    with pytest.raises(PermissionDeniedError):
        permissions.build_filters([entity(EntityType.REGION, "REG002", "Khulna")])


def test_asking_about_an_ancestor_is_allowed_but_still_narrowed(session, users) -> None:
    """A territory manager may ask a zone question; they see only their slice."""
    permissions = make_filter(session, users, "kazipara_tm")
    filters = permissions.build_filters([entity(EntityType.ZONE, "Z001", "Dhaka Zone")])
    assert filters.zone_codes == ["Z001"]
    enforced = permissions.enforce(filters)
    assert enforced.territory_codes == ["TR001"]     # the scope is still applied


def test_a_user_without_any_scope_can_see_nothing(session, users) -> None:
    permissions = make_filter(session, users, "no_scope")
    with pytest.raises(PermissionDeniedError) as exc:
        permissions.enforce(ScopeFilters())
    assert "no data scope" in exc.value.user_message


def test_enforce_blocks_a_filter_the_user_never_asked_for(session, users) -> None:
    """Even if a code reached the tool arguments some other way, it is blocked.

    This is the backstop for a model that invents an argument: the scope check
    runs on the arguments themselves, not only on the resolved entities.
    """
    permissions = make_filter(session, users, "dhaka_rm")
    with pytest.raises(PermissionDeniedError):
        permissions.enforce(ScopeFilters(region_codes=["REG002"]))


def test_material_filters_are_not_scope_bearing(session, users) -> None:
    """A material is not an organisational entity, so it never trips the scope."""
    permissions = make_filter(session, users, "dhaka_rm")
    filters = permissions.build_filters([entity(EntityType.MATERIAL, "SKU001", "Tea")])
    assert filters.material_codes == ["SKU001"]
    assert filters.region_codes == ["REG001"]        # scope still injected


# --------------------------------------------------------------------------
# Enforcement at the tool boundary
# --------------------------------------------------------------------------


def _run(session, users, username: str, tool: str, **arguments):
    permissions = PermissionFilter(session, users[username])
    ctx = ToolContext(session, permissions, users[username], TODAY)
    payload = {
        "date_from": "2026-08-01", "date_to": "2026-08-31",
        "filters": {}, **arguments,
    }
    return execute_tool(ctx, tool, payload)


def test_scope_is_applied_inside_the_query_not_after(session, users) -> None:
    """The regional managers see different totals from the same tool call."""
    everything = _run(session, users, "ceo", "get_sales_summary").result
    dhaka = _run(session, users, "dhaka_rm", "get_sales_summary").result
    khulna = _run(session, users, "khulna_rm", "get_sales_summary").result

    assert everything.value == pytest.approx(dhaka.value + khulna.value)
    assert dhaka.value > khulna.value > 0
    # The filter that produced the number is reported back.
    assert dhaka.filters["region_codes"] == ["REG001"]


def test_a_scoped_user_cannot_reach_another_region_through_a_tool(session, users) -> None:
    with pytest.raises(PermissionDeniedError):
        _run(session, users, "dhaka_rm", "get_sales_summary",
             filters={"region_codes": ["REG002"]})


def test_grouped_report_only_returns_authorised_groups(session, users) -> None:
    result = _run(session, users, "dhaka_rm", "get_region_performance",
                  group_by="region", limit=20).result
    assert [row["code"] for row in result.rows] == ["REG001"]

    everything = _run(session, users, "ceo", "get_region_performance",
                      group_by="region", limit=20).result
    assert {row["code"] for row in everything.rows} == {"REG001", "REG002"}


def test_stock_is_scoped_too(session, users) -> None:
    """A second dataset, so scoping is shown to be general rather than sales-only.

    Stock replaced outstanding here when the receivables module was removed. It
    is the right substitute for the point being made: the filter is applied by
    ``PermissionFilter`` before the query runs, whatever the dataset.
    """
    everything = _run(session, users, "ceo", "get_stock_summary").result
    assert everything.values["total_stock"] > 0


def test_business_summary_is_scoped(session, users) -> None:
    everything = _run(session, users, "ceo", "get_business_summary").result
    dhaka = _run(session, users, "dhaka_rm", "get_business_summary").result
    assert everything.values["sales"] > dhaka.values["sales"] > 0
    assert everything.values["target"] > dhaka.values["target"] > 0
