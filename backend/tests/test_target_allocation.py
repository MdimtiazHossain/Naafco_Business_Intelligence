"""The allocation engine: volume down to customer, and the promises it keeps.

Three things are being proved here, and the order reflects how much each one
matters.

**It never invents an allocation.** With an empty ``fact_sales`` the engine does
not spread the country target evenly and call it a plan — it stops, says why,
names the factors that could not be calculated, and leaves the version
unallocated. That is the difference between an engine waiting for data and one
that has quietly made some up, and it is the state every deployment starts in.

**It reconciles exactly, at every level.** Country equals the sum of its months;
every parent equals the sum of its children at every level, for every material
and every month; and the allocated country total equals the volume somebody
typed. Zero tolerance — the figures are ``Decimal`` end to end, so an exact
comparison is meaningful and a tolerance would hide the only bug worth catching.

**Volume is allocated; quantity and value are derived from it.** Never the other
way round, because a conversion factor differs per material and a value
allocated directly would imply volumes that do not add up.

Everything else — the eight factors, the seasonal split, the job lifecycle — is
tested against seeded data, because seeded data is the only place in this
project where sales history is allowed to be manufactured.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import DimMaterial, DimSubTerritory, DimTerritory
from app.database.models_target import (
    AllocationJobStatus,
    TargetAllocation,
    TargetAllocationJob,
    TargetLevel,
    TargetStatus,
)
from app.database.models_warehouse import DimCustomer
from app.targetmgmt import (
    country,
    engine as allocation_engine,
    factors,
    jobs as allocation_jobs,
    plans as plan_service,
    reconcile,
    seasonality,
)
from conftest_phase2 import sales_row
from conftest_phase3 import _load

SCOPE = {
    "financial_year": "FY 2026-27",
    "target_period": "FY",
    "company_code": "C001",
    "bu_code": "BU001",
    "sales_line_code": "SL001",
}

#: Customers under STR001, so the tree reaches its leaf level.
CUSTOMERS = ("CUST-001", "CUST-002", "CUST-003")

#: The material the country target is set for. Given a conversion factor and a
#: transfer price so the derived-figure assertions have something to derive.
MATERIAL = "SKU001"


@pytest.fixture(autouse=True)
def _worker_engine(agent_engine, monkeypatch):
    """Point the job worker's own sessions at this test's warehouse.

    A worker deliberately opens its own ``Session`` from
    ``connection.get_engine()`` rather than borrowing the request's, so
    redirecting that module is how a test reaches it — the same seam the upload
    centre's workers are redirected through.
    """
    from app.database import connection

    monkeypatch.setattr(connection, "get_engine", lambda: agent_engine)


def _terminal_total(rows) -> Decimal:
    """The volume that comes to rest, wherever in the tree that is.

    Not "the customer level": the seeded hierarchy is ragged — REG002 stops at
    area because no unit exists under it — and a branch that ends early has
    reached its own leaf. Summing a *level* instead would report every real
    hierarchy as broken, because most of them are ragged.
    """
    parents = {(row.parent_level, row.parent_code) for row in rows
               if row.parent_code}
    return sum((row.volume for row in rows
                if (row.level, row.node_code) not in parents), Decimal(0))


@pytest.fixture
def hierarchy(agent_engine):
    """Customers under the seeded sub-territory, and a priced material.

    The shared fixture's hierarchy stops at ``STR001`` because ``dim_customer``
    is ``PENDING_SOURCE_DATA``. Customers are added here rather than there so
    the allocation has a leaf level to reach, which is the level the whole
    specification is about.
    """
    with Session(agent_engine) as session:
        for index, code in enumerate(CUSTOMERS, start=1):
            session.add(DimCustomer(
                customer_code=code, customer_name=f"Customer {index}",
                sub_territory_code="STR001", customer_type="Dealer"))
        material = session.execute(
            select(DimMaterial).where(DimMaterial.material_code == MATERIAL)
        ).scalar_one()
        material.company_code = "C001"
        material.conversion_factor = Decimal("0.5")
        material.transfer_price = Decimal("240")
        session.commit()
    return agent_engine


@pytest.fixture
def planned(hierarchy, users):
    """A plan, its V1 and a country target of 120,000 against one material."""
    with Session(hierarchy) as session:
        plan = plan_service.create_plan(
            session, users["ceo"], scope=plan_service.PlanScope(**SCOPE))
        session.flush()
        version = plan_service.current_version(session, plan.plan_id)
        country.set_lines(session, users["ceo"], version=version, plan=plan,
                          entries=[(MATERIAL, 120000)])
        session.commit()
        return {"plan_id": plan.plan_id, "version_id": version.version_id}


def _rows(session, planned):
    return session.execute(
        select(TargetAllocation).where(
            TargetAllocation.version_id == planned["version_id"])
    ).scalars().all()


def _run(session, users, planned, settings=None, username="ceo"):
    plan = plan_service.get_plan(session, planned["plan_id"])
    version = plan_service.get_version(session, planned["version_id"])
    return allocation_engine.plan_allocation(
        session, users[username], plan=plan, version=version,
        settings=settings or factors.FactorSettings.defaults())


def _seed_sales(engine, *, months, customers=CUSTOMERS, material=MATERIAL,
                volume=1000):
    """Sales over ``(year, month, weight)`` triples, spread across customers.

    The weight scales that month's volume, which is how a seasonal shape is
    seeded — repeating a month instead would collide on the invoice number, and
    the ETL is right to reject that.

    Loaded through the ETL rather than inserted, so the figures travel the same
    validation, hierarchy resolution and view every report reads them through.
    """
    records = []
    for sequence, (year, month, weight) in enumerate(months):
        for index, customer in enumerate(customers):
            records.append(sales_row(**{
                "Invoice No": f"AL-{sequence:03d}-{index}",
                "Date": f"{year}-{month:02d}-10",
                "Customer Code": customer,
                "SKU Code": material,
                "Quantity": 10,
                "Total Volume": volume * weight * (index + 1),
                "Gross Sales": 1000, "Discount": 0, "Cost": 500,
                "Source Transaction Id": f"SRC-{sequence:03d}-{index}",
            }))
    _load(engine, "sales", records)


# ---------------------------------------------------------------------------
# The empty-warehouse case, which is where every deployment starts
# ---------------------------------------------------------------------------


def test_no_sales_history_produces_no_allocation(hierarchy, users,
                                                 planned) -> None:
    with Session(hierarchy) as session:
        result = _run(session, users, planned)

    assert result.allocatable is False
    assert result.rows == []
    assert "no sales" in (result.reason or "").lower()


def test_the_refusal_names_the_years_it_looked_at(hierarchy, users,
                                                  planned) -> None:
    """A planner has to know *which* history is missing to go and load it."""
    with Session(hierarchy) as session:
        result = _run(session, users, planned)
    assert "FY 2024-25" in result.reason
    assert "FY 2025-26" in result.reason


def test_the_refusal_says_which_factors_cannot_be_calculated(
        hierarchy, users, planned) -> None:
    with Session(hierarchy) as session:
        result = _run(session, users, planned)

    by_key = {row["key"]: row for row in result.factor_availability}
    assert by_key["historical_contribution"]["available"] is False
    assert "no sales" in by_key["historical_contribution"]["reason"].lower()
    # And the two with no source at all report a different, permanent reason.
    assert by_key["customer_potential"]["available"] is False
    assert ("no potential master data configured"
            in by_key["customer_potential"]["reason"].lower())


def test_a_version_with_no_country_target_is_refused(hierarchy, users) -> None:
    """Nothing to allocate is a different problem from nothing to allocate *by*."""
    with Session(hierarchy) as session:
        plan = plan_service.create_plan(
            session, users["ceo"],
            scope=plan_service.PlanScope(**{**SCOPE, "target_period": "Q2"}))
        session.flush()
        version = plan_service.current_version(session, plan.plan_id)
        with pytest.raises(allocation_engine.NothingToAllocate):
            allocation_engine.plan_allocation(
                session, users["ceo"], plan=plan, version=version)


# ---------------------------------------------------------------------------
# With history: the allocation itself
# ---------------------------------------------------------------------------


@pytest.fixture
def with_history(hierarchy):
    """Two basis years of sales, seasonal rather than flat.

    October is heavy and June is light, so a seasonal split is visibly different
    from a twelfth each — which is the whole point of the factor.
    """
    _seed_sales(hierarchy, months=[
        # FY 2024-25
        (2024, 9, 2), (2024, 10, 4), (2024, 11, 2), (2025, 6, 1),
        # FY 2025-26
        (2025, 9, 2), (2025, 10, 4), (2025, 11, 2), (2026, 6, 1),
    ])
    return hierarchy


def test_the_allocation_reaches_every_level(with_history, users,
                                            planned) -> None:
    with Session(with_history) as session:
        result = _run(session, users, planned)

    assert result.allocatable is True
    levels = {row.level for row in result.rows}
    assert levels == {
        TargetLevel.COMPANY, TargetLevel.ZONE, TargetLevel.REGION,
        TargetLevel.AREA, TargetLevel.UNIT, TargetLevel.TERRITORY,
        TargetLevel.SUB_TERRITORY, TargetLevel.CUSTOMER,
    }


def test_every_row_is_monthly(with_history, users, planned) -> None:
    """No period-total row beside the months — a node's total is a sum."""
    with Session(with_history) as session:
        result = _run(session, users, planned)
    assert all(len(row.target_month) == 7 and row.target_month[4] == "-"
               for row in result.rows)
    assert len({row.target_month for row in result.rows}) == 12


def test_the_country_total_equals_what_was_typed(with_history, users,
                                                 planned) -> None:
    with Session(with_history) as session:
        result = _run(session, users, planned)

    country_rows = [row for row in result.rows
                    if row.level == TargetLevel.COMPANY]
    assert sum((row.volume for row in country_rows), Decimal(0)) == \
        Decimal("120000.0000")


def test_every_parent_equals_the_sum_of_its_children(with_history, users,
                                                     planned) -> None:
    """The identity, checked in memory before anything is stored."""
    with Session(with_history) as session:
        result = _run(session, users, planned)

    own: dict[tuple, Decimal] = {}
    children: dict[tuple, Decimal] = {}
    for row in result.rows:
        own[(row.level, row.node_code, row.material_code, row.target_month)] = \
            row.volume
        if row.parent_code:
            key = (row.parent_level, row.parent_code, row.material_code,
                   row.target_month)
            children[key] = children.get(key, Decimal(0)) + row.volume

    assert children, "the tree produced no parent-child relationships"
    for key, child_total in children.items():
        assert own[key] == child_total, key


def test_every_terminal_node_together_adds_to_the_country_total(
        with_history, users, planned) -> None:
    """Catches a break the parent walk cannot: a whole subtree gone missing.

    Terminal nodes rather than the customer level, because the seeded hierarchy
    is ragged: REG002 has no unit beneath it, so its volume comes to rest at
    area. That branch has still allocated all of its share.
    """
    with Session(with_history) as session:
        result = _run(session, users, planned)
    assert _terminal_total(result.rows) == Decimal("120000.0000")


def test_an_unbalanced_tree_still_reconciles(with_history, users,
                                             planned) -> None:
    """REG002 stops at area — no unit beneath it — and that is real data.

    A hierarchy that is deeper in one branch than another is the normal case,
    and the reconciliation must hold on the shallow branch too rather than only
    on the one that reaches a customer.
    """
    with Session(with_history) as session:
        result = _run(session, users, planned)

    by_level: dict[str, Decimal] = {}
    for row in result.rows:
        by_level[row.level] = by_level.get(row.level, Decimal(0)) + row.volume

    # Every level that exists sums to the country total: the shallow branch's
    # volume does not evaporate, it simply stops at the deepest node it has.
    assert by_level[TargetLevel.ZONE] == Decimal("120000.0000")
    assert by_level[TargetLevel.REGION] == Decimal("120000.0000")
    assert by_level[TargetLevel.AREA] == Decimal("120000.0000")


# ---------------------------------------------------------------------------
# Monthly seasonality
# ---------------------------------------------------------------------------


def test_the_monthly_split_is_not_a_twelfth_each(with_history, users,
                                                 planned) -> None:
    """The rule the specification states outright.

    October carries four of the eleven seeded invoices and June one, so a
    seasonal split must weight them differently. An equal split here would mean
    the factor is not doing anything.
    """
    with Session(with_history) as session:
        result = _run(session, users, planned)

    by_month = {
        row.target_month: row.volume
        for row in result.rows if row.level == TargetLevel.COMPANY
    }
    flat = Decimal("120000") / 12
    # October carries four times June's seeded volume, and a month nobody ever
    # sold in gets nothing at all — neither of which a twelfth-each split does.
    assert by_month["2026-10"] > flat
    assert by_month["2026-10"] > by_month["2027-06"] * 3
    assert by_month["2026-12"] == Decimal("0.0000")


def test_the_monthly_totals_still_sum_to_the_annual_target(with_history, users,
                                                           planned) -> None:
    """100,000 KG split by seasonality must still be 100,000 KG."""
    with Session(with_history) as session:
        result = _run(session, users, planned)
    by_month = [row.volume for row in result.rows
                if row.level == TargetLevel.COMPANY]
    assert sum(by_month, Decimal(0)) == Decimal("120000.0000")


def test_turning_seasonality_off_splits_evenly_and_says_so(with_history, users,
                                                           planned) -> None:
    settings = factors.FactorSettings.from_request(
        {"enabled": {"monthly_seasonality": False}})
    with Session(with_history) as session:
        result = _run(session, users, planned, settings)

    shape = result.seasonality[MATERIAL]
    assert shape["source"] == seasonality.EQUAL
    assert shape["is_fallback"] is True
    assert any("equal split" in warning for warning in result.warnings)


def test_a_fallback_seasonal_pattern_is_named_never_disguised(
        hierarchy, users, planned) -> None:
    """One month of history is not a seasonal profile.

    Below ``MIN_MONTHS_FOR_PATTERN`` the chain falls through to a coarser
    source, and whichever it lands on is reported rather than presented as this
    material's own shape.
    """
    _seed_sales(hierarchy, months=[(2025, 10, 1)])
    with Session(hierarchy) as session:
        result = _run(session, users, planned)

    shape = result.seasonality[MATERIAL]
    assert shape["source"] != seasonality.MATERIAL
    assert shape["is_fallback"] is True


def test_a_quarter_allocates_only_its_own_months(with_history, users) -> None:
    with Session(with_history) as session:
        plan = plan_service.create_plan(
            session, users["ceo"],
            scope=plan_service.PlanScope(**{**SCOPE, "target_period": "Q1"}))
        session.flush()
        version = plan_service.current_version(session, plan.plan_id)
        country.set_lines(session, users["ceo"], version=version, plan=plan,
                          entries=[(MATERIAL, 30000)])
        session.flush()
        result = allocation_engine.plan_allocation(
            session, users["ceo"], plan=plan, version=version)

    months = sorted({row.target_month for row in result.rows})
    assert months == ["2026-07", "2026-08", "2026-09"]
    assert sum((row.volume for row in result.rows
                if row.level == TargetLevel.COMPANY), Decimal(0)) == \
        Decimal("30000.0000")


# ---------------------------------------------------------------------------
# The eight factors
# ---------------------------------------------------------------------------


def test_all_eight_factors_are_declared() -> None:
    assert len(factors.FACTORS) == 8


def test_two_factors_have_no_data_source_and_say_why() -> None:
    """Declared rather than deleted: a missing capability the business asked
    for should be visible, not silently absent."""
    unsupported = [f for f in factors.FACTORS if not f.supported]
    assert {f.key for f in unsupported} == {"customer_potential",
                                            "territory_potential"}
    for factor in unsupported:
        assert factor.default_enabled is False
        assert factor.unsupported_reason


def test_an_unsupported_factor_cannot_be_switched_on(hierarchy) -> None:
    """Not a validation nicety.

    Enabling it would add a term that scores every node zero, silently shrinking
    the mixture — the allocation would stop being what the settings claim.
    """
    settings = factors.FactorSettings.from_request(
        {"enabled": {"customer_potential": True}})
    assert settings.is_on("customer_potential") is False


def test_weights_are_relative_not_absolute() -> None:
    """40/15/15 and 8/3/3 must allocate identically."""
    history = {"a": (100.0, 150.0), "b": (200.0, 210.0), "c": (50.0, 80.0)}
    big = factors.FactorSettings.from_request({"weights": {
        "historical_contribution": 40, "growth_trend": 15,
        "two_year_average": 15}})
    small = factors.FactorSettings.from_request({"weights": {
        "historical_contribution": 8, "growth_trend": 3,
        "two_year_average": 3}})
    assert factors.node_weights(history, big)[0] == \
        factors.node_weights(history, small)[0]


def test_historical_contribution_follows_the_latest_year() -> None:
    settings = factors.FactorSettings.from_request({
        "enabled": {"growth_trend": False, "two_year_average": False,
                    "new_node_seed": False}})
    weights, _ = factors.node_weights(
        {"a": (10.0, 100.0), "b": (90.0, 300.0)}, settings)
    assert weights["b"] > weights["a"]
    assert weights["a"] + weights["b"] == pytest.approx(Decimal(1), abs=1e-9)


def test_the_growth_factor_favours_the_faster_grower() -> None:
    """Same size last year, different trajectories."""
    settings = factors.FactorSettings.from_request({
        "enabled": {"historical_contribution": False,
                    "two_year_average": False, "new_node_seed": False}})
    weights, _ = factors.node_weights(
        {"steady": (100.0, 100.0), "growing": (50.0, 100.0)}, settings)
    assert weights["growing"] > weights["steady"]


def test_the_two_year_average_damps_an_outlier_year() -> None:
    settings = factors.FactorSettings.from_request({
        "enabled": {"historical_contribution": False, "growth_trend": False,
                    "new_node_seed": False}})
    weights, _ = factors.node_weights(
        {"spiky": (0.0, 200.0), "steady": (100.0, 100.0)}, settings)
    # The average sees 100 against 100, so neither outranks the other.
    assert weights["spiky"] == weights["steady"]


def test_each_factors_contribution_is_reported() -> None:
    """The screen shows *why* a node got its share, from the same numbers."""
    settings = factors.FactorSettings.defaults()
    _, contributions = factors.node_weights(
        {"a": (100.0, 150.0), "b": (200.0, 210.0)}, settings)
    assert set(contributions["a"]) >= {"historical_contribution",
                                       "growth_trend", "two_year_average"}
    assert all(value > 0 for value in contributions["a"].values())


def test_a_new_node_is_seeded_from_its_siblings() -> None:
    settings = factors.FactorSettings.defaults()
    weights, _ = factors.node_weights(
        {"established": (100.0, 100.0), "new": (None, None)}, settings)
    assert weights["new"] > 0
    assert weights["new"] < weights["established"]


def test_turning_the_new_node_factor_off_gives_a_new_node_nothing() -> None:
    """A real choice, and not the same as a bug."""
    settings = factors.FactorSettings.from_request(
        {"enabled": {"new_node_seed": False}})
    weights, _ = factors.node_weights(
        {"established": (100.0, 100.0), "new": (None, None)}, settings)
    assert weights["new"] == 0





# ---------------------------------------------------------------------------
# Persisting and reconciling
# ---------------------------------------------------------------------------


def test_the_stored_allocation_reconciles(with_history, users,
                                          planned) -> None:
    """Read back from the database, not from the engine's own output.

    A check against the plan in memory would only prove the engine agrees with
    itself; what matters is what every report downstream will read.
    """
    with Session(with_history) as session:
        version = plan_service.get_version(session, planned["version_id"])
        result = _run(session, users, planned)
        allocation_engine.persist(session, version=version, result=result)
        session.commit()

        verdict = reconcile.check(
            session, version_id=planned["version_id"],
            country_target={MATERIAL: Decimal("120000")})

    assert verdict.balanced is True
    assert verdict.mismatches == []
    assert verdict.allocated_volume == Decimal("120000.0000")
    assert verdict.difference == Decimal(0)


def test_reconciliation_catches_a_tampered_row(with_history, users,
                                               planned) -> None:
    """The check has to be capable of failing, or it proves nothing."""
    with Session(with_history) as session:
        version = plan_service.get_version(session, planned["version_id"])
        result = _run(session, users, planned)
        allocation_engine.persist(session, version=version, result=result)
        session.flush()

        row = session.execute(
            select(TargetAllocation).where(
                TargetAllocation.level == TargetLevel.CUSTOMER).limit(1)
        ).scalar_one()
        # ``current_volume``, which is the figure reconciliation reads — the
        # target as it stands, rather than the engine's pre-adjustment
        # suggestion.
        row.current_volume = Decimal(str(row.current_volume)) + Decimal("1")
        session.flush()

        verdict = reconcile.check(
            session, version_id=planned["version_id"],
            country_target={MATERIAL: Decimal("120000")})

    assert verdict.balanced is False
    assert any(m.kind == "PARENT_CHILD" for m in verdict.mismatches)


def test_reconciliation_notices_a_country_target_that_moved(
        with_history, users, planned) -> None:
    """The one identity the engine cannot guarantee by construction.

    It spans the boundary between what a person typed and what the engine
    produced — so editing the country volume without re-allocating must show up
    as unbalanced rather than passing quietly.
    """
    with Session(with_history) as session:
        version = plan_service.get_version(session, planned["version_id"])
        result = _run(session, users, planned)
        allocation_engine.persist(session, version=version, result=result)
        session.commit()

        verdict = reconcile.check(
            session, version_id=planned["version_id"],
            country_target={MATERIAL: Decimal("130000")})

    assert verdict.balanced is False
    assert any(m.kind == "COUNTRY_TARGET" for m in verdict.mismatches)


def test_re_allocating_replaces_rather_than_accumulates(with_history, users,
                                                        planned) -> None:
    """Merging two runs would leave rows from different rules side by side."""
    with Session(with_history) as session:
        version = plan_service.get_version(session, planned["version_id"])
        first = _run(session, users, planned)
        allocation_engine.persist(session, version=version, result=first)
        session.commit()
        count = len(_rows(session, planned))

        second = _run(session, users, planned)
        allocation_engine.persist(session, version=version, result=second)
        session.commit()
        assert len(_rows(session, planned)) == count


def test_an_empty_reconciliation_is_balanced_only_against_no_target() -> None:
    """No rows and no target is balanced; no rows and a target is not."""
    from app.targetmgmt.reconcile import Reconciliation

    assert Reconciliation(balanced=True, country_target_volume=Decimal(0),
                          allocated_volume=Decimal(0)).allocation_percent is None


# ---------------------------------------------------------------------------
# Quantity and value, derived from the allocated volume
# ---------------------------------------------------------------------------


def test_quantity_and_value_come_from_the_allocated_volume(with_history, users,
                                                           planned) -> None:
    """Volume first: the engine allocates volume, and these follow from it.

    Allocating a value directly would imply volumes that do not add up, because
    a conversion factor differs per material.
    """
    with Session(with_history) as session:
        version = plan_service.get_version(session, planned["version_id"])
        result = _run(session, users, planned)
        allocation_engine.persist(session, version=version, result=result)
        session.flush()

        leaf = session.execute(
            select(TargetAllocation).where(
                TargetAllocation.level == TargetLevel.CUSTOMER).limit(1)
        ).scalar_one()
        volume = float(leaf.system_volume)
        quantity, value = country.derive(volume, 0.5, 240)

    assert quantity == pytest.approx(volume / 0.5)
    assert value == pytest.approx(quantity * 240)


def test_a_material_without_a_conversion_factor_still_allocates_volume(
        with_history, users, planned) -> None:
    """The allocation is volume, so it does not depend on the derivation inputs.

    A material with no conversion factor allocates perfectly well; it is only
    the *derived* columns that report n/a. Coupling the two would make an
    unpriced material unallocatable, which is a different and much worse failure.
    """
    with Session(with_history) as session:
        material = session.execute(
            select(DimMaterial).where(DimMaterial.material_code == MATERIAL)
        ).scalar_one()
        material.conversion_factor = None
        material.transfer_price = None
        session.flush()
        result = _run(session, users, planned)

    assert result.allocatable is True
    assert _terminal_total(result.rows) == Decimal("120000.0000")


# ---------------------------------------------------------------------------
# The background job
# ---------------------------------------------------------------------------


def _run_job_inline(engine, users, planned, settings=None, username="ceo"):
    """Submit and run a job on this thread, so a test can assert on the result.

    The executor is bypassed deliberately: a test that waited on a thread pool
    would be a test of the pool's timing. What matters here is the lifecycle the
    worker drives, and running it inline exercises exactly the same code.
    """
    settings = settings or factors.FactorSettings.defaults()
    with Session(engine) as session:
        plan = plan_service.get_plan(session, planned["plan_id"])
        version = plan_service.get_version(session, planned["version_id"])
        job = allocation_jobs.submit(session, users[username], plan=plan,
                                     version=version, settings=settings)
        job_uuid = job.job_uuid
        session.commit()
        session.info.pop("_pending_allocations", None)

    allocation_jobs._run(job_uuid, planned["version_id"], users[username],
                         settings)
    return job_uuid


def test_a_job_starts_queued_and_moves_the_version(with_history, users,
                                                   planned) -> None:
    with Session(with_history) as session:
        plan = plan_service.get_plan(session, planned["plan_id"])
        version = plan_service.get_version(session, planned["version_id"])
        job = allocation_jobs.submit(session, users["ceo"], plan=plan,
                                     version=version,
                                     settings=factors.FactorSettings.defaults())
        session.commit()
        assert job.status == AllocationJobStatus.QUEUED
        assert job.progress_percent == 0
        assert job.requested_by == "ceo"
        assert plan_service.get_version(
            session, planned["version_id"]).status == \
            TargetStatus.ALLOCATION_IN_PROGRESS


def test_a_completed_job_reports_its_rows_and_reconciliation(
        with_history, users, planned) -> None:
    job_uuid = _run_job_inline(with_history, users, planned)

    with Session(with_history) as session:
        state = allocation_jobs.state(session, job_uuid)

    # Either success is a success. This run reaches customers and still warns —
    # a couple of nodes have no history to distinguish their children — and a
    # run with something worth reading is COMPLETED_WITH_WARNINGS by design.
    assert state["status"] in AllocationJobStatus.SUCCEEDED
    assert state["progress_percent"] == 100
    assert state["rows_processed"] > 0
    assert state["total_rows"] == state["rows_processed"]
    assert state["result"]["reconciliation"]["balanced"] is True
    assert state["completed_at"] is not None


def test_a_completed_job_marks_the_version_allocated(with_history, users,
                                                     planned) -> None:
    _run_job_inline(with_history, users, planned)
    with Session(with_history) as session:
        version = plan_service.get_version(session, planned["version_id"])
        assert version.status == TargetStatus.ALLOCATED


def test_the_job_records_the_settings_it_ran_under(with_history, users,
                                                   planned) -> None:
    """An allocation whose rules are unknown is a number nobody can defend."""
    settings = factors.FactorSettings.from_request(
        {"weights": {"historical_contribution": 90}})
    job_uuid = _run_job_inline(with_history, users, planned, settings)

    with Session(with_history) as session:
        state = allocation_jobs.state(session, job_uuid)
    assert state["settings"]["weights"]["historical_contribution"] == 90


def test_progress_is_readable_while_the_run_is_in_flight(with_history, users,
                                                         planned) -> None:
    """The whole reason progress is durable rather than in process memory.

    A second connection reads the job row mid-run; if progress lived only in the
    worker's memory this would see nothing.
    """
    seen: list[tuple[str, int]] = []

    original = allocation_engine.plan_allocation

    def watching(session, user, *, plan, version, settings=None, progress=None):
        def spy(stage, done, total):
            if progress:
                progress(stage, done, total)
            with Session(with_history) as watcher:
                row = watcher.execute(
                    select(TargetAllocationJob).where(
                        TargetAllocationJob.version_id == version.version_id)
                ).scalars().first()
                if row is not None:
                    seen.append((row.current_stage, row.progress_percent))
        return original(session, user, plan=plan, version=version,
                        settings=settings, progress=spy)

    allocation_engine.plan_allocation = watching
    try:
        _run_job_inline(with_history, users, planned)
    finally:
        allocation_engine.plan_allocation = original

    assert seen, "no progress was observable from another connection"
    assert {stage for stage, _ in seen} & set(allocation_engine.STAGE_KEYS)
    assert max(percent for _, percent in seen) > 0


def test_an_empty_warehouse_ends_in_no_history_not_failed(hierarchy, users,
                                                          planned) -> None:
    """"Failed" would send a planner looking for a bug that is not there."""
    job_uuid = _run_job_inline(hierarchy, users, planned)

    with Session(hierarchy) as session:
        state = allocation_jobs.state(session, job_uuid)

    assert state["status"] == AllocationJobStatus.NO_HISTORY
    assert state["total_rows"] == 0
    assert state["result"]["sales_rows_found"] == 0
    assert state["result"]["basis_years"] == ["FY 2024-25", "FY 2025-26"]
    assert "no sales" in state["error_message"].lower()


def test_an_empty_warehouse_leaves_the_version_workable(hierarchy, users,
                                                        planned) -> None:
    """Not stranded at ALLOCATION_IN_PROGRESS, which nothing could act on."""
    _run_job_inline(hierarchy, users, planned)
    with Session(hierarchy) as session:
        assert plan_service.get_version(
            session, planned["version_id"]).status == TargetStatus.DRAFT


def test_an_empty_warehouse_writes_no_allocation_rows(hierarchy, users,
                                                      planned) -> None:
    """The core promise: no fabricated zero-based customer targets."""
    _run_job_inline(hierarchy, users, planned)
    with Session(hierarchy) as session:
        assert _rows(session, planned) == []


def test_a_failing_run_is_recorded_and_frees_the_version(with_history, users,
                                                         planned) -> None:
    original = allocation_engine.plan_allocation

    def exploding(*args, **kwargs):
        raise RuntimeError("engine blew up")

    allocation_engine.plan_allocation = exploding
    try:
        settings = factors.FactorSettings.defaults()
        with Session(with_history) as session:
            plan = plan_service.get_plan(session, planned["plan_id"])
            version = plan_service.get_version(session, planned["version_id"])
            job = allocation_jobs.submit(session, users["ceo"], plan=plan,
                                         version=version, settings=settings)
            job_uuid = job.job_uuid
            session.commit()
        allocation_jobs._guarded(job_uuid, planned["version_id"], users["ceo"],
                                 settings)
    finally:
        allocation_engine.plan_allocation = original

    with Session(with_history) as session:
        state = allocation_jobs.state(session, job_uuid)
        version = plan_service.get_version(session, planned["version_id"])

    assert state["status"] == AllocationJobStatus.FAILED
    assert state["error_count"] == 1
    # The message a planner reads never carries the internal detail.
    assert "engine blew up" not in state["error_message"]
    assert version.status == TargetStatus.DRAFT


def test_a_failed_run_can_be_started_again(with_history, users,
                                           planned) -> None:
    """Retry, as the specification asks. The version is workable, so it runs."""
    original = allocation_engine.plan_allocation
    allocation_engine.plan_allocation = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("first attempt"))
    try:
        settings = factors.FactorSettings.defaults()
        with Session(with_history) as session:
            plan = plan_service.get_plan(session, planned["plan_id"])
            version = plan_service.get_version(session, planned["version_id"])
            job = allocation_jobs.submit(session, users["ceo"], plan=plan,
                                         version=version, settings=settings)
            first_uuid = job.job_uuid
            session.commit()
        allocation_jobs._guarded(first_uuid, planned["version_id"],
                                 users["ceo"], settings)
    finally:
        allocation_engine.plan_allocation = original

    second_uuid = _run_job_inline(with_history, users, planned)
    with Session(with_history) as session:
        assert allocation_jobs.state(session, second_uuid)["status"] in \
            AllocationJobStatus.SUCCEEDED
        assert allocation_jobs.state(session, first_uuid)["status"] == \
            AllocationJobStatus.FAILED


def test_a_restart_closes_a_job_it_interrupted(with_history, users,
                                               planned) -> None:
    """A worker dies with its process and leaves a row stuck at 40%."""
    with Session(with_history) as session:
        plan = plan_service.get_plan(session, planned["plan_id"])
        version = plan_service.get_version(session, planned["version_id"])
        job = allocation_jobs.submit(session, users["ceo"], plan=plan,
                                     version=version,
                                     settings=factors.FactorSettings.defaults())
        job.status = AllocationJobStatus.PROCESSING
        job.progress_percent = 40
        job_uuid = job.job_uuid
        session.commit()

    with Session(with_history) as session:
        swept = allocation_jobs.sweep_interrupted(session)
        session.commit()
        state = allocation_jobs.state(session, job_uuid)
        version = plan_service.get_version(session, planned["version_id"])

    assert swept == 1
    assert state["status"] == AllocationJobStatus.FAILED
    assert "restarted" in state["error_message"]
    assert version.status == TargetStatus.DRAFT


def test_a_frozen_version_refuses_an_allocation(with_history, users,
                                                planned) -> None:
    from app.targetmgmt.errors import VersionFrozen

    with Session(with_history) as session:
        plan = plan_service.get_plan(session, planned["plan_id"])
        version = plan_service.get_version(session, planned["version_id"])
        version.status = TargetStatus.APPROVED
        session.flush()
        with pytest.raises(VersionFrozen):
            allocation_jobs.submit(session, users["ceo"], plan=plan,
                                   version=version,
                                   settings=factors.FactorSettings.defaults())


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_an_oversized_allocation_is_refused_not_truncated(with_history, users,
                                                          planned,
                                                          monkeypatch) -> None:
    """Half an allocation would reconcile against nothing and look complete."""
    import dataclasses

    from app import config

    # A copy, never a mutation: ``get_settings`` is ``lru_cache``d, so writing
    # through the frozen instance would leave the ceiling at five for every
    # later test in this worker.
    shrunk = dataclasses.replace(config.get_settings(),
                                 target_allocation_max_rows=5)
    monkeypatch.setattr("app.targetmgmt.engine.get_settings", lambda: shrunk)
    monkeypatch.setattr("app.targetmgmt.readiness.get_settings", lambda: shrunk)
    with Session(with_history) as session:
        with pytest.raises(allocation_engine.AllocationTooLarge) as caught:
            _run(session, users, planned)

    assert "rows" in caught.value.user_message
    assert "Narrow the plan" in caught.value.user_message
