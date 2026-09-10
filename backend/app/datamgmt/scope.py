"""Applying the caller's data scope to a master-data query.

The rule the whole platform follows is that scope constrains the *query*, never
the result: a regional manager's request must not read another region's rows,
not even to discard them. That is straightforward for facts, which carry every
organisational code, and needs thought for dimensions, which do not:

* An **organisational dimension** *is* a level. ``dim_area`` is scoped by asking
  which area codes lie inside the user's scope — resolved through the same
  nine-way hierarchy join the map uses, so the answer cannot disagree with the
  map's or the report's.

* **Customers, sales force and warehouses** have no organisational column at
  all. Their scope is derived from the facts — a customer belongs to the
  territories it has traded in — again by reusing the map's resolver rather than
  writing a second definition of the same relationship.

* **Products** belong to no region. The workbook exposes no link between a SKU
  and any organisational level, so there is nothing to scope by, and inventing
  one would hide products from managers rather than protect anything. They are
  visible to everyone who holds the section, exactly as the product filter
  dropdown already treats them.

An unrestricted role skips all of this: no filter is added, because none applies.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..ai.permission_filter import PermissionFilter, UserContext
from ..org.hierarchy import (
    ORG_CHAIN,
    OrgScope,
    resolve_business_entities,
    resolve_org_scope,
)
from ..security.scope import ORG
from .catalogue import ManagedEntity

#: ``region_code`` -> ``region``, the level name the hierarchy resolver uses.
def _level_name(scope_level: str) -> str:
    return scope_level.removesuffix("_code")


@dataclass
class ScopeResult:
    """Which codes of one entity the caller may see.

    ``codes is None`` means "no restriction" — an unrestricted role, or an
    entity that is not scope-bearing. That is deliberately distinct from an
    empty set, which means "restricted, and nothing qualifies": the first
    returns everything, the second returns nothing.
    """

    codes: set[str] | None = None
    #: Human description of the scope, for the response header.
    description: str = "all regions"

    @property
    def unrestricted(self) -> bool:
        return self.codes is None

    @property
    def empty(self) -> bool:
        return self.codes is not None and not self.codes


def _user_scope_filters(user: UserContext) -> dict[str, str | None]:
    """The user's own scope, as hierarchy filters.

    A scope holding several codes at one level cannot be expressed as a single
    filter, so those levels are left out here and the resulting org scope is
    intersected against them afterwards.
    """
    return {
        _level_name(level): codes[0]
        for level, codes in user.data_scope.items()
        if len(codes) == 1 and ORG.holds(level)
    }


def resolve(session: Session, user: UserContext,
            entity: ManagedEntity) -> ScopeResult:
    """Which codes of ``entity`` this user may see."""
    if user.is_unrestricted:
        return ScopeResult(codes=None, description=user.describe_scope())

    if not user.data_scope:
        # A restricted account with no scope assigned can see no business data.
        # Returning an empty set rather than raising keeps the table rendering
        # with an honest "no records" instead of an error page.
        return ScopeResult(codes=set(), description="no data scope")

    if entity.scope_level is None and entity.fact_scope_type is None:
        return ScopeResult(codes=None, description=user.describe_scope())

    org_scope = _org_scope(session, user)

    if entity.scope_level is not None:
        level = _level_name(entity.scope_level)
        codes = set(org_scope.of(level))
        # Intersect with any multi-code scope at this exact level, which the
        # single-value filter above could not express.
        explicit = user.data_scope.get(entity.scope_level)
        if explicit:
            codes &= set(explicit)
        return ScopeResult(codes=codes, description=user.describe_scope())

    # Customer / sales force / warehouse: membership comes from the facts.
    business = resolve_business_entities(
        session, org_scope, types=[entity.fact_scope_type or ""],
    )
    return ScopeResult(
        codes={item.code for item in business.get(entity.fact_scope_type or "", [])},
        description=user.describe_scope(),
    )


def _org_scope(session: Session, user: UserContext) -> OrgScope:
    """The organisational slice the user's scope selects, at every level.

    Multi-code scopes are handled by resolving each code and unioning the
    results: two regions is two subtrees, and a manager over both should see
    both.
    """
    single = _user_scope_filters(user)
    multi = {
        level: codes for level, codes in user.data_scope.items()
        if len(codes) > 1 and ORG.holds(level)
    }
    if not multi:
        return resolve_org_scope(session, single)

    combined = OrgScope()
    for level, codes in multi.items():
        for code in codes:
            filters = {**single, _level_name(level): code}
            partial = resolve_org_scope(session, filters)
            for chain_level in ORG_CHAIN:
                combined.codes.setdefault(chain_level, set()).update(
                    partial.of(chain_level)
                )
            combined.nodes.extend(partial.nodes)
    return combined


def check_record(session: Session, user: UserContext, entity: ManagedEntity,
                 code: str) -> bool:
    """Whether one specific record is inside the caller's scope.

    Used before every read of a single record and before every write, so the
    scope is a boundary on writes as much as on reads: a regional manager cannot
    edit a territory they cannot see by addressing it directly.
    """
    if user.is_unrestricted:
        return True
    if entity.scope_level is not None:
        # ``PermissionFilter`` already answers this for organisational levels,
        # including the ancestor case, so it is reused rather than re-derived.
        return PermissionFilter(session, user).is_within_scope(
            entity.scope_level, code
        )
    result = resolve(session, user, entity)
    return result.unrestricted or code in (result.codes or set())


__all__ = ["ScopeResult", "resolve", "check_record"]
