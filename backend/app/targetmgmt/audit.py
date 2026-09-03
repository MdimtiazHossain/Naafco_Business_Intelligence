"""The business audit trail: every action on a target, with its reason.

Separate from :mod:`app.auth.audit`, which is the *security* trail an
administrator reads beside logins and permission changes. This one is what a
planner reads on the Audit Trail screen, and it answers a different question:
what was this figure, what did it become, and why.

Two rules make it trustworthy, and both are structural rather than
conventional:

* **It is written by the backend at the moment of the action.** There is no
  endpoint that writes a row here on its own, so nothing in the interface can
  add an entry that did not happen.
* **Nothing updates or deletes a row.** This module offers ``record`` and
  nothing else. A correction is a new action with its own row, which is the
  only way the sequence of what actually happened stays readable.

``record`` never raises into the caller. An action that succeeded must not be
undone because its audit row could not be written — but it must also not pass
silently, so the failure is logged with the action it belonged to.
"""

from __future__ import annotations

import logging

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ..database.models_target import TargetAudit

logger = logging.getLogger("app.targetmgmt.audit")


class TargetAction:
    """What was done. Stored verbatim, so these strings are part of the data.

    Named for the business act rather than the table write, because that is how
    the Audit Trail screen reads: "Target Submitted", not "row updated".
    """

    PLAN_CREATED = "PLAN_CREATED"
    PLAN_DELETED = "PLAN_DELETED"
    VERSION_CREATED = "VERSION_CREATED"
    COUNTRY_TARGET_EDITED = "COUNTRY_TARGET_EDITED"
    TARGET_UPLOADED = "TARGET_UPLOADED"
    ALLOCATION_GENERATED = "ALLOCATION_GENERATED"
    TARGET_SUBMITTED = "TARGET_SUBMITTED"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    REVISION_APPROVED = "REVISION_APPROVED"
    REVISION_REJECTED = "REVISION_REJECTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    TARGET_LOCKED = "TARGET_LOCKED"
    MANAGEMENT_ADJUSTED = "MANAGEMENT_ADJUSTED"
    MATRIX_UPDATED = "MATRIX_UPDATED"

    ALL = (PLAN_CREATED, PLAN_DELETED, VERSION_CREATED, COUNTRY_TARGET_EDITED,
           TARGET_UPLOADED, ALLOCATION_GENERATED, TARGET_SUBMITTED,
           REVISION_REQUESTED, REVISION_APPROVED, REVISION_REJECTED,
           APPROVED, REJECTED, TARGET_LOCKED, MANAGEMENT_ADJUSTED,
           MATRIX_UPDATED)


def record(session: Session, *, action: str, plan_id: int | None = None,
           version_id: int | None = None, actor: str | None = None,
           actor_role: str | None = None, node_label: str | None = None,
           old_value: str | None = None, new_value: str | None = None,
           reason: str | None = None) -> TargetAudit | None:
    """Write one trail entry. Flushed, not committed — the caller owns the commit.

    Flushing rather than committing is what keeps the entry and the action it
    describes in **one transaction**: if the action rolls back, so does its
    audit row, and the trail never claims something happened that did not.

    ``actor`` and ``actor_role`` are stored as text rather than joined to
    ``app_user``. An audit row must stay readable after an account is renamed or
    removed, and it must record the role held *then* — a user promoted next year
    did not approve last year's target as a zone manager.
    """
    entry = TargetAudit(
        plan_id=plan_id,
        version_id=version_id,
        action=action,
        actor=(actor or "")[:64] or None,
        actor_role=(actor_role or "")[:32] or None,
        node_label=(node_label or "")[:200] or None,
        old_value=old_value,
        new_value=new_value,
        reason=reason,
    )
    try:
        session.add(entry)
        session.flush()
        return entry
    except Exception:  # noqa: BLE001 - auditing must not undo the action
        logger.exception("could not write target audit entry for action %s", action)
        return None


def to_dict(entry: TargetAudit) -> dict:
    """One row as the Audit Trail screen renders it."""
    return {
        "audit_id": entry.audit_id,
        "plan_id": entry.plan_id,
        "version_id": entry.version_id,
        "action": entry.action,
        "actor": entry.actor,
        "actor_role": entry.actor_role,
        "node_label": entry.node_label,
        "old_value": entry.old_value,
        "new_value": entry.new_value,
        "reason": entry.reason,
        "occurred_at": entry.occurred_at.isoformat() if entry.occurred_at else None,
    }


def trail(session: Session, *, plan_id: int | None = None,
          version_id: int | None = None, action: str | None = None,
          actor: str | None = None, limit: int = 200,
          offset: int = 0) -> dict:
    """The business trail, newest first, with a total the page count comes from.

    Newest first because the question a reader brings to this screen is almost
    always "what just happened", and the total is returned beside the rows so a
    reader can tell a filter that found forty entries from one that found forty
    thousand and is showing the first page.

    Filtered on the server rather than in the browser. A plan's trail grows
    without bound — every country-target edit, every allocation, every revision,
    every signature — and shipping all of it so the browser can hide most of it
    would get slower exactly as the record it exists to keep gets more valuable.
    """
    conditions = []
    if plan_id is not None:
        conditions.append(TargetAudit.plan_id == plan_id)
    if version_id is not None:
        conditions.append(TargetAudit.version_id == version_id)
    if action:
        conditions.append(TargetAudit.action == action)
    if actor:
        conditions.append(TargetAudit.actor == actor)

    statement = select(TargetAudit)
    counter = select(func.count(TargetAudit.audit_id))
    if conditions:
        statement = statement.where(and_(*conditions))
        counter = counter.where(and_(*conditions))

    rows = session.execute(
        statement.order_by(TargetAudit.audit_id.desc())
        .limit(max(1, min(limit, 500))).offset(max(0, offset))
    ).scalars().all()
    return {
        "rows": [to_dict(row) for row in rows],
        "total": int(session.execute(counter).scalar() or 0),
        "actions": list(TargetAction.ALL),
    }


__all__ = ["TargetAction", "record", "to_dict", "trail"]
