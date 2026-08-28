"""Signing a target off: the chain, whose turn it is, and what blocks the last step.

The review tree shows a manager the numbers. :mod:`revisions` is how a figure
gets challenged. This is how a version stops being a proposal and becomes the
target — and what it has to satisfy first.

**The chain is walked in order, from the bottom.** A step approves only once
every approving step beneath it has, which is what makes the sequence in
``target_approval_matrix`` mean something rather than being a label. Approving
out of order is refused with the name of the step that is still outstanding, so
the answer to "why can't I approve this?" is on the screen rather than in a log.

**Approval is recorded, never inferred.** ``target_approval`` is append-only and
holds one row per act, carrying the role and sequence held **at the time**. A
matrix reordered next year must not rewrite the order last year's target went
through, and a status column alone could not say who signed or in what order.

**The last step is gated on three things, and each is reported separately.**
Every step below approved; reconciliation balanced at every level; no revision
still open. They fail for different reasons and are fixed in different places,
so collapsing them into one "cannot approve" would send a reader looking in the
wrong place. A version that fails any of them stays ``PARTIALLY_APPROVED`` —
which is a real state, not a near-miss.

**Approving is not locking.** An approved version can still be sent back until
it is locked, and only locking writes into ``fact_target``. That separation is
deliberate: approval is a judgement, locking is the act that freezes figures a
sales force will be measured on, and the two are held by the same role at
different moments rather than in one click.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..database.models_target import (
    ApprovalAction,
    RevisionStatus,
    TargetApproval,
    TargetLevel,
    TargetPlan,
    TargetRevision,
    TargetStatus,
    TargetVersion,
)
from . import (
    audit as target_audit,
    country,
    matrix,
    plans as plan_service,
    reconcile,
)
from .errors import TargetManagementError

#: Versions that can be acted on by an approver at all. ``ALLOCATED`` is absent
#: on purpose: a target that has not been submitted has not been put to anybody,
#: and approving it would skip the act that says it was ready.
APPROVABLE: tuple[str, ...] = (
    TargetStatus.UNDER_REVIEW,
    TargetStatus.PARTIALLY_APPROVED,
)


class NotSubmittable(TargetManagementError):
    """A version that has no allocation to put in front of anybody."""

    code = "TARGET_NOT_SUBMITTABLE"

    def __init__(self, status: str) -> None:
        super().__init__(
            f"a {status} version cannot be submitted",
            user_message=(
                f"This target is {status.replace('_', ' ').lower()}. Only an "
                f"allocated target can be submitted for approval."
            ),
            details={"status": status},
        )


class NotApprovable(TargetManagementError):
    """The version is not in front of the chain right now."""

    code = "TARGET_NOT_APPROVABLE"

    def __init__(self, status: str) -> None:
        super().__init__(
            f"a {status} version takes no approval",
            user_message=(
                f"This target is {status.replace('_', ' ').lower()} and is not "
                f"awaiting approval."
            ),
            details={"status": status},
        )


class OutOfTurn(TargetManagementError):
    """A step of the chain acting before the steps beneath it have.

    Named rather than generic. "Waiting on the Area Manager" is actionable;
    "you cannot approve this" sends a reader to an administrator.
    """

    code = "TARGET_APPROVAL_OUT_OF_TURN"

    def __init__(self, waiting_on: list[str]) -> None:
        pretty = ", ".join(role.replace("_", " ").title() for role in waiting_on)
        super().__init__(
            f"waiting on {waiting_on}",
            user_message=(
                f"This target is still with {pretty}. Approval runs upward from "
                f"the bottom of the hierarchy, so your step opens once theirs "
                f"is done."
            ),
            details={"waiting_on": waiting_on},
        )


class ApprovalBlocked(TargetManagementError):
    """Final approval refused, with each unmet condition named separately.

    Three conditions that fail for different reasons and are fixed in different
    places. Collapsing them into one message would send a reader looking in the
    wrong one.
    """

    code = "TARGET_APPROVAL_BLOCKED"

    def __init__(self, reasons: list[str]) -> None:
        super().__init__(
            "; ".join(reasons),
            user_message=(
                "This target cannot receive final approval yet. "
                + " ".join(reasons)
            ),
            details={"reasons": reasons},
        )


class AlreadyActed(TargetManagementError):
    """This role has already recorded a decision on this version.

    Refused rather than silently repeated. A second identical approval adds a
    row that says nothing new, and an approver who has changed their mind is
    doing something else — sending the version back — which has its own action.
    """

    code = "TARGET_ALREADY_ACTED"

    def __init__(self, role: str) -> None:
        super().__init__(
            f"{role} has already approved this version",
            user_message=(
                f"{role.replace('_', ' ').title()} has already approved this "
                f"target. To change that decision, send the version back for "
                f"review."
            ),
            details={"role": role},
        )


@dataclass(frozen=True)
class ChainStep:
    """One step of the chain and where this version stands against it."""

    role: str
    hierarchy_level: str
    approval_sequence: int
    can_approve: bool
    approved: bool
    actor: str | None
    acted_at: str | None
    is_current: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "hierarchy_level": self.hierarchy_level,
            "approval_sequence": self.approval_sequence,
            "can_approve": self.can_approve,
            "approved": self.approved,
            "actor": self.actor,
            "acted_at": self.acted_at,
            "is_current": self.is_current,
        }


# ---------------------------------------------------------------------------
# Reading the state of the chain
# ---------------------------------------------------------------------------


def _acts(session: Session, version_id: int) -> dict[str, TargetApproval]:
    """The latest **version-level** act by each role. Node acts are not these.

    ``node_code IS NULL`` is the filter, and it is the distinction the table was
    built with: a row naming a node is an act on one figure, and a row naming
    none covers the whole version. Granting somebody's revision request is not
    signing off on the target — an approver who decides a revision and is then
    told they have already approved the version would have had their signature
    taken without giving it, which is the one thing an approval log must never
    do.

    Latest rather than first, because a version sent back and re-approved has
    two rows for the same role and the second is what stands. Both rows survive
    — this reads the log, it does not prune it.
    """
    rows = session.execute(
        select(TargetApproval)
        .where(TargetApproval.version_id == version_id,
               TargetApproval.node_code.is_(None))
        .order_by(TargetApproval.approval_id)
    ).scalars().all()
    latest: dict[str, TargetApproval] = {}
    for row in rows:
        if row.actor_role and row.action in (ApprovalAction.APPROVED,
                                             ApprovalAction.REJECTED):
            latest[row.actor_role] = row
    return latest


def steps(session: Session, version_id: int) -> list[ChainStep]:
    """Every step of the chain, bottom-up, with its state on this version.

    Steps that cannot approve are included and marked. The Sales Officer and
    Territory Manager sit in the chain without approving at it — they are where
    a target is *questioned* — and hiding them would make the sequence numbers
    on the Approval Matrix screen look like they skipped.
    """
    acted = _acts(session, version_id)
    current = _next_step(session, version_id)
    result: list[ChainStep] = []
    for row in matrix.chain(session):
        act = acted.get(row.role)
        result.append(ChainStep(
            role=row.role,
            hierarchy_level=row.hierarchy_level,
            approval_sequence=row.approval_sequence or 0,
            can_approve=row.can_approve,
            approved=bool(act and act.action == ApprovalAction.APPROVED),
            actor=act.actor if act else None,
            acted_at=act.acted_at.isoformat() if act and act.acted_at else None,
            is_current=(current is not None and current.role == row.role),
        ))
    return result


def _next_step(session: Session, version_id: int) -> matrix.MatrixRow | None:
    """The lowest approving step that has not approved yet, or ``None``.

    ``None`` means every approving step is done, which is the condition the
    final gate is checked against.
    """
    acted = _acts(session, version_id)
    for row in matrix.approvers(session):
        act = acted.get(row.role)
        if act is None or act.action != ApprovalAction.APPROVED:
            return row
    return None


def outstanding_below(session: Session, version_id: int,
                      row: matrix.MatrixRow) -> list[str]:
    """Which approving steps beneath ``row`` have not approved.

    The list, not a boolean — the refusal names them, and a chain waiting on two
    roles is a different problem from one waiting on one.
    """
    acted = _acts(session, version_id)
    waiting: list[str] = []
    for candidate in matrix.approvers(session):
        if (candidate.approval_sequence or 0) >= (row.approval_sequence or 0):
            break
        act = acted.get(candidate.role)
        if act is None or act.action != ApprovalAction.APPROVED:
            waiting.append(candidate.role)
    return waiting


def final_blockers(session: Session, version_id: int) -> list[str]:
    """What stands between this version and final approval. Empty means nothing.

    Each condition is its own sentence because each is fixed somewhere else: an
    unbalanced reconciliation is fixed by re-running the allocation, an open
    revision by deciding it, an incomplete chain by whoever is next in it.
    """
    reasons: list[str] = []

    open_revisions = int(session.execute(
        select(func.count(TargetRevision.revision_id)).where(
            TargetRevision.version_id == version_id,
            TargetRevision.status.in_(RevisionStatus.OPEN))
    ).scalar() or 0)
    if open_revisions:
        reasons.append(
            f"{open_revisions} revision request"
            f"{'s are' if open_revisions != 1 else ' is'} still open.")

    # The country target is read here rather than passed in, because a blocker
    # list that depended on its caller remembering to supply it would be a
    # gate that could be bypassed by forgetting.
    lines = country.list_lines(session, version_id)
    result = reconcile.check(
        session, version_id=version_id,
        country_target={line.material_code: Decimal(str(line.target_volume))
                        for line in lines if line.target_volume})
    if not result.balanced:
        count = len(result.mismatches)
        reasons.append(
            f"Reconciliation does not balance at {count} "
            f"node{'s' if count != 1 else ''}.")

    acted = _acts(session, version_id)
    remaining = [row.role for row in matrix.approvers(session)
                 if (act := acted.get(row.role)) is None
                 or act.action != ApprovalAction.APPROVED]
    if remaining:
        pretty = ", ".join(role.replace("_", " ").title() for role in remaining)
        reasons.append(f"Still with {pretty}.")
    return reasons


# ---------------------------------------------------------------------------
# Acting
# ---------------------------------------------------------------------------


def submit(session: Session, user: UserContext, *, plan: TargetPlan,
           version: TargetVersion, comment: str | None = None) -> TargetVersion:
    """Put an allocated target in front of the chain.

    Refused unless the version is ``ALLOCATED``: submitting a draft would ask
    people to approve a target that has no numbers in it, and submitting an
    approved one would restart a journey that has finished.
    """
    if version.status != TargetStatus.ALLOCATED:
        raise NotSubmittable(version.status)

    plan_service.set_version_status(
        session, user, version_id=version.version_id,
        new_status=TargetStatus.UNDER_REVIEW, reason=comment)
    session.add(TargetApproval(
        version_id=version.version_id, action=ApprovalAction.SUBMITTED,
        actor=user.username, actor_role=user.role, comment=comment))
    session.flush()
    return version


def approve(session: Session, user: UserContext, *, plan: TargetPlan,
            version: TargetVersion, comment: str | None = None) -> dict[str, Any]:
    """Record this role's approval, and advance the version if it was the last.

    The version reaches ``APPROVED`` only when every approving step has signed
    **and** :func:`final_blockers` is empty. Until then it is
    ``PARTIALLY_APPROVED`` — a real state describing a target part-way up the
    chain, not a failure.
    """
    if version.status not in APPROVABLE:
        raise NotApprovable(version.status)

    row = matrix.for_role(session, user.role)
    if row is None or not row.in_chain or not row.can_approve:
        raise matrix.NotInApprovalChain(user.role)

    acted = _acts(session, version.version_id)
    if (existing := acted.get(user.role)) is not None \
            and existing.action == ApprovalAction.APPROVED:
        raise AlreadyActed(user.role)

    waiting = outstanding_below(session, version.version_id, row)
    if waiting:
        raise OutOfTurn(waiting)

    session.add(TargetApproval(
        version_id=version.version_id, level=row.hierarchy_level,
        node_code=None, action=ApprovalAction.APPROVED, actor=user.username,
        actor_role=user.role, approval_sequence=row.approval_sequence,
        comment=comment))
    session.flush()

    remaining = _next_step(session, version.version_id)
    blockers = final_blockers(session, version.version_id)
    if remaining is None and not blockers:
        new_status = TargetStatus.APPROVED
    else:
        new_status = TargetStatus.PARTIALLY_APPROVED
        if remaining is None and blockers:
            # Every signature is in and the target still cannot be approved.
            # Refused rather than quietly parked, because the last approver is
            # entitled to know their approval did not complete the job.
            raise ApprovalBlocked(blockers)

    plan_service.set_version_status(
        session, user, version_id=version.version_id, new_status=new_status,
        reason=comment)
    return {
        "status": new_status,
        "steps": [step.to_dict() for step in steps(session, version.version_id)],
        "blockers": blockers,
    }


def reject(session: Session, user: UserContext, *, plan: TargetPlan,
           version: TargetVersion, comment: str) -> dict[str, Any]:
    """Send the whole version back, with a reason.

    A reason is required. A rejection with none tells its author that the target
    is wrong and nothing about what to change, which is the least useful message
    this workflow can produce.
    """
    if version.status not in APPROVABLE:
        raise NotApprovable(version.status)
    if not (comment or "").strip():
        from .errors import ReasonRequired
        raise ReasonRequired()

    row = matrix.for_role(session, user.role)
    if row is None or not row.in_chain or not row.can_reject:
        raise matrix.NotInApprovalChain(user.role)

    session.add(TargetApproval(
        version_id=version.version_id, level=row.hierarchy_level,
        action=ApprovalAction.REJECTED, actor=user.username,
        actor_role=user.role, approval_sequence=row.approval_sequence,
        comment=comment.strip()))
    session.flush()

    plan_service.set_version_status(
        session, user, version_id=version.version_id,
        new_status=TargetStatus.REJECTED, reason=comment.strip())
    return {
        "status": TargetStatus.REJECTED,
        "steps": [step.to_dict() for step in steps(session, version.version_id)],
        "blockers": [],
    }


def send_back(session: Session, user: UserContext, *, plan: TargetPlan,
              version: TargetVersion, comment: str) -> dict[str, Any]:
    """Return an approved version to review before it is locked.

    Approval is not irreversible until the lock. A later approver who spots a
    problem after somebody else signed must be able to stop it, and doing so is
    a distinct act from rejecting outright — the version goes back to the chain
    rather than back to its author.
    """
    if version.status not in (TargetStatus.APPROVED,
                              TargetStatus.PARTIALLY_APPROVED):
        raise NotApprovable(version.status)
    if not (comment or "").strip():
        from .errors import ReasonRequired
        raise ReasonRequired()

    session.add(TargetApproval(
        version_id=version.version_id,
        action=ApprovalAction.REVISION_REQUESTED, actor=user.username,
        actor_role=user.role, comment=comment.strip()))
    session.flush()
    plan_service.set_version_status(
        session, user, version_id=version.version_id,
        new_status=TargetStatus.UNDER_REVIEW, reason=comment.strip())
    return {
        "status": TargetStatus.UNDER_REVIEW,
        "steps": [step.to_dict() for step in steps(session, version.version_id)],
        "blockers": final_blockers(session, version.version_id),
    }


# ---------------------------------------------------------------------------
# My Approvals
# ---------------------------------------------------------------------------


def queue(session: Session, user: UserContext) -> dict[str, Any]:
    """Everything waiting on this person, and nothing waiting on anybody else.

    Two lists, because they are two different acts: versions where it is this
    role's turn in the chain, and individual revision requests routed here. A
    single merged list would have to invent a shared shape for them and would
    lose the distinction between signing off on a whole target and granting one
    figure.

    A role outside the chain gets empty lists and an explanation rather than an
    error: an administrator legitimately holds the section, configures the
    matrix, and approves nothing.
    """
    row = matrix.for_role(session, user.role)
    notes: list[str] = []
    if row is None or not row.in_chain:
        notes.append(
            f"{user.role.replace('_', ' ').title()} is not part of the approval "
            f"chain, so no target is waiting on it. An administrator configures "
            f"the chain on the Approval Matrix screen.")
        return {"versions": [], "revisions": [], "notes": notes,
                "role": user.role, "matrix": None}

    versions: list[dict[str, Any]] = []
    if row.can_approve:
        rows = session.execute(
            select(TargetVersion, TargetPlan)
            .join(TargetPlan, TargetPlan.plan_id == TargetVersion.plan_id)
            .where(TargetVersion.status.in_(APPROVABLE))
            .order_by(TargetVersion.version_id.desc())
        ).all()
        for version, plan in rows:
            acted = _acts(session, version.version_id).get(user.role)
            if acted is not None and acted.action == ApprovalAction.APPROVED:
                continue
            waiting = outstanding_below(session, version.version_id, row)
            versions.append({
                "plan": plan_service.plan_to_dict(plan),
                "version": plan_service.version_to_dict(version),
                "is_my_turn": not waiting,
                "waiting_on": waiting,
                "blockers": final_blockers(session, version.version_id),
            })

    revisions = _revisions_for(session, user, row)
    if not versions and not revisions:
        notes.append("Nothing is waiting for your approval.")
    return {
        "versions": versions,
        "revisions": revisions,
        "notes": notes,
        "role": user.role,
        "matrix": row.to_dict(),
    }


def _revisions_for(session: Session, user: UserContext,
                   row: matrix.MatrixRow) -> list[dict[str, Any]]:
    """Open requests this role is the one to decide.

    An escalated request goes to the role it names and to nobody else — that is
    what escalation *is*. An ordinary one goes to any approving step at or above
    the node's level, which is the same rule :func:`matrix.may_act_on` applies
    when the decision is actually made, so the queue cannot offer something the
    decision would refuse.
    """
    from . import revisions as revision_service

    if not row.can_approve:
        return []

    open_rows = session.execute(
        select(TargetRevision, TargetVersion, TargetPlan)
        .join(TargetVersion,
              TargetVersion.version_id == TargetRevision.version_id)
        .join(TargetPlan, TargetPlan.plan_id == TargetVersion.plan_id)
        .where(and_(TargetRevision.status.in_(RevisionStatus.OPEN)))
        .order_by(TargetRevision.revision_id.desc())
    ).all()

    result: list[dict[str, Any]] = []
    for revision, version, plan in open_rows:
        if revision.status == RevisionStatus.ESCALATED:
            if revision.escalated_to_role != row.role:
                continue
        elif not matrix.may_act_on(row, revision.level or TargetLevel.COMPANY):
            continue
        entry = revision_service.to_dict(revision)
        entry["plan_code"] = plan.plan_code
        entry["version_no"] = version.version_no
        entry["version_status"] = version.status
        result.append(entry)
    return result


def history(session: Session, version_id: int) -> list[dict[str, Any]]:
    """Every act on this version, oldest first. Append-only, so this is complete."""
    rows = session.execute(
        select(TargetApproval)
        .where(TargetApproval.version_id == version_id)
        .order_by(TargetApproval.approval_id)
    ).scalars().all()
    return [
        {
            "approval_id": row.approval_id,
            "level": row.level,
            "node_code": row.node_code,
            "action": row.action,
            "actor": row.actor,
            "actor_role": row.actor_role,
            "approval_sequence": row.approval_sequence,
            "comment": row.comment,
            "acted_at": row.acted_at.isoformat() if row.acted_at else None,
        }
        for row in rows
    ]


def state(session: Session, user: UserContext, *,
          version_id: int) -> dict[str, Any]:
    """Everything the approval panel draws for one version."""
    row = matrix.for_role(session, user.role)
    current = _next_step(session, version_id)
    version = plan_service.get_version(session, version_id)
    acted = _acts(session, version_id).get(user.role)
    return {
        "steps": [step.to_dict() for step in steps(session, version_id)],
        "history": history(session, version_id),
        "blockers": final_blockers(session, version_id),
        "my_role": user.role,
        "my_matrix": row.to_dict() if row else None,
        "my_turn": bool(row and row.can_approve and current is not None
                        and current.role == row.role
                        and version.status in APPROVABLE),
        "already_acted": (acted.action if acted else None),
        "current_role": current.role if current else None,
    }


__all__ = [
    "APPROVABLE",
    "ChainStep",
    "NotSubmittable",
    "NotApprovable",
    "OutOfTurn",
    "ApprovalBlocked",
    "AlreadyActed",
    "steps",
    "outstanding_below",
    "final_blockers",
    "submit",
    "approve",
    "reject",
    "send_back",
    "queue",
    "history",
    "state",
]
