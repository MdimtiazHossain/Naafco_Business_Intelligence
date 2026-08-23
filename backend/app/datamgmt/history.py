"""Recording and reading what happened to a record.

Two things are written on every change, and they are not redundant:

* an **audit entry** — the platform-wide "who did what, when", queried by user
  and by action, which security review reads;
* a **change-log entry** — this record's own history, queried by record, which
  the detail page's History tab reads.

The change log keeps values; the audit log keeps none. That split is deliberate.
The audit trail is read broadly and should not become a place business values
accumulate, while a history tab is useless without them: "Territory changed" is
not an answer, "Territory changed from TR001 to TR004" is.

Only the fields that actually moved are stored. A record with forty columns
edited in one is forty lines of noise and one line of signal if the whole row is
kept twice.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from ..ai import queries as q
from ..ai.permission_filter import UserContext
from ..auth import audit
from ..database.models_admin import ChangeAction, DataChangeLog
from ..database.models_ai import AuditAction
from .catalogue import ManagedEntity

#: Change action -> the audit action recorded alongside it.
_AUDIT_ACTION: dict[str, str] = {
    ChangeAction.CREATED: AuditAction.RECORD_CREATED,
    ChangeAction.UPDATED: AuditAction.RECORD_UPDATED,
    ChangeAction.DELETED: AuditAction.RECORD_DELETED,
    ChangeAction.RESTORED: AuditAction.RECORD_RESTORED,
    ChangeAction.VOIDED: AuditAction.RECORD_VOIDED,
    ChangeAction.UNVOIDED: AuditAction.RECORD_UNVOIDED,
}


@dataclass
class Change:
    """What one write actually altered."""

    action: str
    old_values: dict[str, Any]
    new_values: dict[str, Any]

    @property
    def fields(self) -> list[str]:
        return sorted({*self.old_values, *self.new_values})

    @property
    def is_empty(self) -> bool:
        return not self.old_values and not self.new_values


def diff(before: dict[str, Any], after: dict[str, Any]) -> Change:
    """The fields whose value changed, old and new.

    Compared as text, because a form submits ``"12"`` where the column holds
    ``Decimal("12.0000")`` and reporting that as a change would fill the history
    with edits nobody made.
    """
    old: dict[str, Any] = {}
    new: dict[str, Any] = {}
    for name, value in after.items():
        previous = before.get(name)
        if _comparable(previous) == _comparable(value):
            continue
        old[name] = q.normalize_value(previous)
        new[name] = q.normalize_value(value)
    return Change(ChangeAction.UPDATED, old, new)


def _comparable(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{float(value):.6f}"
    try:
        return f"{float(str(value)):.6f}"
    except (TypeError, ValueError):
        return str(value).strip()


def record(session: Session, *, entity: ManagedEntity, record_key: str,
           label: str | None, action: str, user: UserContext,
           change: Change | None = None, reason: str | None = None,
           ip_address: str | None = None) -> DataChangeLog:
    """Write both trails for one change. Caller owns the transaction."""
    entry = DataChangeLog(
        entity_key=entity.key,
        record_key=str(record_key)[:256],
        record_label=label,
        action=action,
        user_id=user.user_id,
        username=user.username,
        role=user.role,
        changed_fields=change.fields if change else None,
        old_values=audit.sanitize(change.old_values) if change else None,
        new_values=audit.sanitize(change.new_values) if change else None,
        reason=reason,
        ip_address=ip_address,
    )
    session.add(entry)

    audit.record(
        session, action=_AUDIT_ACTION.get(action, AuditAction.ADMIN_CHANGE),
        user_id=user.user_id, username=user.username,
        resource=f"{entity.key}:{record_key}",
        ip_address=ip_address,
        detail={
            "entity": entity.key,
            "record": str(record_key),
            "label": label,
            # Field *names* only. The values live in the change log.
            "fields": change.fields if change else [],
            "reason": reason,
        },
    )
    session.flush()
    return entry


def for_record(session: Session, entity: ManagedEntity, record_key: str, *,
               limit: int = 100, offset: int = 0,
               ) -> tuple[list[dict[str, Any]], int]:
    """One record's history, newest first, with the total for paging."""
    conditions = (
        DataChangeLog.entity_key == entity.key,
        DataChangeLog.record_key == str(record_key),
    )
    total = session.execute(
        select(func.count()).select_from(DataChangeLog).where(*conditions)
    ).scalar_one()
    rows = session.execute(
        select(DataChangeLog).where(*conditions)
        .order_by(desc(DataChangeLog.change_id)).limit(limit).offset(offset)
    ).scalars().all()
    return [to_dict(row) for row in rows], total


def to_dict(entry: DataChangeLog) -> dict[str, Any]:
    """One history line, with the per-field before/after already paired up.

    Pairing them here rather than in the browser means the History tab renders
    the same way wherever it is used, and a field present in one side only —
    a value first set, or cleared — still produces a row.
    """
    old = entry.old_values or {}
    new = entry.new_values or {}
    return {
        "change_id": entry.change_id,
        "action": entry.action,
        "username": entry.username,
        "role": entry.role,
        "reason": entry.reason,
        "created_at": entry.created_at,
        "changed_fields": entry.changed_fields or [],
        "changes": [
            {"field": name, "old": old.get(name), "new": new.get(name)}
            for name in sorted({*old, *new})
        ],
    }


def summarise(session: Session, entity: ManagedEntity, record_key: str,
              ) -> dict[str, Any] | None:
    """The most recent change, for the "last modified" line on a detail page."""
    entry = session.execute(
        select(DataChangeLog)
        .where(DataChangeLog.entity_key == entity.key,
               DataChangeLog.record_key == str(record_key))
        .order_by(desc(DataChangeLog.change_id)).limit(1)
    ).scalar_one_or_none()
    if entry is None:
        return None
    return {
        "action": entry.action,
        "username": entry.username,
        "at": entry.created_at,
    }


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


__all__ = ["Change", "diff", "record", "for_record", "summarise", "to_dict",
           "now"]
