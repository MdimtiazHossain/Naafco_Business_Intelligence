"""The allocation engine: country volume down to customer, month by month.

**Volume first, and only volume.** The engine allocates Target Volume and
nothing else. Quantity and Value are *derived from the allocated volume* after
the fact — ``quantity = volume / conversion_factor`` and
``value = quantity * transfer_price`` — never allocated in their own right. That
ordering is not a preference: a conversion factor differs per material, so a
value allocated directly would imply volumes that do not add up to the country
target, and the reconciliation this module exists to guarantee would be
unachievable.

**The shape of a run.** Two phases, and the split matters:

1. **Compute** — reads only. The tree, the two-year history, the seasonal
   shapes, the factor weights and every rounding decision happen here, in
   memory, holding no write lock. Progress can be reported freely throughout,
   because nothing is blocking a second connection.
2. **Persist** — one short transaction that deletes the version's previous
   allocation and bulk-inserts the new one. All-or-nothing: a half-written
   allocation would reconcile against nothing and still look complete.

That is the opposite arrangement from the ETL, which computes and writes in one
long transaction and therefore cannot report progress through the database at
all. Allocation can, because its expensive half is pure reading.

**Rows exist at every level, not only at the leaf.** A customer row names its
parent, that row names *its* parent, and so on to the company — so the review
tree is a direct read, reconciliation is checkable as stored data rather than as
a re-derivation, and a later master change that moves a territory into another
region cannot retroactively reshape a target somebody already approved. The
cost is roughly a fifth more rows than leaves alone.

**Nothing is fabricated when there is no history.** With no sales in the basis
years the engine does not fall back to an even spread and call it an allocation:
:func:`plan_allocation` returns a result whose ``allocatable`` is False, naming
the factors that could not be calculated, and the version stays unallocated.
That is the whole difference between an engine that is waiting for data and one
that has quietly made some up.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import islice
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ..ai import queries
from ..ai.permission_filter import UserContext
from ..ai.schemas import ScopeFilters
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
from ..database.models_target import (
    TargetAllocation,
    TargetLevel,
    TargetPlan,
    TargetStatus,
    TargetVersion,
)
from ..database.models_warehouse import DimCustomer
from ..etl.calendar import FinancialYearConfig
from . import (
    adjustments as adjustment_service,
    country,
    factors,
    history as history_module,
    rounding,
    seasonality,
)
from .errors import TargetManagementError

#: The stages a run passes through, in the order the screen lists them.
#:
#: Named here rather than in the job module because they describe the *work*,
#: not the plumbing that runs it — a caller that invokes the engine directly
#: reports the same stages a background job does.
STAGES: tuple[tuple[str, str], ...] = (
    ("HISTORICAL_ANALYSIS", "Historical Analysis"),
    ("MATERIAL_ALLOCATION", "Brand / SKU Allocation"),
    ("MONTHLY_ALLOCATION", "Monthly Allocation"),
    ("CUSTOMER_ALLOCATION", "Customer Allocation"),
    ("RECONCILIATION", "Reconciliation"),
    ("FINALIZATION", "Finalization"),
)
STAGE_KEYS: tuple[str, ...] = tuple(key for key, _ in STAGES)
STAGE_LABEL: dict[str, str] = dict(STAGES)


class AllocationTooLarge(TargetManagementError):
    """The run would write more rows than the configured ceiling allows.

    A **hard safety limit**, never a truncation. Half an allocation reconciles
    against nothing, skips whichever customers happened to sort last, and looks
    exactly like a complete one — so the run is refused outright and the
    existing target version is left untouched and usable.

    The refusal carries the whole projection, because "too big" is not
    actionable and "400,000 rows from 200 materials over 12 months across 167
    nodes, against a 250,000 limit" is.
    """

    code = "TARGET_ALLOCATION_TOO_LARGE"

    def __init__(self, projection: dict[str, Any]) -> None:
        projected = projection["projected_rows"]
        limit = projection["maximum_rows"]
        super().__init__(
            f"projected {projected} allocation rows exceeds {limit}",
            user_message=(
                f"Estimated allocation contains {projected:,} rows, exceeding "
                f"the configured safety limit of {limit:,} rows. Please "
                f"increase TARGET_ALLOCATION_MAX_ROWS or narrow the allocation "
                f"scope. "
                f"({projection['materials']:,} material(s) x "
                f"{projection['months']} month(s) x "
                f"{projection['nodes']:,} node(s).) Nothing has been generated "
                f"and the current target is unchanged — no material, customer "
                f"or month has been dropped to make it fit."
            ),
            details=projection,
        )


class NothingToAllocate(TargetManagementError):
    """No country target volume has been entered for this version."""

    code = "TARGET_NOTHING_TO_ALLOCATE"
    user_message = (
        "This version has no country target volume to allocate. Enter a Target "
        "Volume against at least one material first."
    )


@dataclass
class TreeNode:
    """One node of the organisational tree, as the allocation walks it."""

    level: str
    code: str
    name: str | None
    parent_level: str | None
    parent_code: str | None
    children: list["TreeNode"] = field(default_factory=list)

    def leaves(self) -> Iterable["TreeNode"]:
        if not self.children:
            yield self
            return
        for child in self.children:
            yield from child.leaves()

    def walk(self) -> Iterable["TreeNode"]:
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class AllocatedRow:
    """One allocation row, before it becomes a database row.

    ``volume`` is what stands; ``system_volume`` is what the factors alone
    produced for this node, before any management adjustment naming it moved it.
    They differ only at an adjusted node — a node further down is split from its
    parent's *actual* figure and was never itself adjusted, so for it the two are
    the same and saying otherwise would invent a suggestion nobody made.
    """

    level: str
    node_code: str
    parent_level: str | None
    parent_code: str | None
    material_code: str
    target_month: str
    volume: Decimal
    system_volume: Decimal | None = None

    @property
    def suggested(self) -> Decimal:
        """What the engine suggested, which is the volume where nothing moved."""
        return self.volume if self.system_volume is None else self.system_volume


@dataclass
class AllocationPlan:
    """Everything a run produced, or the reason it produced nothing."""

    allocatable: bool
    reason: str | None
    rows: list[AllocatedRow]
    months: list[str]
    materials: list[str]
    node_count: int
    customer_count: int
    seasonality: dict[str, dict[str, Any]]
    factor_availability: list[dict[str, Any]]
    equal_split_nodes: list[str]
    warnings: list[str]
    country_volume: Decimal
    #: The deepest level this run actually reached. Never assumed to be
    #: customer: a Customer Master with no sub-territory mapping allocates to
    #: sub-territory, and that is a real allocation which must be labelled as
    #: one rather than presented as a customer-level result.
    allocation_level: str = TargetLevel.COMPANY
    #: What each management adjustment did, for the screen's System Suggested /
    #: Adjustment / Final columns.
    adjustments: list[dict[str, Any]] = field(default_factory=list)
    #: Sales rows the basis years held. Zero is the whole explanation for a
    #: refusal to allocate.
    sales_rows_found: int = 0

    @property
    def row_count(self) -> int:
        return len(self.rows)


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------


def build_tree(session: Session, plan: TargetPlan) -> TreeNode:
    """The organisational tree under a plan, from the master data.

    Read from the dimension tables through the parent codes they already
    carry — ``dim_region.zone_code``, ``dim_area.region_code`` and so on — so
    this is the same hierarchy every report walks, not a second copy of it.
    Retired nodes are excluded from *selection*, which is what ``is_deleted``
    means; a target already allocated to one keeps it.
    """
    root = TreeNode(level=TargetLevel.COMPANY, code=plan.company_code,
                    name=None, parent_level=None, parent_code=None)

    levels: Sequence[tuple[str, Any, str, str, str]] = (
        (TargetLevel.ZONE, DimZone, "zone_code", "zone_name", "sales_line_code"),
        (TargetLevel.REGION, DimRegion, "region_code", "region_name", "zone_code"),
        (TargetLevel.AREA, DimArea, "area_code", "area_name", "region_code"),
        (TargetLevel.UNIT, DimUnit, "unit_code", "unit_name", "area_code"),
        (TargetLevel.TERRITORY, DimTerritory, "territory_code", "territory_name",
         "unit_code"),
        (TargetLevel.SUB_TERRITORY, DimSubTerritory, "sub_territory_code",
         "sub_territory_name", "territory_code"),
    )

    # The zone level hangs off the plan's sales line rather than off the company
    # root: business unit and sales line are fixed by the plan, so they are not
    # levels of the tree — every node beneath shares them.
    parents: dict[str, TreeNode] = {plan.sales_line_code: root}
    previous_level = TargetLevel.COMPANY

    for level, model, code_field, name_field, parent_field in levels:
        rows = session.execute(
            select(model).where(model.is_deleted.is_(False))
        ).scalars().all()
        next_parents: dict[str, TreeNode] = {}
        for row in rows:
            parent_code = getattr(row, parent_field, None)
            parent = parents.get(parent_code)
            if parent is None:
                continue
            node = TreeNode(
                level=level, code=getattr(row, code_field),
                name=getattr(row, name_field, None),
                parent_level=parent.level, parent_code=parent.code,
            )
            parent.children.append(node)
            next_parents[node.code] = node
        parents = next_parents
        previous_level = level
        if not parents:
            # The hierarchy stops here on this data. Returning what exists is
            # right: an allocation to territory level is a real allocation, and
            # inventing sub-territories to reach a deeper leaf would be worse.
            return root

    customers = session.execute(
        select(DimCustomer).where(DimCustomer.is_deleted.is_(False))
    ).scalars().all()
    for customer in customers:
        parent = parents.get(customer.sub_territory_code)
        if parent is None:
            continue
        parent.children.append(TreeNode(
            level=TargetLevel.CUSTOMER, code=customer.customer_code,
            name=customer.customer_name,
            parent_level=previous_level, parent_code=parent.code,
        ))
    return root


# ---------------------------------------------------------------------------
# History at the leaf
# ---------------------------------------------------------------------------


def leaf_history(session: Session, filters: ScopeFilters,
                 basis_years: Sequence[str],
                 config: FinancialYearConfig,
                 ) -> dict[tuple[str, str], list[float]]:
    """``{(customer_code, material_code): [earlier, later]}`` volume.

    One query per basis year, grouped at the finest grain the allocation needs.
    Everything above customer is rolled up from this in memory rather than
    re-queried per level: eight queries per material per level would be
    thousands of round trips for the same numbers.
    """
    table = queries.view(session, queries.SALES_VIEW)
    result: dict[tuple[str, str], list[float]] = {}

    for index, financial_year in enumerate(basis_years):
        date_from, date_to = history_module.year_window(financial_year, config)
        statement = (
            select(table.c.customer_code, table.c.material_code,
                   func.sum(table.c.volume).label("volume"))
            .select_from(table)
            .group_by(table.c.customer_code, table.c.material_code)
        )
        conditions = queries.filter_conditions(table, filters, date_from, date_to)
        if conditions:
            statement = statement.where(and_(*conditions))

        for row in session.execute(statement):
            if not row.customer_code or not row.material_code or row.volume is None:
                continue
            key = (row.customer_code, row.material_code)
            slot = result.setdefault(key, [0.0] * len(basis_years))
            slot[index] = float(row.volume)
    return result


def roll_up(tree: TreeNode, leaf: dict[tuple[str, str], list[float]],
            years: int) -> dict[tuple[str, str, str], list[float]]:
    """Leaf history summed to every ancestor.

    Keyed ``(level, node_code, material_code)``. A parent's history is the sum
    of its children's, computed once here so no level can disagree with the one
    below it — the same reason a parent's *target* is a sum rather than a
    separate figure.
    """
    totals: dict[tuple[str, str, str], list[float]] = {}

    def visit(node: TreeNode) -> dict[str, list[float]]:
        own: dict[str, list[float]] = {}
        if node.level == TargetLevel.CUSTOMER:
            for (customer_code, material_code), volumes in leaf.items():
                if customer_code == node.code:
                    own[material_code] = list(volumes)
        else:
            for child in node.children:
                for material_code, volumes in visit(child).items():
                    slot = own.setdefault(material_code, [0.0] * years)
                    for index, value in enumerate(volumes):
                        slot[index] += value
        for material_code, volumes in own.items():
            totals[(node.level, node.code, material_code)] = volumes
        return own

    visit(tree)
    return totals


# ---------------------------------------------------------------------------
# Planning a run
# ---------------------------------------------------------------------------


def plan_allocation(session: Session, user: UserContext, *, plan: TargetPlan,
                    version: TargetVersion,
                    settings: factors.FactorSettings | None = None,
                    progress: Callable[[str, int, int], None] | None = None,
                    ) -> AllocationPlan:
    """Compute a whole allocation without writing anything.

    Pure with respect to the database — it reads and returns. That is what makes
    the engine testable without a job runner, and what lets the caller decide
    whether to persist, preview or discard the result.
    """
    settings = settings or factors.FactorSettings.defaults()
    config = FinancialYearConfig.from_settings()
    report = progress or (lambda stage, done, total: None)

    lines = country.list_lines(session, version.version_id)
    country_lines = {line.material_code: Decimal(str(line.target_volume))
                     for line in lines if line.target_volume}
    if not country_lines:
        raise NothingToAllocate()

    months = seasonality.plan_months(plan.financial_year, plan.target_period,
                                     config)
    years = history_module.basis_years(plan, config)
    filters = history_module.plan_filters(plan, user, session)

    report(STAGE_KEYS[0], 0, 1)
    leaf = leaf_history(session, filters, years, config)
    has_history = any(any(v > 0 for v in volumes) for volumes in leaf.values())
    sales_rows_found = len(leaf)

    tree = build_tree(session, plan)
    nodes = list(tree.walk())
    customer_count = sum(1 for node in nodes
                         if node.level == TargetLevel.CUSTOMER)

    monthly_totals = _monthly_scope_volume(session, filters, years, config)
    availability = factors.availability(
        settings,
        has_sales_history=has_history,
        has_monthly_history=len([m for m in monthly_totals.values() if m > 0])
        >= seasonality.MIN_MONTHS_FOR_PATTERN,
    )

    level = deepest_level(tree)

    if not has_history:
        # The honest stop. Not an even spread wearing an allocation's clothes.
        return AllocationPlan(
            allocatable=False,
            reason=(
                f"No sales were recorded in {' or '.join(years)} for this "
                f"plan's scope, so there is nothing to allocate by. Load the "
                f"sales history for those years and run the allocation again."
            ),
            rows=[], months=months, materials=sorted(country_lines),
            node_count=len(nodes), customer_count=customer_count,
            seasonality={}, factor_availability=[a.to_dict() for a in availability],
            equal_split_nodes=[], warnings=[],
            country_volume=sum(country_lines.values(), Decimal(0)),
            allocation_level=level, adjustments=[],
            sales_rows_found=sales_rows_found,
        )

    projected = len(country_lines) * len(months) * len(nodes)
    limit = get_settings().target_allocation_max_rows
    if projected > limit:
        # A second gate. The request path checks this first, from master-data
        # counts, so a planner is refused before the version moves; this one
        # catches a tree that turned out larger than the projection, and refuses
        # rather than writing a partial allocation.
        raise AllocationTooLarge({
            "projected_rows": projected, "maximum_rows": limit,
            "exceeds": True, "financial_year": plan.financial_year,
            "target_period": plan.target_period, "months": len(months),
            "materials": len(country_lines), "nodes": len(nodes),
            "customers": customer_count, "allocation_level": level,
            "narrowing_options": [
                "target_period", "company", "business_unit", "sales_line",
                "material_brand", "material",
            ],
        })

    rolled = roll_up(tree, leaf, len(years))
    brands = _material_brands(session, list(country_lines))
    pending = adjustment_service.index(
        adjustment_service.for_version(session, version.version_id)
        if settings.is_on("management_adjustment") else []
    )

    rows: list[AllocatedRow] = []
    shapes: dict[str, dict[str, Any]] = {}
    equal_split: set[str] = set()
    warnings: list[str] = []
    applied: list[dict[str, Any]] = []

    report(STAGE_KEYS[1], 0, len(country_lines))
    for index, (material_code, annual) in enumerate(sorted(country_lines.items())):
        shape = (
            seasonality.for_material(
                session, filters, material_code=material_code,
                material_brand=brands.get(material_code), months=months,
                basis_years=years, config=config)
            if settings.is_on("monthly_seasonality")
            else seasonality.equal(months)
        )
        shapes[material_code] = shape.to_dict()
        if shape.is_fallback:
            warnings.append(
                f"{material_code}: monthly split used the "
                f"{shape.source.lower()} seasonal pattern"
                + ("" if shape.source != seasonality.EQUAL
                   else " — an equal split, because no monthly history exists")
                + "."
            )

        report(STAGE_KEYS[2], index, len(country_lines))
        by_month, _ = rounding.distribute_map(
            annual, {month: shape.weights.get(month, 0.0) for month in months})

        report(STAGE_KEYS[3], index, len(country_lines))
        for month, month_volume in by_month.items():
            rows.append(AllocatedRow(
                level=tree.level, node_code=tree.code, parent_level=None,
                parent_code=None, material_code=material_code,
                target_month=month, volume=month_volume,
            ))
            fraction = (month_volume / annual) if annual else Decimal(0)
            _split_node(tree, material_code, month, month_volume, rolled,
                        settings, rows, equal_split, len(years), pending,
                        applied, fraction)

    report(STAGE_KEYS[4], 1, 1)
    return AllocationPlan(
        allocatable=True, reason=None, rows=rows, months=months,
        materials=sorted(country_lines), node_count=len(nodes),
        customer_count=customer_count, seasonality=shapes,
        factor_availability=[a.to_dict() for a in availability],
        equal_split_nodes=sorted(equal_split), warnings=warnings,
        country_volume=sum(country_lines.values(), Decimal(0)),
        allocation_level=level, adjustments=applied,
        sales_rows_found=sales_rows_found,
    )


def _split_node(node: TreeNode, material_code: str, month: str,
                volume: Decimal,
                rolled: dict[tuple[str, str, str], list[float]],
                settings: factors.FactorSettings,
                rows: list[AllocatedRow], equal_split: set[str],
                years: int,
                pending: dict | None = None,
                applied: list[dict[str, Any]] | None = None,
                month_fraction: Decimal = Decimal(1)) -> None:
    """Distribute one node's volume across its children, then recurse.

    Depth-first, and every level uses the same largest-remainder distributor —
    which is what makes ``parent == sum(children)`` hold at *every* level rather
    than only at the one the caller happened to check.

    A management adjustment is applied **after** the factor split and **within**
    this parent: the named child gains, its siblings give up the same amount pro
    rata, and the parent's total is untouched. So an adjustment changes the
    distribution and never the country target, which is the figure management
    typed and the one every level reconciles against.
    """
    if not node.children:
        return

    child_history = {
        child.code: _years_of(rolled, child.level, child.code, material_code,
                              years)
        for child in node.children
    }
    weights, _ = factors.node_weights(child_history, settings)
    shares, used_equal = rounding.distribute_map(volume, weights)
    if used_equal and volume > 0:
        equal_split.add(f"{node.level}:{node.code}")

    suggested = dict(shares)
    if pending:
        shares = _apply_adjustments(node, material_code, month, shares, pending,
                                    applied, month_fraction)

    for child in node.children:
        share = shares[child.code]
        rows.append(AllocatedRow(
            level=child.level, node_code=child.code,
            parent_level=node.level, parent_code=node.code,
            material_code=material_code, target_month=month, volume=share,
            # The factor split before any adjustment. Equal to ``share``
            # wherever nothing moved, which is every node but the adjusted one.
            system_volume=suggested[child.code],
        ))
        _split_node(child, material_code, month, share, rolled, settings, rows,
                    equal_split, years, pending, applied, month_fraction)


def _apply_adjustments(node: TreeNode, material_code: str, month: str,
                       shares: dict[str, Decimal], pending: dict,
                       applied: list[dict[str, Any]] | None,
                       month_fraction: Decimal) -> dict[str, Decimal]:
    """Apply every adjustment naming a child of this node, one at a time.

    Sequentially rather than all at once, and the order is the stored order:
    each adjustment is funded from the siblings *as they stand after the
    previous one*, so two adjustments under the same parent compose predictably
    instead of both drawing on volume the other has already spent.

    An adjustment its siblings cannot fund raises, and the raise reaches the job
    as a failure rather than being clamped — a clamped adjustment would leave
    the screen showing a Final Target nobody asked for.
    """
    for child in node.children:
        entry = adjustment_service.lookup(pending, child.level, child.code,
                                          material_code)
        if entry is None:
            continue
        # The stored adjustment is a figure for the whole **period**, and this
        # function runs once per month — so it is scaled by this month's share
        # of the year before being applied. Applying it whole in every month
        # would move the period total by twelve times what was asked for, and
        # applying it only in one month would flatten the seasonal profile a
        # management decision has no business reshaping.
        amount = (Decimal(str(entry.adjustment_volume)) * month_fraction
                  ).quantize(rounding.quantum())
        if amount == 0:
            # A month this material never sells in gets no share of the
            # adjustment, so nothing moved. Recording it would put a row on the
            # screen reading "System 0, Adjustment 0, Final 0", which describes
            # nothing that happened.
            continue
        before = shares.get(child.code, Decimal(0))
        shares = adjustment_service.apply_to_siblings(shares, child.code, amount)
        if applied is not None:
            applied.append(adjustment_service.AppliedAdjustment(
                level=child.level, node_code=child.code,
                material_code=entry.material_code,
                system_volume=before, adjustment_volume=amount,
                final_volume=shares[child.code], reason=entry.reason,
                adjusted_by=entry.adjusted_by,
                adjusted_at=(entry.adjusted_at.isoformat()
                             if entry.adjusted_at else None),
            ).to_dict())
    return shares


def deepest_level(tree: TreeNode) -> str:
    """The deepest level the tree actually reaches.

    Not the deepest level the schema *has*. A Customer Master carrying no
    sub-territory mapping produces a tree that stops at sub-territory, and the
    run must be labelled with the level it reached rather than the one it was
    aiming at.
    """
    deepest = tree.level
    for node in tree.walk():
        if TargetLevel.ORDERED.index(node.level) > \
                TargetLevel.ORDERED.index(deepest):
            deepest = node.level
    return deepest


def _years_of(rolled: dict[tuple[str, str, str], list[float]], level: str,
              code: str, material_code: str,
              years: int) -> tuple[float | None, float | None]:
    volumes = rolled.get((level, code, material_code))
    if not volumes:
        return (None, None)
    earlier = volumes[0] if volumes[0] else None
    later = volumes[-1] if volumes[-1] else None
    return (earlier, later)


def _material_brands(session: Session, codes: list[str]) -> dict[str, str]:
    if not codes:
        return {}
    return {
        material.material_code: material.material_brand
        for material in session.execute(
            select(DimMaterial).where(DimMaterial.material_code.in_(codes))
        ).scalars()
    }


def _monthly_scope_volume(session: Session, filters: ScopeFilters,
                          basis_years: Sequence[str],
                          config: FinancialYearConfig) -> dict[int, float]:
    windows = [history_module.year_window(year, config) for year in basis_years]
    return seasonality._monthly_volume(session, filters, windows)


# ---------------------------------------------------------------------------
# Persisting
# ---------------------------------------------------------------------------


def persist(session: Session, *, version: TargetVersion,
            result: AllocationPlan,
            reporter: Callable[[int], None] | None = None) -> int:
    """Replace the version's allocation with this one, in one transaction.

    The previous allocation is **deleted**, not merged. A merge would leave rows
    from a run with different settings sitting beside rows from this one, and no
    reader could tell which rule produced which figure — an allocation is one
    coherent answer or it is nothing. The version has not been approved (a
    frozen version refuses the write), so nothing being deleted is a figure
    anyone has signed off.

    The caller owns the commit, so this and the status change it accompanies
    live or die together.

    **The payload is built one batch at a time, never all at once.** Measured on
    this machine an ``AllocatedRow`` costs ~400 bytes and the dict it becomes
    ~470, so materialising the whole payload before inserting held *both* copies
    of the dataset in memory: at 1.5 million rows that is 1.2 GB, and slicing
    the list per batch copied a third time. Generating each batch on demand
    leaves only the rows the engine already holds, which is what makes a
    full-year run at this size a question of time rather than of memory.

    **Duplicates cannot arise and none are counted.** The version's existing
    rows are deleted above, and ``uq_target_allocation_node`` covers the full
    grain — version, level, node, material, month — so a repeat is refused by
    the database rather than by loading the dataset to look for one.
    """
    session.execute(
        TargetAllocation.__table__.delete().where(
            TargetAllocation.version_id == version.version_id)
    )
    if not result.rows:
        return 0

    def _payload(row: AllocatedRow) -> dict[str, Any]:
        return {
            "version_id": version.version_id,
            "level": row.level,
            "node_code": row.node_code,
            "parent_level": row.parent_level,
            "parent_code": row.parent_code,
            "material_code": row.material_code,
            "target_month": row.target_month,
            "system_volume": row.suggested,
            "current_volume": row.volume,
            "approved_volume": None,
            "status": TargetStatus.ALLOCATED,
        }

    # Chunked against the bind-parameter ceiling the ETL respects for the same
    # reason: SQLite refuses a statement with more than 32,766 of them, and an
    # allocation is exactly the shape that trips it.
    chunk = max(1, 30000 // 10)
    written = 0
    stream = iter(result.rows)
    while True:
        batch = [_payload(row) for row in islice(stream, chunk)]
        if not batch:
            break
        session.execute(TargetAllocation.__table__.insert(), batch)
        written += len(batch)
        if reporter is not None:
            reporter(written)
        # Released before the next batch is built, so peak memory is one batch
        # rather than the whole payload.
        del batch
    session.flush()
    return written


__all__ = [
    "STAGES",
    "STAGE_KEYS",
    "STAGE_LABEL",
    "AllocationTooLarge",
    "NothingToAllocate",
    "TreeNode",
    "AllocatedRow",
    "AllocationPlan",
    "build_tree",
    "deepest_level",
    "leaf_history",
    "roll_up",
    "plan_allocation",
    "persist",
]
