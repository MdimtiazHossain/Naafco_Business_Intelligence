"""Comparing two versions, and the dashboard that says where every plan stands.

Both are read-only and neither computes a figure anybody else already reports.
Three promises are pinned.

**Added and removed are not zero.** A node in one version and not the other has
no target there, which is a different statement from a target of nothing — so
its figure is absent, its change is absent, and its status says which side it is
missing from.

**A total is taken from the roots, never by summing the rows.** Summing a tree
adds each figure once per level it appears at, which would report one movement
several times over.

**The dashboard claims only counts of workflow state and figures somebody
typed.** No scoped aggregate, because one headline volume would mean something
different to every reader; no achievement percentage, because that is the Target
page's question and two screens computing it is how they come to disagree.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.permission_filter import UserContext
from app.database.models_ai import Role
from app.database.models_target import (
    AllocationJobStatus,
    TargetAllocation,
    TargetAllocationJob,
    TargetLevel,
    TargetStatus,
)
from app.targetmgmt import (
    approvals,
    compare,
    country,
    dashboard,
    engine as allocation_engine,
    lock as target_lock,
    plans as plan_service,
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
from test_target_approvals import chain_users  # noqa: F401


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def two_versions(with_history, users, planned):
    """V1 at 120,000 and V2 at 150,000, both allocated. The ordinary case."""
    with Session(with_history) as session:
        plan = plan_service.get_plan(session, planned["plan_id"])
        first = plan_service.get_version(session, planned["version_id"])
        allocation_engine.persist(session, version=first,
                                  result=_run(session, users, planned))
        session.commit()

        second = plan_service.create_version(
            session, users["ceo"], plan_id=plan.plan_id,
            reason="Country target raised after the September review.")
        session.flush()
        country.set_lines(session, users["ceo"], version=second, plan=plan,
                          entries=[(MATERIAL, 150000)])
        session.flush()
        allocation_engine.persist(
            session, version=second,
            result=_run(session, users, {"plan_id": plan.plan_id,
                                         "version_id": second.version_id}))
        session.commit()
        return {"plan_id": plan.plan_id, "base": first.version_id,
                "head": second.version_id}


def _open(engine, ids):
    session = Session(engine)
    plan = plan_service.get_plan(session, ids["plan_id"])
    return session, plan


def _compare(session, plan, ids, user, material_code=None):
    return compare.compare(session, user, plan=plan,
                           base_version_id=ids["base"],
                           version_id=ids["head"],
                           material_code=material_code)


def _by_node(result, level, code):
    return next(row for row in result["rows"]
                if row["level"] == level and row["node_code"] == code)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def test_the_headline_is_the_roots_not_the_sum_of_the_rows(with_history,
                                                           two_versions,
                                                           users) -> None:
    """Summing a tree reports one movement once per level it appears at."""
    session, plan = _open(with_history, two_versions)
    result = _compare(session, plan, two_versions, users["ceo"])
    totals = result["totals"]
    assert totals["base_volume"] == pytest.approx(120000.0)
    assert totals["volume"] == pytest.approx(150000.0)
    assert totals["change"] == pytest.approx(30000.0)
    assert totals["change_percent"] == pytest.approx(25.0)
    session.close()


def test_every_node_carries_both_sides_and_its_movement(with_history,
                                                        two_versions,
                                                        users) -> None:
    session, plan = _open(with_history, two_versions)
    result = _compare(session, plan, two_versions, users["ceo"])
    root = _by_node(result, TargetLevel.COMPANY, "C001")
    assert root["base_volume"] == pytest.approx(120000.0)
    assert root["volume"] == pytest.approx(150000.0)
    assert root["change"] == pytest.approx(30000.0)
    assert root["status"] == compare.INCREASED
    session.close()


def test_rows_arrive_in_reading_order_with_a_depth(with_history, two_versions,
                                                   users) -> None:
    """The same shape the review tree sends, so the browser draws one table."""
    session, plan = _open(with_history, two_versions)
    result = _compare(session, plan, two_versions, users["ceo"])
    assert result["rows"][0]["depth"] == 0
    assert result["rows"][0]["level"] == TargetLevel.COMPANY
    seen: set[str] = set()
    for row in result["rows"]:
        if row["parent_code"] and row["status"] != compare.REMOVED:
            assert f"{row['parent_level']}:{row['parent_code']}" in seen
        seen.add(f"{row['level']}:{row['node_code']}")
    session.close()


def test_a_node_only_the_newer_version_has_reads_as_added(with_history,
                                                          two_versions,
                                                          users) -> None:
    """Absent on one side, never zero — and no percentage against nothing."""
    session, plan = _open(with_history, two_versions)
    session.execute(TargetAllocation.__table__.delete().where(
        TargetAllocation.version_id == two_versions["base"],
        TargetAllocation.node_code == CUSTOMERS[0]))
    session.commit()

    result = _compare(session, plan, two_versions, users["ceo"])
    row = _by_node(result, TargetLevel.CUSTOMER, CUSTOMERS[0])
    assert row["status"] == compare.ADDED
    assert row["base_volume"] is None
    assert row["change"] is None
    assert row["change_percent"] is None
    session.close()


def test_a_node_only_the_older_version_has_reads_as_removed(with_history,
                                                            two_versions,
                                                            users) -> None:
    """Listed rather than dropped: omitting it would under-report the change."""
    session, plan = _open(with_history, two_versions)
    session.execute(TargetAllocation.__table__.delete().where(
        TargetAllocation.version_id == two_versions["head"],
        TargetAllocation.node_code == CUSTOMERS[0]))
    session.commit()

    result = _compare(session, plan, two_versions, users["ceo"])
    row = _by_node(result, TargetLevel.CUSTOMER, CUSTOMERS[0])
    assert row["status"] == compare.REMOVED
    assert row["volume"] is None
    assert row["change"] is None
    assert any("removed rather than as zero" in note
               for note in result["notes"])
    session.close()


def test_an_unchanged_node_says_so(with_history, users, planned) -> None:
    """A version copied without re-allocating moves nothing, and reads as nothing."""
    with Session(with_history) as session:
        plan = plan_service.get_plan(session, planned["plan_id"])
        first = plan_service.get_version(session, planned["version_id"])
        result = _run(session, users, planned)
        allocation_engine.persist(session, version=first, result=result)
        session.commit()
        second = plan_service.create_version(
            session, users["ceo"], plan_id=plan.plan_id, reason="No change.")
        session.flush()
        allocation_engine.persist(
            session, version=second,
            result=_run(session, users, {"plan_id": plan.plan_id,
                                         "version_id": second.version_id}))
        session.commit()

        verdict = compare.compare(session, users["ceo"], plan=plan,
                                  base_version_id=first.version_id,
                                  version_id=second.version_id)
    assert verdict["totals"]["change"] == pytest.approx(0.0)
    assert verdict["totals"]["nodes_changed"] == 0
    assert all(row["status"] == compare.UNCHANGED for row in verdict["rows"])


def test_the_country_lines_are_carried_on_both_sides(with_history,
                                                     two_versions,
                                                     users) -> None:
    """The figure a person entered — where an allocation movement is explained."""
    session, plan = _open(with_history, two_versions)
    result = _compare(session, plan, two_versions, users["ceo"])
    line = next(row for row in result["country"]
                if row["material_code"] == MATERIAL)
    assert line["base_volume"] == pytest.approx(120000.0)
    assert line["volume"] == pytest.approx(150000.0)
    assert line["status"] == compare.INCREASED
    session.close()


def test_two_versions_of_different_plans_are_refused(with_history, users,
                                                     two_versions) -> None:
    """A difference between two plans is two unrelated targets subtracted."""
    session, plan = _open(with_history, two_versions)
    other = plan_service.create_plan(session, users["ceo"], scope=plan_service.PlanScope(
        financial_year="FY 2027-28", target_period="FY", company_code="C001",
        bu_code="BU001", sales_line_code="SL001"))
    session.flush()
    other_version = plan_service.current_version(session, other.plan_id)
    session.commit()

    with pytest.raises(compare.CrossPlanComparison):
        compare.compare(session, users["ceo"], plan=plan,
                        base_version_id=two_versions["base"],
                        version_id=other_version.version_id)
    session.close()


def test_a_version_with_no_allocation_is_refused_by_name(with_history, users,
                                                         two_versions) -> None:
    session, plan = _open(with_history, two_versions)
    third = plan_service.create_version(
        session, users["ceo"], plan_id=plan.plan_id, reason="Not run yet.")
    session.commit()

    with pytest.raises(compare.NothingToCompare) as caught:
        compare.compare(session, users["ceo"], plan=plan,
                        base_version_id=two_versions["base"],
                        version_id=third.version_id)
    assert "not been allocated" in caught.value.user_message
    session.close()


def test_a_material_filter_narrows_both_sides(with_history, two_versions,
                                              users) -> None:
    session, plan = _open(with_history, two_versions)
    result = _compare(session, plan, two_versions, users["ceo"],
                      material_code=MATERIAL)
    assert result["material_code"] == MATERIAL
    assert result["totals"]["volume"] == pytest.approx(150000.0)
    assert MATERIAL in result["materials"]
    session.close()


def test_scope_narrows_where_the_comparison_starts(with_history, two_versions,
                                                   chain_users) -> None:
    """A regional manager sees their region's real movements, not a narrowed
    country total labelled "Country"."""
    session, plan = _open(with_history, two_versions)
    full = _compare(session, plan, two_versions, chain_users[Role.MANAGEMENT])
    scoped = _compare(session, plan, two_versions,
                      chain_users[Role.REGIONAL_MANAGER])

    assert len(scoped["rows"]) < len(full["rows"])
    assert scoped["rows"][0]["node_code"] == "REG001"
    assert scoped["rows"][0]["depth"] == 0
    assert not any(row["level"] == TargetLevel.COMPANY
                   for row in scoped["rows"])
    session.close()


def test_a_reader_whose_scope_names_nothing_gets_an_explanation(
        with_history, two_versions) -> None:
    """An empty view with a reason, never an error and never somebody else's data."""
    session, plan = _open(with_history, two_versions)
    stranger = UserContext(user_id=99, username="nobody",
                           role=Role.REGIONAL_MANAGER,
                           data_scope={"region_code": ["REG-NONE"]})
    result = _compare(session, plan, two_versions, stranger)
    assert result["rows"] == []
    assert any("part of the business you hold" in note
               for note in result["notes"])
    session.close()


def test_the_version_list_marks_one_that_cannot_be_compared(with_history,
                                                            two_versions,
                                                            users) -> None:
    """Listed and marked rather than hidden — a missing entry reads as a fault."""
    session, plan = _open(with_history, two_versions)
    third = plan_service.create_version(
        session, users["ceo"], plan_id=plan.plan_id, reason="Not run yet.")
    session.commit()

    options = compare.options(session, plan.plan_id)
    by_id = {row["version_id"]: row for row in options}
    assert by_id[two_versions["head"]]["has_allocation"] is True
    assert by_id[third.version_id]["has_allocation"] is False
    session.close()


def test_a_percentage_against_a_zero_base_is_absent(with_history,
                                                    two_versions,
                                                    users) -> None:
    """Undefined rather than infinite — the rule every ratio here follows."""
    session, plan = _open(with_history, two_versions)
    session.execute(
        TargetAllocation.__table__.update()
        .where(TargetAllocation.version_id == two_versions["base"],
               TargetAllocation.node_code == CUSTOMERS[0])
        .values(current_volume=Decimal("0")))
    session.commit()

    result = _compare(session, plan, two_versions, users["ceo"])
    row = _by_node(result, TargetLevel.CUSTOMER, CUSTOMERS[0])
    assert row["base_volume"] == pytest.approx(0.0)
    assert row["change_percent"] is None
    assert row["change"] is not None
    session.close()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_the_stages_partition_the_plans(with_history, two_versions,
                                        users) -> None:
    """Four stages of one journey, so a plan is in exactly one of them."""
    session, plan = _open(with_history, two_versions)
    board = dashboard.summary(session, users["ceo"])
    assert sum(board["stages"].values()) == len(board["plans"])
    assert set(board["stages"]) == set(dashboard.STAGES)
    session.close()


def test_a_plan_with_no_country_target_reads_absent_not_zero(with_history,
                                                             users) -> None:
    """Nobody having typed a target is not somebody having typed nothing."""
    with Session(with_history) as session:
        plan_service.create_plan(session, users["ceo"], scope=plan_service.PlanScope(
            financial_year="FY 2028-29", target_period="FY",
            company_code="C001", bu_code="BU001", sales_line_code="SL001"))
        session.commit()
        board = dashboard.summary(session, users["ceo"])
    entry = next(row for row in board["plans"]
                 if row["plan"]["financial_year"] == "FY 2028-29")
    assert entry["country_volume"] is None
    assert entry["allocated_percent"] is None


def test_the_allocated_total_comes_from_the_root(with_history, two_versions,
                                                 users) -> None:
    """Not from summing every row, which would count the target eight times."""
    session, plan = _open(with_history, two_versions)
    board = dashboard.summary(session, users["ceo"])
    entry = next(row for row in board["plans"]
                 if row["plan"]["plan_id"] == plan.plan_id)
    assert entry["allocated_volume"] == pytest.approx(150000.0)
    assert entry["allocated_percent"] == pytest.approx(100.0)
    session.close()


def test_an_allocated_but_unsubmitted_plan_needs_attention(with_history,
                                                           two_versions,
                                                           users) -> None:
    """And the reason travels with it, so the count is one a reader can act on."""
    session, plan = _open(with_history, two_versions)
    for step in (TargetStatus.ALLOCATION_IN_PROGRESS, TargetStatus.ALLOCATED):
        plan_service.set_version_status(session, users["ceo"],
                                        version_id=two_versions["head"],
                                        new_status=step)
    session.commit()

    board = dashboard.summary(session, users["ceo"])
    entry = next(row for row in board["attention"]
                 if row["plan"]["plan_id"] == plan.plan_id)
    assert "not_submitted" in entry["attention"]
    assert board["attention_reasons"]["not_submitted"]
    session.close()


def test_an_approved_but_unlocked_plan_needs_attention(with_history,
                                                       two_versions, users,
                                                       chain_users) -> None:
    """The agreed figures are not yet the target anybody reports against."""
    session, plan = _open(with_history, two_versions)
    version = plan_service.get_version(session, two_versions["head"])
    for step in (TargetStatus.ALLOCATION_IN_PROGRESS, TargetStatus.ALLOCATED):
        plan_service.set_version_status(session, users["ceo"],
                                        version_id=version.version_id,
                                        new_status=step)
    approvals.submit(session, users["ceo"], plan=plan, version=version)
    session.commit()
    for role in (Role.UNIT_MANAGER, Role.AREA_MANAGER, Role.REGIONAL_MANAGER,
                 Role.ZONE_MANAGER, Role.BUSINESS_UNIT_HEAD, Role.MANAGEMENT):
        approvals.approve(session, chain_users[role], plan=plan, version=version)
        session.commit()

    board = dashboard.summary(session, users["ceo"])
    entry = next(row for row in board["attention"]
                 if row["plan"]["plan_id"] == plan.plan_id)
    assert "approved_not_locked" in entry["attention"]
    session.close()


def test_a_failed_run_is_reported_on_the_plan(with_history, two_versions,
                                              users) -> None:
    session, plan = _open(with_history, two_versions)
    session.add(TargetAllocationJob(
        job_uuid="job-failed-1", plan_id=plan.plan_id,
        version_id=two_versions["head"], status=AllocationJobStatus.FAILED,
        requested_by="ceo"))
    session.commit()

    board = dashboard.summary(session, users["ceo"])
    entry = next(row for row in board["plans"]
                 if row["plan"]["plan_id"] == plan.plan_id)
    assert "last_run_failed" in entry["attention"]
    session.close()


def test_a_settled_plan_needs_no_attention(with_history, two_versions,
                                           users) -> None:
    """A count nobody can act on is worse than no count at all."""
    session, plan = _open(with_history, two_versions)
    board = dashboard.summary(session, users["ceo"])
    entry = next(row for row in board["plans"]
                 if row["plan"]["plan_id"] == plan.plan_id)
    assert entry["attention"] == []
    session.close()


def test_the_dashboard_reports_the_readers_own_queue(with_history,
                                                     two_versions,
                                                     chain_users) -> None:
    """Delegated to the approvals module rather than recomputed here."""
    session, plan = _open(with_history, two_versions)
    board = dashboard.summary(session, chain_users[Role.ADMIN])
    assert board["my_queue"]["versions"] == 0
    assert board["my_queue"]["revisions"] == 0
    assert any("not part of the approval chain" in note
               for note in board["my_queue"]["notes"])
    session.close()


def test_the_year_filter_narrows_the_plans(with_history, two_versions,
                                           users) -> None:
    session, plan = _open(with_history, two_versions)
    plan_service.create_plan(session, users["ceo"], scope=plan_service.PlanScope(
        financial_year="FY 2028-29", target_period="FY", company_code="C001",
        bu_code="BU001", sales_line_code="SL001"))
    session.commit()

    every = dashboard.summary(session, users["ceo"])
    narrowed = dashboard.summary(session, users["ceo"],
                                 financial_year="FY 2028-29")
    assert len(narrowed["plans"]) < len(every["plans"])
    assert all(row["plan"]["financial_year"] == "FY 2028-29"
               for row in narrowed["plans"])
    session.close()


def test_the_dashboard_carries_recent_activity(with_history, two_versions,
                                               users) -> None:
    """Read from the trail, not from a second record of what happened."""
    session, plan = _open(with_history, two_versions)
    board = dashboard.summary(session, users["ceo"])
    assert board["recent"]
    assert all("action" in entry for entry in board["recent"])
    session.close()


def test_every_status_belongs_to_exactly_one_stage(with_history) -> None:
    """The four counts add up to the plans behind them, or they mean nothing.

    A status matching no group would drop its plan out of the strip while
    leaving it in the list below — a headline that quietly fails to add up.
    """
    grouped = [status for group in dashboard.STAGES.values() for status in group]
    assert len(grouped) == len(set(grouped)), "a status is in two stages"
    assert set(grouped) == set(TargetStatus.ALL)


def test_the_stage_counts_cover_every_plan_not_just_the_page(
        with_history, two_versions, users) -> None:
    """The strip counts every plan; the list beneath it is a page of them.

    Counting only the page would make the headline shrink as the list grew,
    which is the opposite of what a headline is for.
    """
    session, plan = _open(with_history, two_versions)
    for year in ("FY 2028-29", "FY 2029-30", "FY 2030-31"):
        plan_service.create_plan(session, users["ceo"],
                                 scope=plan_service.PlanScope(
            financial_year=year, target_period="FY", company_code="C001",
            bu_code="BU001", sales_line_code="SL001"))
    session.commit()

    board = dashboard.summary(session, users["ceo"], limit=2)
    assert len(board["plans"]) == 2
    assert sum(board["stages"].values()) == board["plan_count"]
    session.close()
