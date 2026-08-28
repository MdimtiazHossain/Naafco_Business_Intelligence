"""The safeguards: what the allocation engine refuses to pretend.

Step 6 proved the engine is mathematically correct. This file is about the other
three requirements — that it is *data-aware*, *transparent* and *auditable* —
and every test here is really the same test asked a different way: does the
system tell a business user the truth about what it does and does not know?

Four things are pinned:

* **The five data states are five.** "No sales loaded", "sales loaded but none
  states a volume", "customers present but unmapped", "a customer naming a
  sub-territory that does not exist" and "no customers at all" are five
  different situations with five different fixes, and a tick-or-cross cannot
  tell them apart.
* **The allocation level is reported, never assumed.** A Customer Master with no
  sub-territory mapping allocates to sub-territory. That is a real allocation
  and is labelled as one — it is never presented as a completed customer-level
  result.
* **A management adjustment is node-level and absolute**, funded by siblings so
  the country total management typed does not move, and carrying its reason, its
  author and its timestamp.
* **The row ceiling is a refusal.** Nothing is truncated, nothing is partially
  generated, and the existing version is left exactly as it was.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import DimMaterial
from app.database.models_target import (
    AllocationJobStatus,
    TargetAudit,
    TargetLevel,
    TargetStatus,
)
from app.database.models_warehouse import DimCustomer
from app.targetmgmt import (
    adjustments,
    country,
    datastate,
    engine as allocation_engine,
    factors,
    jobs as allocation_jobs,
    plans as plan_service,
    readiness,
    reconcile,
)
from test_target_allocation import (  # noqa: F401  (fixtures are reused wholesale)
    CUSTOMERS,
    MATERIAL,
    SCOPE,
    _run,
    _run_job_inline,
    _seed_sales,
    _terminal_total,
    _worker_engine,
    hierarchy,
    planned,
    with_history,
)


def _rows(session, planned):
    from app.database.models_target import TargetAllocation

    return session.execute(
        select(TargetAllocation).where(
            TargetAllocation.version_id == planned["version_id"])
    ).scalars().all()


# ---------------------------------------------------------------------------
# 9. The five data states are not one state
# ---------------------------------------------------------------------------


def test_an_empty_sales_table_is_no_data(hierarchy, users, planned) -> None:
    with Session(hierarchy) as session:
        plan = plan_service.get_plan(session, planned["plan_id"])
        version = plan_service.get_version(session, planned["version_id"])
        report = readiness.check(session, users["ceo"], plan=plan,
                                 version=version)

    sales = {row["key"]: row for row in report["checks"]}["sales_history"]
    assert sales["state"] == datastate.NO_DATA
    assert sales["blocking"] is True
    assert report["ready"] is False


def test_sales_rows_without_a_volume_are_insufficient_data(
        hierarchy, users, planned) -> None:
    """A different problem with a different fix.

    ``NO_DATA`` needs a sales file; this needs the Total Volume column in the
    file that is already loaded. Reporting both as "no data" would send somebody
    to upload a file they have already uploaded.
    """
    _seed_sales(hierarchy, months=[(2025, 9, 1)], volume=0)
    with Session(hierarchy) as session:
        plan = plan_service.get_plan(session, planned["plan_id"])
        version = plan_service.get_version(session, planned["version_id"])
        report = readiness.check(session, users["ceo"], plan=plan,
                                 version=version)

    sales = {row["key"]: row for row in report["checks"]}["sales_history"]
    assert sales["state"] == datastate.INSUFFICIENT_DATA
    assert sales["facts"]["sales_rows"] > 0
    assert "Total Volume" in sales["action"]


def test_unmapped_customers_are_insufficient_data(agent_engine) -> None:
    """The deployment's actual state, and the reason this gate exists."""
    with Session(agent_engine) as session:
        session.add(DimCustomer(customer_code="U-1", customer_name="Unmapped",
                                sub_territory_code=None))
        session.commit()
        health = readiness.customer_mapping_health(session)
        state = readiness._mapping_state(health, TargetLevel.SUB_TERRITORY)

    assert health["unmapped"] == 1 and health["usable"] == 0
    assert state.state == datastate.INSUFFICIENT_DATA
    assert "Sub-Territory Code" in state.action
    # And it says outright that geography is master data, not a consequence of
    # who somebody bought from.
    assert "Nothing is inferred from sales history" in state.action


def test_a_customer_naming_an_unknown_sub_territory_is_invalid_data(
        agent_engine) -> None:
    """Present and self-contradictory is not the same as missing."""
    with Session(agent_engine) as session:
        session.add(DimCustomer(customer_code="U-2", customer_name="Dangling",
                                sub_territory_code="STR999"))
        session.commit()
        health = readiness.customer_mapping_health(session)
        state = readiness._mapping_state(health, TargetLevel.SUB_TERRITORY)

    assert health["mapped_to_unknown_sub_territory"] == 1
    assert state.state == datastate.INVALID_DATA


def test_no_customers_at_all_is_not_applicable(agent_engine) -> None:
    """Nothing missing, nothing wrong — the split simply does not apply."""
    with Session(agent_engine) as session:
        state = readiness._mapping_state(
            readiness.customer_mapping_health(session),
            TargetLevel.SUB_TERRITORY)
    assert state.state == datastate.NOT_APPLICABLE
    assert state.blocking is False


def test_a_potential_master_is_not_available_rather_than_missing() -> None:
    """Loading a file will not fix it — somebody has to introduce the master."""
    rows = {row.key: row for row in factors.availability(
        factors.FactorSettings.defaults(), has_sales_history=True,
        has_monthly_history=True)}
    for key in ("customer_potential", "territory_potential"):
        assert rows[key].available is False
        assert "not available" in rows[key].reason.lower()
        assert "no potential master data configured" in rows[key].reason.lower()


def test_a_valid_zero_is_not_a_blocking_state() -> None:
    """Zero is an answer. Every other state in ``BLOCKING`` is not."""
    assert datastate.VALID_ZERO not in datastate.BLOCKING
    assert datastate.DataState(datastate.VALID_ZERO, "sold nothing").ok is True


def test_every_state_has_a_tone() -> None:
    for state in datastate.ALL:
        assert state in datastate.TONE


# ---------------------------------------------------------------------------
# 1. The potential factors, and the door left open for them
# ---------------------------------------------------------------------------


def test_the_potential_factors_are_never_derived_from_sales() -> None:
    """Using sales again as "potential" would count one signal twice."""
    assert factors._factor_value("customer_potential", (100.0, 900.0)) == 0.0
    assert factors._factor_value("territory_potential", (100.0, 900.0)) == 0.0


def test_an_unsupported_potential_factor_cannot_be_switched_on() -> None:
    settings = factors.FactorSettings.from_request(
        {"enabled": {"customer_potential": True, "territory_potential": True}})
    assert settings.is_on("customer_potential") is False
    assert set(settings.active_share_weights()) == {
        "historical_contribution", "growth_trend", "two_year_average"}


def test_the_future_potential_master_shape_is_pinned() -> None:
    """A contract described only in prose drifts.

    This is what a Potential Master would have to carry for the factor to switch
    on without the allocation code changing.
    """
    assert factors.POTENTIAL_MASTER_COLUMNS == (
        "customer_code", "potential_volume", "potential_category",
        "effective_from", "effective_to", "source", "status",
    )


def test_registering_a_source_switches_the_factors_on() -> None:
    """The architecture is ready for a master that does not exist yet.

    No edit to the factor declarations, no edit to the engine: registering a
    source is the whole change.
    """
    class Fake:
        def potential_for(self, level, node_codes, as_of=None):
            return {code: 1.0 for code in node_codes}

    assert factors.FACTOR_BY_KEY["customer_potential"].supported is False
    factors.set_potential_source(Fake())
    try:
        assert factors.FACTOR_BY_KEY["customer_potential"].supported is True
        settings = factors.FactorSettings.from_request(
            {"enabled": {"customer_potential": True}})
        assert settings.is_on("customer_potential") is True
    finally:
        factors.set_potential_source(None)
    assert factors.FACTOR_BY_KEY["customer_potential"].supported is False


# ---------------------------------------------------------------------------
# 2. Management adjustment: node-level, absolute, audited
# ---------------------------------------------------------------------------


def _adjust(session, users, planned, *, level, node_code, volume,
            reason="Market opportunity in this region.", material_code=None):
    plan = plan_service.get_plan(session, planned["plan_id"])
    version = plan_service.get_version(session, planned["version_id"])
    return adjustments.record(session, users["ceo"], version=version, plan=plan,
                              level=level, node_code=node_code,
                              adjustment_volume=volume, reason=reason,
                              material_code=material_code)


ADJUSTMENT_ON = {"enabled": {"management_adjustment": True}}


def test_there_is_no_percentage_adjustment_left_to_cancel_itself() -> None:
    """The mechanism the specification asked to be removed is gone.

    Settings carry weights and switches only; an adjustment is a stored volume
    against a node, which is the only form that survives re-normalisation.
    """
    assert set(factors.FactorSettings.defaults().to_dict()) == {
        "weights", "enabled"}


def test_an_adjustment_raises_its_node_and_is_funded_by_siblings(
        with_history, users, planned) -> None:
    """System Suggested 10,000 -> Adjustment +500 -> Final 10,500."""
    settings = factors.FactorSettings.from_request(ADJUSTMENT_ON)
    with Session(with_history) as session:
        before = _run(session, users, planned, settings)
        base = {
            row.node_code: row.volume for row in before.rows
            if row.level == TargetLevel.REGION and row.target_month == "2026-10"
        }
        _adjust(session, users, planned, level=TargetLevel.REGION,
                node_code="REG001", volume=1200)
        session.flush()
        after = _run(session, users, planned, settings)

    now = {
        row.node_code: row.volume for row in after.rows
        if row.level == TargetLevel.REGION and row.target_month == "2026-10"
    }
    assert now["REG001"] > base["REG001"]
    assert now["REG002"] < base["REG002"]
    # The parent never moves, so the country target management typed still holds.
    assert sum(now.values(), Decimal(0)) == sum(base.values(), Decimal(0))


def test_the_period_total_moves_by_the_stated_amount(with_history, users,
                                                     planned) -> None:
    """The adjustment is a figure for the period, not for each of twelve months."""
    settings = factors.FactorSettings.from_request(ADJUSTMENT_ON)
    with Session(with_history) as session:
        before = _run(session, users, planned, settings)
        base = sum((row.volume for row in before.rows
                    if row.level == TargetLevel.REGION
                    and row.node_code == "REG001"), Decimal(0))
        _adjust(session, users, planned, level=TargetLevel.REGION,
                node_code="REG001", volume=1200)
        session.flush()
        after = _run(session, users, planned, settings)

    moved = sum((row.volume for row in after.rows
                 if row.level == TargetLevel.REGION
                 and row.node_code == "REG001"), Decimal(0))
    # Within one rounding unit per month, which is what largest remainder
    # guarantees when one figure is apportioned across twelve.
    assert abs((moved - base) - Decimal("1200")) <= Decimal("0.0012")


def test_an_adjusted_allocation_still_reconciles_everywhere(with_history, users,
                                                            planned) -> None:
    """"The system must re-reconcile parent and child totals after adjustment"."""
    settings = factors.FactorSettings.from_request(ADJUSTMENT_ON)
    with Session(with_history) as session:
        _adjust(session, users, planned, level=TargetLevel.REGION,
                node_code="REG001", volume=1200)
        session.flush()
        version = plan_service.get_version(session, planned["version_id"])
        result = _run(session, users, planned, settings)
        allocation_engine.persist(session, version=version, result=result)
        session.commit()
        verdict = reconcile.check(
            session, version_id=planned["version_id"],
            country_target={MATERIAL: Decimal("120000")})

    assert verdict.balanced is True
    assert _terminal_total(result.rows) == Decimal("120000.0000")


def test_an_adjustment_reports_every_column_the_screen_shows(
        with_history, users, planned) -> None:
    settings = factors.FactorSettings.from_request(ADJUSTMENT_ON)
    with Session(with_history) as session:
        _adjust(session, users, planned, level=TargetLevel.REGION,
                node_code="REG001", volume=1200,
                reason="Herbicide demand review.")
        session.flush()
        result = _run(session, users, planned, settings)

    # A month this material never sells in gets no share of the adjustment, so
    # the entries that describe an actual movement are the non-zero ones.
    entry = next(row for row in result.adjustments
                 if row["node_code"] == "REG001"
                 and Decimal(row["adjustment_volume"]) != 0)
    assert Decimal(entry["final_volume"]) > Decimal(entry["system_volume"])
    assert entry["adjustment_percent"] is not None
    assert entry["reason"] == "Herbicide demand review."
    assert entry["adjusted_by"] == "ceo"
    assert entry["adjusted_at"] is not None


def test_an_adjustment_needs_a_reason(with_history, users, planned) -> None:
    from app.targetmgmt.errors import ReasonRequired

    with Session(with_history) as session:
        with pytest.raises(ReasonRequired):
            _adjust(session, users, planned, level=TargetLevel.REGION,
                    node_code="REG001", volume=500, reason="   ")


def test_the_company_root_cannot_be_adjusted(with_history, users,
                                             planned) -> None:
    """That is a country-target edit, with its own screen and approval path."""
    with Session(with_history) as session:
        with pytest.raises(adjustments.UnknownAdjustmentLevel):
            _adjust(session, users, planned, level=TargetLevel.COMPANY,
                    node_code="C001", volume=500)


def test_an_unfundable_adjustment_is_refused_not_clamped() -> None:
    """A clamp leaves the screen showing a Final Target nobody asked for."""
    with pytest.raises(adjustments.AdjustmentUnfundable):
        adjustments.apply_to_siblings(
            {"a": Decimal("100"), "b": Decimal("300")}, "a", Decimal("500"))


def test_an_adjustment_below_zero_is_refused() -> None:
    with pytest.raises(adjustments.AdjustmentBelowZero):
        adjustments.apply_to_siblings(
            {"a": Decimal("100"), "b": Decimal("300")}, "a", Decimal("-150"))


def test_redistribution_keeps_the_parent_exact() -> None:
    shares = {"a": Decimal("33.3333"), "b": Decimal("33.3333"),
              "c": Decimal("33.3334")}
    total = sum(shares.values(), Decimal(0))
    after = adjustments.apply_to_siblings(shares, "a", Decimal("10"))
    assert sum(after.values(), Decimal(0)) == total
    assert after["a"] == Decimal("43.3333")


def test_an_only_child_is_left_alone() -> None:
    """Nobody to fund it, and the parent's total is fixed. A shape, not a fault."""
    shares = {"a": Decimal("100")}
    assert adjustments.apply_to_siblings(shares, "a", Decimal("50")) == shares


def test_an_adjustment_is_ignored_while_the_factor_is_off(with_history, users,
                                                          planned) -> None:
    with Session(with_history) as session:
        _adjust(session, users, planned, level=TargetLevel.REGION,
                node_code="REG001", volume=1200)
        session.flush()
        result = _run(session, users, planned)  # defaults: the factor is off
    assert result.adjustments == []


def test_an_adjustment_survives_a_re_run(with_history, users, planned) -> None:
    """Stored as an input, so a re-run after loading more sales keeps it."""
    settings = factors.FactorSettings.from_request(ADJUSTMENT_ON)
    with Session(with_history) as session:
        _adjust(session, users, planned, level=TargetLevel.REGION,
                node_code="REG001", volume=1200)
        session.commit()
    with Session(with_history) as session:
        assert len(adjustments.for_version(session, planned["version_id"])) == 1
        result = _run(session, users, planned, settings)
    assert any(row["node_code"] == "REG001" for row in result.adjustments)


def test_setting_an_adjustment_twice_replaces_it(with_history, users,
                                                 planned) -> None:
    with Session(with_history) as session:
        _adjust(session, users, planned, level=TargetLevel.REGION,
                node_code="REG001", volume=1200)
        _adjust(session, users, planned, level=TargetLevel.REGION,
                node_code="REG001", volume=800, reason="Revised downward.")
        session.commit()
        stored = adjustments.for_version(session, planned["version_id"])
    assert len(stored) == 1
    assert Decimal(str(stored[0].adjustment_volume)) == Decimal("800")


def test_an_adjustment_is_audited_with_its_reason(with_history, users,
                                                  planned) -> None:
    with Session(with_history) as session:
        _adjust(session, users, planned, level=TargetLevel.REGION,
                node_code="REG001", volume=1200,
                reason="Herbicide demand review.")
        session.commit()
        entry = session.execute(
            select(TargetAudit).where(
                TargetAudit.action == "MANAGEMENT_ADJUSTED")
        ).scalars().first()

    assert entry is not None
    assert entry.new_value == "1200"
    assert entry.reason == "Herbicide demand review."
    assert entry.actor == "ceo"


def test_withdrawing_an_adjustment_leaves_its_record_behind(with_history, users,
                                                            planned) -> None:
    """The instruction goes; the fact that it was once made does not."""
    with Session(with_history) as session:
        row = _adjust(session, users, planned, level=TargetLevel.REGION,
                      node_code="REG001", volume=1200)
        adjustment_id = row.adjustment_id
        session.commit()

    with Session(with_history) as session:
        plan = plan_service.get_plan(session, planned["plan_id"])
        version = plan_service.get_version(session, planned["version_id"])
        assert adjustments.remove(session, users["ceo"], version=version,
                                  plan=plan, adjustment_id=adjustment_id)
        session.commit()
        assert adjustments.for_version(session, planned["version_id"]) == []
        trail = session.execute(
            select(TargetAudit).where(
                TargetAudit.action == "MANAGEMENT_ADJUSTED")
        ).scalars().all()
    assert len(trail) == 2


# ---------------------------------------------------------------------------
# 3. The row ceiling is a refusal
# ---------------------------------------------------------------------------


def _tiny_limit(monkeypatch, limit: int = 5) -> None:
    """Shrink the row ceiling for one test.

    A **copy** of the settings, never a mutation of the real one: ``get_settings``
    is ``lru_cache``d, so writing through ``object.__setattr__`` would change the
    single frozen instance every other test in the same worker shares — and the
    ceiling would stay at five for the rest of the run.
    """
    import dataclasses

    from app import config

    original = config.get_settings()
    shrunk = dataclasses.replace(original,
                                 target_allocation_max_rows=limit)
    monkeypatch.setattr("app.targetmgmt.readiness.get_settings", lambda: shrunk)
    monkeypatch.setattr("app.targetmgmt.engine.get_settings", lambda: shrunk)


def test_an_oversized_run_never_moves_the_version(with_history, users, planned,
                                                  monkeypatch) -> None:
    """"The existing target version must remain intact and usable"."""
    _tiny_limit(monkeypatch)
    with Session(with_history) as session:
        plan = plan_service.get_plan(session, planned["plan_id"])
        version = plan_service.get_version(session, planned["version_id"])
        with pytest.raises(allocation_engine.AllocationTooLarge):
            allocation_jobs.submit(session, users["ceo"], plan=plan,
                                   version=version,
                                   settings=factors.FactorSettings.defaults())
        session.rollback()

    with Session(with_history) as session:
        assert plan_service.get_version(
            session, planned["version_id"]).status == TargetStatus.DRAFT
        assert _rows(session, planned) == []


def test_the_refusal_names_every_dimension_of_the_projection(
        with_history, users, planned, monkeypatch) -> None:
    """"Too big" is not actionable; the numbers behind it are."""
    _tiny_limit(monkeypatch)
    with Session(with_history) as session:
        plan = plan_service.get_plan(session, planned["plan_id"])
        version = plan_service.get_version(session, planned["version_id"])
        with pytest.raises(allocation_engine.AllocationTooLarge) as caught:
            allocation_jobs.submit(session, users["ceo"], plan=plan,
                                   version=version,
                                   settings=factors.FactorSettings.defaults())

    detail = caught.value.details
    for key in ("projected_rows", "maximum_rows", "financial_year",
                "target_period", "months", "materials", "nodes",
                "allocation_level", "narrowing_options"):
        assert key in detail, key
    assert detail["projected_rows"] > detail["maximum_rows"]
    assert set(detail["narrowing_options"]) >= {
        "target_period", "company", "business_unit", "sales_line",
        "material_brand", "material"}
    assert "unchanged" in caught.value.user_message


def test_the_readiness_gate_reports_the_ceiling_before_anything_starts(
        with_history, users, planned, monkeypatch) -> None:
    _tiny_limit(monkeypatch)
    with Session(with_history) as session:
        plan = plan_service.get_plan(session, planned["plan_id"])
        version = plan_service.get_version(session, planned["version_id"])
        report = readiness.check(session, users["ceo"], plan=plan,
                                 version=version)

    limit = {row["key"]: row for row in report["checks"]}["row_limit"]
    assert limit["state"] == datastate.INSUFFICIENT_DATA
    assert report["ready"] is False
    assert report["projection"]["exceeds"] is True


# ---------------------------------------------------------------------------
# 4, 5. The allocation level is reported, never assumed
# ---------------------------------------------------------------------------


def test_a_run_that_reaches_customers_says_so(with_history, users,
                                              planned) -> None:
    job_uuid = _run_job_inline(with_history, users, planned)
    with Session(with_history) as session:
        state = allocation_jobs.state(session, job_uuid)
    assert state["allocation_level"] == TargetLevel.CUSTOMER


def test_an_unmapped_customer_base_allocates_to_sub_territory_and_says_so(
        agent_engine, users) -> None:
    """The requirement stated outright: never present a sub-territory
    allocation as a completed customer-level one."""
    _seed_sales(agent_engine, months=[(2024, 9, 2), (2025, 9, 2)],
                customers=["CUST-001"])
    with Session(agent_engine) as session:
        material = session.execute(
            select(DimMaterial).where(DimMaterial.material_code == MATERIAL)
        ).scalar_one()
        material.company_code = "C001"
        plan = plan_service.create_plan(
            session, users["ceo"], scope=plan_service.PlanScope(**SCOPE))
        session.flush()
        version = plan_service.current_version(session, plan.plan_id)
        country.set_lines(session, users["ceo"], version=version, plan=plan,
                          entries=[(MATERIAL, 60000)])
        session.commit()
        scoped = {"plan_id": plan.plan_id, "version_id": version.version_id}

    job_uuid = _run_job_inline(agent_engine, users, scoped)
    with Session(agent_engine) as session:
        state = allocation_jobs.state(session, job_uuid)

    assert state["allocation_level"] == TargetLevel.SUB_TERRITORY
    assert state["status"] == AllocationJobStatus.COMPLETED_WITH_WARNINGS
    assert any("not customer" in warning
               for warning in state["result"]["warnings"])


def test_a_partial_level_allocation_still_reconciles(agent_engine,
                                                     users) -> None:
    """Allocating to a valid higher level is supported, not blocked."""
    _seed_sales(agent_engine, months=[(2024, 9, 2), (2025, 9, 2)],
                customers=["CUST-001"])
    with Session(agent_engine) as session:
        material = session.execute(
            select(DimMaterial).where(DimMaterial.material_code == MATERIAL)
        ).scalar_one()
        material.company_code = "C001"
        plan = plan_service.create_plan(
            session, users["ceo"], scope=plan_service.PlanScope(**SCOPE))
        session.flush()
        version = plan_service.current_version(session, plan.plan_id)
        country.set_lines(session, users["ceo"], version=version, plan=plan,
                          entries=[(MATERIAL, 60000)])
        session.commit()
        scoped = {"plan_id": plan.plan_id, "version_id": version.version_id}

    _run_job_inline(agent_engine, users, scoped)
    with Session(agent_engine) as session:
        verdict = reconcile.check(
            session, version_id=scoped["version_id"],
            country_target={MATERIAL: Decimal("60000")})
    assert verdict.balanced is True


def test_the_deepest_reachable_level_is_read_from_the_masters(
        agent_engine) -> None:
    with Session(agent_engine) as session:
        level, health = readiness.deepest_reachable_level(session)
    assert level == TargetLevel.SUB_TERRITORY
    assert health["usable"] == 0


# ---------------------------------------------------------------------------
# 6, 7. Manual retry, and the run history that makes it usable
# ---------------------------------------------------------------------------


def test_the_run_history_lists_failures_beside_successes(with_history, users,
                                                         planned) -> None:
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
            failed = job.job_uuid
            session.commit()
        allocation_jobs._guarded(failed, planned["version_id"], users["ceo"],
                                 settings)
    finally:
        allocation_engine.plan_allocation = original

    _run_job_inline(with_history, users, planned)

    with Session(with_history) as session:
        runs, total = allocation_jobs.history(
            session, version_id=planned["version_id"])

    assert total == 2
    statuses = {run["status"] for run in runs}
    assert AllocationJobStatus.FAILED in statuses
    assert statuses & set(AllocationJobStatus.SUCCEEDED)


def test_each_run_carries_the_fields_the_history_view_needs(with_history, users,
                                                            planned) -> None:
    _run_job_inline(with_history, users, planned)
    with Session(with_history) as session:
        runs, _ = allocation_jobs.history(session,
                                          version_id=planned["version_id"])

    run = runs[0]
    for key in ("job_id", "version_id", "started_by", "started_at",
                "completed_at", "status", "projected_rows", "generated_rows",
                "allocation_level", "sales_data_available", "error_count",
                "warning_count"):
        assert key in run, key
    assert run["projected_rows"] is not None
    assert run["sales_data_available"] is True


def test_sales_availability_is_three_valued(hierarchy, users, planned) -> None:
    """Never looked, looked and found none, found some — three answers.

    ``None`` for a run that failed before reading, ``False`` for one that read
    and found nothing, ``True`` otherwise. A boolean would merge the first two.
    """
    _run_job_inline(hierarchy, users, planned)
    with Session(hierarchy) as session:
        runs, _ = allocation_jobs.history(session,
                                          version_id=planned["version_id"])
    assert runs[0]["sales_rows_found"] == 0
    assert runs[0]["sales_data_available"] is False


def test_a_retry_is_a_new_run_that_leaves_the_old_one_alone(with_history, users,
                                                            planned) -> None:
    first = _run_job_inline(with_history, users, planned)
    with Session(with_history) as session:
        version = plan_service.get_version(session, planned["version_id"])
        version.status = TargetStatus.DRAFT
        session.commit()

    second = _run_job_inline(with_history, users, planned)
    assert first != second

    with Session(with_history) as session:
        runs, total = allocation_jobs.history(
            session, version_id=planned["version_id"])
    assert total == 2
    assert {run["job_id"] for run in runs} == {first, second}


def test_nothing_retries_by_itself(with_history, users, planned) -> None:
    """Manual only. A failed run stays failed until somebody asks again."""
    original = allocation_engine.plan_allocation
    allocation_engine.plan_allocation = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("boom"))
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
        runs, total = allocation_jobs.history(
            session, version_id=planned["version_id"])
    assert total == 1
    assert runs[0]["status"] == AllocationJobStatus.FAILED
    assert runs[0]["error_count"] == 1


# ---------------------------------------------------------------------------
# 8. The readiness gate as a whole
# ---------------------------------------------------------------------------


def test_the_gate_lists_every_condition(hierarchy, users, planned) -> None:
    with Session(hierarchy) as session:
        plan = plan_service.get_plan(session, planned["plan_id"])
        version = plan_service.get_version(session, planned["version_id"])
        report = readiness.check(session, users["ceo"], plan=plan,
                                 version=version)

    keys = {row["key"] for row in report["checks"]}
    assert keys == {
        "country_target", "conversion_factor", "transfer_price",
        "material_master", "sales_history", "hierarchy", "customer_mapping",
        "allocation_rules", "row_limit",
    }


def test_the_gate_says_which_level_is_reachable(hierarchy, users,
                                                planned) -> None:
    """Preferable to discovering it halfway through a large background job.

    This fixture *does* map its customers, so the gate should say customer —
    the unmapped case is covered by ``test_the_deepest_reachable_level_is_read
    _from_the_masters`` against the shared fixture, which does not.
    """
    with Session(hierarchy) as session:
        plan = plan_service.get_plan(session, planned["plan_id"])
        version = plan_service.get_version(session, planned["version_id"])
        report = readiness.check(session, users["ceo"], plan=plan,
                                 version=version)

    assert report["allocation_level"] == TargetLevel.CUSTOMER
    assert report["allocation_level_is_customer"] is True
    assert report["customer_mapping"]["usable"] == len(CUSTOMERS)


def test_a_missing_conversion_factor_does_not_block_the_run(
        with_history, users, planned) -> None:
    """The engine allocates volume, which needs neither input.

    Coupling them would make an unpriced material unallocatable, which is a
    different and much worse failure than an n/a in two columns.
    """
    with Session(with_history) as session:
        material = session.execute(
            select(DimMaterial).where(DimMaterial.material_code == MATERIAL)
        ).scalar_one()
        material.conversion_factor = None
        material.transfer_price = None
        session.flush()
        plan = plan_service.get_plan(session, planned["plan_id"])
        version = plan_service.get_version(session, planned["version_id"])
        report = readiness.check(session, users["ceo"], plan=plan,
                                 version=version)

    by_key = {row["key"]: row for row in report["checks"]}
    assert by_key["conversion_factor"]["state"] == datastate.NO_DATA
    assert "Volume allocation is unaffected" in by_key["conversion_factor"]["detail"]
    assert "conversion_factor" not in report["blocking"]


def test_a_ready_plan_reports_ready(with_history, users, planned) -> None:
    with Session(with_history) as session:
        plan = plan_service.get_plan(session, planned["plan_id"])
        version = plan_service.get_version(session, planned["version_id"])
        report = readiness.check(session, users["ceo"], plan=plan,
                                 version=version)
    assert report["ready"] is True
    assert report["blocking"] == []
    assert report["allocation_level"] == TargetLevel.CUSTOMER
