"""User-account helpers shared by the admin API and the CLI script.

The account lifecycle has two representations that must never disagree:
``AppUser.status`` (ACTIVE / INACTIVE / LOCKED), which is what an administrator
sees and sets, and ``AppUser.is_active``, which is what the login path and the
Phase 3 user loader check. :func:`apply_status` is the single place that keeps
them in step, so there is no way to lock an account and still leave it able to
sign in.
"""

from __future__ import annotations

from typing import Any

from ..database.models_ai import AppUser, UserStatus


def apply_status(user: AppUser, status: str) -> str:
    """Set the account status and derive ``is_active`` from it."""
    if status not in UserStatus.ALL:
        raise ValueError(
            f"Unknown status {status!r}. Valid: {', '.join(UserStatus.ALL)}."
        )
    user.status = status
    user.is_active = status == UserStatus.ACTIVE
    return status


def status_of(user: AppUser) -> str:
    """The account's status, tolerating rows written before the column existed."""
    if getattr(user, "status", None):
        return user.status
    return UserStatus.ACTIVE if user.is_active else UserStatus.INACTIVE


def admin_payload(user: AppUser) -> dict[str, Any]:
    """The full administrative view of a user. Never includes the password hash."""
    from ..api.routes_auth import user_payload

    return {
        **user_payload(user),
        "is_active": user.is_active,
        "status": status_of(user),
        "department": user.department,
        "designation": user.designation,
        "mobile": user.phone_number,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


__all__ = ["apply_status", "status_of", "admin_payload"]
