"""Resolving one map filter into every entity that belongs under it.

A filter at any level must produce the selected entity, everything beneath it,
and the ancestors that place it — in one pass, without the front end asking
level by level.

Two different relationships have to be resolved, because the schema holds them
in two different places:

**Organisational levels** (company → sub-territory) are a fixed nine-deep chain
of foreign keys between the Phase 1 dimensions. One outer join across all nine
yields every complete path; filtering those paths gives ancestors *and*
descendants together. Fixed depth means no recursive CTE, and one query means
no N+1 — the cost does not change with how deep the filter sits.

**Customers and sales force** have no organisational column in their dimension
at all — ``dim_customer`` carries none, and only ``dim_sales_force`` has a
``territory_code``. Their real relationship to the hierarchy is recorded on the
**facts**: a customer belongs to the territories it has transacted in. So
membership is derived from ``vw_sales_detail``, which already joins every
organisational code to the customer and sales-force codes. That is the actual
existing relationship; nothing new is invented.

Membership is deliberately derived over **all** history rather than the selected
period. A date or product filter changes the *metrics*, not who belongs where —
so filtering to one product must not make a territory's customers vanish from
the map.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ..ai import queries as q
from ..ai.permission_filter import PermissionFilter, UserContext
from ..ai.schemas import ScopeFilters
from ..database.models import (
    DimArea,
    DimBusinessUnit,
    DimCompany,
    DimRegion,
    DimSalesLine,
    DimSubTerritory,
    DimTerritory,
    DimUnit,
    DimZone,
)
from ..database.models_warehouse import DimSalesForce

#: The organisational chain, shallowest first. Mirrors ``etl.mapping``.
ORG_CHAIN: tuple[str, ...] = (
    "company", "bu", "sales_line", "zone", "region", "area", "unit",
    "territory", "sub_territory",
)

#: Entity types resolved from the facts rather than from a dimension column.
BUSINESS_TYPES: tuple[str, ...] = ("customer", "sales_force")

#: Every type the map can draw, in hierarchy order.
ALL_TYPES: tuple[str, ...] = (*ORG_CHAIN, *BUSINESS_TYPES)

#: level -> (model, code column, name column, parent code column)
_LEVELS: dict[str, tuple[Any, str, str, str | None]] = {
    "company": (DimCompany, "company_code", "company_name", None),
    "bu": (DimBusinessUnit, "bu_code", "bu_name", "company_code"),
    "sales_line": (DimSalesLine, "sales_line_code", "sales_line_name", "bu_code"),
    "zone": (DimZone, "zone_code", "zone_name", "sales_line_code"),
    "region": (DimRegion, "region_code", "region_name", "zone_code"),
    "area": (DimArea, "area_code", "area_name", "region_code"),
    "unit": (DimUnit, "unit_code", "unit_name", "area_code"),
    "territory": (DimTerritory, "territory_code", "territory_name", "unit_code"),
    "sub_territory": (DimSubTerritory, "sub_territory_code", "sub_territory_name",
                      "territory_code"),
}

#: Which fact view carries each business type's link to the hierarchy.
_MEMBERSHIP_VIEWS: dict[str, tuple[str, str]] = {
    "customer": (q.SALES_VIEW, "customer_code"),
    "sales_force": (q.SALES_VIEW, "sales_force_code"),
}

#: The deepest organisational level each business type is attributed to.
_ATTACH_LEVEL: dict[str, str] = {
    "customer": "sub_territory",
    "sales_force": "territory",
}


def code_field(level: str) -> str:
    """``territory`` -> ``territory_code``."""
    return _LEVELS[level][1] if level in _LEVELS else f"{level}_code"


@dataclass
class HierarchyNode:
    """One organisational entity inside the resolved scope."""

    type: str
    code: str
    name: str
    parent_type: str | None
    parent_code: str | None


@dataclass
class OrgScope:
    """The organisational slice a filter selects.

    ``codes`` holds every level's in-scope codes: the selected entity, its
    ancestors (so the map can place it) and its descendants (so the map can
    show what is under it).
    """

    codes: dict[str, set[str]] = field(default_factory=dict)
    nodes: list[HierarchyNode] = field(default_factory=list)
    #: The deepest level the user actually filtered on, for diagnostics.
    selected_level: str | None = None
    selected_code: str | None = None
    #: True when a filter matched nothing at all.
    empty: bool = False

    def of(self, level: str) -> set[str]:
        return self.codes.get(level, set())

    def descendants_of_selection(self) -> dict[str, int]:
        """How many entities sit *below* the selected level, by level."""
        if self.selected_level is None:
            return {level: len(self.of(level)) for level in ORG_CHAIN}
        index = ORG_CHAIN.index(self.selected_level)
        return {
            level: len(self.of(level))
            for level in ORG_CHAIN[index + 1:]
        }


def _path_query():
    """One outer join down the whole chain: every complete organisational path.

    Outer joins matter — a territory with no sub-territory must still produce a
    row, otherwise filtering to it would return nothing.
    """
    columns = []
    for level in ORG_CHAIN:
        model, code, name, _ = _LEVELS[level]
        columns.append(getattr(model, code).label(f"{level}_code"))
        columns.append(getattr(model, name).label(f"{level}_name"))

    statement = select(*columns).select_from(DimCompany)
    joins = [
        (DimBusinessUnit, DimBusinessUnit.company_code == DimCompany.company_code),
        (DimSalesLine, DimSalesLine.bu_code == DimBusinessUnit.bu_code),
        (DimZone, DimZone.sales_line_code == DimSalesLine.sales_line_code),
        (DimRegion, DimRegion.zone_code == DimZone.zone_code),
        (DimArea, DimArea.region_code == DimRegion.region_code),
        (DimUnit, DimUnit.area_code == DimArea.area_code),
        (DimTerritory, DimTerritory.unit_code == DimUnit.unit_code),
        (DimSubTerritory,
         DimSubTerritory.territory_code == DimTerritory.territory_code),
    ]
    for model, condition in joins:
        statement = statement.outerjoin(model, condition)
    return statement


def resolve_org_scope(session: Session, filters: dict[str, str | None]) -> OrgScope:
    """Turn organisational filters into every code in scope, at every level.

    Multiple filters intersect: a row must satisfy *all* of them, so
    ``region=Dhaka & territory=T001`` yields T001 only if T001 really sits in
    Dhaka — and yields nothing if it does not.
    """
    supplied = {
        level: str(value).strip()
        for level, value in (filters or {}).items()
        if level in _LEVELS and value not in (None, "")
    }

    rows = session.execute(_path_query()).mappings().all()

    matching = [
        row for row in rows
        if all(row.get(f"{level}_code") == code for level, code in supplied.items())
    ]

    scope = OrgScope()
    if supplied:
        deepest = max(supplied, key=lambda level: ORG_CHAIN.index(level))
        scope.selected_level = deepest
        scope.selected_code = supplied[deepest]
    scope.empty = bool(supplied) and not matching

    seen: set[tuple[str, str]] = set()
    for row in matching:
        for index, level in enumerate(ORG_CHAIN):
            code = row.get(f"{level}_code")
            if not code:
                continue
            scope.codes.setdefault(level, set()).add(code)
            key = (level, code)
            if key in seen:
                continue
            seen.add(key)
            parent_type = ORG_CHAIN[index - 1] if index else None
            scope.nodes.append(HierarchyNode(
                type=level,
                code=code,
                name=row.get(f"{level}_name") or code,
                parent_type=parent_type,
                parent_code=row.get(f"{parent_type}_code") if parent_type else None,
            ))
    return scope


@dataclass
class BusinessEntity:
    """A customer or sales-force point and where it belongs."""

    type: str
    code: str
    name: str
    parent_type: str
    parent_code: str | None


def resolve_business_entities(session: Session, scope: OrgScope, *,
                              types: Iterable[str] = BUSINESS_TYPES,
                              limit: int = 5000) -> dict[str, list[BusinessEntity]]:
    """Customers and sales force inside an organisational scope.

    One ``DISTINCT`` per type against the detail view — two queries total,
    regardless of how many entities come back. Deliberately **not** filtered by
    date or product: membership is a standing fact about where something
    belongs, and a product filter must not empty a territory of its customers.
    """
    wanted = [t for t in types if t in _MEMBERSHIP_VIEWS]
    result: dict[str, list[BusinessEntity]] = {t: [] for t in wanted}
    if scope.empty:
        return result

    names = _master_names(session, wanted)

    for entity_type in wanted:
        view_name, code_column = _MEMBERSHIP_VIEWS[entity_type]
        table = q.view(session, view_name)
        if code_column not in table.c:
            continue

        # Candidate parent levels, deepest first. One row per entity is what the
        # map needs — a customer trading in two sub-territories is still one
        # point — so the query groups by the entity and picks the deepest level
        # that is actually populated. ``min`` ignores NULLs, which is exactly the
        # behaviour wanted when a fact carries no sub-territory.
        preferred = _ATTACH_LEVEL[entity_type]
        candidates = [
            level for level in reversed(ORG_CHAIN[: ORG_CHAIN.index(preferred) + 1])
            if code_field(level) in table.c
        ]
        if not candidates:
            continue

        conditions = [table.c[code_column].isnot(None)]
        conditions += _scope_conditions(table, scope)

        statement = (
            select(
                table.c[code_column].label("code"),
                *[func.min(table.c[code_field(level)]).label(level)
                  for level in candidates],
            )
            .where(and_(*conditions))
            .group_by(table.c[code_column])
            .limit(limit)
        )

        for row in session.execute(statement).mappings():
            parent_type, parent_code = None, None
            for level in candidates:            # deepest first
                if row.get(level):
                    parent_type, parent_code = level, row[level]
                    break
            result[entity_type].append(BusinessEntity(
                type=entity_type,
                code=row["code"],
                # No master record exists for these dimensions yet, so the code
                # is the honest label rather than an invented name.
                name=names.get(entity_type, {}).get(row["code"]) or row["code"],
                parent_type=parent_type or preferred,
                parent_code=parent_code,
            ))

    _add_master_customers(session, scope, result, names)
    _add_master_sales_force(session, scope, result, names)
    return result


def _add_master_customers(session: Session, scope: OrgScope,
                          result: dict[str, list[BusinessEntity]],
                          names: dict[str, dict[str, str]] | None = None) -> None:
    """Add customers from the master's own ``sub_territory_code``.

    This field is the **authoritative** Sub-Territory → Customer mapping, so it
    takes precedence over the fact-derived membership above in both directions:

    * a customer assigned to a sub-territory appears there even if it has never
      transacted, which the fact-derived rule could never show;
    * a customer whose master assignment differs from where it has traded is
      shown where the master says, because that is the record of where it
      *belongs* rather than where it happened to buy.

    The fact-derived rule stays as the fallback for customers the field has not
    been set on, so nothing that worked before stops working.
    """
    from ..database.models_warehouse import DimCustomer

    if "customer" not in result:
        return
    sub_territories = scope.of("sub_territory")
    if not sub_territories:
        return

    # Every assignment, not only the in-scope ones. Both halves of "authoritative"
    # need it: adding the customers assigned *here*, and removing the ones the
    # facts placed here whose master says they belong somewhere else.
    assigned = {
        code: sub_territory_code
        for code, sub_territory_code in session.execute(
            select(DimCustomer.customer_code, DimCustomer.sub_territory_code)
            .where(DimCustomer.sub_territory_code.isnot(None),
                   DimCustomer.is_deleted.is_(False))
        ).all()
    }
    in_scope = set(sub_territories)

    kept: list[BusinessEntity] = []
    for entity in result["customer"]:
        where = assigned.get(entity.code)
        if where is None:
            kept.append(entity)             # unassigned: the facts still decide
            continue
        if where not in in_scope:
            # Assigned outside this filter. Its transactions here do not make it
            # a customer *of* here — that is what the master field settles.
            continue
        entity.parent_type = "sub_territory"
        entity.parent_code = where
        kept.append(entity)
    result["customer"] = kept

    known = {entity.code for entity in kept}
    lookup = (names or {}).get("customer", {})

    rows = session.execute(
        select(DimCustomer.customer_code, DimCustomer.customer_name,
               DimCustomer.sub_territory_code)
        .where(DimCustomer.sub_territory_code.in_(sorted(in_scope)),
               DimCustomer.is_deleted.is_(False))
    ).all()
    for code, name, sub_territory_code in rows:
        if code in known:
            continue
        known.add(code)
        # Assigned here but never seen in a transaction — invisible to the
        # fact-derived rule, and exactly what the master field is for.
        result["customer"].append(BusinessEntity(
            type="customer", code=code, name=lookup.get(code) or name or code,
            parent_type="sub_territory", parent_code=sub_territory_code,
        ))


def _master_names(session: Session, types: Iterable[str]) -> dict[str, dict[str, str]]:
    """Real names from the master dimensions, when those dimensions are populated.

    They are ``PENDING_SOURCE_DATA`` today, so this returns empty maps — but the
    moment a customer master is uploaded, the map starts showing names instead
    of codes with no further change.
    """
    from ..database.models_warehouse import DimCustomer

    sources = {
        "customer": (DimCustomer, DimCustomer.customer_code, DimCustomer.customer_name),
        "sales_force": (DimSalesForce, DimSalesForce.sales_force_code,
                        DimSalesForce.sales_force_name),
    }
    names: dict[str, dict[str, str]] = {}
    for entity_type in types:
        source = sources.get(entity_type)
        if source is None:
            continue
        _model, code, name = source
        names[entity_type] = {
            row_code: row_name
            for row_code, row_name in session.execute(select(code, name)).all()
            if row_name
        }
    return names


def _scope_conditions(table, scope: OrgScope) -> list:
    """Constrain a fact view to the resolved scope.

    Applied at the **deepest** level the scope actually pins, because that is
    the most selective single condition and it implies every level above it.
    """
    for level in reversed(ORG_CHAIN):
        codes = scope.of(level)
        column = code_field(level)
        if codes and column in table.c:
            return [table.c[column].in_(sorted(codes))]
    return []


def _add_master_sales_force(session: Session, scope: OrgScope,
                            result: dict[str, list[BusinessEntity]],
                            names: dict[str, dict[str, str]] | None = None) -> None:
    """Add sales force from the master, which does carry ``territory_code``.

    ``dim_sales_force`` is the one business dimension with a real
    organisational column, so a sales person assigned to a territory appears
    even if they have not transacted there yet.
    """
    if "sales_force" not in result:
        return
    territories = scope.of("territory")
    if not territories:
        return

    rows = session.execute(
        select(DimSalesForce.sales_force_code, DimSalesForce.sales_force_name,
               DimSalesForce.territory_code)
        .where(DimSalesForce.territory_code.in_(sorted(territories)))
    ).all()

    known = {entity.code for entity in result["sales_force"]}
    for code, name, territory_code in rows:
        if code in known:
            continue
        known.add(code)
        result["sales_force"].append(BusinessEntity(
            type="sales_force", code=code, name=name or code,
            parent_type="territory", parent_code=territory_code,
        ))


def apply_data_scope(user: UserContext, session: Session,
                     filters: dict[str, str | None]) -> dict[str, str | None]:
    """Fold the caller's data scope into the requested filters.

    Reuses :class:`PermissionFilter` rather than re-deriving authorisation, so
    the map obeys exactly the rules every report does: an out-of-scope code is
    refused outright, and an unfiltered request is narrowed to the scope instead
    of returning the whole company.
    """
    permissions = PermissionFilter(session, user)
    requested = ScopeFilters()
    from ..ai.permission_filter import FILTER_FIELD_BY_LEVEL

    for level, value in (filters or {}).items():
        column = code_field(level)
        field_name = FILTER_FIELD_BY_LEVEL.get(column)
        if field_name and value:
            setattr(requested, field_name, [str(value)])

    # Raises PermissionDeniedError for anything outside the user's scope.
    enforced = permissions.enforce(requested)

    merged = dict(filters or {})
    for column, field_name in FILTER_FIELD_BY_LEVEL.items():
        values = getattr(enforced, field_name, None)
        if values and not merged.get(column.removesuffix("_code")):
            # A scope covering several codes cannot be pinned to one filter;
            # the resolver still narrows through the ancestor chain.
            if len(values) == 1:
                merged[column.removesuffix("_code")] = values[0]
    return merged


__all__ = [
    "ORG_CHAIN",
    "BUSINESS_TYPES",
    "ALL_TYPES",
    "HierarchyNode",
    "OrgScope",
    "BusinessEntity",
    "resolve_org_scope",
    "resolve_business_entities",
    "apply_data_scope",
    "code_field",
]
