"""The hierarchical review: what a manager sees before signing a target.

Three promises are pinned here.

**The tree is the allocation's own snapshot.** Every row names its parent, and
that chain is read back rather than rebuilt from the organisational masters — so
a territory that moves to another region next month cannot retroactively reshape
a target somebody has already approved.

**Every number that cannot be computed reads as absent, not as zero.** Growth
against a year with no sales is undefined rather than −100%; achievement before
the period has any actuals is `n/a` rather than 0%; a node whose materials lack
a conversion factor reports no quantity rather than a partial one.

**Scope narrows where the tree starts, not what the numbers inside it say.** A
regional manager's tree is rooted at their region, carrying that region's real
figures. They never see a country row holding a narrowed total labelled
"Country" — a figure that is neither the country's nor theirs — and never see
the country's true total either.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import DimMaterial
from app.database.models_target import (
    RevisionStatus,
    TargetAllocation,
    TargetLevel,
    TargetRevision,
)
from app.targetmgmt import (
    engine as allocation_engine,
    factors,
    plans as plan_service,
    review,
)
from test_target_allocation import (  # noqa: F401  (fixtures reused wholesale)
    CUSTOMERS,
    MATERIAL,
    SCOPE,
    _run,
    _seed_sales,
    _worker_engine,
    hierarchy,
    planned,
    with_history,
)


@pytest.fixture
def allocated(with_history, users, planned):
    """A version with a stored allocation, which is what review reads."""
    with Session(with_history) as session:
        version = plan_service.get_version(session, planned["version_id"])
        result = _run(session, users, planned)
        allocation_engine.persist(session, version=version, result=result)
        session.commit()
    return planned


def _tree(engine, users, planned, username="ceo", material_code=None):
    with Session(engine) as session:
        plan = plan_service.get_plan(session, planned["plan_id"])
        return review.tree(session, users[username], plan=plan,
                           version_id=planned["version_id"],
                           material_code=material_code)


def _by_node(result, level, code):
    return next(row for row in result["rows"]
                if row["level"] == level and row["node_code"] == code)


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_an_unallocated_version_has_nothing_to_review(with_history, users,
                                                      planned) -> None:
    result = _tree(with_history, users, planned)
    assert result["rows"] == []
    assert "not been allocated" in result["notes"][0]


def test_the_tree_reaches_every_allocated_level(with_history, users,
                                                allocated) -> None:
    result = _tree(with_history, users, allocated)
    assert set(result["levels"]) == {
        TargetLevel.COMPANY, TargetLevel.ZONE, TargetLevel.REGION,
        TargetLevel.AREA, TargetLevel.UNIT, TargetLevel.TERRITORY,
        TargetLevel.SUB_TERRITORY, TargetLevel.CUSTOMER,
    }


def test_rows_arrive_in_reading_order_with_a_depth(with_history, users,
                                                   allocated) -> None:
    """Flattened on the server, because the browser draws a table.

    Reading order plus a depth is what an expandable table row needs; a nested
    payload would have to be flattened in the browser anyway.
    """
    result = _tree(with_history, users, allocated)
    assert result["rows"][0]["depth"] == 0
    assert result["rows"][0]["level"] == TargetLevel.COMPANY
    # A child never appears before its parent.
    seen: set[str] = set()
    for row in result["rows"]:
        if row["parent_code"]:
            assert f"{row['parent_level']}:{row['parent_code']}" in seen
        seen.add(f"{row['level']}:{row['node_code']}")


def test_a_node_says_whether_it_has_children(with_history, users,
                                             allocated) -> None:
    result = _tree(with_history, users, allocated)
    assert _by_node(result, TargetLevel.COMPANY, "C001")["has_children"] is True
    customers = [row for row in result["rows"]
                 if row["level"] == TargetLevel.CUSTOMER]
    assert customers and all(row["has_children"] is False for row in customers)


def test_each_node_carries_its_name(with_history, users, allocated) -> None:
    """Identified by code, labelled by name — nothing joins on the name."""
    result = _tree(with_history, users, allocated)
    assert _by_node(result, TargetLevel.REGION, "REG001")["name"] == "Dhaka"


# ---------------------------------------------------------------------------
# The arithmetic this screen exists for
# ---------------------------------------------------------------------------


def test_recon_variance_is_zero_at_every_level(with_history, users,
                                               allocated) -> None:
    """Zero by construction, displayed anyway — a checked claim beats a true one."""
    result = _tree(with_history, users, allocated)
    assert all(row["recon_variance"] == 0 for row in result["rows"])
    assert result["reconciliation"]["balanced"] is True
    assert result["reconciliation"]["mismatched_nodes"] == 0


def test_a_tampered_row_shows_as_a_variance(with_history, users,
                                            allocated) -> None:
    """The check has to be capable of failing, or it proves nothing."""
    with Session(with_history) as session:
        row = session.execute(
            select(TargetAllocation).where(
                TargetAllocation.level == TargetLevel.REGION).limit(1)
        ).scalar_one()
        row.current_volume = Decimal(str(row.current_volume)) + Decimal("100")
        session.commit()

    result = _tree(with_history, users, allocated)
    assert result["reconciliation"]["balanced"] is False
    assert result["reconciliation"]["mismatched_nodes"] >= 1
    assert any(row["recon_variance"] != 0 for row in result["rows"])


def test_the_root_total_equals_the_country_target(with_history, users,
                                                  allocated) -> None:
    result = _tree(with_history, users, allocated)
    root = _by_node(result, TargetLevel.COMPANY, "C001")
    assert root["target_volume"] == pytest.approx(120000.0)
    assert result["reconciliation"]["target_volume"] == pytest.approx(120000.0)


def test_quantity_and_value_are_derived_per_material(with_history, users,
                                                     allocated) -> None:
    """One node's volume can span materials with different divisors.

    Dividing the node's total by any single conversion factor would be
    arithmetic about nothing, so each material is derived and then summed.
    """
    result = _tree(with_history, users, allocated)
    root = _by_node(result, TargetLevel.COMPANY, "C001")
    # 120,000 at a conversion factor of 0.5, priced at 240.
    assert root["target_quantity"] == pytest.approx(240000.0)
    assert root["target_value"] == pytest.approx(240000.0 * 240)


def test_a_material_without_a_conversion_factor_suppresses_the_derived_columns(
        with_history, users, allocated) -> None:
    """A partial sum presented as the node's quantity is short by an unknown
    amount, which is worse than an honest n/a."""
    with Session(with_history) as session:
        material = session.execute(
            select(DimMaterial).where(DimMaterial.material_code == MATERIAL)
        ).scalar_one()
        material.conversion_factor = None
        session.commit()

    result = _tree(with_history, users, allocated)
    root = _by_node(result, TargetLevel.COMPANY, "C001")
    assert root["target_quantity"] is None
    assert root["target_value"] is None
    assert MATERIAL in root["missing_derivation"]
    # The volume is unaffected: the engine allocates volume, not quantity.
    assert root["target_volume"] == pytest.approx(120000.0)


# ---------------------------------------------------------------------------
# Figures that cannot be computed
# ---------------------------------------------------------------------------


def test_achievement_is_none_before_the_period_has_actuals(with_history, users,
                                                           allocated) -> None:
    """The design's own footnote, enforced.

    FY 2026-27 has no actual sales yet, and a ratio that cannot be computed is
    never shown as zero.
    """
    result = _tree(with_history, users, allocated)
    assert all(row["achievement_percent"] is None for row in result["rows"])
    assert any("never shown as zero" in note for note in result["notes"])


def test_growth_is_none_where_last_year_had_no_sales(hierarchy, users,
                                                     planned) -> None:
    """Growth from nothing is undefined, not −100%."""
    _seed_sales(hierarchy, months=[(2025, 9, 2), (2025, 10, 4)],
                customers=[CUSTOMERS[0]])
    with Session(hierarchy) as session:
        version = plan_service.get_version(session, planned["version_id"])
        result = _run(session, users, planned)
        allocation_engine.persist(session, version=version, result=result)
        session.commit()

    tree = _tree(hierarchy, users, planned)
    untraded = [row for row in tree["rows"]
                if row["level"] == TargetLevel.CUSTOMER
                and row["node_code"] != CUSTOMERS[0]]
    assert untraded, "expected a customer with no history"
    assert all(row["growth_percent"] is None for row in untraded)
    assert all(row["previous_year_volume"] is None for row in untraded)


def test_growth_is_computed_where_last_year_had_sales(with_history, users,
                                                      allocated) -> None:
    result = _tree(with_history, users, allocated)
    root = _by_node(result, TargetLevel.COMPANY, "C001")
    assert root["previous_year_volume"] is not None
    assert root["growth_percent"] is not None


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_a_regional_manager_sees_their_region_as_the_root(with_history, users,
                                                          allocated) -> None:
    """Not a country row carrying a narrowed total labelled "Country"."""
    result = _tree(with_history, users, allocated, username="dhaka_rm")

    assert result["rows"], "the regional manager saw nothing"
    assert result["rows"][0]["level"] == TargetLevel.REGION
    assert result["rows"][0]["node_code"] == "REG001"
    assert result["rows"][0]["depth"] == 0
    levels = {row["level"] for row in result["rows"]}
    assert TargetLevel.COMPANY not in levels
    assert TargetLevel.ZONE not in levels


def test_a_regional_manager_sees_their_regions_real_figures(with_history, users,
                                                            allocated) -> None:
    """Narrowing the tree must not narrow the numbers inside it."""
    everything = _tree(with_history, users, allocated)
    scoped = _tree(with_history, users, allocated, username="dhaka_rm")

    full_region = _by_node(everything, TargetLevel.REGION, "REG001")
    scoped_region = _by_node(scoped, TargetLevel.REGION, "REG001")
    assert scoped_region["target_volume"] == full_region["target_volume"]


def test_a_scoped_reader_never_sees_another_branch(with_history, users,
                                                   allocated) -> None:
    result = _tree(with_history, users, allocated, username="dhaka_rm")
    assert all(row["node_code"] != "REG002" for row in result["rows"])


def test_a_reader_scoped_outside_the_allocation_sees_an_empty_tree(
        with_history, users, allocated) -> None:
    """A legitimate empty view, not an error and not somebody else's data."""
    from app.ai.permission_filter import UserContext
    from app.database.models_ai import Role

    elsewhere = UserContext(user_id=99, username="other",
                            role=Role.REGIONAL_MANAGER,
                            data_scope={"region_code": ["REG999"]})
    with Session(with_history) as session:
        plan = plan_service.get_plan(session, allocated["plan_id"])
        result = review.tree(session, elsewhere, plan=plan,
                             version_id=allocated["version_id"])

    assert result["rows"] == []
    assert "nothing here for you to review" in result["notes"][0]


def test_the_scope_is_described_in_words(with_history, users,
                                         allocated) -> None:
    assert _tree(with_history, users, allocated)["scope"] == "Full country scope"
    assert "Scoped to" in _tree(with_history, users, allocated,
                                username="dhaka_rm")["scope"]


# ---------------------------------------------------------------------------
# Filtering, adjustments and revisions
# ---------------------------------------------------------------------------


def test_the_tree_can_be_narrowed_to_one_material(with_history, users,
                                                  allocated) -> None:
    result = _tree(with_history, users, allocated, material_code=MATERIAL)
    assert result["material_code"] == MATERIAL
    assert result["rows"]
    assert MATERIAL in result["materials"]


def test_an_unknown_material_narrows_to_nothing(with_history, users,
                                                allocated) -> None:
    result = _tree(with_history, users, allocated, material_code="NO-SUCH")
    assert result["rows"] == []


def test_an_adjusted_node_is_marked(with_history, users, planned) -> None:
    """`system_volume` and `current_volume` differ only where one was moved."""
    from app.targetmgmt import adjustments

    settings = factors.FactorSettings.from_request(
        {"enabled": {"management_adjustment": True}})
    with Session(with_history) as session:
        plan = plan_service.get_plan(session, planned["plan_id"])
        version = plan_service.get_version(session, planned["version_id"])
        adjustments.record(session, users["ceo"], version=version, plan=plan,
                           level=TargetLevel.REGION, node_code="REG001",
                           adjustment_volume=1200,
                           reason="Herbicide demand review.")
        session.flush()
        result = _run(session, users, planned, settings)
        allocation_engine.persist(session, version=version, result=result)
        session.commit()

    tree = _tree(with_history, users, planned)
    region = _by_node(tree, TargetLevel.REGION, "REG001")
    assert region["adjusted"] is True
    assert region["target_volume"] != region["system_volume"]
    # And the root still holds exactly what was typed.
    assert _by_node(tree, TargetLevel.COMPANY, "C001")["target_volume"] == \
        pytest.approx(120000.0)


def test_an_open_revision_is_counted_against_its_node(with_history, users,
                                                      allocated) -> None:
    """Counted from the revision table, not inferred from a status.

    A node can carry an open request while its own figure looks settled, which
    is exactly the case a manager must not miss before signing.
    """
    with Session(with_history) as session:
        row = session.execute(
            select(TargetAllocation).where(
                TargetAllocation.level == TargetLevel.CUSTOMER).limit(1)
        ).scalar_one()
        session.add(TargetRevision(
            allocation_id=row.allocation_id,
            system_volume=row.system_volume,
            requested_volume=Decimal(str(row.system_volume)) - Decimal("10"),
            reason="Customer potential reduced.",
            status=RevisionStatus.PENDING, requested_by="dhaka_rm",
        ))
        session.commit()
        node = (row.level, row.node_code)

    result = _tree(with_history, users, allocated)
    target = _by_node(result, node[0], node[1])
    assert target["pending_revisions"] == 1
    assert all(other["pending_revisions"] == 0 for other in result["rows"]
               if (other["level"], other["node_code"]) != node)


def test_a_settled_revision_is_not_counted(with_history, users,
                                           allocated) -> None:
    with Session(with_history) as session:
        row = session.execute(
            select(TargetAllocation).where(
                TargetAllocation.level == TargetLevel.CUSTOMER).limit(1)
        ).scalar_one()
        session.add(TargetRevision(
            allocation_id=row.allocation_id,
            system_volume=row.system_volume,
            requested_volume=row.system_volume,
            reason="Approved already.",
            status=RevisionStatus.APPROVED, requested_by="dhaka_rm",
        ))
        session.commit()

    result = _tree(with_history, users, allocated)
    assert all(row["pending_revisions"] == 0 for row in result["rows"])


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


def test_the_endpoint_serves_the_tree(with_history, users, allocated) -> None:
    from fastapi.testclient import TestClient

    from app.api.deps import get_session
    from app.auth.security import hash_password
    from app.database.models_ai import AppUser, Role
    from app.main import app

    password = "Correct-Horse-9"
    with Session(with_history) as session:
        session.add(AppUser(username="root", display_name="Administrator",
                            role=Role.SUPER_ADMIN, is_active=True,
                            password_hash=hash_password(password)))
        session.commit()

    def _override():
        session = Session(bind=with_history, expire_on_commit=False, future=True)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _override
    try:
        client = TestClient(app)
        token = client.post("/api/auth/login",
                            json={"username": "root",
                                  "password": password}).json()["access_token"]
        response = client.get(
            f"/api/target-management/versions/{allocated['version_id']}/review",
            headers={"Authorization": f"Bearer {token}"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reconciliation"]["balanced"] is True
    assert body["rows"][0]["level"] == TargetLevel.COMPANY
    assert body["scope"] == "Full country scope"


def test_previous_year_sales_roll_up_the_tree(with_history, users,
                                              allocated) -> None:
    """A parent's PY figure is the sum of its children's.

    Necessary, not tidy. A sales row states a territory and a customer but no
    sub-territory, so the view's ``sub_territory_code`` is NULL on it — and
    without the roll-up a sub-territory would report no sales while the
    customers beneath it reported plenty. A column that visibly fails to add up
    on the one screen whose purpose is showing that things add up.
    """
    result = _tree(with_history, users, allocated)
    sub = _by_node(result, TargetLevel.SUB_TERRITORY, "STR001")
    customers = [row["previous_year_volume"] for row in result["rows"]
                 if row["level"] == TargetLevel.CUSTOMER
                 and row["parent_code"] == "STR001"
                 and row["previous_year_volume"] is not None]

    assert customers, "expected customers with history"
    assert sub["previous_year_volume"] == pytest.approx(sum(customers))
    assert sub["growth_percent"] is not None


def test_a_branch_with_no_sales_stays_absent_rather_than_zero(
        with_history, users, allocated) -> None:
    """Sold nothing recorded is ``None``, which is what makes growth read n/a.

    Zero would be a measurement, and growth against it would be a division that
    should never have been attempted.
    """
    result = _tree(with_history, users, allocated)
    khulna = _by_node(result, TargetLevel.REGION, "REG002")
    assert khulna["previous_year_volume"] is None
    assert khulna["growth_percent"] is None
    # Its target is real, though — the allocation reached it.
    assert khulna["target_volume"] > 0


def test_a_reader_with_no_data_scope_is_told_so_plainly(with_history,
                                                        allocated) -> None:
    """Three cases, not two — and the empty one names who can fix it.

    A restricted account with no scope sees nothing, which is the platform's
    own rule. Describing that as "Scoped to no data scope by your role" was
    ungrammatical and told the reader neither what was wrong nor what to do.
    """
    from app.ai.permission_filter import UserContext
    from app.database.models_ai import Role

    stranger = UserContext(user_id=99, username="new_joiner",
                           role=Role.REGIONAL_MANAGER)
    result = _tree(with_history, {"new_joiner": stranger}, allocated,
                   username="new_joiner")
    assert result["rows"] == []
    assert "no data scope" in result["scope"]
    assert "administrator" in result["scope"].lower()


def test_a_scoped_reader_is_told_what_they_hold(with_history, users,
                                                allocated) -> None:
    from app.ai.permission_filter import UserContext
    from app.database.models_ai import Role

    manager = UserContext(user_id=98, username="dhaka",
                          role=Role.REGIONAL_MANAGER,
                          data_scope={"region_code": ["REG001"]})
    result = _tree(with_history, {"dhaka": manager}, allocated,
                   username="dhaka")
    assert result["rows"]
    assert "REG001" in result["scope"]
