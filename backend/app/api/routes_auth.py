"""Authentication endpoints: login, current user, logout, password change.

The token names a user and nothing more. Role and data scope are always re-read
from the database on each request, so authorisation stays exactly where Phase 3
put it — in ``PermissionFilter`` — and a token can never widen access.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..auth import audit
from ..auth.permissions import action_matrix, allowed_sections
from ..auth.security import (
    DEFAULT_TOKEN_MINUTES,
    create_access_token,
    hash_password,
    needs_rehash,
    validate_password_strength,
    verify_password,
)
from ..config import get_settings
from ..database.models_ai import AppUser, AuditAction, Role
from .deps import get_current_user, get_session

logger = logging.getLogger("app.api.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

#: One message for every failure mode, so the endpoint cannot be used to work
#: out which usernames exist or which accounts are disabled.
INVALID_CREDENTIALS = "Incorrect username or password."


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=255,
                          description="Username or email address.")
    password: str = Field(min_length=1, max_length=256)
    remember: bool = False


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: dict[str, Any]


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class PreferencesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferred_language: str | None = Field(None, pattern="^(en|bn)$")
    theme: str | None = Field(None, pattern="^(light|dark|system)$")


def user_payload(user: AppUser) -> dict[str, Any]:
    """The profile the frontend may see. No hash, no secret."""
    return {
        "user_id": user.user_id,
        "username": user.username,
        "display_name": user.display_name or user.username,
        "email": user.email,
        "role": user.role,
        "employee_id": user.employee_id,
        "data_scope": user.data_scope or {},
        "preferred_language": user.preferred_language,
        "theme": user.theme,
        "is_admin": user.role in Role.ADMIN_ROLES,
        "last_login_at": user.last_login_at,
    }


@router.post("/login", response_model=LoginResponse)
def login(request: LoginRequest, http_request: Request,
          session: Session = Depends(get_session)) -> LoginResponse:
    """Exchange a username (or email) and password for an access token."""
    identifier = request.username.strip()
    user = session.execute(
        select(AppUser).where(
            (AppUser.username == identifier) | (AppUser.email == identifier)
        )
    ).scalar_one_or_none()

    ip = audit.client_ip(http_request)
    agent = http_request.headers.get("user-agent")

    if user is None or not user.is_active or not verify_password(
        request.password, user.password_hash
    ):
        audit.record(
            session, action=AuditAction.LOGIN_FAILED, username=identifier,
            resource="auth", ip_address=ip, user_agent=agent, success=False,
            detail={"reason": "invalid_credentials"},
        )
        session.commit()
        # Deliberately uniform: unknown user, wrong password and disabled
        # account are indistinguishable to the caller.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID_CREDENTIALS,
                            headers={"WWW-Authenticate": "Bearer"})

    # Transparently upgrade a hash stored under weaker parameters.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(request.password)

    minutes = DEFAULT_TOKEN_MINUTES * (7 if request.remember else 1)
    token, expires_in = create_access_token(
        user.username, user.user_id, user.role, expires_minutes=minutes
    )
    user.last_login_at = dt.datetime.now(dt.timezone.utc)

    audit.record(session, action=AuditAction.LOGIN, user_id=user.user_id,
                 username=user.username, resource="auth", ip_address=ip,
                 user_agent=agent, detail={"remember": request.remember})
    session.commit()

    return LoginResponse(access_token=token, expires_in=expires_in,
                         user=user_payload(user))


@router.get("/me")
def me(session: Session = Depends(get_session),
       user: UserContext = Depends(get_current_user)) -> dict[str, Any]:
    """The signed-in user's profile and effective data scope."""
    record = session.get(AppUser, user.user_id)
    if record is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown or inactive user.")
    settings = get_settings()
    return {
        **user_payload(record),
        "scope_description": user.describe_scope(),
        # The sections the caller may open. The frontend uses this to hide what
        # it would be refused anyway — a convenience, not the control: every
        # endpoint re-resolves the same permissions server-side.
        "sections": allowed_sections(session, record.user_id, record.role),
        # And what they may *do* inside each one, for the same reason: a Delete
        # button the API would refuse should not be offered. Every write
        # endpoint re-resolves this server-side before acting.
        "actions": action_matrix(session, record.user_id, record.role),
        "company_name": settings.company_name,
        "currency": settings.currency_code,
        "financial_year_start_month": settings.financial_year_start_month,
    }


@router.post("/logout")
def logout(http_request: Request, session: Session = Depends(get_session),
           user: UserContext = Depends(get_current_user)) -> dict[str, str]:
    """Record the logout.

    Tokens are stateless and short-lived, so the client discards its copy. The
    event is audited so sessions remain traceable.
    """
    audit.record(session, action=AuditAction.LOGOUT, user_id=user.user_id,
                 username=user.username, resource="auth",
                 ip_address=audit.client_ip(http_request))
    session.commit()
    return {"status": "signed out"}


@router.post("/password")
def change_password(request: PasswordChangeRequest, http_request: Request,
                    session: Session = Depends(get_session),
                    user: UserContext = Depends(get_current_user)) -> dict[str, str]:
    """Change the signed-in user's own password."""
    record = session.get(AppUser, user.user_id)
    if record is None or not verify_password(request.current_password,
                                             record.password_hash):
        audit.record(session, action=AuditAction.ADMIN_CHANGE, user_id=user.user_id,
                     username=user.username, resource="password", success=False,
                     ip_address=audit.client_ip(http_request),
                     detail={"reason": "current_password_incorrect"})
        session.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Your current password is incorrect.")

    problems = validate_password_strength(request.new_password)
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "The new password " + "; ".join(problems) + ".")

    record.password_hash = hash_password(request.new_password)
    audit.record(session, action=AuditAction.ADMIN_CHANGE, user_id=user.user_id,
                 username=user.username, resource="password",
                 ip_address=audit.client_ip(http_request),
                 detail={"changed": "own_password"})
    session.commit()
    return {"status": "password changed"}


@router.patch("/preferences")
def update_preferences(request: PreferencesRequest,
                       session: Session = Depends(get_session),
                       user: UserContext = Depends(get_current_user),
                       ) -> dict[str, Any]:
    """Persist language and theme so they follow the user between devices."""
    record = session.get(AppUser, user.user_id)
    if record is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown or inactive user.")
    if request.preferred_language:
        record.preferred_language = request.preferred_language
    if request.theme:
        record.theme = request.theme
    session.commit()
    return user_payload(record)
