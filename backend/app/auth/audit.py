"""Audit logging.

Records who did what. Everything written here is safe to keep: the action, the
resource, sanitised parameters, and the request origin. Secrets are stripped by
:func:`sanitize` before anything is stored, so a careless caller cannot leak a
password or token into the audit trail.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from ..database.models_ai import AuditAction, AuditLog

logger = logging.getLogger("app.auth.audit")

#: Keys whose values must never be recorded, matched case-insensitively as
#: substrings so ``db_password`` and ``X-Api-Key`` are both caught.
SECRET_KEYS = (
    "password", "passwd", "secret", "token", "api_key", "apikey", "authorization",
    "credential", "connection_string", "database_url", "dsn", "cookie", "session_id",
)

REDACTED = "[redacted]"
MAX_VALUE_LENGTH = 500


def is_secret(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SECRET_KEYS)


def sanitize(detail: Any, _depth: int = 0) -> Any:
    """Strip secrets and bound the size of an audit detail payload."""
    if _depth > 4:
        return REDACTED
    if isinstance(detail, dict):
        return {
            key: (REDACTED if is_secret(str(key)) else sanitize(value, _depth + 1))
            for key, value in detail.items()
        }
    if isinstance(detail, (list, tuple)):
        return [sanitize(item, _depth + 1) for item in detail][:50]
    if isinstance(detail, str) and len(detail) > MAX_VALUE_LENGTH:
        return detail[:MAX_VALUE_LENGTH] + "…"
    return detail


def record(session: Session, *, action: str, user_id: int | None = None,
           username: str | None = None, resource: str | None = None,
           detail: dict[str, Any] | None = None, ip_address: str | None = None,
           user_agent: str | None = None, success: bool = True) -> AuditLog:
    """Write one audit entry. Never raises into the caller's request path."""
    entry = AuditLog(
        user_id=user_id,
        username=username,
        action=action,
        resource=resource,
        detail=sanitize(detail) if detail else None,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:255] or None,
        success=success,
    )
    try:
        session.add(entry)
        session.flush()
    except Exception:  # noqa: BLE001 - auditing must not break the request
        logger.exception("could not write audit entry for action %s", action)
        session.rollback()
    return entry


def recent(session: Session, *, limit: int = 100, offset: int = 0,
           action: str | None = None, username: str | None = None,
           ) -> tuple[list[AuditLog], int]:
    """Audit entries, newest first, with the total for pagination."""
    conditions = []
    if action:
        conditions.append(AuditLog.action == action)
    if username:
        conditions.append(AuditLog.username == username)

    total = session.execute(
        select(func.count()).select_from(AuditLog).where(*conditions)
    ).scalar_one()
    rows = session.execute(
        select(AuditLog).where(*conditions)
        .order_by(desc(AuditLog.audit_id)).limit(limit).offset(offset)
    ).scalars().all()
    return list(rows), total


def to_dict(entry: AuditLog) -> dict[str, Any]:
    return {
        "audit_id": entry.audit_id,
        "user_id": entry.user_id,
        "username": entry.username,
        "action": entry.action,
        "resource": entry.resource,
        "detail": entry.detail,
        "ip_address": entry.ip_address,
        "success": entry.success,
        "created_at": entry.created_at,
    }


def client_ip(request) -> str | None:
    """Best-effort client IP, honouring one proxy hop."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host[:64] if request.client else None


__all__ = ["record", "recent", "to_dict", "sanitize", "is_secret", "client_ip",
           "AuditAction"]
