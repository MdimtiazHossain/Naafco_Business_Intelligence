"""The filter space: every control stays usable, whatever else is selected.

These are the regression cases for the defect this module was written to fix.
The old filter bar narrowed each control by that level's **immediate** parent
column, so selecting a Company and opening Territory asked ``dim_territory`` for
the rows whose ``unit_code`` equalled a company code. There are none, so the
control drew an empty list and could not be used at all; Business Unit appeared
to work only because company happens to be its immediate parent.

The property pinned here is the one that fixes it: **a level is never gated on
its parent being chosen.** It offers every value the master data still allows
given everything else selected, in both directions and across the three
structures — organisational, plant and item — that share ``company_code``.

The seeded hierarchy these run against is

    C001 -> BU001 -> SL001 -> Z001 -> REG001 -> AR001 -> UN001 -> TR001 -> STR001
                                   -> REG002 -> AR002 -> UN002 -> TR002

so REG001 and REG002 are two genuinely separate branches of one company, which
is what makes "keep the valid selection, drop the invalid one" testable.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.ai.permission_filter import PermissionFilter
from app.api.filter_space import FilterSpace
from app.database.models_warehouse import DimCustomer
from app.etl.mapping import MasterDataIndex

#: Every organisational control the bar draws, shallowest first.
ORG_CONTROLS = [
    "company_code", "bu_code", "sales_line_code", "zone_code", "region_code",
    "area_code", "unit_code", "territory_code", "sub_territory_code",
]


@pytest.fixture
def permissions(session, users) -> PermissionFilter:
    return PermissionFilter(session, users["ceo"], MasterDataIndex(session))


@pytest.fixture
def customer(session: Session) -> str:
    session.add(DimCustomer(customer_code="CUST-A", customer_name="ABC Customer",
                            sub_territory_code="STR001"))
    session.commit()
    return "CUST-A"


def space(session: Session) -> FilterSpace:
    """A space built now — it is a snapshot, so it must follow the fixtures."""
    return FilterSpace(session, with_customers=True)


def codes(session: Session, level: str, selection) -> set[str]:
    return space(session).codes_for(level, selection)


# ==========================================================================
# Test 1 — a Company alone leaves every level selectable
# ==========================================================================


@pytest.mark.parametrize("level", ORG_CONTROLS)
def test_company_alone_leaves_every_level_selectable(session, level) -> None:
    """The defect, pinned at every level rather than only at Territory.

    Under the immediate-parent cascade this returned nothing for Sales Line and
    everything below it. A control with no options is a control the user cannot
    use, which is what "Territory cannot be selected" actually was.
    """
    assert codes(session, level, {"company_code": ["C001"]})


def test_territory_is_offered_under_a_company(session) -> None:
    """The exact case reported: Company selected, Territory must be selectable.

    ``dim_territory.unit_code`` never holds a company code, so the old query
    matched zero rows. Both seeded territories sit under C001 and both must be
    offered.
    """
    assert codes(session, "territory_code", {"company_code": ["C001"]}) == {
        "TR001", "TR002",
    }


def test_no_parent_selected_offers_everything(session) -> None:
    """Test 6: Company = All restricts nothing."""
    assert codes(session, "territory_code", {}) == {"TR001", "TR002"}
    assert codes(session, "sub_territory_code", {}) == {"STR001"}


# ==========================================================================
# Tests 2-5 — combinations that skip levels in between
# ==========================================================================


def test_company_and_territory(session) -> None:
    """Test 2. Territory is selectable with Business Unit left at All."""
    selection = {"company_code": ["C001"], "territory_code": ["TR001"]}
    # The levels in between are narrowed to TR001's own branch...
    assert codes(session, "region_code", selection) == {"REG001"}
    assert codes(session, "area_code", selection) == {"AR001"}
    assert codes(session, "unit_code", selection) == {"UN001"}
    # ...and Territory itself still offers both, because a filter never
    # restricts itself — otherwise the other territory becomes unreachable.
    assert codes(session, "territory_code", selection) == {"TR001", "TR002"}


def test_company_region_and_territory(session) -> None:
    """Test 3. Both work, and the region genuinely narrows the territories."""
    selection = {"company_code": ["C001"], "region_code": ["REG001"]}
    assert codes(session, "territory_code", selection) == {"TR001"}
    assert codes(session, "territory_code",
                 {**selection, "region_code": ["REG002"]}) == {"TR002"}


def test_company_business_unit_and_territory(session) -> None:
    """Test 4."""
    selection = {"company_code": ["C001"], "bu_code": ["BU001"],
                 "territory_code": ["TR001"]}
    assert codes(session, "bu_code", selection) == {"BU001"}
    assert codes(session, "sub_territory_code", selection) == {"STR001"}


def test_company_territory_and_sub_territory(session) -> None:
    """Test 5."""
    selection = {"company_code": ["C001"], "territory_code": ["TR001"],
                 "sub_territory_code": ["STR001"]}
    assert codes(session, "sub_territory_code", selection) == {"STR001"}
    assert codes(session, "region_code", selection) == {"REG001"}


# ==========================================================================
# Test 8 — the child selected first
# ==========================================================================


def test_a_territory_alone_recalculates_the_levels_above_it(session) -> None:
    """Test 8: select Territory first, then read back the compatible Company.

    The upward direction. Nothing above the territory is selected, so every
    ancestor control must narrow to that territory's own branch — which is what
    lets the user then pick the Company without contradicting themselves.
    """
    selection = {"territory_code": ["TR002"]}
    assert codes(session, "company_code", selection) == {"C001"}
    assert codes(session, "region_code", selection) == {"REG002"}
    assert codes(session, "area_code", selection) == {"AR002"}
    assert codes(session, "unit_code", selection) == {"UN002"}


def test_a_customer_narrows_every_level_above_it(session, customer) -> None:
    """A customer implies nine levels, and each control shows only that branch."""
    selection = {"customer_code": [customer]}
    assert codes(session, "sub_territory_code", selection) == {"STR001"}
    assert codes(session, "territory_code", selection) == {"TR001"}
    assert codes(session, "company_code", selection) == {"C001"}


# ==========================================================================
# Test 6 — multi-select
# ==========================================================================


def test_multiple_values_at_one_level_union_their_children(session) -> None:
    """Selecting two regions offers the territories of both, not of neither."""
    assert codes(session, "territory_code",
                 {"region_code": ["REG001", "REG002"]}) == {"TR001", "TR002"}
    assert codes(session, "area_code",
                 {"region_code": ["REG001", "REG002"]}) == {"AR001", "AR002"}


def test_multiple_values_still_narrow_the_levels_above(session) -> None:
    """A union going down is still an intersection going up."""
    assert codes(session, "company_code",
                 {"territory_code": ["TR001", "TR002"]}) == {"C001"}


# ==========================================================================
# §8 — a filter never restricts itself
# ==========================================================================


@pytest.mark.parametrize("level", ORG_CONTROLS)
def test_a_level_does_not_filter_itself(session, level) -> None:
    """Its own selection is excluded, or the unselected values would vanish.

    A user who picked one territory must still be able to reach the others; a
    control that applied its own value would show exactly what is already
    chosen and nothing else, and could never be changed to anything but All.
    """
    everything = codes(session, level, {})
    one = sorted(everything)[:1]
    assert codes(session, level, {level: one}) == everything


# ==========================================================================
# Tests 5 and 7 — what survives a change
# ==========================================================================


def test_an_invalid_selection_is_dropped_and_the_rest_survives(session) -> None:
    """Test 7. Change the region under a territory that does not belong to it.

    Only the territory goes. The region the user just chose is the anchor and is
    never pruned — with two mutually exclusive filters selected, either could be
    dropped to satisfy the data, and only the user's last action says which one
    they meant.
    """
    kept, removed = space(session).prune(
        {"region_code": ["REG002"], "territory_code": ["TR001"]},
        anchor=["region_code"],
    )
    assert kept == {"region_code": ["REG002"]}
    assert removed == {"territory_code": ["TR001"]}


def test_a_still_valid_selection_is_preserved(session) -> None:
    """§5. Changing a parent must not reset a child that is still valid."""
    kept, removed = space(session).prune(
        {"region_code": ["REG001"], "territory_code": ["TR001"]},
        anchor=["region_code"],
    )
    assert kept == {"region_code": ["REG001"], "territory_code": ["TR001"]}
    assert removed == {}


def test_only_the_invalid_values_of_a_multiple_selection_are_dropped(
    session,
) -> None:
    """The half of a multiple selection that still fits is kept."""
    kept, removed = space(session).prune(
        {"region_code": ["REG001"], "territory_code": ["TR001", "TR002"]},
        anchor=["region_code"],
    )
    assert kept["territory_code"] == ["TR001"]
    assert removed == {"territory_code": ["TR002"]}


def test_a_filter_with_no_master_list_is_never_pruned(session) -> None:
    """A batch code is transaction data and has no master to be invalid against.

    Silently dropping it would be worse than leaving it: the user would see
    their filter disappear with no explanation and no way to tell why.
    """
    kept, _ = space(session).prune(
        {"region_code": ["REG001"], "batch_code": ["B-77"]},
        anchor=["region_code"],
    )
    assert kept["batch_code"] == ["B-77"]


# ==========================================================================
# Scope, and the levels with no master list
# ==========================================================================


def test_options_are_permission_scoped(session, users) -> None:
    """A regional manager is offered their own region and no other.

    The same check the agent and the reports use, so a filter can never offer a
    value whose report would be refused.
    """
    scoped = PermissionFilter(session, users["dhaka_rm"], MasterDataIndex(session))
    answer = space(session).options_for("region_code", {}, scoped)
    assert [option["code"] for option in answer.options] == ["REG001"]


def test_scope_survives_an_unrelated_selection(session, users) -> None:
    """Selecting a company must not widen what a scoped user may see."""
    scoped = PermissionFilter(session, users["dhaka_rm"], MasterDataIndex(session))
    answer = space(session).options_for("territory_code",
                                        {"company_code": ["C001"]}, scoped)
    assert [option["code"] for option in answer.options] == ["TR001"]


def test_an_unknown_level_offers_nothing_rather_than_everything(session) -> None:
    """A level with no master list is not silently treated as unconstrained."""
    assert codes(session, "batch_code", {}) == set()


def test_options_carry_the_master_s_name_as_the_label(session) -> None:
    """The label is read from the master, so a rename reaches every filter."""
    answer = space(session).options_for("region_code", {})
    labels = {option["code"]: option["label"] for option in answer.options}
    assert labels["REG001"] == "Dhaka"
