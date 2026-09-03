"""The organisational chain: Zone, Region, Area, Territory, Sub-Territory.

Extracted from the map's own test file when the map was removed. These
tests never concerned the map: they cover ``resolve_org_scope``,
``apply_data_scope`` and ``filter_codes``, which Data Management and the
permission filter depend on just as much as the map ever did.
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
from app.ai.exceptions import PermissionDeniedError
from app.ai.permission_filter import UserContext
from app.org.hierarchy import (
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


def codes_of(payload: dict, entity_type: str) -> set[str]:
    return {e["id"] for e in payload["entities"] if e["type"] == entity_type}


# ==========================================================================
# The specification's acceptance test (item 26)
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
