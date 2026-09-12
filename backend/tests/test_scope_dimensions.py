"""The scope dimension registry: two chains, and neither one leaking into the other.

Step 1 of giving stock a data scope by plant. Nothing reads
:mod:`app.security.scope` yet, so what is pinned here is that the registry
*reproduces* today's organisational behaviour exactly, and that the three ways
a second chain could silently corrupt the first are all closed.

The seeded hierarchy these run against is

    C001 -> BU001 -> SL001 -> Z001 -> REG001 -> AR001 -> UN001 -> TR001 -> STR001
    C001 -> PL01 -> {PL01|SL01, PL01|SL02}

so one company is reached by both chains, which is the case the whole design
turns on.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.ai import queries as q
from app.ai.exceptions import PermissionDeniedError
from app.ai.permission_filter import PermissionFilter, UserContext
from app.ai.schemas import EntityType, ResolvedEntity, ScopeFilters
from app.api.deps import enforce_report_scope
from app.api.routes_masterdata import FILTER_PARENTS
from app.database.models import DimPlant, plant_key
from app.database.models_ai import Role
from app.etl.mapping import BINDING_BY_LEVEL, LEVEL_DEPTH
from app.reporting.service import ReportFilters
from app.security import scope as s

#: The organisational level -> ``ScopeFilters`` field map as this platform has
#: always had it, written out **once, here, on purpose**.
#:
#: Everywhere else in this codebase a second copy of a list is a defect; a
#: golden master is the exception, and this is one. Comparing the registry
#: against ``permission_filter.FILTER_FIELD_BY_LEVEL`` would prove nothing now
#: that the registry is where that name comes from — the assertion would hold
#: however wrong both were. Nine levels, spelled these nine ways, is a security
#: boundary, and the whole claim of this work is that it did not move.
ORG_FILTER_FIELD_TODAY: dict[str, str] = {
    "company_code": "company_codes",
    "bu_code": "business_unit_codes",
    "sales_line_code": "sales_line_codes",
    "zone_code": "zone_codes",
    "region_code": "region_codes",
    "area_code": "area_codes",
    "unit_code": "unit_codes",
    "territory_code": "territory_codes",
    "sub_territory_code": "sub_territory_codes",
}


# ---------------------------------------------------------------------------
# The registry reproduces today's organisational scope, exactly
# ---------------------------------------------------------------------------


def test_org_dimension_is_todays_org_scope_unchanged():
    """The whole claim of this work: nothing about the sales chain moves.

    Both halves matter. The org dimension must still be exactly those nine
    levels mapped exactly those nine ways, *and* the module every caller
    imports the map from must now carry the plant chain as well — one of those
    without the other is a half-applied change.
    """
    from app.ai import permission_filter

    assert s.ORG.filter_fields() == ORG_FILTER_FIELD_TODAY
    assert permission_filter.FILTER_FIELD_BY_LEVEL == {
        **ORG_FILTER_FIELD_TODAY,
        "plant_code": "plant_codes",
        "storage_location_key": "storage_location_keys",
    }


def test_org_depth_agrees_with_the_warehouse_bindings():
    """Depth inside the org dimension is the depth the ETL already uses.

    ``LEVEL_DEPTH`` is what ``_scope_level_is_redundant`` reads today, so a
    dimension that ordered its levels differently would change which scope
    levels get collapsed out of a query.
    """
    for code_field in s.ORG.code_fields():
        assert s.ORG.depth(code_field) == LEVEL_DEPTH[code_field]


def test_every_org_level_is_a_real_warehouse_binding():
    assert set(s.ORG.code_fields()) == set(BINDING_BY_LEVEL)


# ---------------------------------------------------------------------------
# Every level names something that actually exists
# ---------------------------------------------------------------------------


def test_every_level_sets_a_real_scope_filter():
    """A level whose filter field does not exist would apply no filter at all.

    This is what catches the irregular one: ``bu_code`` sets
    ``business_unit_codes``, and a derived-by-suffix name would have produced
    ``bu_codes``, which ``ScopeFilters`` does not have — so the scope would have
    been set on nothing and silently dropped.
    """
    for code_field, filter_field in s.FILTER_FIELD_BY_LEVEL.items():
        assert filter_field in ScopeFilters.model_fields, code_field


def test_plant_levels_name_columns_the_stock_view_carries(seeded_engine: Engine):
    """A scope level the stock view cannot filter on could never be enforced.

    Asked of the view itself rather than of a list, because the view is what
    ``queries.filter_conditions`` interrogates when it decides whether to apply
    a filter or skip it.
    """
    with Session(bind=seeded_engine, future=True) as session:
        columns = q.view(session, q.MATERIAL_STOCK_VIEW).c
        for code_field in s.PLANT.code_fields():
            assert code_field in columns


def test_plant_chain_matches_the_filter_bars_own_parent_map():
    """Two statements of ``company -> plant -> storage location``, kept equal.

    ``FILTER_PARENTS`` drives the filter bar's cascade and this drives
    containment. A reader whose scope contained a plant the bar thought sat
    under a different company would be refused their own filter selection.
    """
    assert s._LOCATION_PARENT == {
        level: FILTER_PARENTS[level]
        for level in ("plant_code", "storage_location_key")
    }


# ---------------------------------------------------------------------------
# The three traps
# ---------------------------------------------------------------------------


def test_plant_is_not_an_organisational_level():
    """Trap 1. ``LEVEL_DEPTH.get('plant_code', 0)`` would read as shallowest.

    That default is what would have made ``_scope_level_is_redundant`` judge a
    plant scope already implied by any deeper level the caller named, and drop
    it from the query — a scope silently not applied.
    """
    assert "plant_code" not in LEVEL_DEPTH
    assert "plant_code" not in BINDING_BY_LEVEL
    assert not s.ORG.holds("plant_code")


def test_depth_raises_across_dimensions_rather_than_answering_zero():
    """Trap 1 again, from the other side: asking has to be an error, not a guess."""
    with pytest.raises(KeyError):
        s.ORG.depth("plant_code")
    with pytest.raises(KeyError):
        s.PLANT.depth("region_code")


def test_containment_is_asked_per_dimension_and_a_foreign_level_gets_nothing(
    seeded_engine: Engine,
):
    """Trap 2. The org index has no plant, so it must not be asked about one.

    ``MasterDataIndex.ancestors_of`` would raise on ``plant_code``; a caller
    that swallowed that into "no ancestors" would refuse a plant-scoped user
    every plant they own.
    """
    with Session(bind=seeded_engine, future=True) as session:
        indexes = s.ScopeIndexes(session)
        assert indexes.ancestors_of(s.ORG, "plant_code", "PL01") == {}
        assert indexes.ancestors_of(s.PLANT, "region_code", "REG001") == {}


def test_a_dimension_the_scope_says_nothing_about_abstains():
    """Trap 3. Silence is not denial.

    A region-scoped reader's scope makes no claim about plants. Treating the
    plant dimension as constrained for them would refuse the stock page to
    every organisationally scoped user in the business.
    """
    assert [d.key for d in s.constrained_dimensions({"region_code": ["REG001"]})] == ["org"]
    assert [d.key for d in s.constrained_dimensions({"plant_code": ["PL01"]})] == ["plant"]
    assert [d.key for d in s.constrained_dimensions({})] == []


# ---------------------------------------------------------------------------
# The shared level
# ---------------------------------------------------------------------------


def test_company_belongs_to_both_dimensions_as_one_level():
    assert [d.key for d in s.dimensions_of("company_code")] == ["org", "plant"]
    assert s.ORG.levels[0] is s.PLANT.levels[0]
    assert s.LEVEL_BY_CODE_FIELD["company_code"].filter_field == "company_codes"


def test_a_company_scope_constrains_both_dimensions():
    """The one level through which a scope can narrow stock *and* sales at once."""
    assert [d.key for d in s.constrained_dimensions({"company_code": ["C001"]})] == [
        "org", "plant",
    ]


def test_dimensions_of_an_unknown_level_is_empty():
    assert s.dimensions_of("warehouse_code") == ()
    assert s.unknown_levels({"region_code": ["R"], "warehouse_code": ["W"]}) == (
        "warehouse_code",
    )


def test_scope_in_splits_a_mixed_scope():
    mixed = {"region_code": ["REG001"], "plant_code": ["PL01"], "company_code": ["C001"]}
    assert s.scope_in(mixed, s.ORG) == {
        "region_code": ["REG001"], "company_code": ["C001"],
    }
    assert s.scope_in(mixed, s.PLANT) == {
        "plant_code": ["PL01"], "company_code": ["C001"],
    }


# ---------------------------------------------------------------------------
# What an administrator may assign
# ---------------------------------------------------------------------------


def test_a_storage_location_is_contained_but_never_granted():
    """It is in the chain so a plant scope covers what is inside it.

    Scoping somebody to one shelf of one plant is not a thing anybody has asked
    for, and a control whose only outcome is confusion should not be offered.
    """
    assert s.PLANT.holds("storage_location_key")
    assert "storage_location_key" not in s.GRANTABLE_LEVELS
    assert "plant_code" in s.GRANTABLE_LEVELS


def test_grantable_levels_are_todays_nine_plus_plant():
    assert set(s.GRANTABLE_LEVELS) == set(ORG_FILTER_FIELD_TODAY) | {"plant_code"}


# ---------------------------------------------------------------------------
# LocationIndex
# ---------------------------------------------------------------------------


def test_location_index_walks_plant_up_to_company(seeded_engine: Engine):
    with Session(bind=seeded_engine, future=True) as session:
        index = s.LocationIndex(session)
        assert index.ancestors_of("plant_code", "PL01") == {
            "plant_code": "PL01", "company_code": "C001",
        }


def test_location_index_walks_a_storage_location_the_whole_way(seeded_engine: Engine):
    with Session(bind=seeded_engine, future=True) as session:
        index = s.LocationIndex(session)
        assert index.ancestors_of("storage_location_key", "PL01|SL01") == {
            "storage_location_key": "PL01|SL01",
            "plant_code": "PL01",
            "company_code": "C001",
        }


def test_an_unknown_code_is_outside_every_scope_rather_than_an_error(
    seeded_engine: Engine,
):
    with Session(bind=seeded_engine, future=True) as session:
        index = s.LocationIndex(session)
        assert index.ancestors_of("plant_code", "NOPE") == {"plant_code": "NOPE"}
        assert not index.has("plant_code", "NOPE")


def test_a_plant_code_under_two_companies_derives_no_company(seeded_engine: Engine):
    """The platform's rule: a parent is derived only where the master is unambiguous.

    ``dim_plant`` is unique on ``company|plant``, not on the plant code, so the
    same code can in principle name two plants. Picking one of the two companies
    would place the plant inside a company that may not own it — the guess this
    system exists to avoid.
    """
    with Session(bind=seeded_engine, future=True) as session:
        session.add(DimPlant(plant_key=plant_key("C002", "PL01"),
                             company_code="C002", plant_code="PL01",
                             plant_name="Khulna Plant"))
        session.commit()

        index = s.LocationIndex(session)
        assert index.ambiguous == {"PL01": {"C001", "C002"}}
        assert index.ancestors_of("plant_code", "PL01") == {"plant_code": "PL01"}
        # It still exists — it is the *parent* that is unknown, not the plant.
        assert index.has("plant_code", "PL01")


# ---------------------------------------------------------------------------
# ScopeIndexes
# ---------------------------------------------------------------------------


def test_exists_answers_for_both_chains(seeded_engine: Engine):
    with Session(bind=seeded_engine, future=True) as session:
        indexes = s.ScopeIndexes(session)
        assert indexes.exists("region_code", "REG001")
        assert indexes.exists("plant_code", "PL01")
        assert indexes.exists("storage_location_key", "PL01|SL01")
        assert not indexes.exists("plant_code", "PL99")
        assert not indexes.exists("region_code", "REG999")


def test_a_plant_question_does_not_load_the_whole_master_index(seeded_engine: Engine):
    """The lazy half of ``ScopeIndexes``.

    ``MasterDataIndex`` loads every dimension for the ETL's benefit;
    ``LocationIndex`` is two small selects. A stock request should pay for one
    of them, not both.
    """
    with Session(bind=seeded_engine, future=True) as session:
        indexes = s.ScopeIndexes(session)
        assert indexes.ancestors_of(s.PLANT, "plant_code", "PL01")
        assert indexes._master is None
        assert indexes.of(s.PLANT) is indexes.location
        assert indexes.of(s.ORG) is indexes.master


# ---------------------------------------------------------------------------
# The permission filter, rewired onto dimensions
# ---------------------------------------------------------------------------
#
# A plant scope cannot be *granted* until the admin endpoint accepts one, which
# is the next step. These build one directly, which is the point: the machinery
# has to be right before the door is opened.


def plant_user(*codes: str) -> UserContext:
    return UserContext(user_id=901, username="plant_mgr",
                       role=Role.REGIONAL_MANAGER, display_name="Plant Manager",
                       data_scope={"plant_code": list(codes)})


def company_user(code: str = "C001") -> UserContext:
    return UserContext(user_id=902, username="company_mgr",
                       role=Role.REGIONAL_MANAGER, display_name="Company Manager",
                       data_scope={"company_code": [code]})


def test_has_scope_in_answers_per_dimension(session, users):
    org = PermissionFilter(session, users["dhaka_rm"])
    assert org.has_scope_in(s.ORG)
    assert not org.has_scope_in(s.PLANT)

    plant = PermissionFilter(session, plant_user("PL01"))
    assert not plant.has_scope_in(s.ORG)
    assert plant.has_scope_in(s.PLANT)

    unrestricted = PermissionFilter(session, users["ceo"])
    assert unrestricted.has_scope_in(s.ORG)
    assert unrestricted.has_scope_in(s.PLANT)


def test_containment_abstains_across_dimensions_and_the_gate_does_not(session):
    """The two halves of the design, asserted together because they look alike.

    Containment says a plant scope does not *forbid* a region — true, and the
    only answer that keeps the stock page working for an organisationally
    scoped reader, whose scope equally says nothing about plants. What stops
    that abstention becoming consent is the gate beside it.
    """
    permissions = PermissionFilter(session, plant_user("PL01"))
    assert permissions.is_within_scope("region_code", "REG001")
    assert not permissions.has_scope_in(s.ORG)


def test_a_scope_level_is_only_redundant_inside_its_own_chain(session, users):
    permissions = PermissionFilter(session, users["dhaka_rm"])
    # Within the sales chain, unchanged: a named area was already proven to sit
    # inside the region, so filtering on the region too repeats a condition.
    assert permissions._scope_level_is_redundant("region_code", {"area_code"})
    assert not permissions._scope_level_is_redundant("region_code", {"zone_code"})
    # Across chains, never. This is the assertion that fails on a flat depth
    # table with a ``.get(level, 0)`` default, which is what a plant scope
    # would have been dropped by.
    assert not permissions._scope_level_is_redundant("region_code", {"plant_code"})
    plant = PermissionFilter(session, plant_user("PL01"))
    assert not plant._scope_level_is_redundant("plant_code", {"region_code"})
    assert plant._scope_level_is_redundant("plant_code", {"storage_location_key"})


def test_an_org_scope_is_still_injected_exactly_as_before(session, users):
    permissions = PermissionFilter(session, users["dhaka_rm"])
    assert permissions.enforce(ScopeFilters()).region_codes == ["REG001"]
    assert permissions.build_filters([]).region_codes == ["REG001"]
    named = permissions.build_filters([
        ResolvedEntity(entity_type=EntityType.AREA, code="AR001",
                       label="Mirpur", term="Mirpur"),
    ])
    assert named.area_codes == ["AR001"]
    assert named.region_codes == []          # implied by the area, so dropped


def test_a_plant_scope_is_injected_and_a_plant_outside_it_refused(session):
    permissions = PermissionFilter(session, plant_user("PL01"))
    assert permissions.enforce(ScopeFilters()).plant_codes == ["PL01"]
    assert permissions.enforce(ScopeFilters(plant_codes=["PL01"])).plant_codes == ["PL01"]
    with pytest.raises(PermissionDeniedError):
        permissions.enforce(ScopeFilters(plant_codes=["PL99"]))


def test_a_plant_scope_contains_the_storage_locations_inside_it(session):
    permissions = PermissionFilter(session, plant_user("PL01"))
    assert permissions.is_within_scope("storage_location_key", "PL01|SL01")
    assert not permissions.is_within_scope("storage_location_key", "PL01|NOPE")


def test_a_company_scope_binds_both_chains(session):
    """What putting ``company_code`` in both dimensions actually buys."""
    permissions = PermissionFilter(session, company_user())
    assert permissions.is_within_scope("plant_code", "PL01")
    assert permissions.is_within_scope("region_code", "REG001")
    assert permissions.has_scope_in(s.ORG)
    assert permissions.has_scope_in(s.PLANT)


def test_scope_levels_sorts_inside_each_chain_without_comparing_across():
    user = UserContext(user_id=903, username="both", role=Role.REGIONAL_MANAGER,
                       data_scope={"company_code": ["C001"],
                                   "region_code": ["REG001"],
                                   "plant_code": ["PL01"]})
    # Deepest first within the sales chain, then the plant chain. The version
    # keyed on one flat depth table raised ``KeyError`` on the third entry.
    assert user.scope_levels() == ["region_code", "company_code", "plant_code"]


# ---------------------------------------------------------------------------
# What a view can and cannot honour
# ---------------------------------------------------------------------------


def test_unhonourable_levels_names_the_scope_a_view_would_drop(session, users):
    """``filter_conditions`` skips a filter naming a column the view lacks.

    Right for an optional narrowing and silently wrong for a scope. Reporting
    it is what lets a report choose to disclose or to refuse, instead of
    doing neither and answering with everybody's figures.
    """
    stock = q.view(session, q.MATERIAL_STOCK_VIEW).c
    sales = q.view(session, q.SALES_VIEW).c

    region_scoped = PermissionFilter(session, users["dhaka_rm"])
    assert region_scoped.unhonourable_levels(stock) == ("region_code",)
    assert region_scoped.unhonourable_levels(sales) == ()

    plant_scoped = PermissionFilter(session, plant_user("PL01"))
    assert plant_scoped.unhonourable_levels(stock) == ()
    assert plant_scoped.unhonourable_levels(sales) == ("plant_code",)


def test_honourability_is_asked_level_by_level_not_chain_by_chain(session):
    """The case that makes the distinction matter, and it is not hypothetical.

    A scope granting a region *and* a sub-territory is two conditions ANDed.
    ``vw_target_vs_actual`` carries ``region_code`` and nothing below it, so
    honouring the reachable half alone is **wider** than the grant — and where
    the two sit on different branches, wider by exactly the rows the
    sub-territory was there to exclude. A dimension-level test would call this
    scope honourable, because the organisational chain plainly applies.

    This example used to be Credit Control, which carried a sub-territory and no
    region. Revision 0040 gave that view the whole hierarchy, so it stopped being
    an illustration of anything — see the test below, which now pins the opposite.
    """
    user = UserContext(user_id=905, username="mixed", role=Role.REGIONAL_MANAGER,
                       data_scope={"region_code": ["REG002"],
                                   "sub_territory_code": ["STR001"]})
    target = q.view(session, q.TARGET_VS_ACTUAL_VIEW).c
    assert "region_code" in target and "sub_territory_code" not in target
    assert PermissionFilter(session, user).unhonourable_levels(target) == (
        "sub_territory_code",
    )


def both_scopes() -> UserContext:
    return UserContext(user_id=907, username="dual", role=Role.REGIONAL_MANAGER,
                       data_scope={"region_code": ["REG001"], "plant_code": ["PL01"]})


def test_a_missing_column_and_a_missing_dimension_are_not_the_same_thing(session):
    """The distinction the whole check turns on, and it cost a live refusal.

    Both views lack a column the caller is scoped at, for opposite reasons. A
    sale states no plant — there is nothing behind it to hide — while a credit
    invoice *has* a region through its customer's sub-territory and the view
    merely does not join that far up.
    """
    sales = q.view(session, q.SALES_VIEW).c
    credit = q.view(session, q.CREDIT_INVOICE_VIEW).c

    assert s.dimension_applies(s.ORG, sales)
    assert not s.dimension_applies(s.PLANT, sales)
    # Credit has both: a plant column of its own, and a sub-territory that is
    # the bottom of the organisational chain.
    assert s.dimension_applies(s.ORG, credit)
    assert s.dimension_applies(s.PLANT, credit)
    # Material stock has no organisational dimension at all — revision 0016's
    # statement about the source, read off the view.
    assert not s.dimension_applies(s.ORG, q.view(session, q.MATERIAL_STOCK_VIEW).c)


def test_a_region_and_plant_scope_is_served_sales_by_its_region(session):
    """The defect this rule was reported for.

    An account holding a region *and* a plant was refused every sales question,
    because the plant scope looked like a narrowing the sales view had dropped.
    It is not one: no sales row has a plant, so nothing was being widened, and
    their region entitlement was sitting right there unapplied.
    """
    from app.ai.tools import ToolContext

    user = both_scopes()
    ctx = ToolContext(session=session, permissions=PermissionFilter(session, user),
                      user=user)
    assert PermissionFilter(session, user).unhonourable_levels(
        q.view(session, q.SALES_VIEW).c) == ()
    assert ctx.scoped(ScopeFilters(), q.SALES_VIEW).region_codes == ["REG001"]
    # And their stock is still narrowed by the plant.
    assert ctx.scoped(ScopeFilters(), q.MATERIAL_STOCK_VIEW).plant_codes == ["PL01"]


def test_a_region_and_plant_scope_is_now_honoured_on_credit(session):
    """The reversal, and the one view in this file that changed sides.

    Credit Control used to be this module's sharpest example: it carried a plant
    but no region, so the plant half of a dual scope was enforceable and the
    region half was not — and dropping the unenforceable half would have served
    that plant's invoices from customers outside the region. It was the case a
    coarser fix (skip any chain the view carries none of the caller's levels
    for) would have silently broken.

    Revision 0040 gave the view the sales hierarchy, so **both** halves are now
    real columns and the same caller is narrowed rather than refused. That is
    asserted here rather than simply deleted: a test that stops existing leaves
    nobody able to tell whether the behaviour changed or the check did.
    """
    from app.ai.tools import ToolContext

    user = both_scopes()
    permissions = PermissionFilter(session, user)
    assert permissions.unhonourable_levels(
        q.view(session, q.CREDIT_INVOICE_VIEW).c) == ()

    ctx = ToolContext(session=session, permissions=permissions, user=user)
    scoped = ctx.scoped(ScopeFilters(), q.CREDIT_INVOICE_VIEW)
    # Both halves applied, which is the whole point: honouring one alone was
    # always wider than the grant.
    assert scoped.region_codes == ["REG001"]
    assert scoped.plant_codes == ["PL01"]


def test_a_company_scope_is_honourable_on_both_views(session):
    permissions = PermissionFilter(session, company_user())
    for view_name in (q.MATERIAL_STOCK_VIEW, q.SALES_VIEW):
        assert permissions.unhonourable_levels(q.view(session, view_name).c) == ()


def test_an_unrestricted_caller_has_no_scope_to_lose(session, users):
    permissions = PermissionFilter(session, users["ceo"])
    assert permissions.unhonourable_levels(q.view(session, q.SALES_VIEW).c) == ()


# ---------------------------------------------------------------------------
# The declared policy, and what each surface does with it
# ---------------------------------------------------------------------------


def test_stock_discloses_and_everything_else_refuses():
    """The one asymmetry in the platform's scope handling, pinned.

    Stock is not held below company, so an organisationally scoped reader is
    shown the whole of what exists with a note. Sales and target *are* held by
    territory and customer, so the same silence there would be somebody else's
    figures.
    """
    assert q.scope_policy(q.MATERIAL_STOCK_VIEW) is s.ScopePolicy.DISCLOSE
    for view_name in (q.SALES_VIEW, q.TARGET_VIEW, q.TARGET_VS_ACTUAL_VIEW,
                      q.CREDIT_INVOICE_VIEW):
        assert q.scope_policy(view_name) is s.ScopePolicy.REFUSE


def test_a_view_nobody_declared_a_policy_for_refuses():
    """A report nobody has thought about does not get to answer a scoped caller."""
    assert q.scope_policy("vw_something_new") is s.ScopePolicy.REFUSE


def test_a_plant_scoped_caller_is_refused_sales_and_served_stock(session):
    """The whole point of the exercise, at the tool layer."""
    from app.ai.exceptions import ScopeNotEnforceable
    from app.ai.tools import ToolContext

    user = plant_user("PL01")
    ctx = ToolContext(session=session, permissions=PermissionFilter(session, user),
                      user=user)
    assert ctx.scoped(ScopeFilters(), q.MATERIAL_STOCK_VIEW).plant_codes == ["PL01"]
    with pytest.raises(ScopeNotEnforceable):
        ctx.scoped(ScopeFilters(), q.SALES_VIEW)


def test_an_org_scoped_caller_is_served_both(session, users):
    """Unchanged, and the half that would be easy to break.

    Stock discloses rather than refusing, so a regional manager keeps the stock
    page they have always had.
    """
    from app.ai.tools import ToolContext

    user = users["dhaka_rm"]
    ctx = ToolContext(session=session, permissions=PermissionFilter(session, user),
                      user=user)
    assert ctx.scoped(ScopeFilters(), q.MATERIAL_STOCK_VIEW).region_codes == ["REG001"]
    assert ctx.scoped(ScopeFilters(), q.SALES_VIEW).region_codes == ["REG001"]


def test_every_tool_names_a_view_with_a_declared_policy():
    """A tool reading a view outside the policy table would refuse everybody.

    ``scope_policy`` defaults to REFUSE, which is the right default and a very
    quiet way to break a report — so the views the tools actually pass are
    checked against the table rather than assumed to be in it.
    """
    import re
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "app" / "ai" / "tools.py"
    named = set(re.findall(r"ctx\.scoped\([^)]*?((?:q\.[A-Z_]+VIEW(?:,\s*)?)+)",
                           source.read_text(encoding="utf-8")))
    passed = {v for group in named for v in re.findall(r"q\.([A-Z_]+VIEW)", group)}
    assert passed, "no tool call site names a view any more — check the pattern"
    for constant in passed:
        assert getattr(q, constant) in q.SCOPE_POLICY, constant


# ---------------------------------------------------------------------------
# The gates, which must not read abstention as consent
# ---------------------------------------------------------------------------


def test_a_report_refuses_a_scope_it_is_not_held_by(session):
    """Without the check this returns the caller's own filter, unnarrowed.

    The supplied region passes containment (the plant chain is silent about
    regions), and ``if supplied: return filters`` then hands back a request
    carrying no scope at all — the whole company's sales, to somebody scoped
    to one plant.
    """
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        enforce_report_scope(session, plant_user("PL01"),
                             ReportFilters(region_code="REG001"), "vw_daily_sales")
    assert raised.value.status_code == 403


def test_a_report_still_narrows_an_org_scoped_caller(session, users):
    narrowed = enforce_report_scope(session, users["dhaka_rm"], ReportFilters(),
                                    "vw_daily_sales")
    assert narrowed.region_code == "REG001"


def test_the_stock_report_narrows_a_plant_scoped_caller(session):
    """The report endpoint's half of the feature.

    ``ReportFilters`` gained ``plant_code`` for exactly this: without a field to
    put it in, the one report a plant scope is *for* could not be narrowed by it.
    """
    narrowed = enforce_report_scope(session, plant_user("PL01"), ReportFilters(),
                                    q.MATERIAL_STOCK_VIEW)
    assert narrowed.plant_code == "PL01"


def test_the_stock_report_still_serves_an_org_scoped_caller(session, users):
    """Disclose, not refuse — and the filter it pins must be one stock carries.

    A caller scoped to a region has nothing the stock view can express, so the
    request goes through unnarrowed exactly as it always has.
    """
    narrowed = enforce_report_scope(session, users["dhaka_rm"], ReportFilters(),
                                    q.MATERIAL_STOCK_VIEW)
    assert narrowed.region_code is None
    assert narrowed.plant_code is None


def test_a_report_pins_a_level_its_view_can_actually_filter_on(session):
    """A scope in both chains, on a view that carries only one of them.

    Taking the deepest level regardless would pin ``region_code`` on the stock
    view, which discards it — leaving the plant scope unapplied by the very
    function that exists to apply it.
    """
    user = UserContext(user_id=906, username="both", role=Role.REGIONAL_MANAGER,
                       data_scope={"region_code": ["REG001"], "plant_code": ["PL01"]})
    narrowed = enforce_report_scope(session, user, ReportFilters(),
                                    q.MATERIAL_STOCK_VIEW)
    assert narrowed.plant_code == "PL01"
    assert narrowed.region_code is None


def test_a_report_refuses_a_scope_its_own_view_cannot_express(session, users):
    """A hole that predates the plant work, closed by the same rule.

    ``vw_target_vs_actual`` carries ``region_code`` and nothing below it, so an
    area-scoped caller had their scope applied here and then dropped by
    ``_apply_filters`` — and was served the whole country's targets.
    """
    from fastapi import HTTPException

    assert "area_code" not in q.view(session, "vw_target_vs_actual").c
    with pytest.raises(HTTPException) as raised:
        enforce_report_scope(session, users["mirpur_am"], ReportFilters(),
                             "vw_target_vs_actual")
    assert raised.value.status_code == 403
    # And the region-scoped caller that view *can* express is still served.
    narrowed = enforce_report_scope(session, users["dhaka_rm"], ReportFilters(),
                                    "vw_target_vs_actual")
    assert narrowed.region_code == "REG001"


# ---------------------------------------------------------------------------
# Granting one
# ---------------------------------------------------------------------------


def test_an_administrator_may_grant_a_plant_scope(session):
    from app.api.routes_admin import _validate_scope

    assert _validate_scope(session, {"plant_code": ["PL01"]}) == {"plant_code": ["PL01"]}
    assert _validate_scope(
        session, {"region_code": ["REG001"], "plant_code": ["PL01"]}
    ) == {"region_code": ["REG001"], "plant_code": ["PL01"]}


def test_a_plant_code_is_checked_against_the_plant_master(session):
    """Not against ``MasterDataIndex.ids``, which has no plants in it.

    Validating there would have rejected every real plant with "does not exist
    in the master data" — the right refusal for entirely the wrong reason.
    """
    from fastapi import HTTPException

    from app.api.routes_admin import _validate_scope

    with pytest.raises(HTTPException) as raised:
        _validate_scope(session, {"plant_code": ["PL99"]})
    assert "plant_code=PL99" in raised.value.detail


def test_a_storage_location_scope_cannot_be_granted(session):
    from fastapi import HTTPException

    from app.api.routes_admin import _validate_scope

    with pytest.raises(HTTPException) as raised:
        _validate_scope(session, {"storage_location_key": ["PL01|SL01"]})
    assert "Unknown scope level" in raised.value.detail


# ---------------------------------------------------------------------------
# What the admin form is told
# ---------------------------------------------------------------------------


def test_a_shared_level_is_offered_by_one_chain_only():
    """``company_code`` is in both chains and must be drawn once.

    A form with one control per chain would otherwise show Company twice — two
    controls writing one key, where filling in both leaves an administrator with
    no way to tell which took effect and no reason to trust the answer.
    """
    offered = dict(
        (dimension.key, levels) for dimension, levels in s.grantable_by_dimension()
    )
    assert offered["org"][0] == "company_code"
    assert "company_code" not in offered["plant"]
    assert offered["plant"] == ("plant_code",)

    everything = [level for levels in offered.values() for level in levels]
    assert len(everything) == len(set(everything))
    assert set(everything) == set(s.GRANTABLE_LEVELS)


def test_the_admin_payload_offers_both_chains_and_the_flat_list(session, users):
    """The endpoint publishes both shapes, and they have to agree.

    ``scope_levels`` is what the form read before there was a second chain, and
    a bundle older than this server still reads it. ``scope_dimensions`` is what
    lets a newer one draw a control per chain without carrying its own list of
    which level is a plant.
    """
    from app.api.routes_admin import roles as roles_endpoint

    payload = roles_endpoint(session=session)
    assert payload["scope_levels"] == sorted(s.GRANTABLE_LEVELS)
    assert [d["key"] for d in payload["scope_dimensions"]] == ["org", "plant"]

    from_chains = [
        level["code_field"]
        for dimension in payload["scope_dimensions"]
        for level in dimension["levels"]
    ]
    assert sorted(from_chains) == payload["scope_levels"]
    # Every level carries a name a person can read, never a bare column name.
    plant = payload["scope_dimensions"][1]
    assert plant["levels"] == [{"code_field": "plant_code", "label": "plant"}]
