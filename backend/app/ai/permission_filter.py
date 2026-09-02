"""Permission filtering — the security boundary of the agent.

Two rules govern everything here:

1. **Authorised filters are injected before execution.** A tool never runs an
   unrestricted query whose results are then trimmed; the user's scope becomes
   part of the query itself.
2. **The agent cannot widen the scope.** The scope is read from the database for
   the authenticated user. Nothing the LLM emits — and nothing a user types —
   can remove or extend it.

Scope is hierarchical and is checked against the real master hierarchy (reusing
``etl.mapping.MasterDataIndex``, not a second copy of it): a regional manager
scoped to ``REG001`` may ask about an area inside that region, and may ask a
zone-level question that is silently narrowed to their region — but asking about
a different region is refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from sqlalchemy.orm import Session

from ..database.models_ai import AppUser, Role
from ..etl.mapping import BINDING_BY_LEVEL, LEVEL_BINDINGS, LEVEL_DEPTH, MasterDataIndex
from .exceptions import PermissionDeniedError
from .schemas import EntityType, ResolvedEntity, ScopeFilters

#: Entity type -> the scope key and the ``ScopeFilters`` field it drives.
LEVEL_BY_ENTITY: dict[EntityType, str] = {
    EntityType.COMPANY: "company_code",
    EntityType.BUSINESS_UNIT: "bu_code",
    EntityType.SALES_LINE: "sales_line_code",
    EntityType.ZONE: "zone_code",
    EntityType.REGION: "region_code",
    EntityType.AREA: "area_code",
    EntityType.UNIT: "unit_code",
    EntityType.TERRITORY: "territory_code",
    EntityType.SUB_TERRITORY: "sub_territory_code",
}

FILTER_FIELD_BY_LEVEL: dict[str, str] = {
    "company_code": "company_codes",
    "bu_code": "business_unit_codes",
    "sales_line_code": "sales_line_codes",
    "zone_code": "zone_codes",
    "region_code": "region_codes",
    "area_code": "area_codes",
    "unit_code": "unit_codes",
    "territory_code": "territory_codes",
    "sub_territory_code": "sub_territory_codes",
}

NON_ORG_FILTER_FIELD: dict[EntityType, str] = {
    EntityType.CUSTOMER: "customer_codes",
    EntityType.SALES_FORCE: "sales_force_codes",
    # A material narrows sales, target and stock alike since revision 0022. Like
    # the two above it carries no permission of its own — it belongs to no
    # region, so it can only narrow what the caller's organisational scope
    # already allows.
    EntityType.MATERIAL: "material_codes",
}


@dataclass
class UserContext:
    """The authenticated user, as the agent sees them."""

    user_id: int
    username: str
    role: str
    display_name: str | None = None
    #: ``{"region_code": ["REG001"]}``. Empty for an unrestricted role.
    data_scope: dict[str, list[str]] = field(default_factory=dict)
    preferred_language: str = "en"

    @classmethod
    def from_user(cls, user: AppUser) -> "UserContext":
        scope = {
            level: [str(code) for code in codes]
            for level, codes in (user.data_scope or {}).items()
            if level in FILTER_FIELD_BY_LEVEL and codes
        }
        return cls(
            user_id=user.user_id,
            username=user.username,
            role=user.role,
            display_name=user.display_name,
            data_scope=scope,
            preferred_language=user.preferred_language or "en",
        )

    @property
    def is_unrestricted(self) -> bool:
        """Management with no explicit scope sees the whole company."""
        return self.role in Role.UNRESTRICTED and not self.data_scope

    def scope_levels(self) -> list[str]:
        """Scope levels, deepest first — the deepest is the binding constraint."""
        return sorted(self.data_scope, key=lambda level: -LEVEL_DEPTH[level])

    def describe_scope(self) -> str:
        if self.is_unrestricted:
            return "all regions"
        if not self.data_scope:
            return "no data scope"
        parts = [
            f"{level.replace('_code', '').replace('_', ' ')} "
            f"{', '.join(codes)}"
            for level, codes in self.data_scope.items()
        ]
        return "; ".join(parts)


class PermissionFilter:
    """Decides what a user may see, and rewrites filters accordingly."""

    def __init__(self, session: Session, user: UserContext,
                 master_index: MasterDataIndex | None = None) -> None:
        self.session = session
        self.user = user
        self._index = master_index

    @property
    def index(self) -> MasterDataIndex:
        if self._index is None:
            self._index = MasterDataIndex(self.session)
        return self._index

    # -- scope maths --------------------------------------------------------

    def _ancestors(self, level: str, code: str) -> dict[str, str]:
        """The code plus every ancestor code, keyed by level."""
        if level not in BINDING_BY_LEVEL:
            return {}
        return dict(self.index.ancestors_of(level, code))

    def _is_within_scope(self, level: str, code: str) -> bool:
        """True when ``code`` is inside (or is) something the user may see."""
        if self.user.is_unrestricted:
            return True
        if not self.user.data_scope:
            return False

        chain = self._ancestors(level, code)
        for scope_level, scope_codes in self.user.data_scope.items():
            # The requested entity sits under one of the user's scoped entities.
            if chain.get(scope_level) in scope_codes:
                return True
            # The requested entity is an ancestor of the user's scope: allowed,
            # but the scope filter below narrows the result to their slice.
            if LEVEL_DEPTH.get(scope_level, 99) > LEVEL_DEPTH.get(level, 99):
                for scope_code in scope_codes:
                    if self._ancestors(scope_level, scope_code).get(level) == code:
                        return True
        return False

    def is_within_scope(self, level: str, code: str) -> bool:
        """Public scope test, used by the filter and search endpoints.

        Sharing this with the agent guarantees that a filter dropdown and a
        report can never disagree about what a user is allowed to see.
        """
        return self._is_within_scope(level, code)

    # -- entity checks ------------------------------------------------------

    def check_entities(self, entities: Sequence[ResolvedEntity]) -> None:
        """Refuse the whole request if any organisational entity is out of scope.

        This runs **before** any tool executes, so unauthorised data is never
        read, not even transiently.
        """
        if self.user.is_unrestricted:
            return
        for entity in entities:
            level = LEVEL_BY_ENTITY.get(entity.entity_type)
            if level is None:
                continue    # product / customer / material: not scope-bearing
            if not self._is_within_scope(level, entity.code):
                # The refusal echoes what the reader *typed*, never what the
                # master data calls it. Naming the record turned every refusal
                # into a lookup: a reader with no access to REG002 could type
                # the code and be told back "Khulna", and walk the whole
                # organisational master one refusal at a time. Echoing their own
                # words discloses nothing they did not already have, and is just
                # as actionable — they know which word was refused.
                #
                # ``details`` keeps the label: it feeds the audit trail and the
                # learning signals, and never reaches the client.
                raise PermissionDeniedError(
                    f"user {self.user.username} denied {level}={entity.code}",
                    user_message=(
                        f"You don't have permission to access "
                        f"'{entity.term}' ({entity.entity_type.value.replace('_', ' ')}) "
                        f"data. Your access covers {self.user.describe_scope()}."
                    ),
                    details={"entity": entity.label, "entity_type": entity.entity_type.value},
                )

    def has_any_scope(self) -> bool:
        """A restricted user with no scope at all can see nothing."""
        return self.user.is_unrestricted or bool(self.user.data_scope)

    # -- filter construction ------------------------------------------------

    def scope_filters(self) -> ScopeFilters:
        """The user's own scope, as tool filters."""
        filters = ScopeFilters()
        for level, codes in self.user.data_scope.items():
            field_name = FILTER_FIELD_BY_LEVEL[level]
            setattr(filters, field_name, list(codes))
        return filters

    def build_filters(self, entities: Iterable[ResolvedEntity]) -> ScopeFilters:
        """Combine the question's entities with the user's scope.

        The result is always at least as narrow as the scope. Where the user
        named something *inside* their scope, that narrower code wins for its
        level and the broader scope level is dropped — filtering on both would
        be redundant, and on a lower level it is strictly narrower anyway.
        """
        entities = list(entities)
        self.check_entities(entities)

        filters = ScopeFilters()
        requested_levels: set[str] = set()

        for entity in entities:
            level = LEVEL_BY_ENTITY.get(entity.entity_type)
            if level is not None:
                field_name = FILTER_FIELD_BY_LEVEL[level]
                codes = list(getattr(filters, field_name))
                if entity.code not in codes:
                    codes.append(entity.code)
                setattr(filters, field_name, codes)
                requested_levels.add(level)
                continue
            field_name = NON_ORG_FILTER_FIELD.get(entity.entity_type)
            if field_name:
                codes = list(getattr(filters, field_name))
                if entity.code not in codes:
                    codes.append(entity.code)
                setattr(filters, field_name, codes)

        if self.user.is_unrestricted:
            return filters

        for level, codes in self.user.data_scope.items():
            if self._scope_level_is_redundant(level, requested_levels):
                continue
            field_name = FILTER_FIELD_BY_LEVEL[level]
            existing = list(getattr(filters, field_name))
            merged = [c for c in codes if c not in existing] + existing
            setattr(filters, field_name, merged if not existing else existing)
        return filters

    def _scope_level_is_redundant(self, scope_level: str,
                                  requested_levels: set[str]) -> bool:
        """A scope level adds nothing once a deeper level has been requested.

        The deeper request was already proven to sit inside the scope by
        :meth:`check_entities`, so filtering on the broader level too would only
        repeat a condition that is already implied.
        """
        scope_depth = LEVEL_DEPTH.get(scope_level, 0)
        return any(
            LEVEL_DEPTH.get(level, 0) >= scope_depth for level in requested_levels
        )

    def enforce(self, filters: ScopeFilters) -> ScopeFilters:
        """Final gate applied to whatever filters a tool is about to run with.

        Called on the tool arguments themselves, so even if an LLM invented a
        region code the user may not see, the query is still constrained.
        """
        if self.user.is_unrestricted:
            return filters

        if not self.user.data_scope:
            raise PermissionDeniedError(
                f"user {self.user.username} has no data scope",
                user_message=(
                    "Your account has no data scope assigned, so I can't show any "
                    "business data. Please contact your administrator."
                ),
            )

        for entity_type, level in LEVEL_BY_ENTITY.items():
            field_name = FILTER_FIELD_BY_LEVEL[level]
            for code in getattr(filters, field_name):
                if not self._is_within_scope(level, code):
                    raise PermissionDeniedError(
                        f"user {self.user.username} denied {level}={code}",
                        user_message=(
                            f"You don't have permission to access data for "
                            f"{code}. Your access covers {self.user.describe_scope()}."
                        ),
                    )

        merged = filters.model_copy(deep=True)
        requested_levels = {
            level for level in FILTER_FIELD_BY_LEVEL
            if getattr(merged, FILTER_FIELD_BY_LEVEL[level])
        }
        for level, codes in self.user.data_scope.items():
            if self._scope_level_is_redundant(level, requested_levels):
                continue
            field_name = FILTER_FIELD_BY_LEVEL[level]
            if not getattr(merged, field_name):
                setattr(merged, field_name, list(codes))
        return merged


def load_user_context(session: Session, username: str) -> UserContext | None:
    """Look up an active user by username."""
    from sqlalchemy import select

    user = session.execute(
        select(AppUser).where(AppUser.username == username, AppUser.is_active.is_(True))
    ).scalar_one_or_none()
    return UserContext.from_user(user) if user else None


__all__ = [
    "UserContext",
    "PermissionFilter",
    "load_user_context",
    "LEVEL_BY_ENTITY",
    "FILTER_FIELD_BY_LEVEL",
    "NON_ORG_FILTER_FIELD",
]
