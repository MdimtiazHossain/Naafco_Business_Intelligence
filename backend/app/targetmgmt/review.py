"""The hierarchical review: every node of an allocation, and whether it adds up.

This is the screen a manager opens to judge a target before signing it — Country
to Zone to Region to Area to Unit to Territory to Sub-Territory to Customer, each
row carrying what it was allocated, what it sold last year, what it has sold so
far, and whether its own figure agrees with the sum of its children.

**The tree is read from the stored allocation, not rebuilt from the masters.**
Every allocation row names its parent, and that chain is a *snapshot* of the
hierarchy as it stood when the run happened. Rebuilding from ``dim_region`` and
friends would mean a territory moved to another region next month silently
reshaping a target somebody already approved — which is exactly what storing
``parent_code`` exists to prevent.

**Recon variance is the number this screen is really for.** It is the sum of a
node's children minus the node's own figure, and it is zero at every level by
construction — the allocation engine distributes with largest remainder, so
children cannot come to more or less than their parent. Showing it anyway is the
point: a claim that is checked and displayed is worth more than a claim that is
merely true, and final approval is blocked while any level disagrees.

**Achievement is ``n/a`` before the year has happened, never 0%.** A target for
a financial year with no actual sales yet has no achievement — the ratio cannot
be computed, and a ratio that cannot be computed is never rendered as zero. The
same rule the rest of this platform follows.

**Scope narrows the tree to its roots, and does not narrow the numbers inside
it.** A regional manager sees their region as the top of the tree, with that
region's real figures beneath it. They do *not* see the country node carrying a
narrowed total labelled "Country" — a figure that is neither the country's nor
labelled as anything else is worse than not showing the row, and showing the
country's true total would hand them data outside their scope.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ..ai import queries
from ..ai.permission_filter import UserContext
from ..database.models import (
    DimArea,
    DimCompany,
    DimMaterial,
    DimRegion,
    DimSubTerritory,
    DimTerritory,
    DimUnit,
    DimZone,
)
from ..database.models_target import (
    RevisionStatus,
    TargetAllocation,
    TargetLevel,
    TargetPlan,
    TargetRevision,
)
from ..database.models_warehouse import DimCustomer
from ..etl.calendar import FinancialYearConfig
from . import country, history as history_module, seasonality

#: Which dimension carries each level's display name, and under which column.
#:
#: A node is identified by its code and *labelled* by its name — the same
#: separation ``0024_customer_name_on_views`` made for the customer on a sales
#: report. Nothing joins on the name and nothing may.
NAME_SOURCES: tuple[tuple[str, Any, str, str], ...] = (
    (TargetLevel.COMPANY, DimCompany, "company_code", "company_name"),
    (TargetLevel.ZONE, DimZone, "zone_code", "zone_name"),
    (TargetLevel.REGION, DimRegion, "region_code", "region_name"),
    (TargetLevel.AREA, DimArea, "area_code", "area_name"),
    (TargetLevel.UNIT, DimUnit, "unit_code", "unit_name"),
    (TargetLevel.TERRITORY, DimTerritory, "territory_code", "territory_name"),
    (TargetLevel.SUB_TERRITORY, DimSubTerritory, "sub_territory_code",
     "sub_territory_name"),
    (TargetLevel.CUSTOMER, DimCustomer, "customer_code", "customer_name"),
)


def node_names(session: Session) -> dict[tuple[str, str], str]:
    """``(level, code) -> name`` for every level the tree can reach.

    One query per level rather than a join per row: the tree is a few thousand
    nodes at most, and eight small selects beat eight joins repeated per node.
    Retired records are included — a target allocated to a node that has since
    been retired must still show that node's name, because retiring removes a
    record from *selection*, not from history.
    """
    names: dict[tuple[str, str], str] = {}
    for level, model, code_field, name_field in NAME_SOURCES:
        for code, name in session.execute(
            select(getattr(model, code_field), getattr(model, name_field))
        ):
            if code:
                names[(level, code)] = name
    return names


def _scope_roots(user: UserContext,
                 nodes: dict[tuple[str, str], dict]) -> set[tuple[str, str]]:
    """Where this reader's tree starts.

    Unrestricted readers start at the allocation's own root. A scoped reader
    starts at the deepest nodes their scope names that the allocation actually
    contains — so a regional manager's tree is their region, with their region's
    real figures, rather than a country node showing a total that is neither the
    country's nor theirs.
    """
    if user.is_unrestricted:
        return {key for key, node in nodes.items() if node["parent_code"] is None}

    from ..ai.permission_filter import FILTER_FIELD_BY_LEVEL

    roots: set[tuple[str, str]] = set()
    for scope_level, field in FILTER_FIELD_BY_LEVEL.items():
        codes = user.data_scope.get(scope_level) or []
        # ``region_code`` -> ``region``; the tree's levels drop the suffix.
        level = scope_level[:-5] if scope_level.endswith("_code") else scope_level
        for code in codes:
            if (level, code) in nodes:
                roots.add((level, code))
    return roots


def _descendants(roots: set[tuple[str, str]],
                 children: dict[tuple[str, str], list[tuple[str, str]]],
                 ) -> set[tuple[str, str]]:
    visible: set[tuple[str, str]] = set()
    stack = list(roots)
    while stack:
        key = stack.pop()
        if key in visible:
            continue
        visible.add(key)
        stack.extend(children.get(key, ()))
    return visible


def tree(session: Session, user: UserContext, *, plan: TargetPlan,
         version_id: int, material_code: str | None = None) -> dict[str, Any]:
    """Every visible node of one version's allocation, with its own arithmetic."""
    config = FinancialYearConfig.from_settings()

    conditions = [TargetAllocation.version_id == version_id]
    if material_code:
        conditions.append(TargetAllocation.material_code == material_code)

    rows = session.execute(
        select(TargetAllocation.level, TargetAllocation.node_code,
               TargetAllocation.parent_level, TargetAllocation.parent_code,
               TargetAllocation.material_code, TargetAllocation.status,
               func.sum(TargetAllocation.current_volume).label("volume"),
               func.sum(TargetAllocation.system_volume).label("system_volume"))
        .where(and_(*conditions))
        .group_by(TargetAllocation.level, TargetAllocation.node_code,
                  TargetAllocation.parent_level, TargetAllocation.parent_code,
                  TargetAllocation.material_code, TargetAllocation.status)
    ).all()

    if not rows:
        return {
            "rows": [], "reconciliation": _empty_reconciliation(),
            "scope": _describe_scope(user), "notes": _notes(True, False),
            "materials": [], "material_code": material_code,
            "levels": [],
        }

    #: ``(level, code)`` -> the node, plus its per-material volumes so quantity
    #: and value can be derived material by material rather than from one
    #: conversion factor standing in for all of them.
    nodes: dict[tuple[str, str], dict] = {}
    per_material: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(dict)
    children: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)

    for (level, node_code, parent_level, parent_code, material, status,
         volume, system_volume) in rows:
        key = (level, node_code)
        node = nodes.setdefault(key, {
            "level": level, "node_code": node_code,
            "parent_level": parent_level, "parent_code": parent_code,
            "status": status, "volume": Decimal(0),
            "system_volume": Decimal(0),
        })
        node["volume"] += Decimal(str(volume or 0))
        node["system_volume"] += Decimal(str(system_volume or 0))
        current = per_material[key].get(material, Decimal(0))
        per_material[key][material] = current + Decimal(str(volume or 0))

    for key, node in nodes.items():
        if node["parent_code"]:
            children[(node["parent_level"], node["parent_code"])].append(key)

    visible = _descendants(_scope_roots(user, nodes), children)
    if not visible:
        # A reader whose scope names nothing in this allocation sees an empty
        # tree rather than somebody else's. Not an error: a regional manager
        # looking at a plan for another sales line is a legitimate, empty view.
        return {
            "rows": [], "reconciliation": _empty_reconciliation(),
            "scope": _describe_scope(user), "notes": _notes(False, True),
            "materials": sorted({row[4] for row in rows}),
            "material_code": material_code, "levels": [],
        }

    names = node_names(session)
    factors = _derivation_inputs(session, {row[4] for row in rows})
    py_volume = _roll_up(_previous_year_volume(session, user, plan, config,
                                               nodes, material_code),
                         nodes, children)
    actuals = _roll_up(_actual_volume(session, user, plan, config, nodes,
                                      material_code),
                       nodes, children)
    open_revisions = _open_revisions(session, version_id)

    ordered = _depth_first(nodes, children, _scope_roots(user, nodes))
    out: list[dict[str, Any]] = []
    mismatches = 0

    for depth, key in ordered:
        if key not in visible:
            continue
        node = nodes[key]
        child_keys = [child for child in children.get(key, ())
                      if child in visible]
        child_total = sum((nodes[child]["volume"] for child in child_keys),
                          Decimal(0))
        # Zero at every level by construction; displayed anyway, because a
        # checked claim is worth more than a true one nobody can see.
        variance = child_total - node["volume"] if child_keys else Decimal(0)
        if variance != 0:
            mismatches += 1

        quantity, value, missing = _derive(per_material[key], factors)
        previous = py_volume.get(key)
        actual = actuals.get(key)

        out.append({
            "level": node["level"],
            "node_code": node["node_code"],
            "name": names.get(key),
            "parent_level": node["parent_level"],
            "parent_code": node["parent_code"],
            "depth": depth,
            "has_children": bool(child_keys),
            "target_volume": float(node["volume"]),
            "system_volume": float(node["system_volume"]),
            # The engine's figure and the standing figure differ only where a
            # management adjustment moved one, so this is what the review screen
            # shows as "adjusted".
            "adjusted": node["volume"] != node["system_volume"],
            "target_quantity": quantity,
            "target_value": value,
            "missing_derivation": missing,
            "previous_year_volume": previous,
            "growth_percent": _percent_change(previous, float(node["volume"])),
            "actual_volume": actual,
            "achievement_percent": _achievement(actual, float(node["volume"])),
            "recon_variance": float(variance),
            "pending_revisions": open_revisions.get(key, 0),
            "status": node["status"],
        })

    return {
        "rows": out,
        "reconciliation": {
            "balanced": mismatches == 0,
            "mismatched_nodes": mismatches,
            "node_count": len(out),
            "target_volume": float(sum(
                (nodes[key]["volume"] for key in _scope_roots(user, nodes)
                 if key in visible), Decimal(0))),
        },
        "scope": _describe_scope(user),
        "notes": _notes(False, False),
        "materials": sorted({row[4] for row in rows}),
        "material_code": material_code,
        "levels": [level for level in TargetLevel.ORDERED
                   if any(row["level"] == level for row in out)],
    }


def _depth_first(nodes: dict[tuple[str, str], dict],
                 children: dict[tuple[str, str], list[tuple[str, str]]],
                 roots: set[tuple[str, str]]) -> list[tuple[int, tuple[str, str]]]:
    """The tree flattened in reading order, each row carrying its depth.

    Flattened on the server rather than nested, because the browser draws a
    table: a nested payload would have to be flattened there anyway, and
    depth-plus-order is what an expandable table row actually needs.

    Sorted by volume within each parent, largest first — a manager scanning a
    region reads the territories that matter most at the top.
    """
    out: list[tuple[int, tuple[str, str]]] = []

    def visit(key: tuple[str, str], depth: int) -> None:
        out.append((depth, key))
        for child in sorted(children.get(key, ()),
                            key=lambda k: (-nodes[k]["volume"], k[1])):
            visit(child, depth + 1)

    for root in sorted(roots, key=lambda k: (-nodes[k]["volume"], k[1])):
        visit(root, 0)
    return out


def _derivation_inputs(session: Session,
                       codes: set[str]) -> dict[str, tuple[float | None,
                                                           float | None]]:
    """``material_code -> (conversion factor, transfer price)``."""
    if not codes:
        return {}
    return {
        material.material_code: (
            float(material.conversion_factor)
            if material.conversion_factor is not None else None,
            float(material.transfer_price)
            if material.transfer_price is not None else None,
        )
        for material in session.execute(
            select(DimMaterial).where(
                DimMaterial.material_code.in_([c for c in codes if c]))
        ).scalars()
    }


def _derive(volumes: dict[str, Decimal],
            factors: dict[str, tuple[float | None, float | None]],
            ) -> tuple[float | None, float | None, list[str]]:
    """A node's quantity and value, summed material by material.

    Material by material because a conversion factor differs per material — one
    node's volume spanning three materials has three divisors, and dividing the
    node's total by any one of them would be arithmetic about nothing.

    Suppressed to ``None`` the moment any material on the node cannot be
    derived, for the reason the country total is: a partial sum presented as the
    node's quantity is short by an unknown amount, which is worse than an
    honest ``n/a``.
    """
    quantity = 0.0
    value = 0.0
    missing: list[str] = []
    for material_code, volume in volumes.items():
        conversion, price = factors.get(material_code, (None, None))
        item_quantity, item_value = country.derive(float(volume), conversion,
                                                   price)
        if item_quantity is None or item_value is None:
            missing.append(material_code)
            continue
        quantity += item_quantity
        value += item_value
    if missing:
        return None, None, sorted(missing)
    return quantity, value, []


def _roll_up(direct: dict[tuple[str, str], float],
             nodes: dict[tuple[str, str], dict],
             children: dict[tuple[str, str], list[tuple[str, str]]],
             ) -> dict[tuple[str, str], float]:
    """A parent's sales figure is the sum of its children's.

    Necessary rather than merely tidy. A sales row states a territory and a
    customer but **not** a sub-territory, so the view's ``sub_territory_code``
    is NULL on it and a sub-territory would report no sales while the customers
    beneath it reported plenty — a column that visibly fails to add up, on the
    one screen whose entire purpose is showing that things add up.

    So every node with children takes their sum, and only a leaf keeps the
    figure the view gave it directly. The PY and Achievement columns then
    reconcile exactly as the volume column does, which is what lets a manager
    check a parent against its children by eye.

    A node with no children and no figure of its own stays absent, not zero: it
    sold nothing *recorded*, and ``None`` is what makes growth read ``n/a``
    rather than −100%.
    """
    rolled: dict[tuple[str, str], float] = {}

    def visit(key: tuple[str, str]) -> float | None:
        child_keys = children.get(key, ())
        if not child_keys:
            value = direct.get(key)
            if value is not None:
                rolled[key] = value
            return value

        total = 0.0
        seen = False
        for child in child_keys:
            child_value = visit(child)
            if child_value is not None:
                total += child_value
                seen = True
        if seen:
            rolled[key] = total
            return total
        # No child recorded anything. Fall back to whatever the view knew about
        # this node itself, which is right for a level the sales data *does*
        # state — a region with sales that never reached a customer row.
        value = direct.get(key)
        if value is not None:
            rolled[key] = value
        return value

    for key, node in nodes.items():
        if node["parent_code"] is None:
            visit(key)
    return rolled


def _sales_volume(session: Session, user: UserContext, plan: TargetPlan,
                  window: tuple, nodes: dict[tuple[str, str], dict],
                  material_code: str | None) -> dict[tuple[str, str], float]:
    """Sales volume per node over one window, at every level in the tree.

    Read from ``vw_sales_detail`` through the same scope layer as every other
    report, then bucketed by each organisational column the view carries — so a
    region's figure here and a region's figure on the Sales page are the same
    number produced the same way.
    """
    table = queries.view(session, queries.SALES_VIEW)
    filters = history_module.plan_filters(plan, user, session)
    date_from, date_to = window

    columns = [(level, TargetLevel.CODE_FIELD[level])
               for level in TargetLevel.ORDERED
               if TargetLevel.CODE_FIELD[level] in table.c]
    if not columns:
        return {}

    statement = select(
        *[table.c[field] for _, field in columns],
        func.sum(table.c.volume).label("volume"),
    ).select_from(table).group_by(*[table.c[field] for _, field in columns])

    conditions = queries.filter_conditions(table, filters, date_from, date_to)
    if material_code and "material_code" in table.c:
        conditions.append(table.c.material_code == material_code)
    if conditions:
        statement = statement.where(and_(*conditions))

    totals: dict[tuple[str, str], float] = defaultdict(float)
    for row in session.execute(statement):
        volume = float(row.volume or 0)
        if not volume:
            continue
        for index, (level, _) in enumerate(columns):
            code = row[index]
            if code and (level, code) in nodes:
                totals[(level, code)] += volume
    return dict(totals)


def _previous_year_volume(session: Session, user: UserContext,
                          plan: TargetPlan, config: FinancialYearConfig,
                          nodes: dict[tuple[str, str], dict],
                          material_code: str | None,
                          ) -> dict[tuple[str, str], float]:
    """Last year's actual volume per node — the growth comparison's base."""
    years = history_module.basis_years(plan, config)
    if not years:
        return {}
    return _sales_volume(session, user, plan,
                         history_module.year_window(years[-1], config),
                         nodes, material_code)


def _actual_volume(session: Session, user: UserContext, plan: TargetPlan,
                   config: FinancialYearConfig,
                   nodes: dict[tuple[str, str], dict],
                   material_code: str | None) -> dict[tuple[str, str], float]:
    """Actual volume so far in the plan's *own* period.

    Empty for a future financial year, which is the ordinary case for a target
    being reviewed — and is why achievement reads ``n/a`` rather than 0%.
    """
    months = seasonality.plan_months(plan.financial_year, plan.target_period,
                                     config)
    if not months:
        return {}
    import datetime as dt

    first = dt.date(int(months[0][:4]), int(months[0][-2:]), 1)
    last_year, last_month = int(months[-1][:4]), int(months[-1][-2:])
    if last_month == 12:
        last = dt.date(last_year, 12, 31)
    else:
        last = dt.date(last_year, last_month + 1, 1) - dt.timedelta(days=1)
    return _sales_volume(session, user, plan, (first, last), nodes,
                         material_code)


def _open_revisions(session: Session,
                    version_id: int) -> dict[tuple[str, str], int]:
    """How many revision requests are still outstanding at each node.

    Final approval is blocked while any remain, so this is the column a manager
    scans before signing — and it is counted from ``target_revision`` rather
    than inferred from a status, because a node can carry an open request while
    its own figure looks settled.
    """
    rows = session.execute(
        select(TargetAllocation.level, TargetAllocation.node_code,
               func.count(TargetRevision.revision_id))
        .select_from(TargetRevision)
        .join(TargetAllocation,
              TargetAllocation.allocation_id == TargetRevision.allocation_id)
        .where(TargetAllocation.version_id == version_id,
               TargetRevision.status.in_(RevisionStatus.OPEN))
        .group_by(TargetAllocation.level, TargetAllocation.node_code)
    ).all()
    return {(level, code): int(count) for level, code, count in rows}


def _percent_change(before: float | None, after: float) -> float | None:
    """Growth against last year, or ``None`` where it cannot be computed.

    ``None`` for a node with no sales last year: growth from nothing is
    undefined, not infinite, and the screen renders ``n/a``.
    """
    if before is None or before == 0:
        return None
    return ((after - before) / before) * 100


def _achievement(actual: float | None, target: float) -> float | None:
    """Actual over target, or ``None`` before the year has happened.

    The design's own footnote, enforced: a target for a financial year with no
    actual sales yet has no achievement, and a ratio that cannot be computed is
    never shown as zero.
    """
    if actual is None or target == 0:
        return None
    return (actual / target) * 100


def _describe_scope(user: UserContext) -> str:
    """How this reader's scope narrowed the tree, in words.

    Three cases, not two. A restricted account with **no** scope at all sees
    nothing — the rule ``PermissionFilter.has_any_scope`` states — and it
    deserves its own sentence: ``describe_scope`` renders that state as "no data
    scope", which read back as "Scoped to no data scope by your role" and told a
    reader neither what was wrong nor who could put it right.
    """
    if user.is_unrestricted:
        return "Full country scope"
    if not user.data_scope:
        return ("Your account has no data scope, so no part of this target is "
                "visible to you. An administrator grants one.")
    return f"Scoped to {user.describe_scope()} by your role"


def _empty_reconciliation() -> dict[str, Any]:
    return {"balanced": True, "mismatched_nodes": 0, "node_count": 0,
            "target_volume": 0.0}


def _notes(no_allocation: bool, out_of_scope: bool) -> list[str]:
    if no_allocation:
        return [
            "This version has not been allocated yet, so there is no tree to "
            "review. Generate an allocation first."
        ]
    if out_of_scope:
        return [
            "This allocation contains no node inside your data scope. Nothing "
            "is hidden from the totals — there is simply nothing here for you "
            "to review."
        ]
    return [
        "Recon variance is the child total minus the node's own target. It is "
        "zero at every level, so each parent reconciles with its children.",
        "Achievement reads n/a rather than 0% where the period has no actual "
        "sales yet — a ratio that cannot be computed is never shown as zero.",
    ]


__all__ = ["NAME_SOURCES", "node_names", "tree"]
