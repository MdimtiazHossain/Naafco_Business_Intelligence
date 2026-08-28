"""The approval chain: who signs at which level, in what order, within what limit.

Configuration, not code. The *roles* are fixed by
:class:`app.database.models_ai.Role`, but which of them signs off on a target,
in what order, and how far each may move a figure without escalating is a
decision every deployment makes for itself — so it lives in
``target_approval_matrix`` and an administrator owns it.

**The chain runs bottom-up.** Sequence 1 is the sub-territory, where a target is
first questioned by the person who has to hit it, and the last sequence is
Management. A target is *allocated* downwards and *reviewed* upwards, and the
two directions are not the same journey: the allocation asks "how should this
number be split?", the review asks "is my share of it right?", and the second
question can only be answered from the bottom.

**A NULL sequence is outside the chain, not first in it.** The SFE / MIS
administrator configures the run and signs off on nothing. Reading NULL as 0
would put them at the head of an approval workflow they have no business being
in, so every read here filters them out explicitly rather than sorting them.

**A NULL adjustment limit is unlimited; zero is "may not change a figure".**
Both are real settings and they are kept distinguishable, which is why
:func:`within_limit` tests for ``None`` rather than falling back to a number.

Nothing in this module decides *whether* a user holds the APPROVE or REVISE
action — that is the section permission chain in :mod:`app.security.sections`,
checked before a request reaches here. The matrix is the finer question the
section cannot answer: a Regional Manager holds APPROVE everywhere they hold the
section, and the matrix is what says they approve at the region and may move a
figure by up to 10% before it has to go higher.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..database.models_target import (
    DEFAULT_APPROVAL_MATRIX,
    TargetApprovalMatrix,
    TargetLevel,
)
from . import audit as target_audit
from .errors import TargetManagementError


class UnknownMatrixRole(TargetManagementError):
    """A matrix row naming a role this deployment does not have.

    Refused at the edit rather than stored, because a row nobody can hold is a
    link in the chain that can never be completed — and a target would sit
    waiting for an approver who does not exist.
    """

    code = "TARGET_MATRIX_ROLE_UNKNOWN"

    def __init__(self, role: str) -> None:
        super().__init__(
            f"{role} is not a role",
            user_message=(
                f"{role} is not a role in this system. Choose one of the "
                f"configured roles."
            ),
            details={"role": role},
        )


class UnknownMatrixLevel(TargetManagementError):
    """A matrix row naming a level the allocator cannot reach.

    The failure CLAUDE.md warns about, caught at the edit: a stale name in a
    configuration table does not degrade to a missing item, it routes a target
    to a node that cannot exist.
    """

    code = "TARGET_MATRIX_LEVEL_UNKNOWN"

    def __init__(self, level: str) -> None:
        super().__init__(
            f"{level} is not an allocation level",
            user_message=(
                f"{level} is not a level a target is allocated to. Choose one "
                f"of {', '.join(TargetLevel.ORDERED)}."
            ),
            details={"level": level},
        )


class DuplicateMatrixSequence(TargetManagementError):
    """Two roles claiming the same place in the chain.

    Refused rather than ordered arbitrarily. "Who signs next?" must have one
    answer, and a tie would make it depend on how the rows happened to sort.
    """

    code = "TARGET_MATRIX_SEQUENCE_DUPLICATE"

    def __init__(self, sequence: int, roles: list[str]) -> None:
        super().__init__(
            f"sequence {sequence} claimed by {roles}",
            user_message=(
                f"Sequence {sequence} is set for more than one role "
                f"({', '.join(roles)}). Each step of the chain takes one role."
            ),
            details={"sequence": sequence, "roles": roles},
        )


class NotInApprovalChain(TargetManagementError):
    """The caller's role holds no sequence, so it approves nothing.

    Distinct from a permission refusal. The role may legitimately hold the
    section and the APPROVE action — an administrator does — and still sit
    outside the chain, which is a statement about the workflow rather than about
    the person.
    """

    code = "TARGET_NOT_IN_APPROVAL_CHAIN"

    def __init__(self, role: str) -> None:
        super().__init__(
            f"{role} holds no approval sequence",
            user_message=(
                f"{role} is not part of the approval chain, so it cannot "
                f"approve or reject a target. An administrator configures the "
                f"chain on the Approval Matrix screen."
            ),
            details={"role": role},
        )


@dataclass(frozen=True)
class MatrixRow:
    """One role's place in the chain, read once and passed around.

    A frozen snapshot rather than the ORM object: an approval records the
    sequence and role held **at the time it happened**, and a matrix reordered
    afterwards must not rewrite the order a past approval went through.
    """

    role: str
    hierarchy_level: str
    approval_sequence: int | None
    can_edit: bool
    can_approve: bool
    can_reject: bool
    can_revise: bool
    adjustment_limit_percent: float | None
    is_active: bool

    @property
    def in_chain(self) -> bool:
        """Sequenced and active. NULL is outside the chain, never step zero."""
        return self.is_active and self.approval_sequence is not None

    @property
    def unlimited(self) -> bool:
        return self.adjustment_limit_percent is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "hierarchy_level": self.hierarchy_level,
            "approval_sequence": self.approval_sequence,
            "can_edit": self.can_edit,
            "can_approve": self.can_approve,
            "can_reject": self.can_reject,
            "can_revise": self.can_revise,
            "adjustment_limit_percent": self.adjustment_limit_percent,
            "is_active": self.is_active,
            #: Spelled out rather than left to the browser to infer from NULL.
            #: "Unlimited" and "0%" look alike as an empty cell and mean
            #: opposite things.
            "limit_label": ("Unlimited" if self.adjustment_limit_percent is None
                            else f"±{self.adjustment_limit_percent:g}%"),
        }


def _row(entry: TargetApprovalMatrix) -> MatrixRow:
    return MatrixRow(
        role=entry.role,
        hierarchy_level=entry.hierarchy_level,
        approval_sequence=entry.approval_sequence,
        can_edit=bool(entry.can_edit),
        can_approve=bool(entry.can_approve),
        can_reject=bool(entry.can_reject),
        can_revise=bool(entry.can_revise),
        adjustment_limit_percent=(None if entry.adjustment_limit_percent is None
                                  else float(entry.adjustment_limit_percent)),
        is_active=bool(entry.is_active),
    )


def load(session: Session) -> dict[str, MatrixRow]:
    """``role -> MatrixRow`` for every configured role, active or not.

    Inactive rows are returned rather than filtered: the Approval Matrix screen
    has to show a deactivated role in order to offer switching it back on, and
    every *decision* here asks :attr:`MatrixRow.in_chain`, which excludes them.
    """
    return {
        entry.role: _row(entry)
        for entry in session.execute(select(TargetApprovalMatrix)).scalars()
    }


def for_role(session: Session, role: str) -> MatrixRow | None:
    """One role's row, or ``None`` if this deployment has not configured it.

    ``None`` is not an error here. A role added to the system after the matrix
    was seeded has no row until an administrator gives it one, and the honest
    answer is that it holds no place in the chain — not that something is
    broken.
    """
    entry = session.execute(
        select(TargetApprovalMatrix).where(TargetApprovalMatrix.role == role)
    ).scalar_one_or_none()
    return _row(entry) if entry is not None else None


def chain(session: Session) -> list[MatrixRow]:
    """The sequenced roles, bottom-up. Administrators are absent, not sorted."""
    rows = [row for row in load(session).values() if row.in_chain]
    return sorted(rows, key=lambda row: row.approval_sequence or 0)


def approvers(session: Session) -> list[MatrixRow]:
    """The steps of the chain that actually approve.

    A role can sit in the chain and not approve at it — the Sales Officer and
    the Territory Manager both do, because a target is *questioned* at their
    level rather than signed off there. They still hold a sequence, because that
    is what says a revision from them routes upward rather than nowhere.
    """
    return [row for row in chain(session) if row.can_approve]


def level_of(session: Session, role: str) -> str | None:
    """The level this role approves at, or ``None`` if it holds no row."""
    row = for_role(session, role)
    return row.hierarchy_level if row is not None else None


def within_limit(row: MatrixRow | None, change_percent: float | None) -> bool:
    """Whether a change of this size is inside the role's adjustment limit.

    ``None`` for the limit is unlimited and ``None`` for the change is "no
    system figure to measure against", which is treated as inside the limit —
    a revision against a node the engine never produced a figure for cannot be
    a percentage of anything, and refusing it on a number that does not exist
    would be arithmetic about nothing.

    The comparison is on the **magnitude**. A limit is a distance, and a role
    trusted to raise a figure by 10% is trusted to cut it by 10%.
    """
    if row is None:
        return False
    if row.adjustment_limit_percent is None:
        return True
    if change_percent is None:
        return True
    return abs(change_percent) <= row.adjustment_limit_percent + 1e-9


def escalation_target(session: Session, row: MatrixRow | None,
                      change_percent: float | None) -> str | None:
    """The role a change too large for ``row`` has to go to instead.

    The lowest step above the requester whose limit covers the change — not the
    top of the chain. Escalating a 12% change straight to Management when the
    zone manager's 15% covers it would put every routine correction in front of
    the person least able to judge it.

    ``None`` when nobody's limit covers it, which is a real outcome only if
    every limit is finite; with Management unlimited by default, the answer is
    Management.
    """
    if row is None or row.approval_sequence is None:
        return None
    for candidate in chain(session):
        if (candidate.approval_sequence or 0) <= row.approval_sequence:
            continue
        if not candidate.can_approve:
            continue
        if within_limit(candidate, change_percent):
            return candidate.role
    return None


def may_act_on(row: MatrixRow | None, level: str) -> bool:
    """Whether this role's step of the chain covers a node at ``level``.

    A role approves at its own level **and everything below it**. A regional
    manager signing off on their region is implicitly signing off on the areas,
    units and territories inside it — the region's figure *is* the sum of them —
    so refusing them the area beneath would make the region unapprovable by
    anyone who could actually see it.

    The reverse is refused: a level above is somebody else's to sign.
    """
    if row is None or not row.in_chain:
        return False
    try:
        own = TargetLevel.ORDERED.index(row.hierarchy_level)
        node = TargetLevel.ORDERED.index(level)
    except ValueError:
        return False
    return node >= own


# ---------------------------------------------------------------------------
# Editing the matrix
# ---------------------------------------------------------------------------


def update(session: Session, user: UserContext,
           entries: list[dict[str, Any]]) -> list[MatrixRow]:
    """Replace the configuration for the named roles. Audited, never deleted.

    A role absent from ``entries`` is **left alone**, not removed. The screen
    sends what it changed; treating omission as deletion would let a partially
    loaded form silently empty the chain, and an approval workflow with no rows
    is not a blank slate — it is one in which nobody can approve anything.

    Deactivating is how a role leaves the chain, and it is reversible.
    """
    from ..database.models_ai import Role

    known_roles = set(Role.ALL)
    current = load(session)

    # Validated in full before anything is written: a half-applied chain is a
    # worse state than a refused edit.
    proposed = {role: row.approval_sequence for role, row in current.items()}
    for entry in entries:
        role = str(entry.get("role") or "").strip()
        if role not in known_roles:
            raise UnknownMatrixRole(role or "(blank)")
        level = str(entry.get("hierarchy_level") or "").strip()
        if level and level not in TargetLevel.ORDERED:
            raise UnknownMatrixLevel(level)
        if "approval_sequence" in entry:
            proposed[role] = entry["approval_sequence"]

    by_sequence: dict[int, list[str]] = {}
    for role, sequence in proposed.items():
        if sequence is None:
            continue
        by_sequence.setdefault(int(sequence), []).append(role)
    for sequence, roles in sorted(by_sequence.items()):
        if len(roles) > 1:
            raise DuplicateMatrixSequence(sequence, sorted(roles))

    for entry in entries:
        role = str(entry["role"]).strip()
        record = session.execute(
            select(TargetApprovalMatrix).where(TargetApprovalMatrix.role == role)
        ).scalar_one_or_none()
        if record is None:
            record = TargetApprovalMatrix(
                role=role, hierarchy_level=TargetLevel.COMPANY)
            session.add(record)
        before = _row(record) if record.matrix_id else None

        for field in ("hierarchy_level", "approval_sequence", "can_edit",
                      "can_approve", "can_reject", "can_revise",
                      "adjustment_limit_percent", "is_active"):
            if field in entry:
                setattr(record, field, entry[field])
        record.updated_by = user.username
        session.flush()

        target_audit.record(
            session, action=target_audit.TargetAction.MATRIX_UPDATED,
            actor=user.username, actor_role=user.role, node_label=role,
            old_value=_describe(before), new_value=_describe(_row(record)),
            reason=entry.get("reason"),
        )

    return chain(session)


def _describe(row: MatrixRow | None) -> str | None:
    """A matrix row as one readable line for the audit trail.

    Text rather than JSON: the Audit Trail screen puts old and new side by side
    for a human to compare, and a serialised object is not something a human
    compares at a glance.
    """
    if row is None:
        return None
    sequence = "outside chain" if row.approval_sequence is None \
        else f"step {row.approval_sequence}"
    limit = "unlimited" if row.adjustment_limit_percent is None \
        else f"±{row.adjustment_limit_percent:g}%"
    rights = ",".join(name for name, held in (
        ("edit", row.can_edit), ("approve", row.can_approve),
        ("reject", row.can_reject), ("revise", row.can_revise)) if held) or "none"
    state = "active" if row.is_active else "inactive"
    return f"{row.hierarchy_level} · {sequence} · {rights} · {limit} · {state}"


def seeded_defaults() -> list[dict[str, Any]]:
    """The shipped chain, for a screen that offers to restore it.

    Read from :data:`DEFAULT_APPROVAL_MATRIX` so there is one copy of the
    default. Restoring is an ordinary :func:`update`, which means it is audited
    like any other edit rather than being a silent reset.
    """
    return [
        {
            "role": role, "hierarchy_level": level, "approval_sequence": sequence,
            "can_edit": edit, "can_approve": approve, "can_reject": reject,
            "can_revise": revise, "adjustment_limit_percent": limit,
            "is_active": True,
        }
        for role, level, sequence, edit, approve, reject, revise, limit
        in DEFAULT_APPROVAL_MATRIX
    ]


def percent_change(system_volume: Decimal | float | None,
                   requested_volume: Decimal | float) -> float | None:
    """The requested change as a percentage of what the engine produced.

    ``None`` when there is nothing to measure against — a system volume of zero
    has no percentage, and reporting one would be a division this platform
    refuses everywhere else. The revision is still legitimate; it simply routes
    on its size in volume rather than in percent.
    """
    if system_volume is None:
        return None
    base = Decimal(str(system_volume))
    if base == 0:
        return None
    return float((Decimal(str(requested_volume)) - base) / base * 100)


__all__ = [
    "MatrixRow",
    "UnknownMatrixRole",
    "UnknownMatrixLevel",
    "DuplicateMatrixSequence",
    "NotInApprovalChain",
    "load",
    "for_role",
    "chain",
    "approvers",
    "level_of",
    "within_limit",
    "escalation_target",
    "may_act_on",
    "update",
    "seeded_defaults",
    "percent_change",
]
