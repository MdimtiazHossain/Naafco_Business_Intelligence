"""The filter hierarchy, in both directions.

A parent narrows what its children offer, and a child selects the parents its
master data implies. The second direction is resolved by
:func:`resolve_ancestors`, and everything it returns must come from a master
table by code — never from a name, a prefix or a guess.

The property these tests care about most is that the two directions agree: a
customer the cascade offers under a territory must resolve back to that same
territory. If they can disagree, a report can be narrowed to a slice its own
filter bar says it is not showing.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.ai.permission_filter import PermissionFilter
from app.api.filter_space import FilterSpace
from app.api.routes_masterdata import FILTER_PARENTS, resolve_ancestors
from app.database.models import DimMaterial
from app.database.models_warehouse import DimCustomer
from app.etl.mapping import LEVEL_BINDINGS, MasterDataIndex

#: The chain the seeded master data declares, deepest first.
CHAIN = {
    "sub_territory_code": "STR001",
    "territory_code": "TR001",
    "unit_code": "UN001",
    "area_code": "AR001",
    "region_code": "REG001",
    "zone_code": "Z001",
    "sales_line_code": "SL001",
    "bu_code": "BU001",
    "company_code": "C001",
}


@pytest.fixture
def permissions(session, users) -> PermissionFilter:
    return PermissionFilter(session, users["ceo"], MasterDataIndex(session))


@pytest.fixture
def customer(session: Session) -> str:
    """One customer, placed by the master field rather than by its facts."""
    session.add(DimCustomer(customer_code="CUST-A", customer_name="ABC Customer",
                            sub_territory_code="STR001"))
    session.commit()
    return "CUST-A"


# ==========================================================================
# The declaration
# ==========================================================================


def test_the_hierarchy_is_derived_from_the_etl_bindings() -> None:
    """One declaration, not two.

    ``LEVEL_BINDINGS`` is what the ETL resolves master data with. If the filter
    bar carried its own copy the two could drift, and a filter would then narrow
    a report along a hierarchy the warehouse was not built on.
    """
    for binding in LEVEL_BINDINGS:
        assert FILTER_PARENTS[binding.code_field] == binding.parent_code_field


def test_customer_hangs_below_sub_territory() -> None:
    """The one edge that is declared rather than derived.

    A customer is not an organisational level — it has no children and is not in
    ``LEVEL_BINDINGS`` — but ``dim_customer.sub_territory_code`` states its
    parent, and that column is where the edge comes from.
    """
    assert FILTER_PARENTS["customer_code"] == "sub_territory_code"


# ==========================================================================
# Child -> parent
# ==========================================================================


def test_a_customer_resolves_its_whole_organisational_chain(
    session, permissions, customer,
) -> None:
    """Nine levels from one selection, and the level asked about is not echoed."""
    resolved = resolve_ancestors(session, permissions, "customer_code", customer)
    assert resolved == CHAIN
    assert "customer_code" not in resolved


@pytest.mark.parametrize(
    ("level", "code", "expected"),
    [
        ("territory_code", "TR001",
         {k: v for k, v in CHAIN.items()
          if k not in ("sub_territory_code", "territory_code")}),
        ("material_code", "MAT-001",
         {"material_brand": "Example Brand", "material_group_code": "MG01"}),
        ("storage_location_key", "PL01|SL01",
         {"plant_code": "PL01", "company_code": "C001"}),
        ("plant_code", "PL01", {"company_code": "C001"}),
        # Nothing above a company, so nothing to resolve.
        ("company_code", "C001", {}),
    ],
)
def test_each_level_resolves_the_parents_its_master_states(
    session, permissions, level, code, expected,
) -> None:
    assert resolve_ancestors(session, permissions, level, code) == expected


def test_an_unknown_code_resolves_to_nothing(session, permissions) -> None:
    """A code with no master row invents no parents."""
    assert resolve_ancestors(session, permissions, "customer_code", "NOPE") == {}


def test_an_ambiguous_parent_is_not_guessed(session, permissions) -> None:
    """A brand under two material groups implies neither of them.

    This is the rule that keeps auto-selection honest. Picking one of the two
    would silently narrow every report to half the brand, and the filter bar
    would show a group the user never chose as the reason.
    """
    session.add(DimMaterial(material_code="MAT-009",
                            material_description="Premium Tea Sachet",
                            material_group_code="MG02", material_group_name="Dairy",
                            material_brand_code="MB01",
                            material_brand="Example Brand"))
    session.commit()

    # MAT-001 puts "Example Brand" in MG01; MAT-009 puts it in MG02.
    assert resolve_ancestors(session, permissions, "material_brand",
                             "Example Brand") == {}
    # The material itself is still exact — its own row states both.
    assert resolve_ancestors(session, permissions, "material_code", "MAT-009") == {
        "material_brand": "Example Brand", "material_group_code": "MG02",
    }


# ==========================================================================
# Parent -> child, and the two directions agreeing
# ==========================================================================


def _codes(session: Session, level: str, selection, permissions) -> list[str]:
    """Options for one level, from a space built **now**.

    A :class:`FilterSpace` is a snapshot of the master data, so it has to be
    taken after the fixtures that add rows have run — a space built in a fixture
    would not contain a customer a later fixture inserts.
    """
    space = FilterSpace(session, with_customers=True)
    return [option["code"]
            for option in space.options_for(level, selection, permissions).options]


@pytest.mark.parametrize("level", list(CHAIN))
def test_a_customer_is_offered_under_every_one_of_its_ancestors(
    session, permissions, customer, level,
) -> None:
    """Selecting **any** level above the customer still offers that customer.

    Not only the immediate parent. This is the property the old cascade did not
    have: it narrowed each control by the level's own parent column, so a
    company selection asked ``dim_territory`` for rows whose ``unit_code`` was a
    company code and offered nothing at all.
    """
    assert _codes(session, "customer_code", {level: [CHAIN[level]]},
                  permissions) == [customer]


def test_a_customer_is_not_offered_under_an_unrelated_parent(
    session, permissions, customer,
) -> None:
    """REG002 is a real region, and this customer is not in it."""
    assert _codes(session, "customer_code", {"region_code": ["REG002"]},
                  permissions) == []


def test_the_two_directions_agree(session, permissions, customer) -> None:
    """Every customer offered under a territory resolves back to that territory.

    The round trip is the point: the filter space and the resolver read the same
    ``ancestors_of`` walk, so a customer cannot be offered in one place and
    report itself as belonging somewhere else.
    """
    for code in _codes(session, "customer_code", {"territory_code": ["TR001"]},
                       permissions):
        chain = resolve_ancestors(session, permissions, "customer_code", code)
        assert chain["territory_code"] == "TR001"
