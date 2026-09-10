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

**There is more than one hierarchy.** A scope level belongs to a *dimension* —
see :mod:`app.security.scope`, which declares them — and depth, redundancy and
containment are all computed inside one. The sales hierarchy is one dimension;
``company -> plant -> storage location``, which is the only chain a stock
position states, is another. Two rules follow, and both are load-bearing:

* **Nothing is compared across dimensions.** A plant is not shallower than a
  region and the question does not arise. Answering it — which is what a
  ``LEVEL_DEPTH.get(level, 0)`` default quietly does — would make a plant scope
  look redundant beside any deeper organisational level and drop it from the
  query.
* **A dimension the scope says nothing about abstains.** A region-scoped
  reader's scope makes no claim about which plants they may see, so it neither
  permits nor refuses one. What a *view* should do when it can honour none of
  the dimensions a caller is scoped in is a different question, asked by
  :meth:`PermissionFilter.assert_scope_is_honourable` and answered per report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from sqlalchemy.orm import Session

from ..database.models_ai import AppUser, Role
from ..etl.mapping import MasterDataIndex
from ..security.scope import (
    FILTER_FIELD_BY_LEVEL,
    SCOPE_DIMENSIONS,
    SCOPE_LEVELS,
    ScopeDimension,
    ScopeIndexes,
    ScopePolicy,
    constrained_dimensions,
    describe_levels,
    dimension_applies,
    dimensions_of,
    scope_in,
)
from .exceptions import PermissionDeniedError, ScopeNotEnforceable
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

# ``FILTER_FIELD_BY_LEVEL`` (level -> the ``ScopeFilters`` attribute it sets) is
# imported above from ``app.security.scope`` and re-exported here rather than
# restated: this is where most callers have always imported it from, and a
# second copy is a second thing to keep in step. It now spans **every**
# dimension, so a consumer that means the *sales* hierarchy specifically should
# import ``ORG`` and ask it for ``filter_fields()`` — several do, and say so.

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
        """Scope levels, deepest first *within each dimension*.

        The deepest level of a chain is that chain's binding constraint, which
        is what a caller wanting one representative filter is after. Across
        chains there is no such thing as deeper, so the dimensions are simply
        kept in registry order and each is sorted inside itself — a total order,
        and an honest one.
        """
        def key(level: str) -> tuple[int, int]:
            for position, dimension in enumerate(SCOPE_DIMENSIONS):
                if dimension.holds(level):
                    return (position, -dimension.depth(level))
            return (len(SCOPE_DIMENSIONS), 0)

        return sorted(self.data_scope, key=key)

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
        # The organisational index is still accepted, and still passed by every
        # caller that already has one — building it twice in one request is the
        # thing that parameter exists to avoid. The plant index is built beside
        # it only if something asks about a plant.
        self._indexes = ScopeIndexes(session, master=master_index)

    @property
    def index(self) -> MasterDataIndex:
        return self._indexes.master

    # -- scope maths --------------------------------------------------------

    def _ancestors(self, dimension: ScopeDimension, level: str,
                   code: str) -> dict[str, str]:
        """The code plus every ancestor code, keyed by level, in one chain."""
        return dict(self._indexes.ancestors_of(dimension, level, code))

    def _within_dimension(self, dimension: ScopeDimension,
                          scope: dict[str, list[str]],
                          level: str, code: str) -> bool:
        """The original containment test, confined to one chain."""
        chain = self._ancestors(dimension, level, code)
        for scope_level, scope_codes in scope.items():
            # The requested entity sits under one of the user's scoped entities.
            if chain.get(scope_level) in scope_codes:
                return True
            # The requested entity is an ancestor of the user's scope: allowed,
            # but the scope filter below narrows the result to their slice.
            if dimension.depth(scope_level) > dimension.depth(level):
                for scope_code in scope_codes:
                    ancestors = self._ancestors(dimension, scope_level, scope_code)
                    if ancestors.get(level) == code:
                        return True
        return False

    def _is_within_scope(self, level: str, code: str) -> bool:
        """True when ``code`` is inside (or is) something the user may see.

        Asked of **every dimension that both holds this level and is constrained
        by the caller's scope, and all of them must agree** — a company scope
        binds the plant chain and the sales chain alike, and a reader allowed a
        company by one and refused it by the other must be refused.

        A dimension the scope says nothing about **abstains**: a plant-scoped
        reader's scope makes no claim about regions, so it does not refuse one.
        That is not a hole — it is the point at which the question stops being
        "may they see this code" and becomes "can this view honour their scope
        at all", which :meth:`assert_scope_is_honourable` answers and each report
        decides on.
        """
        if self.user.is_unrestricted:
            return True
        if not self.user.data_scope:
            return False

        for dimension in dimensions_of(level):
            scope = scope_in(self.user.data_scope, dimension)
            if not scope:
                continue
            if not self._within_dimension(dimension, scope, level, code):
                return False
        # Falling through means either every constrained dimension allowed it,
        # or none of them had anything to say — a plant-scoped reader asked
        # about a region. Silence is not denial: reading it as one would refuse
        # an organisationally scoped reader every plant in the business, which
        # is most of the stock page.
        return True

    def unhonourable_levels(self, columns) -> tuple[str, ...]:
        """Scope levels ``columns`` names no column for — the scope a view drops.

        ``queries.filter_conditions`` **skips** a filter naming a column the view
        does not carry, which is right for an optional narrowing and exactly
        wrong for a scope: it is dropped in silence and the caller is answered
        with everybody's figures.

        **Within a chain the test is level by level.** A scope granting one
        region *and* one sub-territory is two conditions ANDed, so honouring the
        sub-territory alone is wider than honouring both — and where the two
        were granted on different branches, wider by exactly the rows the region
        was there to exclude. Credit Control is precisely that case. Derived
        from the view's own columns, so it cannot drift from what the view
        carries the way a hand-written list of honoured levels can.

        **A chain the dataset does not have is skipped entirely**, and getting
        that wrong refused real work. See
        :func:`app.security.scope.dimension_applies`: a sale states no plant, so
        a plant scope excludes no sales row and discarding it widens nothing,
        while a credit invoice *does* have a region behind its customer that the
        view merely does not join — so dropping a region scope there really does
        widen. The first version of this asked only "which of your levels is
        missing from the view", which refused every sales question from an
        account holding a region *and* a plant: the exact account the second
        chain was added to serve.

        When **no** constrained chain applies, the whole scope is unhonourable
        and the view's policy decides — refuse for sales, disclose for material
        stock, which is how a region-scoped reader keeps the stock page.

        Unrestricted callers have no scope to lose, and so lose none.
        """
        if self.user.is_unrestricted:
            return ()

        dropped: list[str] = []
        applicable = False
        for dimension in constrained_dimensions(self.user.data_scope):
            if not dimension_applies(dimension, columns):
                continue           # a chain this dataset has no notion of
            applicable = True
            dropped.extend(
                level for level in scope_in(self.user.data_scope, dimension)
                if level not in columns
            )

        if not applicable:
            dropped = [level for level, codes in self.user.data_scope.items() if codes]
        order = self.user.scope_levels()
        return tuple(sorted(set(dropped), key=order.index))

    def assert_scope_is_honourable(self, table, policy: ScopePolicy,
                                   subject: str) -> tuple[str, ...]:
        """Apply a report's declared policy to the scope it cannot express.

        Returns the unhonourable levels either way, so a ``DISCLOSE`` caller can
        say what it dropped. Called **before** the query runs, so a refusal can
        never be mistaken for an empty result and unauthorised rows are not read
        even transiently.
        """
        levels = self.unhonourable_levels(table.c)
        if not levels or policy is not ScopePolicy.REFUSE:
            return levels

        held = describe_levels([
            level for level in SCOPE_LEVELS if level in table.c
        ]) or "nothing this scope can be checked against"
        raise ScopeNotEnforceable(
            f"user {self.user.username} scope {levels} unhonourable on {subject}",
            details={"levels": list(levels), "subject": subject},
            user_message=(
                f"Your data scope ({self.user.describe_scope()}) can't be "
                f"applied to {subject}, which is recorded by {held}. A scope "
                f"set at {describe_levels(levels)} level can't be enforced "
                f"here, and I won't answer with figures that ignore it. Ask an "
                f"administrator about your access."
            ),
        )

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

    def has_scope_in(self, dimension: ScopeDimension) -> bool:
        """Whether the caller's scope constrains ``dimension`` at all.

        **The companion every gate over one dimension needs.**
        :meth:`_is_within_scope` *abstains* where a scope says nothing, which is
        the right answer to "does their scope forbid this code" and precisely
        the wrong one for a gate, where abstention reads as consent: a reader
        scoped only in the plant chain would be told yes about every region in
        the country, and a check built solely on containment would let them
        edit, filter and export by any of them.

        So a gate over organisational data asks this first and refuses when it
        is False. That is not the same question as "can this view honour their
        scope" — see :meth:`unhonourable_levels` — which is asked of a
        view's columns rather than of the caller.
        """
        if self.user.is_unrestricted:
            return True
        return bool(scope_in(self.user.data_scope, dimension))

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
        """A scope level adds nothing once a deeper level *in its own chain* was
        requested.

        The deeper request was already proven to sit inside the scope by
        :meth:`check_entities`, so filtering on the broader level too would only
        repeat a condition that is already implied.

        **Depth is compared within a dimension and nowhere else.** A requested
        plant says nothing about which region a scope covers, and a requested
        region says nothing about which plant — so neither makes the other
        redundant. The version of this that read one flat depth table with a
        ``.get(level, 0)`` default would have called a plant scope redundant
        beside any organisational filter at all, and dropped it.
        """
        for dimension in dimensions_of(scope_level):
            scope_depth = dimension.depth(scope_level)
            for level in requested_levels:
                if dimension.holds(level) and dimension.depth(level) >= scope_depth:
                    return True
        return False

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

        # Every scope-bearing level, not only the ones an entity resolver can
        # name. The two lists were the same nine while there was one dimension;
        # a plant is a level a caller can filter on and be scoped at, and is not
        # an ``EntityType`` — the assistant resolves no plants — so driving this
        # from the entity map would have left a plant filter unchecked.
        for level, field_name in FILTER_FIELD_BY_LEVEL.items():
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
