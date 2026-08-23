"""Section authorisation — the second security layer, next to the data scope.

There are two orthogonal questions about any request:

1. **May this user open this part of the application at all?** That is the
   *section* permission, resolved here.
2. **Which slice of the organisation may they see inside it?** That is the
   *data scope*, resolved by :class:`app.ai.permission_filter.PermissionFilter`
   and deliberately left untouched by this module.

Both must pass. A regional manager with ``Sales = ALLOW`` scoped to Dhaka gets
Dhaka sales; the same manager with ``Sales = DENY`` gets ``403`` even though
their scope would have permitted the rows.

Precedence, highest authority first::

    system security   authenticated, active account
            ↓
    role              the section's role ceiling (locked_to_roles)
            ↓                and the role's default / stored role permission
    user override     user_section_permissions row
            ↓
    data scope        applied separately, per query
            ↓
    API result

Any layer that denies is final: a lower layer never re-grants what a higher one
withheld. Concretely, ``locked_to_roles`` is checked *before* the user override
is read, so writing ``admin = ALLOW`` against a VIEWER's row changes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..database.models_admin import RoleSectionPermission, UserSectionPermission
from ..database.models_ai import AuditAction, Role
from ..security.sections import (
    ALLOW,
    DENY,
    SECTION_BY_KEY,
    SECTIONS,
    Action,
    Section,
    get_section,
)

logger = logging.getLogger("app.auth.permissions")

#: One message for every refusal. It never says *why* a section is unavailable,
#: so probing the API cannot map out another user's permissions.
FORBIDDEN_MESSAGE = "You don't have permission to access this information."

#: Source of a resolved decision, surfaced to the admin UI so an administrator
#: can see whether an ALLOW came from the role or from an explicit override.
SOURCE_ROLE_LOCK = "ROLE_LOCK"
SOURCE_ROLE_DEFAULT = "ROLE_DEFAULT"
SOURCE_ROLE_PERMISSION = "ROLE_PERMISSION"
SOURCE_USER_PERMISSION = "USER_PERMISSION"


@dataclass(frozen=True)
class SectionDecision:
    """Why a user does or does not have a section."""

    section: str
    allowed: bool
    source: str
    #: What the role alone would give, ignoring the user override.
    role_allowed: bool
    #: The explicit user override, when one exists.
    user_access: str | None = None
    #: True when the role can never hold this section, so the override is moot.
    locked: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "section": self.section,
            "access": ALLOW if self.allowed else DENY,
            "allowed": self.allowed,
            "source": self.source,
            "role_access": ALLOW if self.role_allowed else DENY,
            "user_access": self.user_access,
            "locked": self.locked,
        }


def role_permission_map(session: Session, role: str) -> dict[str, str]:
    """Stored role-level overrides of the catalogue defaults."""
    rows = session.execute(
        select(RoleSectionPermission.section_key, RoleSectionPermission.access)
        .where(RoleSectionPermission.role == role)
    ).all()
    return {key: access for key, access in rows if key in SECTION_BY_KEY}


def user_permission_map(session: Session, user_id: int) -> dict[str, str]:
    """A user's explicit ALLOW/DENY overrides, keyed by section."""
    rows = session.execute(
        select(UserSectionPermission.section_key, UserSectionPermission.access)
        .where(UserSectionPermission.user_id == user_id)
    ).all()
    return {key: access for key, access in rows if key in SECTION_BY_KEY}


def role_action_map(session: Session, role: str) -> dict[str, dict[str, str]]:
    """Stored role-level action overrides: ``{section: {action: ALLOW/DENY}}``."""
    rows = session.execute(
        select(RoleSectionPermission.section_key, RoleSectionPermission.actions)
        .where(RoleSectionPermission.role == role)
    ).all()
    return {key: dict(actions) for key, actions in rows
            if key in SECTION_BY_KEY and actions}


def user_action_map(session: Session, user_id: int) -> dict[str, dict[str, str]]:
    """A user's explicit per-action overrides, keyed by section."""
    rows = session.execute(
        select(UserSectionPermission.section_key, UserSectionPermission.actions)
        .where(UserSectionPermission.user_id == user_id)
    ).all()
    return {key: dict(actions) for key, actions in rows
            if key in SECTION_BY_KEY and actions}


def resolve_section(
    section: Section,
    role: str,
    *,
    role_overrides: dict[str, str] | None = None,
    user_overrides: dict[str, str] | None = None,
) -> SectionDecision:
    """Apply the precedence chain to one section. Pure; no I/O."""
    # 1. Role ceiling. Nothing below can lift it.
    if not section.role_may_hold(role):
        return SectionDecision(section.key, False, SOURCE_ROLE_LOCK,
                               role_allowed=False, locked=True,
                               user_access=(user_overrides or {}).get(section.key))

    # 2. Role level: a stored role permission, else the catalogue default.
    stored_role = (role_overrides or {}).get(section.key)
    if stored_role is not None:
        role_allowed = stored_role == ALLOW
        role_source = SOURCE_ROLE_PERMISSION
    else:
        role_allowed = section.role_default(role)
        role_source = SOURCE_ROLE_DEFAULT

    # 3. User override.
    user_access = (user_overrides or {}).get(section.key)
    if user_access is not None:
        return SectionDecision(section.key, user_access == ALLOW,
                               SOURCE_USER_PERMISSION, role_allowed=role_allowed,
                               user_access=user_access)

    return SectionDecision(section.key, role_allowed, role_source,
                           role_allowed=role_allowed, user_access=None)


def resolve_all(session: Session, user_id: int, role: str) -> dict[str, SectionDecision]:
    """Every section's decision for one user, in catalogue order."""
    role_overrides = role_permission_map(session, role)
    user_overrides = user_permission_map(session, user_id)
    return {
        section.key: resolve_section(
            section, role, role_overrides=role_overrides, user_overrides=user_overrides
        )
        for section in SECTIONS
    }


def allowed_sections(session: Session, user_id: int, role: str) -> dict[str, bool]:
    """``{section_key: True/False}`` — what the frontend needs to render a menu."""
    return {
        key: decision.allowed
        for key, decision in resolve_all(session, user_id, role).items()
    }


def has_section(session: Session, user: UserContext, section_key: str) -> bool:
    """Whether ``user`` may use ``section_key``."""
    section = get_section(section_key)
    return resolve_section(
        section,
        user.role,
        role_overrides=role_permission_map(session, user.role),
        user_overrides=user_permission_map(session, user.user_id),
    ).allowed


# ---------------------------------------------------------------------------
# Granular actions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionDecision:
    """Why a user may or may not perform one action inside one section."""

    section: str
    action: str
    allowed: bool
    source: str
    #: True when the section itself is denied, so the action never arose.
    section_denied: bool = False
    #: True when the section does not offer this action at all.
    unsupported: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "section": self.section,
            "action": self.action,
            "access": ALLOW if self.allowed else DENY,
            "allowed": self.allowed,
            "source": self.source,
        }


SOURCE_SECTION_DENIED = "SECTION_DENIED"
SOURCE_UNSUPPORTED = "UNSUPPORTED_ACTION"
SOURCE_ACTION_DEFAULT = "ACTION_DEFAULT"
SOURCE_ROLE_ACTION = "ROLE_ACTION"
SOURCE_USER_ACTION = "USER_ACTION"


def resolve_action(
    section: Section,
    action: str,
    role: str,
    *,
    section_allowed: bool,
    role_actions: dict[str, str] | None = None,
    user_actions: dict[str, str] | None = None,
) -> ActionDecision:
    """Apply the action precedence chain. Pure; no I/O.

    The section decision comes first and is absolute: an action inside a section
    the user cannot open is not a question worth asking, so it is denied without
    consulting any override. Below that the order mirrors the section chain —
    catalogue default, then role override, then user override — because an
    administrator setting a user's ``DELETE`` should win over the role's.
    """
    if not section_allowed:
        return ActionDecision(section.key, action, False, SOURCE_SECTION_DENIED,
                              section_denied=True)
    if not section.supports(action):
        return ActionDecision(section.key, action, False, SOURCE_UNSUPPORTED,
                              unsupported=True)

    decision = section.action_default(action, role)
    source = SOURCE_ACTION_DEFAULT

    stored_role = (role_actions or {}).get(action)
    if stored_role is not None:
        decision = stored_role == ALLOW
        source = SOURCE_ROLE_ACTION

    stored_user = (user_actions or {}).get(action)
    if stored_user is not None:
        decision = stored_user == ALLOW
        source = SOURCE_USER_ACTION

    return ActionDecision(section.key, action, decision, source)


def allowed_actions(session: Session, user: UserContext,
                    section_key: str) -> dict[str, bool]:
    """``{action: True/False}`` for one section — what the UI renders from."""
    section = get_section(section_key)
    section_allowed = has_section(session, user, section_key)
    role_actions = role_action_map(session, user.role).get(section_key)
    user_actions = user_action_map(session, user.user_id).get(section_key)
    return {
        action: resolve_action(
            section, action, user.role, section_allowed=section_allowed,
            role_actions=role_actions, user_actions=user_actions,
        ).allowed
        for action in section.actions
    }


def action_matrix(session: Session, user_id: int, role: str,
                  ) -> dict[str, dict[str, bool]]:
    """Every section's actions for one user, computed with three queries.

    Built in bulk rather than per section so ``/auth/me`` stays one round trip
    however many sections the catalogue grows to.
    """
    role_overrides = role_permission_map(session, role)
    user_overrides = user_permission_map(session, user_id)
    role_actions = role_action_map(session, role)
    user_actions = user_action_map(session, user_id)

    matrix: dict[str, dict[str, bool]] = {}
    for section in SECTIONS:
        allowed = resolve_section(
            section, role, role_overrides=role_overrides,
            user_overrides=user_overrides,
        ).allowed
        matrix[section.key] = {
            action: resolve_action(
                section, action, role, section_allowed=allowed,
                role_actions=role_actions.get(section.key),
                user_actions=user_actions.get(section.key),
            ).allowed
            for action in section.actions
        }
    return matrix


def can(session: Session, user: UserContext, section_key: str,
        action: str) -> bool:
    """Whether ``user`` may perform ``action`` in ``section_key``."""
    section = get_section(section_key)
    return resolve_action(
        section, action, user.role,
        section_allowed=has_section(session, user, section_key),
        role_actions=role_action_map(session, user.role).get(section_key),
        user_actions=user_action_map(session, user.user_id).get(section_key),
    ).allowed


def require_action(section_key: str, action: str):
    """Refuse the request unless the caller holds the section *and* the action.

    Used as ``Depends(require_action("master_data", Action.EDIT))``. Both checks
    happen here so no endpoint can accidentally enforce one and forget the
    other, and so the frontend hiding a button is never the only thing standing
    between a user and the operation.
    """
    section = get_section(section_key)
    if not section.supports(action):
        raise ValueError(
            f"Section {section_key!r} does not declare the {action} action. "
            f"Declared: {', '.join(section.actions)}."
        )

    from ..api.deps import get_current_user, get_session

    def dependency(
        request: Request,
        user: UserContext = Depends(get_current_user),
        session: Session = Depends(get_session),
    ) -> UserContext:
        if not can(session, user, section_key, action):
            _record_denial(session, user, f"{section_key}:{action}", request)
            raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN_MESSAGE)
        return user

    return dependency


def require_action_or_403(session: Session, user: UserContext, section_key: str,
                          action: str, request: Request | None = None) -> None:
    """The same check, called imperatively.

    One route can serve several sections — the transaction table serves five —
    so the section is only known once the path parameter has been read, which is
    too late for a dependency.
    """
    if not can(session, user, section_key, action):
        _record_denial(session, user, f"{section_key}:{action}", request)
        raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN_MESSAGE)


def _record_denial(session: Session, user: UserContext, section_key: str,
                   request: Request | None) -> None:
    """Audit a refusal, then leave the session as we found it.

    A denial is written on its own so a refused request still leaves a trace,
    even though the request itself commits nothing.
    """
    from . import audit

    try:
        audit.record(
            session, action=AuditAction.PERMISSION_DENIED, user_id=user.user_id,
            username=user.username, resource=f"section:{section_key}", success=False,
            ip_address=audit.client_ip(request) if request is not None else None,
            detail={"section": section_key, "role": user.role,
                    "path": str(request.url.path) if request is not None else None},
        )
        session.commit()
    except Exception:  # noqa: BLE001 - auditing must not mask the 403
        logger.exception("could not audit permission denial for %s", user.username)
        session.rollback()


def require_section(section_key: str):
    """A dependency that refuses the request unless the caller holds the section.

    Used as ``Depends(require_section("sales"))``. Authorisation therefore lives
    in one place instead of being re-implemented per endpoint, and adding a new
    endpoint to an existing section is a single line.

    Raising ``403`` rather than hiding the route is deliberate: the caller is
    authenticated, so "you may not" is the honest answer, and it is the same
    answer whether the section was denied by role or by override.
    """
    # Fail loudly at import time for a typo'd section name, not at request time.
    get_section(section_key)

    # Imported here, not at module scope: ``api.deps`` is what wires the two
    # dependencies below, and importing it eagerly would close a cycle
    # (deps -> routes -> permissions -> deps).
    from ..api.deps import get_current_user, get_session

    def dependency(
        request: Request,
        user: UserContext = Depends(get_current_user),
        session: Session = Depends(get_session),
    ) -> UserContext:
        if not has_section(session, user, section_key):
            _record_denial(session, user, section_key, request)
            raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN_MESSAGE)
        return user

    return dependency


def require_any_section(*section_keys: str):
    """Allow the request when the caller holds **any** of these sections.

    For endpoints several areas legitimately need. The marker configuration is
    the case this exists for: it is pure appearance — shapes and colours, no
    business figures — and the map, the dashboard legend and the designer all
    need it, so requiring one specific section would lock out a user the data
    was never withheld from.
    """
    for key in section_keys:
        get_section(key)

    from ..api.deps import get_current_user, get_session

    def dependency(
        request: Request,
        user: UserContext = Depends(get_current_user),
        session: Session = Depends(get_session),
    ) -> UserContext:
        if any(has_section(session, user, key) for key in section_keys):
            return user
        _record_denial(session, user, "|".join(section_keys), request)
        raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN_MESSAGE)

    return dependency


def require_admin_role(user: UserContext) -> None:
    """Hard role gate for administration, independent of section permissions."""
    if user.role not in Role.ADMIN_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN_MESSAGE)


__all__ = [
    "SectionDecision",
    "ActionDecision",
    "Action",
    "require_section",
    "require_any_section",
    "require_action",
    "require_action_or_403",
    "require_admin_role",
    "has_section",
    "can",
    "resolve_section",
    "resolve_action",
    "resolve_all",
    "allowed_sections",
    "allowed_actions",
    "action_matrix",
    "role_permission_map",
    "user_permission_map",
    "role_action_map",
    "user_action_map",
    "FORBIDDEN_MESSAGE",
    "SOURCE_ROLE_DEFAULT",
    "SOURCE_ROLE_LOCK",
    "SOURCE_ROLE_PERMISSION",
    "SOURCE_USER_PERMISSION",
    "SOURCE_ACTION_DEFAULT",
    "SOURCE_ROLE_ACTION",
    "SOURCE_USER_ACTION",
    "SOURCE_SECTION_DENIED",
    "SOURCE_UNSUPPORTED",
]
