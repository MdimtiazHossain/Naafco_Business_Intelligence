"""Authentication and audit.

Phase 3 built authorisation (role -> data scope -> query filters). This package
adds the authentication half — password login and signed tokens — without
changing the authorisation model: a token names a user, and their role and scope
are always re-read from the database.
"""

from . import audit
from .security import (
    create_access_token,
    decode_access_token,
    hash_password,
    needs_rehash,
    validate_password_strength,
    verify_password,
)

__all__ = [
    "audit",
    "hash_password",
    "verify_password",
    "needs_rehash",
    "validate_password_strength",
    "create_access_token",
    "decode_access_token",
]
