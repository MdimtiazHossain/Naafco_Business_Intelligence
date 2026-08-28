"""A request to change one node's allocated figure, and what becomes of it.

The review tree shows a manager what their part of the target is. This is what
they do about it when it is wrong.

**A revision is a request, not a change.** It states the figure the engine
produced, the figure the requester wants and the reason — three separate
columns, because "what the system said" and "what we agreed" are different
questions and a request that overwrote the first could answer neither. Nothing
moves until an approver decides, and the decision records its own volume, which
may be neither of the first two: an approver who grants half of what was asked
is a real outcome and the table can say so.

**Asking is not changing, so nearly everyone may ask.** The Sales Officer holds
REVISE and never APPROVE, and that is the point — the person closest to the
customer is the one who knows the target is wrong, and the person who signs it
off is somebody else. The two are separate actions on the section for exactly
this reason.

**The adjustment limit routes the request; it does not refuse it.** A change
larger than the requester's limit is not rejected, it is *escalated*: it goes to
the lowest step of the chain whose limit covers it instead of the approver it
would normally have reached. ``ESCALATED`` is therefore a distinct status rather
than a flag on ``PENDING``, because losing that distinction would make the
adjustment limit unauditable — nobody could tell afterwards whether a request
went the ordinary way or was routed past somebody.

**An approved revision is applied immediately, and funded by the node's
siblings.** The country target does not move: whatever a node gains, its
siblings give up in proportion to what they hold, through the same
largest-remainder distribution the allocation engine uses — so the tree still
reconciles exactly, at every level, the moment the approval lands. The node's
own subtree is then re-split in proportion to what it already held, because a
territory's children must still sum to the territory.

It is also **recorded as a standing adjustment**, so re-running the allocation
after loading more sales does not silently discard a decision somebody signed.
That is the same mechanism management adjustments use, deliberately: a revision
and an adjustment change the numbers in exactly the same way, and differ in who
initiated it and whether anybody had to agree.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..database.models_target import (
    RevisionStatus,
    TargetAdjustment,
    TargetAllocation,
    TargetApproval,
    TargetLevel,
    TargetPlan,
    TargetRevision,
    TargetStatus,
    TargetVersion,
    ApprovalAction,
)
from . import audit as target_audit, matrix, rounding
from .errors import TargetManagementError

#: A revision may be raised while a version is being worked on or reviewed, and
#: never once it is frozen. ``ALLOCATED`` is included because the commonest
#: moment to notice a wrong figure is the first time anybody looks at it, which
#: is before the version has been submitted.
REVISABLE: tuple[str, ...] = (
    TargetStatus.ALLOCATED,
    TargetStatus.UNDER_REVIEW,
    TargetStatus.PARTIALLY_APPROVED,
)


class NodeNotAllocated(TargetManagementError):
    """A revision against a node this version's allocation does not contain."""

    code = "TARGET_NODE_NOT_ALLOCATED"

    def __init__(self, level: str, node_code: str) -> None:
        super().__init__(
            f"{level} {node_code} is not in this allocation",
            user_message=(
                f"{node_code} has no allocated target in this version, so there "
                f"is nothing to revise. Re-run the allocation if it should have "
                f"one."
            ),
            details={"level": level, "node_code": node_code},
        )


class NotRevisable(TargetManagementError):
    """The version is in a state where its figures are not open to challenge."""

    code = "TARGET_NOT_REVISABLE"

    def __init__(self, status: str) -> None:
        super().__init__(
            f"a {status} version takes no revisions",
            user_message=(
                f"This target is {status.replace('_', ' ').lower()} and its "
                f"figures are not open to revision. A locked or approved target "
                f"is changed by creating the next version."
            ),
            details={"status": status},
        )


class InvalidRevisionVolume(TargetManagementError):
    """A requested volume that is unreadable or negative.

    Two messages, not one. "Unreadable" and "negative" send a reader to
    different places, and reading ``12,5OO`` as 125 is the invented figure this
    platform exists not to produce.
    """

    code = "TARGET_REVISION_VOLUME_INVALID"

    def __init__(self, raw: Any, *, negative: bool = False) -> None:
        shown = "(blank)" if raw is None or raw == "" else str(raw)[:32]
        message = (f"A revised target cannot be negative ({shown})."
                   if negative else
                   f"{shown} is not a number this system can read as a volume. "
                   f"Enter digits, optionally with thousands separators.")
        super().__init__(
            f"unreadable revision volume {shown!r}",
            user_message=message,
            details={"value": shown, "negative": negative},
        )


class RevisionNotFound(TargetManagementError):
    """No revision with that identifier."""

    code = "TARGET_REVISION_NOT_FOUND"
    user_message = "That revision request was not found."


class RevisionClosed(TargetManagementError):
    """Already decided. A second decision is a new request, not an edit."""

    code = "TARGET_REVISION_CLOSED"

    def __init__(self, status: str) -> None:
        super().__init__(
            f"revision already {status}",
            user_message=(
                f"This revision has already been {status.lower()}. Raise a new "
                f"request to change the figure again."
            ),
            details={"status": status},
        )


class RevisionOutOfScope(TargetManagementError):
    """The node is outside what this user's data scope covers.

    Refused rather than silently ignored: a request against somebody else's
    territory is a mistake worth naming, and answering it with "not found"
    would leave the requester retrying.
    """

    code = "TARGET_REVISION_OUT_OF_SCOPE"

    def __init__(self, node_code: str) -> None:
        super().__init__(
            f"{node_code} outside caller scope",
            user_message=(
                f"{node_code} is outside the part of the business you hold, so "
                f"you cannot raise or decide a revision on it."
            ),
            details={"node_code": node_code},
        )


class RevisionNotYours(TargetManagementError):
    """An approver whose step of the chain does not cover this node."""

    code = "TARGET_REVISION_NOT_YOURS"

    def __init__(self, role: str, level: str) -> None:
        super().__init__(
            f"{role} does not approve at {level}",
            user_message=(
                f"{role} approves at a different level of the hierarchy, so "
                f"this request is not yours to decide. It sits with the role "
                f"the approval matrix routes it to."
            ),
            details={"role": role, "level": level},
        )


class RevisionExceedsLimit(TargetManagementError):
    """A decision larger than the deciding role's own adjustment limit.

    Checked on the *approved* volume rather than the requested one: an approver
    who grants more than they may is the case the limit exists to stop, and
    granting less than was asked is always inside it.
    """

    code = "TARGET_REVISION_EXCEEDS_LIMIT"

    def __init__(self, role: str, change_percent: float,
                 limit_percent: float) -> None:
        super().__init__(
            f"{change_percent:.2f}% exceeds {role}'s {limit_percent:g}% limit",
            user_message=(
                f"That is a {change_percent:+.1f}% change, and {role} may "
                f"approve up to ±{limit_percent:g}%. It has to go to the role "
                f"above."
            ),
            details={"role": role, "change_percent": change_percent,
                     "limit_percent": limit_percent},
        )


# ---------------------------------------------------------------------------
# Reading the allocation a revision is about
# ---------------------------------------------------------------------------


def _node_conditions(version_id: int, level: str, node_code: str,
                     material_code: str | None):
    conditions = [TargetAllocation.version_id == version_id,
                  TargetAllocation.level == level,
                  TargetAllocation.node_code == node_code]
    if material_code:
        conditions.append(TargetAllocation.material_code == material_code)
    return conditions


def node_volume(session: Session, *, version_id: int, level: str,
                node_code: str, material_code: str | None = None) -> Decimal | None:
    """The node's allocated volume for the whole period, or ``None`` if absent.

    ``None`` rather than zero for a node with no rows: "this version does not
    allocate to that node" and "that node's target is nothing" are different
    answers, and only the second is a figure.
    """
    total = session.execute(
        select(func.sum(TargetAllocation.current_volume)).where(
            and_(*_node_conditions(version_id, level, node_code, material_code)))
    ).scalar()
    return None if total is None else Decimal(str(total))


def _anchor_allocation_id(session: Session, *, version_id: int, level: str,
                          node_code: str,
                          material_code: str | None) -> int | None:
    """The row a revision hangs its CASCADE on: lowest material, lowest month.

    Deterministic on purpose. The anchor is not a figure anybody reads — the
    node is named outright on the revision — but it must be the *same* row on
    every call, or two requests about one node would be pinned to two different
    rows and behave differently when the allocation is regenerated.
    """
    return session.execute(
        select(TargetAllocation.allocation_id)
        .where(and_(*_node_conditions(version_id, level, node_code,
                                      material_code)))
        .order_by(TargetAllocation.material_code, TargetAllocation.target_month,
                  TargetAllocation.allocation_id)
        .limit(1)
    ).scalar()


def _coerce_volume(raw: Any) -> Decimal:
    """A revised volume, or a refusal. Never a zero standing in for a typo.

    The same discipline :func:`country._coerce_volume` applies to a country
    line, for the same reason: this is the number a sales force will be measured
    on, and a silently misread one is worse than a rejected one.
    """
    if raw is None or isinstance(raw, bool):
        raise InvalidRevisionVolume(raw)
    if isinstance(raw, str):
        text = raw.strip().replace(",", "")
        if not text:
            raise InvalidRevisionVolume(raw)
        try:
            value = Decimal(text)
        except Exception as exc:  # noqa: BLE001 - any parse failure is a refusal
            raise InvalidRevisionVolume(raw) from exc
    elif isinstance(raw, (int, float, Decimal)):
        value = Decimal(str(raw))
    else:
        raise InvalidRevisionVolume(raw)

    if not value.is_finite():
        raise InvalidRevisionVolume(raw)
    if value < 0:
        raise InvalidRevisionVolume(raw, negative=True)
    return value.quantize(rounding.quantum())


def _assert_in_scope(user: UserContext, level: str, node_code: str,
                     session: Session, version_id: int) -> None:
    """Refuse a node the caller's data scope does not reach.

    Unrestricted callers pass. A scoped caller passes when the node **is** one
    of their scoped codes or sits underneath one — checked by walking the
    allocation's own stored parent chain, which is the same snapshot the review
    tree reads, so scope and display cannot disagree about where a node sits.
    """
    if user.is_unrestricted:
        return

    from ..ai.permission_filter import FILTER_FIELD_BY_LEVEL

    scoped: set[tuple[str, str]] = set()
    for scope_level in FILTER_FIELD_BY_LEVEL:
        codes = user.data_scope.get(scope_level) or []
        key = scope_level[:-5] if scope_level.endswith("_code") else scope_level
        scoped.update((key, code) for code in codes)
    if not scoped:
        raise RevisionOutOfScope(node_code)

    current: tuple[str, str] | None = (level, node_code)
    seen: set[tuple[str, str]] = set()
    while current is not None and current not in seen:
        if current in scoped:
            return
        seen.add(current)
        parent = session.execute(
            select(TargetAllocation.parent_level, TargetAllocation.parent_code)
            .where(TargetAllocation.version_id == version_id,
                   TargetAllocation.level == current[0],
                   TargetAllocation.node_code == current[1])
            .limit(1)
        ).first()
        current = (parent[0], parent[1]) if parent and parent[1] else None
    raise RevisionOutOfScope(node_code)


# ---------------------------------------------------------------------------
# Raising a request
# ---------------------------------------------------------------------------


def request(session: Session, user: UserContext, *, plan: TargetPlan,
            version: TargetVersion, level: str, node_code: str,
            requested_volume: Any, reason: str,
            material_code: str | None = None) -> TargetRevision:
    """Ask for one node's figure to be changed. Nothing moves yet.

    Routed rather than refused when it exceeds the requester's adjustment limit:
    the request is recorded as ``ESCALATED`` and named the role it went to
    instead, so the routing decision stays readable afterwards.
    """
    if version.status not in REVISABLE:
        raise NotRevisable(version.status)
    if not (reason or "").strip():
        from .errors import ReasonRequired
        raise ReasonRequired()
    if level not in TargetLevel.ORDERED:
        raise NodeNotAllocated(level, node_code)

    system_volume = node_volume(session, version_id=version.version_id,
                                level=level, node_code=node_code,
                                material_code=material_code)
    if system_volume is None:
        raise NodeNotAllocated(level, node_code)
    _assert_in_scope(user, level, node_code, session, version.version_id)

    wanted = _coerce_volume(requested_volume)
    change_percent = matrix.percent_change(system_volume, wanted)

    row = matrix.for_role(session, user.role)
    escalated_to = None
    status = RevisionStatus.PENDING
    if not matrix.within_limit(row, change_percent):
        escalated_to = matrix.escalation_target(session, row, change_percent)
        status = RevisionStatus.ESCALATED

    anchor = _anchor_allocation_id(session, version_id=version.version_id,
                                   level=level, node_code=node_code,
                                   material_code=material_code)
    revision = TargetRevision(
        allocation_id=anchor,
        version_id=version.version_id,
        level=level,
        node_code=node_code,
        material_code=material_code,
        system_volume=system_volume,
        requested_volume=wanted,
        reason=reason.strip(),
        status=status,
        change_percent=change_percent,
        escalated_to_role=escalated_to,
        requested_by=user.username,
    )
    session.add(revision)
    session.flush()

    target_audit.record(
        session, action=target_audit.TargetAction.REVISION_REQUESTED,
        plan_id=plan.plan_id, version_id=version.version_id,
        actor=user.username, actor_role=user.role,
        node_label=_node_label(level, node_code, material_code),
        old_value=_volume_text(system_volume), new_value=_volume_text(wanted),
        reason=reason.strip(),
    )
    return revision


# ---------------------------------------------------------------------------
# Deciding one
# ---------------------------------------------------------------------------


def decide(session: Session, user: UserContext, *, plan: TargetPlan,
           version: TargetVersion, revision_id: int, approve: bool,
           approved_volume: Any = None,
           comment: str | None = None) -> TargetRevision:
    """Grant or refuse a request, and apply it if granted.

    ``approved_volume`` defaults to what was asked. An approver granting a
    different figure is ordinary and is why the column exists — but it is
    checked against *their* adjustment limit, not the requester's, because the
    limit is about what this person may sign.
    """
    revision = session.get(TargetRevision, revision_id)
    if revision is None or revision.version_id != version.version_id:
        raise RevisionNotFound()
    if revision.status not in RevisionStatus.OPEN:
        raise RevisionClosed(revision.status)
    if version.status not in REVISABLE:
        raise NotRevisable(version.status)

    row = matrix.for_role(session, user.role)
    if not matrix.may_act_on(row, revision.level or TargetLevel.COMPANY):
        raise RevisionNotYours(user.role, revision.level or "")
    _assert_in_scope(user, revision.level, revision.node_code, session,
                     version.version_id)

    now = datetime.now(timezone.utc)
    if not approve:
        revision.status = RevisionStatus.REJECTED
        revision.decided_by = user.username
        revision.decided_at = now
        _record_approval(session, version=version, user=user, row=row,
                         action=ApprovalAction.REVISION_REQUESTED,
                         level=revision.level, node_code=revision.node_code,
                         comment=comment)
        session.flush()
        target_audit.record(
            session, action=target_audit.TargetAction.REVISION_REJECTED,
            plan_id=plan.plan_id, version_id=version.version_id,
            actor=user.username, actor_role=user.role,
            node_label=_node_label(revision.level, revision.node_code,
                                   revision.material_code),
            old_value=_volume_text(revision.system_volume),
            new_value=None, reason=comment or revision.reason,
        )
        return revision

    granted = (_coerce_volume(approved_volume) if approved_volume is not None
               else Decimal(str(revision.requested_volume)))
    change = matrix.percent_change(revision.system_volume, granted)
    if not matrix.within_limit(row, change):
        raise RevisionExceedsLimit(
            user.role, change or 0.0,
            row.adjustment_limit_percent if row else 0.0)

    apply_to_allocation(session, version=version, level=revision.level,
                        node_code=revision.node_code,
                        material_code=revision.material_code,
                        approved_volume=granted)
    _store_adjustment(session, user, version=version, level=revision.level,
                      node_code=revision.node_code,
                      material_code=revision.material_code,
                      delta=granted - Decimal(str(revision.system_volume)),
                      reason=f"Revision {revision.revision_id}: {revision.reason}")

    revision.status = RevisionStatus.APPROVED
    revision.approved_volume = granted
    revision.decided_by = user.username
    revision.decided_at = now
    _record_approval(session, version=version, user=user, row=row,
                     action=ApprovalAction.APPROVED, level=revision.level,
                     node_code=revision.node_code, comment=comment)
    session.flush()

    target_audit.record(
        session, action=target_audit.TargetAction.REVISION_APPROVED,
        plan_id=plan.plan_id, version_id=version.version_id,
        actor=user.username, actor_role=user.role,
        node_label=_node_label(revision.level, revision.node_code,
                               revision.material_code),
        old_value=_volume_text(revision.system_volume),
        new_value=_volume_text(granted), reason=comment or revision.reason,
    )
    return revision


# ---------------------------------------------------------------------------
# Applying one
# ---------------------------------------------------------------------------


def apply_to_allocation(session: Session, *, version: TargetVersion, level: str,
                        node_code: str, material_code: str | None,
                        approved_volume: Decimal) -> int:
    """Move a node to ``approved_volume``, funded by its siblings. Exact.

    Worked **one (material, month) slice at a time**, because that is what an
    allocation row is: for a fixed material and month the hierarchy is a plain
    tree of single numbers, and every operation below is arithmetic on that
    tree. Doing it on period totals instead would leave the monthly rows to be
    re-derived, which is the step where a unit goes missing.

    Three moves per slice, in order:

    1. The node's share of the change is apportioned across its slices in
       proportion to what each already holds, so a decision does not flatten a
       seasonal profile — the same rule a management adjustment follows.
    2. The siblings fund it pro rata through
       :func:`adjustments.apply_to_siblings`, so the parent's total, and every
       total above it, is unchanged by construction.
    3. Every node whose figure moved has its subtree re-split in proportion to
       what it held, recursively, so a territory's children still sum to the
       territory.

    Returns the number of allocation rows written.
    """
    from . import adjustments

    rows = session.execute(
        select(TargetAllocation).where(
            TargetAllocation.version_id == version.version_id)
    ).scalars().all()
    if not rows:
        return 0

    #: ``(material, month) -> {(level, code): row}``, plus the parent index.
    slices: dict[tuple[str, str], dict[tuple[str, str], TargetAllocation]] = \
        defaultdict(dict)
    for row in rows:
        slices[(row.material_code, row.target_month)][(row.level, row.node_code)] = row

    relevant = [key for key in slices
                if (material_code is None or key[0] == material_code)
                and (level, node_code) in slices[key]]
    if not relevant:
        return 0

    current_total = sum(
        (Decimal(str(slices[key][(level, node_code)].current_volume))
         for key in relevant), Decimal(0))
    delta = Decimal(str(approved_volume)) - current_total
    if delta == 0:
        return 0

    # The change split across the node's own slices, largest remainder, in
    # proportion to what each holds. A node whose slices are all zero has no
    # profile to preserve, so the change is split evenly between them — which
    # ``distribute_map`` already does when every weight is zero.
    weights = {
        f"{key[0]}|{key[1]}": Decimal(
            str(slices[key][(level, node_code)].current_volume))
        for key in relevant
    }
    per_slice, _ = rounding.distribute_map(delta, weights)

    touched = 0
    for key in relevant:
        slice_rows = slices[key]
        share = per_slice[f"{key[0]}|{key[1]}"]
        if share == 0:
            continue
        node_row = slice_rows[(level, node_code)]

        siblings = {
            code: Decimal(str(row.current_volume))
            for (row_level, code), row in slice_rows.items()
            if row_level == level
            and row.parent_code == node_row.parent_code
            and row.parent_level == node_row.parent_level
        }
        if node_code not in siblings:  # pragma: no cover - defensive
            continue

        if len(siblings) == 1:
            # An only child cannot be funded by anybody. Its parent's figure has
            # to move with it, and so does every figure above — which is a
            # change to the country target, not a revision. Refused rather than
            # half-applied.
            raise OnlyChildRevision(level, node_code)

        updated = adjustments.apply_to_siblings(siblings, node_code, share)
        for code, value in updated.items():
            row = slice_rows[(level, code)]
            if Decimal(str(row.current_volume)) != value:
                row.current_volume = value
                touched += 1
                touched += _resplit_subtree(slice_rows, level, code, value)

    session.flush()
    return touched


class OnlyChildRevision(TargetManagementError):
    """A node with no siblings cannot be revised without moving its parent.

    Not a limitation to work around. The parent's total is fixed by the level
    above it, all the way up to the country target somebody typed — so changing
    an only child means changing that target, which is a different act with its
    own screen and its own approval path.
    """

    code = "TARGET_REVISION_ONLY_CHILD"

    def __init__(self, level: str, node_code: str) -> None:
        super().__init__(
            f"{node_code} has no siblings at {level}",
            user_message=(
                f"{node_code} is the only {level.replace('_', ' ')} under its "
                f"parent, so there is nobody to fund a change to it. Revise the "
                f"level above, or change the country target."
            ),
            details={"level": level, "node_code": node_code},
        )


def _resplit_subtree(slice_rows: dict[tuple[str, str], TargetAllocation],
                     level: str, node_code: str, total: Decimal) -> int:
    """Re-split a node's new figure across its children, and theirs, and so on.

    In proportion to what each already held, through the same largest-remainder
    distribution — so the subtree still sums to its root exactly, and the shape
    the allocation engine produced is preserved rather than replaced by an even
    split.

    A node whose children all hold zero is split evenly, which is what
    ``distribute_map`` does with zero weights and is the only defensible answer:
    there is no profile to preserve.
    """
    children = {
        code: Decimal(str(row.current_volume))
        for (child_level, code), row in slice_rows.items()
        if row.parent_level == level and row.parent_code == node_code
    }
    if not children:
        return 0

    child_level = next(
        row.level for (row_level, code), row in slice_rows.items()
        if row.parent_level == level and row.parent_code == node_code
    )
    updated, _ = rounding.distribute_map(total, children)
    touched = 0
    for code, value in updated.items():
        row = slice_rows[(child_level, code)]
        if Decimal(str(row.current_volume)) != value:
            row.current_volume = value
            touched += 1
        touched += _resplit_subtree(slice_rows, child_level, code, value)
    return touched


def _store_adjustment(session: Session, user: UserContext, *,
                      version: TargetVersion, level: str, node_code: str,
                      material_code: str | None, delta: Decimal,
                      reason: str) -> None:
    """Keep the approved change as a standing instruction for the next run.

    Without this an allocation re-run after loading more sales would silently
    discard a figure somebody signed off — the same failure ``target_adjustment``
    was introduced to prevent for management adjustments, and the reason a
    revision reuses that mechanism rather than inventing a second one.

    An existing standing instruction at the node is **replaced**, not added to:
    the approved figure is what stands, and two contradictory instructions would
    leave the engine choosing between them.
    """
    existing = session.execute(
        select(TargetAdjustment).where(
            TargetAdjustment.version_id == version.version_id,
            TargetAdjustment.level == level,
            TargetAdjustment.node_code == node_code,
            TargetAdjustment.material_code == material_code)
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if existing is None:
        session.add(TargetAdjustment(
            version_id=version.version_id, level=level, node_code=node_code,
            material_code=material_code, adjustment_volume=delta,
            reason=reason, adjusted_by=user.username, adjusted_at=now))
    else:
        existing.adjustment_volume = (
            Decimal(str(existing.adjustment_volume)) + delta)
        existing.reason = reason
        existing.adjusted_by = user.username
        existing.adjusted_at = now
    session.flush()


def _record_approval(session: Session, *, version: TargetVersion,
                     user: UserContext, row: matrix.MatrixRow | None,
                     action: str, level: str | None, node_code: str | None,
                     comment: str | None) -> None:
    """Append one act to the approval log. Never updated, never removed."""
    session.add(TargetApproval(
        version_id=version.version_id, level=level, node_code=node_code,
        action=action, actor=user.username, actor_role=user.role,
        approval_sequence=row.approval_sequence if row else None,
        comment=comment,
    ))


# ---------------------------------------------------------------------------
# Reading them back
# ---------------------------------------------------------------------------


def for_version(session: Session, version_id: int, *,
                status: tuple[str, ...] | None = None) -> list[TargetRevision]:
    conditions = [TargetRevision.version_id == version_id]
    if status:
        conditions.append(TargetRevision.status.in_(status))
    return list(session.execute(
        select(TargetRevision).where(and_(*conditions))
        .order_by(TargetRevision.revision_id.desc())
    ).scalars())


def open_count(session: Session, version_id: int) -> int:
    """How many requests are still outstanding. Final approval waits on zero."""
    return int(session.execute(
        select(func.count(TargetRevision.revision_id)).where(
            TargetRevision.version_id == version_id,
            TargetRevision.status.in_(RevisionStatus.OPEN))
    ).scalar() or 0)


def to_dict(revision: TargetRevision, *, node_name: str | None = None) -> dict[str, Any]:
    """One request as the Revision screen renders it.

    All three volumes are carried, and the approved one stays ``None`` until
    somebody decides — the screen shows a dash there rather than repeating the
    requested figure, because "asked for 90,000" and "granted 90,000" are things
    a reader must be able to tell apart.
    """
    return {
        "revision_id": revision.revision_id,
        "version_id": revision.version_id,
        "level": revision.level,
        "node_code": revision.node_code,
        "node_name": node_name,
        "material_code": revision.material_code,
        "system_volume": float(revision.system_volume),
        "requested_volume": float(revision.requested_volume),
        "approved_volume": (None if revision.approved_volume is None
                            else float(revision.approved_volume)),
        "change_percent": (None if revision.change_percent is None
                           else float(revision.change_percent)),
        "status": revision.status,
        "escalated_to_role": revision.escalated_to_role,
        "reason": revision.reason,
        "requested_by": revision.requested_by,
        "decided_by": revision.decided_by,
        "decided_at": (revision.decided_at.isoformat()
                       if revision.decided_at else None),
        "requested_at": (revision.created_at.isoformat()
                         if revision.created_at else None),
    }


def _node_label(level: str | None, node_code: str | None,
                material_code: str | None) -> str:
    label = f"{(level or '').replace('_', ' ').title()} {node_code or ''}".strip()
    return f"{label} · {material_code}" if material_code else label


def _volume_text(value: Any) -> str:
    return f"{float(value):,.0f}"


__all__ = [
    "REVISABLE",
    "NodeNotAllocated",
    "NotRevisable",
    "InvalidRevisionVolume",
    "RevisionNotFound",
    "RevisionClosed",
    "RevisionOutOfScope",
    "RevisionNotYours",
    "RevisionExceedsLimit",
    "OnlyChildRevision",
    "node_volume",
    "request",
    "decide",
    "apply_to_allocation",
    "for_version",
    "open_count",
    "to_dict",
]
