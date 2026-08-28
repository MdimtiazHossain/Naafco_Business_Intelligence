"""Comparing two versions of a plan: what moved between them, and where.

A version chain answers "what is the target now". This answers the question a
reader actually brings to a chain of three versions: *what changed, and who does
it affect*. It is read-only and stores nothing.

**Only two versions of the same plan are compared.** Comparing V1 of one plan
with V1 of another looks superficially useful and is not: two plans have
different scopes, different materials and different hierarchies, so a "delta"
between them would be arithmetic on two things that were never the same
quantity. The refusal names the two plans rather than producing a table of
apparent movements that are really just two unrelated targets side by side.

**A node in one version and not the other is reported as such, never as zero.**
A customer the newer allocation does not reach has not had its target cut to
nothing — it has no target in that version, and the difference between those two
statements is the whole reason this platform distinguishes absent from zero.
``ADDED`` and ``REMOVED`` are their own statuses and their change percentage is
``null``, because a percentage against nothing is undefined.

**Percent change is suppressed against a zero base.** The same rule every ratio
in this platform follows: a denominator of zero renders ``n/a``, never ``0%``
and never ``∞``.

**The tree is the newer version's shape, with the older one read into it.** Two
versions can have different hierarchies — a re-run after a customer mapping
arrives reaches a level the first did not — and the newer allocation is what
somebody is about to act on, so it is the one whose rows are listed. Nodes the
older version had and the newer does not are appended as ``REMOVED`` rather than
dropped, which is what stops a comparison quietly under-reporting the change.

**Scope narrows where the comparison starts, not what the numbers say** — the
same rule :mod:`review` follows, and for the same reason: a regional manager
sees their region's real movements rather than a country row carrying a narrowed
total labelled "Country".
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..database.models_target import (
    TargetAllocation,
    TargetCountryLine,
    TargetLevel,
    TargetPlan,
    TargetVersion,
)
from . import plans as plan_service, review
from .errors import TargetManagementError

#: What happened to a node between the two versions.
UNCHANGED = "UNCHANGED"
INCREASED = "INCREASED"
DECREASED = "DECREASED"
ADDED = "ADDED"
REMOVED = "REMOVED"

STATUSES: tuple[str, ...] = (UNCHANGED, INCREASED, DECREASED, ADDED, REMOVED)


class CrossPlanComparison(TargetManagementError):
    """Two versions of different plans. Refused rather than approximated.

    Two plans have different scopes, materials and hierarchies, so a difference
    between them is not a movement in one target — it is two unrelated targets
    subtracted from each other, which is a number with no meaning attached to
    it.
    """

    code = "TARGET_COMPARE_CROSS_PLAN"

    def __init__(self, left: str, right: str) -> None:
        super().__init__(
            f"{left} and {right} are different plans",
            user_message=(
                f"{left} and {right} are different plans, with different scopes "
                f"and material lists. A comparison only means something between "
                f"two versions of the same plan."
            ),
            details={"left": left, "right": right},
        )


class NothingToCompare(TargetManagementError):
    """One or both versions have no allocation."""

    code = "TARGET_COMPARE_NO_ALLOCATION"

    def __init__(self, label: str) -> None:
        super().__init__(
            f"{label} has no allocation",
            user_message=(
                f"{label} has not been allocated, so there is nothing to compare "
                f"it against. Run the allocation first."
            ),
            details={"version": label},
        )


def _totals(session: Session, version_id: int,
            material_code: str | None) -> dict[tuple[str, str], Decimal]:
    """``(level, code) -> volume`` for one version, summed over month and material."""
    conditions = [TargetAllocation.version_id == version_id]
    if material_code:
        conditions.append(TargetAllocation.material_code == material_code)
    rows = session.execute(
        select(TargetAllocation.level, TargetAllocation.node_code,
               func.sum(TargetAllocation.current_volume))
        .where(*conditions)
        .group_by(TargetAllocation.level, TargetAllocation.node_code)
    ).all()
    return {(level, code): Decimal(str(total or 0))
            for level, code, total in rows}


def _shape(session: Session, version_id: int,
           material_code: str | None) -> list[TargetAllocation]:
    """One representative row per node, in the order the tree reads.

    The shape is taken from the allocation's own stored parents, exactly as the
    review tree takes it, so a comparison and a review of the same version
    cannot disagree about where a node sits.
    """
    conditions = [TargetAllocation.version_id == version_id]
    if material_code:
        conditions.append(TargetAllocation.material_code == material_code)
    rows = session.execute(
        select(TargetAllocation).where(*conditions)
        .order_by(TargetAllocation.allocation_id)
    ).scalars().all()
    seen: set[tuple[str, str]] = set()
    shape: list[TargetAllocation] = []
    for row in rows:
        key = (row.level, row.node_code)
        if key not in seen:
            seen.add(key)
            shape.append(row)
    return shape


def _percent(before: Decimal, after: Decimal) -> float | None:
    """Change as a percentage of the older figure, or ``None`` at a zero base.

    Undefined rather than infinite: a target that was nothing and is now
    something has not grown by any percentage, and rendering one would invent a
    measurement.
    """
    if before == 0:
        return None
    return float((after - before) / before * 100)


def _status(before: Decimal | None, after: Decimal | None) -> str:
    if before is None:
        return ADDED
    if after is None:
        return REMOVED
    if after > before:
        return INCREASED
    if after < before:
        return DECREASED
    return UNCHANGED


def compare(session: Session, user: UserContext, *, plan: TargetPlan,
            base_version_id: int, version_id: int,
            material_code: str | None = None) -> dict[str, Any]:
    """What moved between two versions of one plan.

    ``base_version_id`` is the older side and ``version_id`` the newer; the
    caller chooses, and the response names both so a reversed pair reads as a
    reversed pair rather than as a sign error.
    """
    base = plan_service.get_version(session, base_version_id)
    head = plan_service.get_version(session, version_id)
    if base.plan_id != head.plan_id or base.plan_id != plan.plan_id:
        raise CrossPlanComparison(f"V{base.version_no}", f"V{head.version_no}")

    before = _totals(session, base_version_id, material_code)
    after = _totals(session, version_id, material_code)
    if not before:
        raise NothingToCompare(f"Version V{base.version_no}")
    if not after:
        raise NothingToCompare(f"Version V{head.version_no}")

    names = review.node_names(session)
    shape = _shape(session, version_id, material_code)
    nodes = {(row.level, row.node_code): row for row in shape}

    # Nodes the older version had and the newer does not are appended rather
    # than dropped: a comparison that silently omitted them would under-report
    # the change by exactly the volume that went missing.
    dropped = [key for key in before if key not in nodes]

    visible = _visible(user, nodes, before, after)
    rows: list[dict[str, Any]] = []
    for row in shape:
        key = (row.level, row.node_code)
        if key not in visible:
            continue
        rows.append(_row(key, names, before.get(key), after.get(key),
                         parent_level=row.parent_level,
                         parent_code=row.parent_code, depth=None))
    for key in sorted(dropped):
        if key not in visible:
            continue
        rows.append(_row(key, names, before.get(key), None,
                         parent_level=None, parent_code=None, depth=None))

    _depths(rows)
    return {
        "base_version": plan_service.version_to_dict(base),
        "version": plan_service.version_to_dict(head),
        "rows": rows,
        "totals": _summary(before, after, visible),
        "country": _country_lines(session, base_version_id, version_id),
        "materials": _materials(session, version_id),
        "material_code": material_code,
        "scope": review._describe_scope(user),
        "notes": _notes(dropped, visible),
        "statuses": list(STATUSES),
    }


def _visible(user: UserContext, nodes, before, after) -> set[tuple[str, str]]:
    """Which nodes this reader may see, by the review tree's own rule.

    Delegated rather than reimplemented: two screens disagreeing about what a
    regional manager can see would be worse than either rule on its own.
    """
    every_key = set(before) | set(after)
    if user.is_unrestricted:
        return every_key

    children: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for key, row in nodes.items():
        if row.parent_code:
            children.setdefault((row.parent_level, row.parent_code), []).append(key)

    roots = review._scope_roots(user, {
        key: {"parent_code": (row.parent_code if key in nodes else None)}
        for key, row in nodes.items()
    })
    if not roots:
        return set()
    return review._descendants(roots, children) & every_key


def _row(key, names, before: Decimal | None, after: Decimal | None, *,
         parent_level, parent_code, depth) -> dict[str, Any]:
    level, code = key
    status = _status(before, after)
    change = (None if before is None or after is None
              else float(after - before))
    return {
        "level": level,
        "node_code": code,
        "name": names.get(key),
        "parent_level": parent_level,
        "parent_code": parent_code,
        "depth": depth,
        "base_volume": None if before is None else float(before),
        "volume": None if after is None else float(after),
        "change": change,
        "change_percent": (None if before is None or after is None
                           else _percent(before, after)),
        "status": status,
    }


def _depths(rows: list[dict[str, Any]]) -> None:
    """Fill in each row's depth from its parent chain, in one pass.

    The rows arrive in the allocation's own order, so a parent is always seen
    before its children and a single pass suffices. A row whose parent is not
    visible — a scoped reader's root, or a removed node — sits at depth 0, which
    is what makes a narrowed tree read as a tree rather than as an indented
    fragment of somebody else's.
    """
    depth_by_key: dict[tuple[str, str], int] = {}
    for row in rows:
        parent = (row["parent_level"], row["parent_code"])
        depth = depth_by_key.get(parent)
        row["depth"] = 0 if depth is None else depth + 1
        depth_by_key[(row["level"], row["node_code"])] = row["depth"]


def _summary(before, after, visible) -> dict[str, Any]:
    """The headline: two totals, their difference, and how many nodes moved.

    Computed over the *visible* nodes at the shallowest level present, not by
    summing every row — summing a tree adds each figure once per level it
    appears at, which would report a movement several times over.
    """
    keys = {key for key in visible}
    if not keys:
        return {"base_volume": None, "volume": None, "change": None,
                "change_percent": None, "nodes_changed": 0, "nodes": 0}

    shallowest = min(TargetLevel.ORDERED.index(level) for level, _ in keys)
    roots = {key for key in keys
             if TargetLevel.ORDERED.index(key[0]) == shallowest}
    base_total = sum((before.get(key, Decimal(0)) for key in roots), Decimal(0))
    head_total = sum((after.get(key, Decimal(0)) for key in roots), Decimal(0))
    changed = sum(1 for key in keys
                  if before.get(key) != after.get(key))
    return {
        "base_volume": float(base_total),
        "volume": float(head_total),
        "change": float(head_total - base_total),
        "change_percent": _percent(base_total, head_total),
        "nodes_changed": changed,
        "nodes": len(keys),
    }


def _country_lines(session: Session, base_version_id: int,
                   version_id: int) -> list[dict[str, Any]]:
    """The typed country volumes on both sides, per material.

    Included because it is the figure a person *entered*, and a movement in the
    allocation is explained first by asking whether the country target itself
    moved. Volume only: quantity and value are derived at read time and would be
    a second, weaker copy of what the country screen already reports.
    """
    def lines(version: int) -> dict[str, Decimal]:
        return {
            code: Decimal(str(volume or 0)) for code, volume in session.execute(
                select(TargetCountryLine.material_code,
                       TargetCountryLine.target_volume)
                .where(TargetCountryLine.version_id == version))
        }

    before, after = lines(base_version_id), lines(version_id)
    out: list[dict[str, Any]] = []
    for material in sorted(set(before) | set(after)):
        left, right = before.get(material), after.get(material)
        out.append({
            "material_code": material,
            "base_volume": None if left is None else float(left),
            "volume": None if right is None else float(right),
            "change": (None if left is None or right is None
                       else float(right - left)),
            "change_percent": (None if left is None or right is None
                               else _percent(left, right)),
            "status": _status(left, right),
        })
    return out


def _materials(session: Session, version_id: int) -> list[str]:
    return sorted({
        code for (code,) in session.execute(
            select(TargetAllocation.material_code)
            .where(TargetAllocation.version_id == version_id)
            .distinct())
    })


def _notes(dropped: list, visible: set) -> list[str]:
    notes: list[str] = []
    if dropped:
        notes.append(
            f"{len(dropped)} node{'s' if len(dropped) != 1 else ''} had a target "
            f"in the earlier version and none in this one. They are listed as "
            f"removed rather than as zero — no target and a target of nothing "
            f"are different statements.")
    if not visible:
        notes.append(
            "Nothing in either version falls inside the part of the business "
            "you hold, so there is nothing here to compare.")
    notes.append(
        "A change is shown against the earlier version. A percentage is left "
        "blank where the earlier figure was zero or absent — a ratio against "
        "nothing is undefined.")
    return notes


def options(session: Session, plan_id: int) -> list[dict[str, Any]]:
    """The versions of one plan that can be compared, newest first.

    A version with no allocation is offered and marked, rather than hidden: a
    reader looking for V2 and not finding it would assume the list was broken,
    where a disabled entry saying "not allocated" answers them.
    """
    rows = session.execute(
        select(TargetVersion).where(TargetVersion.plan_id == plan_id)
        .order_by(TargetVersion.version_no.desc())
    ).scalars().all()
    counts = {
        version_id: count for version_id, count in session.execute(
            select(TargetAllocation.version_id,
                   func.count(TargetAllocation.allocation_id))
            .where(TargetAllocation.version_id.in_(
                [row.version_id for row in rows] or [0]))
            .group_by(TargetAllocation.version_id))
    }
    return [
        {**plan_service.version_to_dict(row),
         "has_allocation": counts.get(row.version_id, 0) > 0}
        for row in rows
    ]


__all__ = [
    "STATUSES",
    "UNCHANGED",
    "INCREASED",
    "DECREASED",
    "ADDED",
    "REMOVED",
    "CrossPlanComparison",
    "NothingToCompare",
    "compare",
    "options",
]
