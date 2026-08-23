"""Create and inspect agent users and their data scopes.

Usage::

    python scripts/manage_users.py list
    python scripts/manage_users.py create ceo --role MANAGEMENT --name "Managing Director"
    python scripts/manage_users.py create dhaka_rm --role REGIONAL_MANAGER \
        --scope region_code=REG001 --name "Dhaka Regional Manager"
    python scripts/manage_users.py create mirpur_am --role AREA_MANAGER \
        --scope area_code=AR001,AR003
    python scripts/manage_users.py deactivate old_user

A user's data scope is what the agent filters every query by. Only the
MANAGEMENT role may be left without a scope; any other role without one can see
no data at all, which the agent reports plainly rather than failing silently.

Scope codes are validated against the Phase 1 master data — a scope naming a
region that does not exist is rejected rather than created.
"""

from __future__ import annotations

import argparse
import sys

import _bootstrap  # noqa: F401  (side effect: sys.path)

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.permission_filter import FILTER_FIELD_BY_LEVEL
from app.database.connection import get_engine
from app.auth.users import apply_status, status_of
from app.database.models_ai import AppUser, Role, UserStatus
from app.etl.mapping import MasterDataIndex

SEPARATOR = "=" * 78


def parse_scope(pairs: list[str]) -> dict[str, list[str]]:
    """``["region_code=REG001,REG002"]`` -> ``{"region_code": ["REG001", "REG002"]}``."""
    scope: dict[str, list[str]] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"Scope must be level=codes, got {pair!r}")
        level, codes = pair.split("=", 1)
        level = level.strip()
        if level not in FILTER_FIELD_BY_LEVEL:
            raise ValueError(
                f"Unknown scope level {level!r}. Valid levels: "
                + ", ".join(sorted(FILTER_FIELD_BY_LEVEL))
            )
        values = [c.strip() for c in codes.split(",") if c.strip()]
        if not values:
            raise ValueError(f"No codes given for {level!r}")
        scope.setdefault(level, []).extend(values)
    return scope


def validate_scope(session: Session, scope: dict[str, list[str]]) -> list[str]:
    """Scope codes that do not exist in the master data."""
    index = MasterDataIndex(session)
    unknown: list[str] = []
    for level, codes in scope.items():
        known = index.ids.get(level, {})
        unknown.extend(f"{level}={code}" for code in codes if code not in known)
    return unknown


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage agent users")
    parser.add_argument("--database-url", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List users and their scopes.")

    create = sub.add_parser("create", help="Create or update a user.")
    create.add_argument("username")
    create.add_argument("--role", required=True, choices=list(Role.ALL))
    create.add_argument("--name", default=None)
    create.add_argument("--employee-id", default=None)
    create.add_argument("--language", default="en")
    create.add_argument("--scope", action="append", default=[],
                        metavar="LEVEL=CODE[,CODE]",
                        help="e.g. region_code=REG001,REG002")

    deactivate = sub.add_parser("deactivate", help="Deactivate a user.")
    deactivate.add_argument("username")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    engine = get_engine(args.database_url)

    try:
        with Session(engine, expire_on_commit=False) as session:
            if args.command == "list":
                return _list(session)
            if args.command == "create":
                return _create(session, args)
            if args.command == "deactivate":
                return _deactivate(session, args.username)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - reported, never raised at the user
        print(f"ERROR: cannot reach the database: {exc}", file=sys.stderr)
        print("Run 'cd backend && alembic upgrade head' and check DATABASE_URL.",
              file=sys.stderr)
        return 2
    return 0


def _list(session: Session) -> int:
    users = session.execute(select(AppUser).order_by(AppUser.username)).scalars().all()
    print(SEPARATOR)
    print("AGENT USERS")
    print(SEPARATOR)
    if not users:
        print("No users yet. Create one with:")
        print("  python scripts/manage_users.py create ceo --role MANAGEMENT")
        return 0
    print(f"{'Username':<18}{'Role':<20}{'Status':<10}Data scope")
    print("-" * 78)
    for user in users:
        scope = user.data_scope or {}
        rendered = "; ".join(f"{k}={','.join(v)}" for k, v in scope.items()) or (
            "(unrestricted)" if user.role in Role.UNRESTRICTED else "(none — sees nothing)"
        )
        print(f"{user.username:<18}{user.role:<20}{status_of(user):<10}"
              f"{rendered}")
    return 0


def _create(session: Session, args: argparse.Namespace) -> int:
    scope = parse_scope(args.scope)

    if scope:
        unknown = validate_scope(session, scope)
        if unknown:
            print("ERROR: these scope codes do not exist in the master data:",
                  file=sys.stderr)
            for item in unknown:
                print(f"  - {item}", file=sys.stderr)
            print("Load master data first, or correct the codes. No user was created.",
                  file=sys.stderr)
            return 2
    elif args.role not in Role.UNRESTRICTED:
        print(f"WARNING: role {args.role} has no data scope, so this user will see "
              "no business data. Pass --scope to grant access.", file=sys.stderr)

    user = session.execute(
        select(AppUser).where(AppUser.username == args.username)
    ).scalar_one_or_none()
    action = "Updated" if user else "Created"
    if user is None:
        user = AppUser(username=args.username)
        session.add(user)

    user.role = args.role
    user.display_name = args.name or user.display_name or args.username
    user.employee_id = args.employee_id or user.employee_id
    user.preferred_language = args.language
    user.data_scope = scope or None
    # ``apply_status`` keeps ``status`` and ``is_active`` in step; setting
    # ``is_active`` alone would leave the admin panel showing a stale status.
    apply_status(user, UserStatus.ACTIVE)
    session.commit()

    print(f"{action} user '{args.username}' with role {args.role}.")
    print(f"Data scope: {scope or ('unrestricted' if args.role in Role.UNRESTRICTED else 'none')}")
    print()
    print("Ask a question as this user:")
    print(f'  python scripts/ask_agent.py "এই মাসের sales কত?" --user {args.username}')
    return 0


def _deactivate(session: Session, username: str) -> int:
    user = session.execute(
        select(AppUser).where(AppUser.username == username)
    ).scalar_one_or_none()
    if user is None:
        print(f"ERROR: no user named {username!r}", file=sys.stderr)
        return 2
    apply_status(user, UserStatus.INACTIVE)
    session.commit()
    print(f"Deactivated '{username}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
