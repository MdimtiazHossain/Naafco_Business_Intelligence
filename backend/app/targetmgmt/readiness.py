"""The pre-flight check: what an allocation would find, before it runs.

Discovering that customers have no sub-territory *halfway through* a background
job is the worst possible time to discover it — the version has already moved,
a worker has read two years of sales, and the planner watching the bar has to be
told the run they started was never going to reach the level they wanted.

So every condition the engine depends on is checked first, cheaply, and reported
as a list a planner can act on:

* the country target exists and states a volume;
* the Material Master carries a conversion factor and a transfer price for
  every material on it;
* two basis years of sales exist for the plan's scope;
* the organisational hierarchy is internally consistent;
* customers are mapped to a sub-territory;
* the allocation rules leave at least one factor able to contribute;
* the projected row count is inside the configured ceiling.

**Each check answers with one of the five data states, not a boolean.** "0
customers mapped" and "no Customer Master loaded" and "customer mapping does not
apply to this plan" are three different situations and a tick-or-cross cannot
tell them apart. See :mod:`app.targetmgmt.datastate`.

**The mapping check is the one that matters most on real data.** A Customer
Master can be complete in every other respect and still carry no
``sub_territory_code`` — which is exactly the state this deployment is in — and
the consequence is precise: allocation can reach sub-territory and no further.
That is reported as a *level*, not as a failure, because an allocation to
territory is a real allocation. What it must never do is claim to have reached
customers.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ..ai import queries
from ..ai.permission_filter import UserContext
from ..config import get_settings
from ..database.models import (
    DimArea,
    DimMaterial,
    DimRegion,
    DimSubTerritory,
    DimTerritory,
    DimUnit,
    DimZone,
)
from ..database.models_target import TargetLevel, TargetPlan, TargetVersion
from ..database.models_warehouse import DimCustomer
from ..etl.calendar import FinancialYearConfig
from . import country, datastate, factors, history as history_module
from . import seasonality


def customer_mapping_health(session: Session) -> dict[str, Any]:
    """How many customers can actually be placed in the hierarchy.

    Counted rather than sampled, and reported as a summary a planner can read
    without opening the Customer Master: total, mapped, unmapped, and how many
    name a sub-territory the organisational master does not have.

    That last count is the difference between *insufficient* and *invalid* data.
    A customer with no sub-territory is a gap somebody has to fill; a customer
    naming ``STR999`` when no such sub-territory exists is a contradiction, and
    loading more customers will not fix it.
    """
    total = session.execute(
        select(func.count()).select_from(DimCustomer)
        .where(DimCustomer.is_deleted.is_(False))
    ).scalar_one()

    mapped = session.execute(
        select(func.count()).select_from(DimCustomer)
        .where(DimCustomer.is_deleted.is_(False),
               DimCustomer.sub_territory_code.isnot(None),
               DimCustomer.sub_territory_code != "")
    ).scalar_one()

    known = {
        code for (code,) in session.execute(
            select(DimSubTerritory.sub_territory_code)
            .where(DimSubTerritory.is_deleted.is_(False)))
    }
    dangling = 0
    if known is not None:
        referenced = session.execute(
            select(DimCustomer.sub_territory_code, func.count())
            .where(DimCustomer.is_deleted.is_(False),
                   DimCustomer.sub_territory_code.isnot(None),
                   DimCustomer.sub_territory_code != "")
            .group_by(DimCustomer.sub_territory_code)
        ).all()
        dangling = sum(count for code, count in referenced if code not in known)

    return {
        "total_customers": total,
        "mapped_to_sub_territory": mapped,
        "unmapped": total - mapped,
        "mapped_to_unknown_sub_territory": dangling,
        "usable": mapped - dangling,
    }


def hierarchy_health(session: Session) -> dict[str, Any]:
    """Broken parent links in the organisational chain, level by level.

    Every level states its own parent code, so a break is a code naming a row
    that is not there — a territory under a unit that was never created. The
    allocation would silently drop that whole subtree, taking its volume with
    it, which is precisely the kind of loss reconciliation cannot see because
    the missing branch has no parent row to disagree with.
    """
    pairs = (
        ("zone", DimZone, "sales_line_code", None),
        ("region", DimRegion, "zone_code", DimZone),
        ("area", DimArea, "region_code", DimRegion),
        ("unit", DimUnit, "area_code", DimArea),
        ("territory", DimTerritory, "unit_code", DimUnit),
        ("sub_territory", DimSubTerritory, "territory_code", DimTerritory),
    )
    broken: dict[str, int] = {}
    counts: dict[str, int] = {}
    for level, model, parent_field, parent_model in pairs:
        rows = session.execute(
            select(model).where(model.is_deleted.is_(False))
        ).scalars().all()
        counts[level] = len(rows)
        if parent_model is None:
            continue
        parent_code_field = parent_field
        known = {
            code for (code,) in session.execute(
                select(getattr(parent_model, parent_code_field))
                .where(parent_model.is_deleted.is_(False)))
        }
        missing = sum(1 for row in rows
                      if getattr(row, parent_field) not in known)
        if missing:
            broken[level] = missing
    return {"counts": counts, "broken_parent_links": broken}


def deepest_reachable_level(session: Session) -> tuple[str, dict[str, Any]]:
    """The deepest hierarchy level an allocation could actually reach.

    Walks down while each level has at least one row, and stops where it does
    not. Customer is reached only when a customer is mapped to a sub-territory
    the master actually has — a Customer Master full of unmapped rows reaches
    sub-territory, which is a real allocation and must be labelled as one rather
    than presented as a customer-level result.
    """
    health = customer_mapping_health(session)
    counts = hierarchy_health(session)["counts"]

    deepest = TargetLevel.COMPANY
    for level in (TargetLevel.ZONE, TargetLevel.REGION, TargetLevel.AREA,
                  TargetLevel.UNIT, TargetLevel.TERRITORY,
                  TargetLevel.SUB_TERRITORY):
        if counts.get(level, 0) > 0:
            deepest = level
        else:
            break

    if deepest == TargetLevel.SUB_TERRITORY and health["usable"] > 0:
        deepest = TargetLevel.CUSTOMER
    return deepest, health


def check(session: Session, user: UserContext, *, plan: TargetPlan,
          version: TargetVersion,
          settings: factors.FactorSettings | None = None) -> dict[str, Any]:
    """Every pre-flight condition, with the projected size of the run.

    Read-only and cheap: it counts rows and reads masters, and never touches the
    allocation tables. Safe to call on every page load, which is the point — the
    gate is only useful if a planner sees it *before* pressing the button.
    """
    settings = settings or factors.FactorSettings.defaults()
    config = FinancialYearConfig.from_settings()
    checks: list[dict[str, Any]] = []

    def add(key: str, label: str, state: datastate.DataState) -> None:
        checks.append({"key": key, "label": label, **state.to_dict()})

    # --- the country target -------------------------------------------------
    lines = country.list_lines(session, version.version_id)
    priced = [line for line in lines if line.target_volume]
    if not priced:
        add("country_target", "Country Target exists", datastate.insufficient(
            "No material on this version states a Target Volume.",
            "Enter a Target Volume on the Country Target tab.",
            materials=len(lines)))
    else:
        add("country_target", "Country Target exists", datastate.available(
            f"{len(priced)} material(s) carry a target volume.",
            materials=len(priced),
            total_volume=float(sum(line.target_volume for line in priced))))

    # --- the derivation inputs ---------------------------------------------
    # These do not block an allocation: the engine allocates *volume*, which
    # needs neither. They block the derived Quantity and Value columns, and a
    # planner should know that before the numbers arrive rather than after.
    without_factor = [line.material_code for line in priced
                      if "conversion_factor" in line.missing]
    without_price = [line.material_code for line in priced
                     if "transfer_price" in line.missing]
    add("conversion_factor", "Conversion Factor available",
        datastate.available("Every targeted material states one.")
        if not without_factor else datastate.no_data(
            f"{len(without_factor)} of {len(priced)} targeted material(s) state "
            f"no Conversion Factor, so Quantity cannot be derived. Volume "
            f"allocation is unaffected.",
            "Load the Conversion Factor column through the Material Master "
            "upload.",
            blocks=False,
            missing=without_factor[:20], missing_count=len(without_factor)))
    add("transfer_price", "Transfer Price available",
        datastate.available("Every targeted material states one.")
        if not without_price else datastate.no_data(
            f"{len(without_price)} of {len(priced)} targeted material(s) state "
            f"no Transfer Price, so Value cannot be derived. Volume allocation "
            f"is unaffected.",
            "Load the Transfer Price column through the Material Master upload.",
            blocks=False,
            missing=without_price[:20], missing_count=len(without_price)))

    # --- the material master ------------------------------------------------
    material_count = session.execute(
        select(func.count()).select_from(DimMaterial)
        .where(DimMaterial.is_deleted.is_(False),
               DimMaterial.company_code == plan.company_code)
    ).scalar_one()
    add("material_master", "Material Master available",
        datastate.available(f"{material_count} material(s) under company "
                            f"{plan.company_code}.", materials=material_count)
        if material_count else datastate.no_data(
            f"No material is registered under company {plan.company_code}.",
            "Load the Material Master for this company."))

    # --- historical sales ---------------------------------------------------
    years = history_module.basis_years(plan, config)
    filters = history_module.plan_filters(plan, user, session)
    sales_rows, sales_volume = _sales_in_scope(session, filters, years, config)
    if sales_rows == 0:
        add("sales_history", "Historical Sales available", datastate.no_data(
            f"No sales rows exist for {' or '.join(years)} in this plan's "
            f"scope, so there is nothing to allocate by.",
            "Load the sales history for those financial years through the "
            "Data Upload centre.",
            sales_rows=0, basis_years=years))
    elif sales_volume == 0:
        # Rows exist and every one states a volume of zero, or none states a
        # volume at all. Not "no data" — the data is there and says nothing
        # useful, which is a different problem with a different fix.
        add("sales_history", "Historical Sales available",
            datastate.insufficient(
                f"{sales_rows:,} sales rows exist for {' or '.join(years)}, but "
                f"none states a volume, so no volume-based basis can be built.",
                "Check that the sales upload carries the Total Volume column.",
                sales_rows=sales_rows, basis_years=years))
    else:
        add("sales_history", "Historical Sales available", datastate.available(
            f"{sales_rows:,} sales rows across {' and '.join(years)}.",
            sales_rows=sales_rows, basis_years=years,
            total_volume=float(sales_volume)))

    # --- the hierarchy ------------------------------------------------------
    hierarchy = hierarchy_health(session)
    if hierarchy["broken_parent_links"]:
        add("hierarchy", "Organisational hierarchy consistent",
            datastate.invalid(
                "Some organisational records name a parent the master does not "
                "have, so their subtree would be dropped from the allocation.",
                "Correct the parent codes in Master Data.",
                broken=hierarchy["broken_parent_links"]))
    else:
        add("hierarchy", "Organisational hierarchy consistent",
            datastate.available("Every level's parent exists.",
                                **hierarchy["counts"]))

    # --- customer mapping ---------------------------------------------------
    level, health = deepest_reachable_level(session)
    add("customer_mapping", "Customer to Sub-Territory mapping",
        _mapping_state(health, level))

    # --- allocation rules ---------------------------------------------------
    availability = factors.availability(
        settings, has_sales_history=sales_volume > 0,
        has_monthly_history=_has_monthly_pattern(session, filters, years, config))
    usable = [row for row in availability if row.enabled and row.available]
    add("allocation_rules", "Allocation rules configured",
        datastate.available(
            f"{len(usable)} factor(s) will drive the split.",
            active=[row.key for row in usable])
        if usable else datastate.insufficient(
            "No allocation factor can contribute, so there is no basis to "
            "distribute by.",
            "Enable a factor whose data is available, or load the data the "
            "enabled ones need."))

    # --- size ---------------------------------------------------------------
    projection = project(session, plan=plan, version=version, level=level,
                         material_count=len(priced), config=config)
    add("row_limit", "Projected rows within limit",
        datastate.available(
            f"{projection['projected_rows']:,} rows, within the "
            f"{projection['maximum_rows']:,} row limit.", **projection)
        # Not ``insufficient``: the projection succeeded and is exact. Nothing
        # is missing — the plan is simply larger than the ceiling, and saying
        # "insufficient data" sent a reader hunting for data that was all there.
        if not projection["exceeds"] else datastate.exceeds_limit(
            f"This allocation would write {projection['projected_rows']:,} rows, "
            f"above the {projection['maximum_rows']:,} row limit.",
            "Narrow the plan: a single quarter, one business unit or sales "
            "line, or fewer materials on the country target.",
            **projection))

    blocking = [row for row in checks if row["blocking"]]
    return {
        "checks": checks,
        "ready": not blocking,
        "blocking": [row["key"] for row in blocking],
        "allocation_level": level,
        "allocation_level_is_customer": level == TargetLevel.CUSTOMER,
        "customer_mapping": health,
        "hierarchy": hierarchy,
        "projection": projection,
        "basis_years": years,
        "factors": [row.to_dict() for row in availability],
    }


def _mapping_state(health: dict[str, Any], level: str) -> datastate.DataState:
    """The customer-mapping verdict, in the vocabulary of the five states."""
    total = health["total_customers"]
    if total == 0:
        return datastate.not_applicable(
            "No customers are registered, so a customer-level split does not "
            "apply. Allocation will reach sub-territory.",
            **health)
    if health["mapped_to_unknown_sub_territory"]:
        return datastate.invalid(
            f"{health['mapped_to_unknown_sub_territory']:,} customer(s) name a "
            f"sub-territory the organisational master does not have.",
            "Correct the Sub-Territory Code on those customers, or load the "
            "missing sub-territories.",
            **health)
    if health["usable"] == 0:
        return datastate.insufficient(
            f"0 of {total:,} customers are mapped to a sub-territory, so "
            f"customer-level allocation cannot be reached. Allocation will stop "
            f"at {level.replace('_', ' ')}.",
            "Load the Sub-Territory Code on the Customer Master. Nothing is "
            "inferred from sales history — a customer's geography is master "
            "data, not a consequence of who they bought from.",
            **health)
    if health["unmapped"]:
        return datastate.insufficient(
            f"{health['usable']:,} of {total:,} customers are mapped. The "
            f"unmapped ones will receive no target.",
            "Load the Sub-Territory Code for the remaining customers.",
            **health)
    return datastate.available(
        f"All {total:,} customers are mapped to a sub-territory.", **health)


def project(session: Session, *, plan: TargetPlan, version: TargetVersion,
            level: str, material_count: int,
            config: FinancialYearConfig | None = None) -> dict[str, Any]:
    """How many rows this run would write, and whether that is allowed.

    Counted from the master data rather than by building the tree, so the gate
    is cheap enough to answer on every page load. It is an **upper bound**: the
    real tree may be smaller where a branch is empty, and a projection that
    under-estimated would let a run start and then hit the ceiling halfway.
    """
    config = config or FinancialYearConfig.from_settings()
    months = seasonality.plan_months(plan.financial_year, plan.target_period,
                                     config)
    counts = hierarchy_health(session)["counts"]

    nodes = 1  # the company root
    for org_level in (TargetLevel.ZONE, TargetLevel.REGION, TargetLevel.AREA,
                      TargetLevel.UNIT, TargetLevel.TERRITORY,
                      TargetLevel.SUB_TERRITORY):
        nodes += counts.get(org_level, 0)
        if org_level == level:
            break
    if level == TargetLevel.CUSTOMER:
        nodes += customer_mapping_health(session)["usable"]

    projected = material_count * len(months) * nodes
    maximum = get_settings().target_allocation_max_rows
    return {
        "projected_rows": projected,
        "maximum_rows": maximum,
        "exceeds": projected > maximum,
        "financial_year": plan.financial_year,
        "target_period": plan.target_period,
        "months": len(months),
        "materials": material_count,
        "nodes": nodes,
        "customers": (customer_mapping_health(session)["usable"]
                      if level == TargetLevel.CUSTOMER else 0),
        "allocation_level": level,
        #: What a planner can actually change to get under the ceiling. Named
        #: rather than left as "narrow the plan", because a plan has six
        #: independent dimensions and guessing which one to cut is the work.
        "narrowing_options": [
            "target_period", "company", "business_unit", "sales_line",
            "material_brand", "material",
        ],
    }


def _sales_in_scope(session: Session, filters, years, config) -> tuple[int, float]:
    """Rows and total volume across the basis years, in one query per year."""
    table = queries.view(session, queries.SALES_VIEW)
    rows = 0
    volume = 0.0
    for financial_year in years:
        date_from, date_to = history_module.year_window(financial_year, config)
        statement = select(func.count(), func.sum(table.c.volume)).select_from(table)
        conditions = queries.filter_conditions(table, filters, date_from, date_to)
        if conditions:
            statement = statement.where(and_(*conditions))
        count, total = session.execute(statement).one()
        rows += int(count or 0)
        volume += float(total or 0)
    return rows, volume


def _has_monthly_pattern(session: Session, filters, years, config) -> bool:
    windows = [history_module.year_window(year, config) for year in years]
    totals = seasonality._monthly_volume(session, filters, windows)
    return len([value for value in totals.values() if value > 0]) >= \
        seasonality.MIN_MONTHS_FOR_PATTERN


__all__ = [
    "customer_mapping_health",
    "hierarchy_health",
    "deepest_reachable_level",
    "check",
    "project",
]
