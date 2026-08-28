"""Reconciliation: proving a parent equals the sum of its children.

The allocation engine *constructs* balanced figures — every split goes through
largest-remainder distribution, so children sum to their parent by arithmetic
rather than by hope. This module exists because "by construction" is a claim,
and a claim about money the whole sales force is measured on deserves to be
checked against what was actually stored.

**Zero tolerance, and that is affordable because nothing here is a float.**
Volumes are ``NUMERIC(18, 4)`` and are read back as ``Decimal``, so an exact
comparison is meaningful. A tolerance would hide precisely the bug this is for:
a distribution that lost a unit would sit inside any epsilon anyone would pick,
and the error compounds every time the allocation is re-run.

Four identities are checked, and together they cover the whole tree:

* the country total equals the sum of its monthly totals, per material;
* every parent node equals the sum of its children, at every level and for every
  material and month;
* every *terminal* node together equals the country total — terminal rather
  than "deepest level", because a real hierarchy is ragged and volume that
  comes to rest at the bottom of a short branch is as final as volume that
  reaches a customer;
* the allocated country total equals the **typed** country target — which is the
  one identity the engine cannot guarantee by construction, because it spans the
  boundary between what a person entered and what the engine produced.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database.models_target import TargetAllocation, TargetLevel


@dataclass
class Mismatch:
    """One place the numbers do not agree, described so it can be found."""

    kind: str
    level: str | None
    node_code: str | None
    material_code: str | None
    target_month: str | None
    expected: Decimal
    actual: Decimal

    @property
    def difference(self) -> Decimal:
        return self.actual - self.expected

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "level": self.level,
            "node_code": self.node_code,
            "material_code": self.material_code,
            "target_month": self.target_month,
            "expected": str(self.expected),
            "actual": str(self.actual),
            "difference": str(self.difference),
        }


@dataclass
class Reconciliation:
    """The verdict, and everything the screen's reconciliation bar needs."""

    balanced: bool
    country_target_volume: Decimal
    allocated_volume: Decimal
    mismatches: list[Mismatch] = field(default_factory=list)
    levels_checked: list[str] = field(default_factory=list)
    node_count: int = 0

    @property
    def difference(self) -> Decimal:
        return self.allocated_volume - self.country_target_volume

    @property
    def allocation_percent(self) -> float | None:
        """Allocated as a percentage of the target, or ``None`` at zero target.

        ``None`` rather than 0 or 100: a percentage of nothing is undefined, and
        the screen renders ``n/a`` — the same rule every other ratio in this
        system follows.
        """
        if self.country_target_volume == 0:
            return None
        return float(self.allocated_volume / self.country_target_volume * 100)

    def to_dict(self) -> dict[str, Any]:
        return {
            "balanced": self.balanced,
            "country_target_volume": str(self.country_target_volume),
            "allocated_volume": str(self.allocated_volume),
            "difference": str(self.difference),
            "allocation_percent": self.allocation_percent,
            "mismatches": [m.to_dict() for m in self.mismatches[:50]],
            "mismatch_count": len(self.mismatches),
            "levels_checked": self.levels_checked,
            "node_count": self.node_count,
        }


def check(session: Session, *, version_id: int,
          country_target: dict[str, Decimal],
          limit: int = 200) -> Reconciliation:
    """Read the stored allocation back and verify every identity.

    Reads rather than trusting the in-memory plan, deliberately: the point is to
    confirm what the *database* holds, which is what every report downstream
    will read. A check against the engine's own output would only prove the
    engine agrees with itself.

    ``limit`` caps how many mismatches are collected. One is already a failure;
    the cap keeps a systematically broken run from building a list as long as
    the allocation.
    """
    rows = session.execute(
        select(TargetAllocation.level, TargetAllocation.node_code,
               TargetAllocation.parent_level, TargetAllocation.parent_code,
               TargetAllocation.material_code, TargetAllocation.target_month,
               # ``current_volume``, not ``system_volume``: reconciliation is
               # about what the target *is*, and a management adjustment moves
               # the former deliberately. Checking the engine's pre-adjustment
               # suggestion would report every adjusted allocation as broken.
               TargetAllocation.current_volume)
        .where(TargetAllocation.version_id == version_id)
    ).all()

    target_total = sum(country_target.values(), Decimal(0))
    if not rows:
        return Reconciliation(
            balanced=not target_total,
            country_target_volume=target_total,
            allocated_volume=Decimal(0),
        )

    #: ``(level, node, material, month) -> volume`` and the children of each.
    own: dict[tuple[str, str, str, str], Decimal] = {}
    children: dict[tuple[str, str, str, str], Decimal] = defaultdict(Decimal)
    levels: set[str] = set()
    nodes: set[tuple[str, str]] = set()

    for (level, node_code, parent_level, parent_code, material_code,
         target_month, volume) in rows:
        value = Decimal(str(volume or 0))
        own[(level, node_code, material_code, target_month)] = value
        levels.add(level)
        nodes.add((level, node_code))
        if parent_level and parent_code:
            children[(parent_level, parent_code, material_code,
                      target_month)] += value

    mismatches: list[Mismatch] = []

    # 1. Every parent equals the sum of its children, per material and month.
    for key, child_total in children.items():
        level, node_code, material_code, target_month = key
        parent_volume = own.get(key)
        if parent_volume is None:
            mismatches.append(Mismatch(
                kind="ORPHANED_CHILDREN", level=level, node_code=node_code,
                material_code=material_code, target_month=target_month,
                expected=child_total, actual=Decimal(0)))
        elif parent_volume != child_total:
            mismatches.append(Mismatch(
                kind="PARENT_CHILD", level=level, node_code=node_code,
                material_code=material_code, target_month=target_month,
                expected=parent_volume, actual=child_total))
        if len(mismatches) >= limit:
            break

    # 2. The country total for each material equals the sum of its months, and
    #    equals what was typed. This is the identity that spans the boundary
    #    between a person's entry and the engine's output.
    country_rows: dict[str, Decimal] = defaultdict(Decimal)
    for (level, node_code, material_code, target_month), value in own.items():
        if level == TargetLevel.COMPANY:
            country_rows[material_code] += value

    for material_code, typed in country_target.items():
        allocated = country_rows.get(material_code, Decimal(0))
        if allocated != typed and len(mismatches) < limit:
            mismatches.append(Mismatch(
                kind="COUNTRY_TARGET", level=TargetLevel.COMPANY,
                node_code=None, material_code=material_code,
                target_month=None, expected=typed, actual=allocated))

    # 3. Every *terminal* node together must still add to the country total,
    #    which catches a break the parent-child walk cannot: a whole subtree
    #    missing from the allocation has no parent row to disagree with.
    #
    #    Terminal, not "deepest level". A real hierarchy is ragged — one region
    #    reaches a customer while another stops at area because no unit has been
    #    created under it — so the volume that comes to rest at the bottom of a
    #    short branch is just as final as the volume that reaches a customer.
    #    Comparing a *level* total instead would report every ragged hierarchy
    #    as broken, which is most of them.
    parents = {(level, code) for (level, code, _, _) in children}
    terminal_total = sum(
        (value for (level, code, _, _), value in own.items()
         if (level, code) not in parents),
        Decimal(0),
    )
    allocated_total = sum(country_rows.values(), Decimal(0))
    has_children = bool(parents)
    if has_children and terminal_total != allocated_total:
        mismatches.append(Mismatch(
            kind="TERMINAL_TOTAL", level=_deepest_level(levels), node_code=None,
            material_code=None, target_month=None,
            expected=allocated_total, actual=terminal_total))

    return Reconciliation(
        balanced=not mismatches,
        country_target_volume=target_total,
        allocated_volume=allocated_total,
        mismatches=mismatches,
        levels_checked=[level for level in TargetLevel.ORDERED if level in levels],
        node_count=len(nodes),
    )


def _deepest_level(levels: Iterable[str]) -> str:
    """The deepest level actually present, which is not always customer.

    A deployment whose Customer Master has not arrived allocates to
    sub-territory, and that is a complete allocation for the hierarchy it has —
    so the leaf identity is checked against the deepest level that exists rather
    than against a level the data may not reach.
    """
    present = [level for level in TargetLevel.ORDERED if level in set(levels)]
    return present[-1] if present else TargetLevel.COMPANY


__all__ = ["Mismatch", "Reconciliation", "check"]
