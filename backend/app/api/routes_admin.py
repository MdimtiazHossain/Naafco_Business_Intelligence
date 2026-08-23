"""Admin panel: users, roles, section permissions, data scopes and audit logs.

Every endpoint depends on :func:`require_admin`, so authorisation is enforced in
the backend. Hiding a menu item in the UI is presentation, not security — an
admin-only route called directly by a non-admin still returns 403.

Three things are kept deliberately separate, because conflating them is how
access-control systems become impossible to reason about:

* the **role** — the user's basic authority, and the ceiling on what they may
  ever hold;
* the **section permission** — which parts of the application they may open,
  configurable per user;
* the **data scope** — which slice of the organisation they see inside those
  parts.

All three are recorded in the audit log whenever they change.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from ..ai.permission_filter import FILTER_FIELD_BY_LEVEL, UserContext
from ..auth import audit
from ..auth.permissions import (
    action_matrix,
    resolve_all,
    role_action_map,
    role_permission_map,
    user_action_map,
    user_permission_map,
)
from ..auth.security import hash_password, validate_password_strength
from ..auth.users import admin_payload, apply_status, status_of
from ..database.models_admin import (
    RoleSectionPermission,
    UploadBatch,
    UploadStatus,
    UserSectionPermission,
)
from ..database.models_ai import AppUser, AuditAction, Role, UserStatus
from ..etl.mapping import MasterDataIndex
from ..security.sections import (
    ACCESS_VALUES,
    ALLOW,
    DENY,
    SECTIONS,
    Action,
    get_section,
)
from .deps import get_session, internal_error, require_admin

logger = logging.getLogger("app.api.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])


class UserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=2, max_length=64,
                          pattern=r"^[A-Za-z0-9._@-]+$")
    password: str = Field(min_length=1, max_length=256)
    role: str
    display_name: str | None = Field(None, max_length=255)
    email: str | None = Field(None, max_length=255)
    employee_id: str | None = Field(None, max_length=64)
    phone_number: str | None = Field(None, max_length=32)
    department: str | None = Field(None, max_length=255)
    designation: str | None = Field(None, max_length=255)
    status: str = Field(UserStatus.ACTIVE)
    preferred_language: str = Field("en", pattern="^(en|bn)$")
    #: ``{"region_code": ["REG001"]}``
    data_scope: dict[str, list[str]] = Field(default_factory=dict)
    #: ``{"sales": "ALLOW", "stock": "DENY"}``. Sections not listed keep the
    #: role default, so a create call need not enumerate all of them.
    section_permissions: dict[str, str] = Field(default_factory=dict)


class UserUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str | None = None
    display_name: str | None = Field(None, max_length=255)
    email: str | None = Field(None, max_length=255)
    employee_id: str | None = Field(None, max_length=64)
    phone_number: str | None = Field(None, max_length=32)
    department: str | None = Field(None, max_length=255)
    designation: str | None = Field(None, max_length=255)
    status: str | None = None
    preferred_language: str | None = Field(None, pattern="^(en|bn)$")
    is_active: bool | None = None
    data_scope: dict[str, list[str]] | None = None
    password: str | None = Field(None, min_length=1, max_length=256)


class PermissionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: ``{section_key: "ALLOW" | "DENY"}``. A section omitted here has its
    #: explicit permission removed, falling back to the role default.
    permissions: dict[str, str]
    #: ``{section_key: {"EDIT": "ALLOW", "DELETE": "DENY"}}``. Optional: a
    #: section with no entry keeps whatever its actions resolve to by default,
    #: so an existing caller that sends only ``permissions`` is unaffected.
    actions: dict[str, dict[str, str]] | None = None


def _validate_role(role: str) -> str:
    if role not in Role.ALL:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown role '{role}'. Valid roles: {', '.join(Role.ALL)}.",
        )
    return role


def _validate_status(value: str) -> str:
    if value not in UserStatus.ALL:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown status '{value}'. Valid: {', '.join(UserStatus.ALL)}.",
        )
    return value


def _validate_permissions(permissions: dict[str, str]) -> dict[str, str]:
    """Check the section keys and the ALLOW/DENY values before anything is saved."""
    cleaned: dict[str, str] = {}
    for key, access in permissions.items():
        try:
            get_section(key)
        except KeyError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        value = str(access).upper()
        if value not in ACCESS_VALUES:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Section '{key}' must be {ALLOW} or {DENY}, not '{access}'.",
            )
        cleaned[key] = value
    return cleaned


def _validate_actions(actions: dict[str, dict[str, str]] | None,
                      ) -> dict[str, dict[str, str]]:
    """Check section keys, action names and ALLOW/DENY before anything is saved.

    An action the section does not declare is rejected rather than stored and
    ignored: silently accepting ``DELETE`` on a report section would leave an
    administrator believing they had configured something that does not exist.
    """
    cleaned: dict[str, dict[str, str]] = {}
    for key, per_action in (actions or {}).items():
        try:
            section = get_section(key)
        except KeyError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        entry: dict[str, str] = {}
        for action, access in (per_action or {}).items():
            name = str(action).upper()
            if not section.supports(name):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"Section '{key}' does not support the '{name}' action. "
                    f"Supported: {', '.join(section.actions)}.",
                )
            value = str(access).upper()
            if value not in ACCESS_VALUES:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"Action '{name}' on '{key}' must be {ALLOW} or {DENY}, "
                    f"not '{access}'.",
                )
            entry[name] = value
        if entry:
            cleaned[key] = entry
    return cleaned


def _actions_need_their_section(permissions: dict[str, str],
                                actions: dict[str, dict[str, str]]) -> None:
    """Action overrides are stored on the section's permission row.

    So a section cannot carry overrides without also carrying an explicit
    ALLOW/DENY. Refusing is better than inventing one: an ALLOW conjured to hold
    an override would silently grant a section the administrator never granted.
    """
    orphans = sorted(set(actions) - set(permissions))
    if orphans:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Action overrides need an explicit section permission. Add "
            f"{', '.join(orphans)} to 'permissions' as well.",
        )


def _validate_scope(session: Session, scope: dict[str, list[str]]) -> dict[str, list[str]]:
    """Reject a scope that names codes the master data does not contain.

    Without this an admin could grant access to a region that does not exist,
    which would silently behave as "no access" and be very hard to diagnose.
    """
    cleaned: dict[str, list[str]] = {}
    for level, codes in (scope or {}).items():
        if level not in FILTER_FIELD_BY_LEVEL:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Unknown scope level '{level}'. Valid levels: "
                + ", ".join(sorted(FILTER_FIELD_BY_LEVEL)),
            )
        values = [str(code).strip() for code in codes if str(code).strip()]
        if values:
            cleaned[level] = values
    if not cleaned:
        return {}

    index = MasterDataIndex(session)
    unknown = [
        f"{level}={code}"
        for level, codes in cleaned.items()
        for code in codes
        if code not in index.ids.get(level, {})
    ]
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "These scope codes do not exist in the master data: " + ", ".join(unknown),
        )
    return cleaned


def _user_or_404(session: Session, user_id: int) -> AppUser:
    user = session.get(AppUser, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")
    return user


def _admin_count(session: Session) -> int:
    return session.execute(
        select(func.count()).select_from(AppUser)
        .where(AppUser.role.in_(Role.ADMIN_ROLES), AppUser.is_active.is_(True))
    ).scalar_one()


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


@router.get("/roles")
def roles(session: Session = Depends(get_session),
          _: UserContext = Depends(require_admin)) -> dict[str, Any]:
    """Roles, their section defaults, and the scope levels the UI can offer."""
    counts = dict(session.execute(
        select(AppUser.role, func.count()).group_by(AppUser.role)
    ).all())
    return {
        "roles": [
            {
                "role": role,
                "unrestricted": role in Role.UNRESTRICTED,
                "is_admin": role in Role.ADMIN_ROLES,
                "user_count": counts.get(role, 0),
                "sections": {
                    section.key: (
                        ALLOW if _role_access(session, role, section.key) else DENY
                    )
                    for section in SECTIONS
                },
            }
            for role in Role.ALL
        ],
        "scope_levels": sorted(FILTER_FIELD_BY_LEVEL),
        "statuses": list(UserStatus.ALL),
    }


def _role_access(session: Session, role: str, section_key: str) -> bool:
    section = get_section(section_key)
    stored = role_permission_map(session, role).get(section_key)
    if not section.role_may_hold(role):
        return False
    if stored is not None:
        return stored == ALLOW
    return section.role_default(role)


@router.get("/sections")
def sections(_: UserContext = Depends(require_admin)) -> dict[str, Any]:
    """The application-section catalogue the permission table is built from.

    Loaded from the application configuration rather than hard-coded in the UI,
    so a section added to the backend appears in the admin screen automatically.
    """
    return {
        "sections": [section.to_dict() for section in SECTIONS],
        "access_values": list(ACCESS_VALUES),
    }


@router.get("/summary")
def summary(session: Session = Depends(get_session),
            _: UserContext = Depends(require_admin)) -> dict[str, Any]:
    """Cards for the admin dashboard."""
    try:
        by_status = dict(session.execute(
            select(AppUser.status, func.count()).group_by(AppUser.status)
        ).all())
        upload_status = dict(session.execute(
            select(UploadBatch.status, func.count()).group_by(UploadBatch.status)
        ).all())
        from datetime import datetime, timedelta, timezone

        since = datetime.now(timezone.utc) - timedelta(days=1)
        uploads_today = session.execute(
            select(func.count()).select_from(UploadBatch)
            .where(UploadBatch.started_at >= since)
        ).scalar_one()
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "admin summary") from exc

    total_users = sum(by_status.values())
    return {
        "total_users": total_users,
        "active_users": by_status.get(UserStatus.ACTIVE, 0),
        "inactive_users": by_status.get(UserStatus.INACTIVE, 0),
        "locked_users": by_status.get(UserStatus.LOCKED, 0),
        "roles": len(Role.ALL),
        "roles_in_use": session.execute(
            select(func.count(func.distinct(AppUser.role)))
        ).scalar_one(),
        "sections": len(SECTIONS),
        "pending_imports": (upload_status.get(UploadStatus.VALIDATED, 0)
                            + upload_status.get(UploadStatus.UPLOADED, 0)),
        "failed_imports": upload_status.get(UploadStatus.FAILED, 0),
        "uploads_today": uploads_today,
    }


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@router.get("/users")
def list_users(
    search: str | None = Query(None, max_length=100),
    role: str | None = Query(None, max_length=32),
    user_status: str | None = Query(None, alias="status", max_length=16),
    sort_by: str = Query("username", pattern="^(username|display_name|role|status|"
                                             "last_login_at|created_at)$"),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
    _: UserContext = Depends(require_admin),
) -> dict[str, Any]:
    conditions = []
    if search:
        needle = f"%{search.strip()}%"
        conditions.append(
            AppUser.username.ilike(needle)
            | AppUser.display_name.ilike(needle)
            | AppUser.email.ilike(needle)
            | AppUser.employee_id.ilike(needle)
        )
    if role:
        conditions.append(AppUser.role == role)
    if user_status:
        conditions.append(AppUser.status == user_status.upper())

    total = session.execute(
        select(func.count()).select_from(AppUser).where(*conditions)
    ).scalar_one()
    order_column = getattr(AppUser, sort_by)
    rows = session.execute(
        select(AppUser).where(*conditions)
        .order_by(desc(order_column) if sort_dir == "desc" else order_column)
        .limit(limit).offset(offset)
    ).scalars().all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "users": [admin_payload(user) for user in rows],
    }


@router.get("/users/{user_id}")
def get_user(user_id: int, session: Session = Depends(get_session),
             _: UserContext = Depends(require_admin)) -> dict[str, Any]:
    return admin_payload(_user_or_404(session, user_id))


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(
    request: UserCreateRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    admin: UserContext = Depends(require_admin),
) -> dict[str, Any]:
    _validate_role(request.role)
    _validate_status(request.status)
    problems = validate_password_strength(request.password)
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "The password " + "; ".join(problems) + ".")
    scope = _validate_scope(session, request.data_scope)
    permissions = _validate_permissions(request.section_permissions)

    if request.role not in Role.UNRESTRICTED and not scope:
        logger.info("creating %s with no data scope; they will see no data",
                    request.username)

    existing = session.execute(
        select(AppUser).where(AppUser.username == request.username)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"A user named '{request.username}' already exists.")

    user = AppUser(
        username=request.username,
        display_name=request.display_name or request.username,
        email=request.email,
        role=request.role,
        employee_id=request.employee_id,
        phone_number=request.phone_number,
        department=request.department,
        designation=request.designation,
        preferred_language=request.preferred_language,
        data_scope=scope or None,
        password_hash=hash_password(request.password),
    )
    apply_status(user, request.status)
    session.add(user)
    try:
        session.flush()
    except Exception as exc:  # noqa: BLE001 - unique email/phone collisions
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "That email or phone number is already in use.") from exc

    if permissions:
        _write_user_permissions(session, user, permissions, admin.username)

    audit.record(session, action=AuditAction.USER_CREATED, user_id=admin.user_id,
                 username=admin.username, resource=f"user:{request.username}",
                 ip_address=audit.client_ip(http_request),
                 detail={"created": request.username, "role": request.role,
                         "status": request.status, "scope": scope,
                         "section_permissions": permissions})
    session.commit()
    return admin_payload(user)


@router.patch("/users/{user_id}")
def update_user(
    user_id: int,
    request: UserUpdateRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    admin: UserContext = Depends(require_admin),
) -> dict[str, Any]:
    user = _user_or_404(session, user_id)

    changed: dict[str, Any] = {}
    #: Each entry becomes its own audit record, so "role changed" and "scope
    #: changed" are searchable events rather than a diff buried in one blob.
    events: list[tuple[str, dict[str, Any]]] = []

    if request.role is not None:
        _validate_role(request.role)
        # Guard against an admin removing the last way back into the panel.
        if (user.role in Role.ADMIN_ROLES and request.role not in Role.ADMIN_ROLES
                and _admin_count(session) <= 1):
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "This is the last administrator; assign another "
                                "administrator before changing this role.")
        if request.role != user.role:
            events.append((AuditAction.ROLE_CHANGED,
                           {"old": user.role, "new": request.role}))
        user.role = request.role
        changed["role"] = request.role

    if request.data_scope is not None:
        old_scope = user.data_scope
        user.data_scope = _validate_scope(session, request.data_scope) or None
        changed["scope"] = user.data_scope
        events.append((AuditAction.SCOPE_CHANGED,
                       {"old": old_scope, "new": user.data_scope}))

    for field in ("display_name", "email", "employee_id", "phone_number",
                  "department", "designation", "preferred_language"):
        value = getattr(request, field)
        if value is not None:
            setattr(user, field, value)
            changed[field] = value

    # ``status`` is the administrator-facing control; ``is_active`` is accepted
    # for backwards compatibility and mapped onto it.
    new_status = request.status
    if new_status is None and request.is_active is not None:
        new_status = UserStatus.ACTIVE if request.is_active else UserStatus.INACTIVE
    if new_status is not None:
        _validate_status(new_status)
        if (new_status != UserStatus.ACTIVE and user.role in Role.ADMIN_ROLES
                and _admin_count(session) <= 1):
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "This is the last administrator and cannot be "
                                "deactivated.")
        old_status = status_of(user)
        apply_status(user, new_status)
        changed["status"] = new_status
        changed["is_active"] = user.is_active
        if new_status != old_status:
            events.append((
                AuditAction.USER_ACTIVATED if new_status == UserStatus.ACTIVE
                else AuditAction.USER_DEACTIVATED,
                {"old": old_status, "new": new_status},
            ))

    if request.password is not None:
        problems = validate_password_strength(request.password)
        if problems:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "The password " + "; ".join(problems) + ".")
        user.password_hash = hash_password(request.password)
        changed["password"] = "reset"      # the value itself is never recorded
        events.append((AuditAction.PASSWORD_RESET, {"target": user.username}))

    ip = audit.client_ip(http_request)
    audit.record(session, action=AuditAction.USER_UPDATED, user_id=admin.user_id,
                 username=admin.username, resource=f"user:{user.username}",
                 ip_address=ip,
                 detail={"updated": user.username, "changes": changed})
    for action, detail in events:
        audit.record(session, action=action, user_id=admin.user_id,
                     username=admin.username, resource=f"user:{user.username}",
                     ip_address=ip, detail={"target": user.username, **detail})
    session.commit()
    return admin_payload(user)


# ---------------------------------------------------------------------------
# Section permissions
# ---------------------------------------------------------------------------


@router.get("/users/{user_id}/permissions")
def get_user_permissions(
    user_id: int,
    session: Session = Depends(get_session),
    _: UserContext = Depends(require_admin),
) -> dict[str, Any]:
    """One user's section permissions: effective, role default and override.

    All three are returned because an administrator needs to see *why* a
    section is on — an ALLOW inherited from the role reads very differently
    from one granted to this person specifically.
    """
    user = _user_or_404(session, user_id)
    decisions = resolve_all(session, user.user_id, user.role)
    effective = action_matrix(session, user.user_id, user.role)
    stored_actions = user_action_map(session, user.user_id)
    return {
        "user": admin_payload(user),
        "sections": [
            {
                **section.to_dict(),
                **decisions[section.key].to_dict(),
                # Three views of the same thing, for the same reason the section
                # itself reports three: what applies, what was set here, and
                # what the role would give on its own.
                "action_access": {
                    action: ALLOW if allowed else DENY
                    for action, allowed in effective[section.key].items()
                },
                "action_overrides": stored_actions.get(section.key, {}),
                "action_defaults": {
                    action: ALLOW if section.action_default(action, user.role)
                    else DENY
                    for action in section.actions
                },
            }
            for section in SECTIONS
        ],
        "access_values": list(ACCESS_VALUES),
        "all_actions": list(Action.ALL),
    }


@router.put("/users/{user_id}/permissions")
def set_user_permissions(
    user_id: int,
    request: PermissionsRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    admin: UserContext = Depends(require_admin),
) -> dict[str, Any]:
    """Replace a user's explicit section permissions.

    A section listed here is stored as an explicit ALLOW or DENY; a section left
    out has any explicit permission removed and falls back to the role default.
    Storing an ALLOW for a section the user's role may never hold is accepted
    but has no effect — :mod:`app.auth.permissions` applies the role ceiling
    first — and the response shows it as denied so the outcome is never a
    surprise.
    """
    user = _user_or_404(session, user_id)
    permissions = _validate_permissions(request.permissions)
    actions = _validate_actions(request.actions)
    _actions_need_their_section(permissions, actions)

    before = user_permission_map(session, user.user_id)
    before_actions = user_action_map(session, user.user_id)
    _write_user_permissions(session, user, permissions, admin.username,
                            replace=True, actions=actions)

    audit.record(session, action=AuditAction.PERMISSION_CHANGED,
                 user_id=admin.user_id, username=admin.username,
                 resource=f"user:{user.username}",
                 ip_address=audit.client_ip(http_request),
                 detail={"target": user.username, "old": before,
                         "new": permissions, "old_actions": before_actions,
                         "new_actions": actions})
    session.commit()

    return get_user_permissions(user_id, session, admin)


def _write_user_permissions(session: Session, user: AppUser,
                            permissions: dict[str, str], actor: str,
                            replace: bool = False,
                            actions: dict[str, dict[str, str]] | None = None,
                            ) -> None:
    """Store the section grants, and the per-action overrides alongside them.

    A section carrying action overrides needs a row even when its access is the
    role default, because the overrides live on that row — so any section named
    in ``actions`` is written whether or not it appears in ``permissions``.
    """
    actions = actions or {}
    existing = {
        row.section_key: row
        for row in session.execute(
            select(UserSectionPermission)
            .where(UserSectionPermission.user_id == user.user_id)
        ).scalars()
    }
    for key, access in permissions.items():
        row = existing.get(key)
        if row is None:
            session.add(UserSectionPermission(
                user_id=user.user_id, section_key=key, access=access,
                actions=actions.get(key) or None, updated_by=actor,
            ))
        else:
            row.access = access
            row.actions = actions.get(key) or None
            row.updated_by = actor
    if replace:
        for key, row in existing.items():
            if key not in permissions:
                session.delete(row)
    session.flush()


@router.get("/roles/{role}/permissions")
def get_role_permissions(
    role: str,
    session: Session = Depends(get_session),
    _: UserContext = Depends(require_admin),
) -> dict[str, Any]:
    """A role's section defaults."""
    _validate_role(role)
    stored = role_permission_map(session, role)
    stored_actions = role_action_map(session, role)
    return {
        "role": role,
        "is_admin": role in Role.ADMIN_ROLES,
        "sections": [
            {
                **section.to_dict(),
                "access": ALLOW if _role_access(session, role, section.key) else DENY,
                "explicit": stored.get(section.key),
                "catalogue_default": ALLOW if section.role_default(role) else DENY,
                "locked": not section.role_may_hold(role),
                "action_overrides": stored_actions.get(section.key, {}),
                "action_defaults": {
                    action: ALLOW if section.action_default(action, role) else DENY
                    for action in section.actions
                },
            }
            for section in SECTIONS
        ],
        "all_actions": list(Action.ALL),
    }


@router.put("/roles/{role}/permissions")
def set_role_permissions(
    role: str,
    request: PermissionsRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    admin: UserContext = Depends(require_admin),
) -> dict[str, Any]:
    """Change a role's section defaults.

    Restricted to a super administrator: a role default applies to every user
    who holds that role, so it is a far broader change than a per-user override.
    """
    if admin.role != Role.SUPER_ADMIN:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only a super administrator can change a role's default permissions.",
        )
    _validate_role(role)
    permissions = _validate_permissions(request.permissions)
    actions = _validate_actions(request.actions)
    _actions_need_their_section(permissions, actions)

    before = role_permission_map(session, role)
    before_actions = role_action_map(session, role)
    existing = {
        row.section_key: row
        for row in session.execute(
            select(RoleSectionPermission).where(RoleSectionPermission.role == role)
        ).scalars()
    }
    for key, access in permissions.items():
        row = existing.get(key)
        if row is None:
            session.add(RoleSectionPermission(role=role, section_key=key,
                                              access=access,
                                              actions=actions.get(key) or None,
                                              updated_by=admin.username))
        else:
            row.access = access
            row.actions = actions.get(key) or None
            row.updated_by = admin.username
    for key, row in existing.items():
        if key not in permissions:
            session.delete(row)
    session.flush()

    audit.record(session, action=AuditAction.PERMISSION_CHANGED,
                 user_id=admin.user_id, username=admin.username,
                 resource=f"role:{role}", ip_address=audit.client_ip(http_request),
                 detail={"role": role, "old": before, "new": permissions,
                         "old_actions": before_actions, "new_actions": actions})
    session.commit()
    return get_role_permissions(role, session, admin)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@router.get("/audit-logs")
def audit_logs(
    action: str | None = Query(None, max_length=48),
    username: str | None = Query(None, max_length=64),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
    _: UserContext = Depends(require_admin),
) -> dict[str, Any]:
    """The audit trail. Entries never contain secrets — see ``auth.audit``."""
    try:
        rows, total = audit.recent(session, limit=limit, offset=offset,
                                   action=action, username=username)
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "audit logs") from exc
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "actions": sorted(
            getattr(AuditAction, name) for name in dir(AuditAction) if name.isupper()
        ),
        "logs": [audit.to_dict(row) for row in rows],
    }
