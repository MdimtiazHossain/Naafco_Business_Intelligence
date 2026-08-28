"""Management adjustment: an absolute change to one node's allocated volume.

**Node-level and absolute, never a global percentage.** A uniform adjustment
applied to every child and then re-normalised is a mathematical no-op — scale
everything by 1.1, divide by the new sum, and you have exactly the distribution
you started with. A slider that does that is worse than no slider, because it
looks as though it did something. So an adjustment names a node and states a
volume:

    System Suggested   10,000 KG
    Adjustment           +500 KG
    Final Target       10,500 KG

**Where the 500 comes from, and why that is the honest answer.** The node's
siblings give it up, pro rata. Management typed a country target; that number is
the plan, and an adjustment is a statement about *distribution* rather than a
way to quietly change the total. So a parent's volume never moves, the country
total stays exactly what was entered, and reconciliation still holds at every
level — which is the promise the whole module rests on. Raising the country
figure itself is a country-target edit followed by a re-allocation, and that is
the right way to say "we are aiming higher", because it goes through the audit
trail and the approval chain like every other change to the plan.

**An adjustment its siblings cannot fund is refused.** If a node's siblings hold
300 between them and somebody asks for +500, there is no arithmetic that
satisfies it without inventing volume or driving a sibling negative. The refusal
names the shortfall rather than clamping silently, because a clamped adjustment
would leave the screen showing a Final Target nobody asked for.

**Adjustments are inputs, not edits.** They are stored against the version and
re-applied on every allocation run, so re-running the engine after loading more
sales does not silently discard management's decisions. Each carries its reason,
its author and its timestamp, which is what makes the Adjustment column on the
review screen answerable to "who decided this, and why".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..database.models_target import (
    TargetAdjustment,
    TargetLevel,
    TargetPlan,
    TargetVersion,
)
from . import audit as target_audit, rounding
from .errors import TargetManagementError
from .plans import assert_editable


class AdjustmentUnfundable(TargetManagementError):
    """The node's siblings cannot give up what the adjustment asks for."""

    code = "TARGET_ADJUSTMENT_UNFUNDABLE"

    def __init__(self, node_code: str, requested: Decimal,
                 available: Decimal) -> None:
        super().__init__(
            f"adjustment of {requested} for {node_code} exceeds {available}",
            user_message=(
                f"An adjustment of {requested:+,} for {node_code} cannot be "
                f"funded: its siblings hold {available:,} between them. Raise "
                f"the country target instead, or reduce the adjustment."
            ),
            details={"node_code": node_code, "requested": str(requested),
                     "available": str(available)},
        )


class AdjustmentBelowZero(TargetManagementError):
    """A negative adjustment larger than the node's own volume."""

    code = "TARGET_ADJUSTMENT_BELOW_ZERO"

    def __init__(self, node_code: str, current: Decimal,
                 requested: Decimal) -> None:
        super().__init__(
            f"adjustment {requested} would take {node_code} below zero",
            user_message=(
                f"{node_code} is allocated {current:,}, so an adjustment of "
                f"{requested:+,} would take it below zero. A target of zero is "
                f"a real instruction; a negative one is not."
            ),
            details={"node_code": node_code, "current": str(current)},
        )


class UnknownAdjustmentLevel(TargetManagementError):
    """An adjustment against a level the allocation does not have."""

    code = "TARGET_ADJUSTMENT_LEVEL_UNKNOWN"

    def __init__(self, level: str) -> None:
        super().__init__(
            f"unknown adjustment level {level}",
            user_message=(
                f"{level!r} is not a level a target is allocated across. "
                f"Adjustments may be made at: "
                f"{', '.join(TargetLevel.ORDERED[1:])}."
            ),
        )


@dataclass(frozen=True)
class AppliedAdjustment:
    """One adjustment and what it did, for the screen and the audit trail."""

    level: str
    node_code: str
    material_code: str | None
    system_volume: Decimal
    adjustment_volume: Decimal
    final_volume: Decimal
    reason: str
    adjusted_by: str | None
    adjusted_at: str | None

    @property
    def adjustment_percent(self) -> float | None:
        """The change as a percentage of the system figure.

        ``None`` against a system volume of zero: a percentage increase from
        nothing is undefined rather than infinite, and the screen renders
        ``n/a`` — the same rule every other ratio here follows.
        """
        if self.system_volume == 0:
            return None
        return float(self.adjustment_volume / self.system_volume * 100)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "node_code": self.node_code,
            "material_code": self.material_code,
            "system_volume": str(self.system_volume),
            "adjustment_volume": str(self.adjustment_volume),
            "final_volume": str(self.final_volume),
            "adjustment_percent": self.adjustment_percent,
            "reason": self.reason,
            "adjusted_by": self.adjusted_by,
            "adjusted_at": self.adjusted_at,
        }


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def record(session: Session, user: UserContext, *, version: TargetVersion,
           plan: TargetPlan, level: str, node_code: str,
           adjustment_volume: Decimal | float | str,
           reason: str, material_code: str | None = None) -> TargetAdjustment:
    """Store an adjustment for the next allocation run to apply.

    Stored rather than applied in place, and that is the design: an adjustment
    is an *input* to the engine, so re-running after loading more sales keeps
    management's decisions instead of quietly discarding them.
    """
    assert_editable(version)
    if level not in TargetLevel.ORDERED or level == TargetLevel.COMPANY:
        # The company root is excluded deliberately: adjusting it would be
        # changing the country target, which is a different act with its own
        # screen, its own audit action and its own approval path.
        raise UnknownAdjustmentLevel(level)
    if not (reason or "").strip():
        from .errors import ReasonRequired

        raise ReasonRequired()

    amount = Decimal(str(adjustment_volume))
    existing = session.execute(
        select(TargetAdjustment).where(
            TargetAdjustment.version_id == version.version_id,
            TargetAdjustment.level == level,
            TargetAdjustment.node_code == node_code,
            TargetAdjustment.material_code.is_(material_code)
            if material_code is None
            else TargetAdjustment.material_code == material_code,
        )
    ).scalar_one_or_none()

    if existing is not None:
        previous = Decimal(str(existing.adjustment_volume))
        existing.adjustment_volume = amount
        existing.reason = reason.strip()
        existing.adjusted_by = user.username
        existing.adjusted_at = datetime.now(timezone.utc)
        row = existing
    else:
        previous = None
        row = TargetAdjustment(
            version_id=version.version_id, level=level, node_code=node_code,
            material_code=material_code, adjustment_volume=amount,
            reason=reason.strip(), adjusted_by=user.username,
            adjusted_at=datetime.now(timezone.utc),
        )
        session.add(row)
    session.flush()

    target_audit.record(
        session, action=target_audit.TargetAction.MANAGEMENT_ADJUSTED,
        plan_id=plan.plan_id, version_id=version.version_id,
        actor=user.username, actor_role=user.role,
        node_label=f"{level} · {node_code}"
                   + (f" · {material_code}" if material_code else ""),
        old_value=str(previous) if previous is not None else None,
        new_value=str(amount), reason=reason.strip(),
    )
    return row


def remove(session: Session, user: UserContext, *, version: TargetVersion,
           plan: TargetPlan, adjustment_id: int) -> bool:
    """Withdraw an adjustment so the next run allocates without it.

    A delete rather than a retirement, and it is the one place in this module
    that removes a row. An adjustment is an *instruction to the engine*, not a
    record of something that happened — withdrawing it means "do not apply this
    next time", and the fact that it once existed survives in ``target_audit``
    where the record belongs.
    """
    assert_editable(version)
    row = session.get(TargetAdjustment, adjustment_id)
    if row is None or row.version_id != version.version_id:
        return False

    target_audit.record(
        session, action=target_audit.TargetAction.MANAGEMENT_ADJUSTED,
        plan_id=plan.plan_id, version_id=version.version_id,
        actor=user.username, actor_role=user.role,
        node_label=f"{row.level} · {row.node_code}",
        old_value=str(row.adjustment_volume), new_value=None,
        reason="Adjustment withdrawn.",
    )
    session.delete(row)
    session.flush()
    return True


def for_version(session: Session, version_id: int) -> list[TargetAdjustment]:
    return list(session.execute(
        select(TargetAdjustment)
        .where(TargetAdjustment.version_id == version_id)
        .order_by(TargetAdjustment.level, TargetAdjustment.node_code)
    ).scalars())


def to_dict(row: TargetAdjustment) -> dict[str, Any]:
    return {
        "adjustment_id": row.adjustment_id,
        "level": row.level,
        "node_code": row.node_code,
        "material_code": row.material_code,
        "adjustment_volume": str(row.adjustment_volume),
        "reason": row.reason,
        "adjusted_by": row.adjusted_by,
        "adjusted_at": row.adjusted_at.isoformat() if row.adjusted_at else None,
    }


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def apply_to_siblings(shares: dict[str, Decimal], node_code: str,
                      amount: Decimal) -> dict[str, Decimal]:
    """Move ``amount`` to ``node_code`` from its siblings, pro rata.

    The parent's total is unchanged by construction: whatever one child gains,
    the others give up in proportion to what they hold. The redistribution goes
    back through :func:`rounding.distribute` so the siblings still sum exactly
    to what is left — subtracting a proportional slice from each and rounding
    independently is how a unit goes missing.

    Refuses rather than clamps. A clamped adjustment leaves the screen showing a
    Final Target nobody asked for, which is worse than being told it cannot be
    funded.
    """
    if node_code not in shares:
        return dict(shares)

    siblings = {code: value for code, value in shares.items()
                if code != node_code}
    if not siblings:
        # An only child cannot be adjusted: there is nobody to fund it and the
        # parent's total is fixed. Returned unchanged rather than raising,
        # because this is a shape of the hierarchy rather than a bad request —
        # and checked *before* the funding test, which would otherwise report a
        # shortfall against siblings that do not exist.
        return dict(shares)

    current = shares[node_code]
    # Quantised to the stored scale before anything else: an unrounded target
    # would reconcile in memory and fail the moment it reached NUMERIC(18, 4).
    target = (current + amount).quantize(rounding.quantum())
    if target < 0:
        raise AdjustmentBelowZero(node_code, current, amount)

    sibling_total = sum(siblings.values(), Decimal(0))
    if amount > sibling_total:
        raise AdjustmentUnfundable(node_code, amount, sibling_total)

    # What the siblings keep is whatever the parent held minus the adjusted
    # child's new figure — derived from ``target`` rather than from ``amount``
    # so the quantising above cannot leave a unit unaccounted for.
    parent_total = sum(shares.values(), Decimal(0))
    remaining = parent_total - target
    redistributed, _ = rounding.distribute_map(remaining, siblings)
    redistributed[node_code] = target
    return redistributed


def index(rows: Iterable[TargetAdjustment]) -> dict[tuple[str, str, str | None],
                                                    TargetAdjustment]:
    """``(level, node_code, material_code) -> adjustment`` for the engine.

    Keyed with the material because an adjustment may name one — "raise Dhaka's
    Glyfon by 500" — or apply to every material at that node.
    """
    return {(row.level, row.node_code, row.material_code): row for row in rows}


def lookup(indexed: dict[tuple[str, str, str | None], TargetAdjustment],
           level: str, node_code: str,
           material_code: str) -> TargetAdjustment | None:
    """The adjustment for one node and material, material-specific first.

    A specific instruction outranks a general one, the same ordering the marker
    resolver uses: an adjustment naming this material wins over one that names
    the node alone, so "raise Dhaka by 500, except Glyfon by 800" reads the way
    it is written.
    """
    return (indexed.get((level, node_code, material_code))
            or indexed.get((level, node_code, None)))


__all__ = [
    "AdjustmentUnfundable",
    "AdjustmentBelowZero",
    "UnknownAdjustmentLevel",
    "AppliedAdjustment",
    "record",
    "remove",
    "for_version",
    "to_dict",
    "apply_to_siblings",
    "index",
    "lookup",
]
