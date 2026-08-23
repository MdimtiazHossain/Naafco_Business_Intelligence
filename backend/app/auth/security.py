"""Password hashing and JWT issuing.

Phase 3 deliberately stopped at *authorisation* — it consumed an already
identified user. Phase 4 adds the missing *authentication* half: a password
login that issues a signed token. The authorisation model underneath is
unchanged: the token only names the user, and their role and data scope are
still read from the database on every request.

Passwords use PBKDF2-HMAC-SHA256 with a per-user random salt and a high
iteration count, from the standard library — no password is ever stored or
logged in clear text.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass

import jwt

logger = logging.getLogger("app.auth")

#: PBKDF2 parameters. Raising ITERATIONS stays backward compatible because the
#: cost is stored inside each hash.
ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 260_000
SALT_BYTES = 16

JWT_ALGORITHM = "HS256"
DEFAULT_TOKEN_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "720"))   # 12 hours


def get_jwt_secret() -> str:
    """The signing secret.

    In production ``JWT_SECRET`` must be set. Outside production a per-process
    random secret is generated so development works out of the box — tokens
    then stop being valid when the process restarts, which is the safe default.
    """
    secret = os.getenv("JWT_SECRET")
    if secret:
        return secret
    if os.getenv("APP_ENV", "development").lower() == "production":
        raise RuntimeError(
            "JWT_SECRET must be set when APP_ENV=production. Refusing to sign "
            "tokens with an ephemeral secret."
        )
    global _EPHEMERAL_SECRET
    if _EPHEMERAL_SECRET is None:
        _EPHEMERAL_SECRET = secrets.token_urlsafe(48)
        logger.warning(
            "JWT_SECRET is not set; using an ephemeral development secret. "
            "Sessions will not survive a restart."
        )
    return _EPHEMERAL_SECRET


_EPHEMERAL_SECRET: str | None = None


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    """``pbkdf2_sha256$iterations$salt$hash`` — self-describing and portable."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join([
        ALGORITHM,
        str(iterations),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ])


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time password check. A missing or malformed hash never matches."""
    if not stored or not password:
        return False
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored.split("$")
        if algorithm != ALGORITHM:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, int(iterations)
    )
    return hmac.compare_digest(candidate, expected)


def needs_rehash(stored: str | None) -> bool:
    """True when a stored hash uses fewer iterations than the current policy."""
    if not stored:
        return True
    try:
        algorithm, iterations, _, _ = stored.split("$")
    except ValueError:
        return True
    return algorithm != ALGORITHM or int(iterations) < ITERATIONS


PASSWORD_MIN_LENGTH = 8


def validate_password_strength(password: str) -> list[str]:
    """Problems with a proposed password; empty means acceptable."""
    problems: list[str] = []
    if len(password) < PASSWORD_MIN_LENGTH:
        problems.append(f"must be at least {PASSWORD_MIN_LENGTH} characters")
    if password.isdigit():
        problems.append("must not be only digits")
    if password.lower() in {"password", "12345678", "qwertyui", "admin123"}:
        problems.append("is too common")
    return problems


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenPayload:
    username: str
    user_id: int
    role: str
    expires_at: dt.datetime


def create_access_token(username: str, user_id: int, role: str,
                        expires_minutes: int = DEFAULT_TOKEN_MINUTES) -> tuple[str, int]:
    """Sign a token for a user. Returns ``(token, expires_in_seconds)``.

    The token carries identity only. Role travels for convenience in the UI but
    is **not** trusted for authorisation: every request re-reads the role and
    data scope from the database, so a stale or tampered token cannot widen
    access.
    """
    now = dt.datetime.now(dt.timezone.utc)
    expires = now + dt.timedelta(minutes=expires_minutes)
    payload = {
        "sub": username,
        "uid": user_id,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)
    return token, int((expires - now).total_seconds())


def decode_access_token(token: str) -> TokenPayload | None:
    """Verify and decode a token. Returns ``None`` for anything invalid."""
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        logger.info("rejected an expired access token")
        return None
    except jwt.InvalidTokenError:
        logger.warning("rejected an invalid access token")
        return None

    username = payload.get("sub")
    user_id = payload.get("uid")
    if not username or user_id is None:
        return None
    return TokenPayload(
        username=str(username),
        user_id=int(user_id),
        role=str(payload.get("role") or ""),
        expires_at=dt.datetime.fromtimestamp(payload["exp"], dt.timezone.utc),
    )


__all__ = [
    "hash_password",
    "verify_password",
    "needs_rehash",
    "validate_password_strength",
    "create_access_token",
    "decode_access_token",
    "TokenPayload",
    "get_jwt_secret",
    "PASSWORD_MIN_LENGTH",
    "DEFAULT_TOKEN_MINUTES",
]
