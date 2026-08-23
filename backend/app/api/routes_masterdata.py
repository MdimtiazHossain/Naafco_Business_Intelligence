"""Master-data endpoints for cascading filters and global search.

Both are RBAC-scoped: a regional manager's filter dropdowns only ever contain
their own region and what sits beneath it, and global search never returns an
entity they may not see. The scope is applied in the query, not in the browser.

Cascading is server-side by design — the frontend asks for "regions under zone
Z001" rather than downloading the whole hierarchy and filtering locally.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import queries
from ..ai.entity_resolver import EntityResolver
from ..ai.permission_filter import LEVEL_BY_ENTITY, PermissionFilter, UserContext
from ..ai.schemas import EntityType
from ..etl.mapping import BINDING_BY_LEVEL, LEVEL_BINDINGS, LEVEL_DEPTH, MasterDataIndex
from .deps import get_current_user, get_session, internal_error
from .filter_space import ATTACHED_LEVELS, SPACE_LEVELS, FilterSpace

logger = logging.getLogger("app.api.masterdata")

router = APIRouter(prefix="/api/master-data", tags=["master-data"])

MAX_OPTIONS = 500

#: **The filter hierarchy: every level and the level above it.**
#:
#: One declaration for the whole application, so a page never carries a private
#: copy and the two directions — parent narrows the child's options, child
#: selects its parents — are read from the same place.
#:
#: The organisational chain is *derived* from ``LEVEL_BINDINGS`` rather than
#: restated, because that tuple is what the ETL resolves master data with; a
#: second copy here could drift from the one the warehouse is actually built on.
#: Note it is nine levels, not eight — ``bu_code`` sits between company and
#: sales line.
#:
#: The remaining edges are declared, and each one names a real column on a real
#: master. Nothing here is inferred from a name:
#:
#: * ``customer_code`` -> ``dim_customer.sub_territory_code`` (revision 0011,
#:   the authoritative Sub-Territory -> Customer mapping).
#: * ``sales_force_code`` -> ``dim_sales_force.territory_code``. Declared for
#:   completeness and inert in practice: that dimension is
#:   ``PENDING_SOURCE_DATA`` and holds no rows, so it resolves nothing until a
#:   master arrives.
#: * the material chain — Group -> Brand -> Material, all three off
#:   ``dim_material``'s own row. Since revision 0022 it is not a stock-only
#:   chain: it is *the* item chain, and it narrows a sales or target report
#:   exactly as it narrows a stock one.
#: * the location chain — Company -> Plant -> Storage Location — which stays
#:   stock-only, because a sale states neither a plant nor a storage location.
FILTER_PARENTS: dict[str, str | None] = {
    **{b.code_field: b.parent_code_field for b in LEVEL_BINDINGS},
    "customer_code": "sub_territory_code",
    "sales_force_code": "territory_code",
    "plant_code": "company_code",
    "storage_location_key": "plant_code",
    "material_group_code": None,
    "material_brand": "material_group_code",
    "material_code": "material_brand",
}

#: The item levels — the Material Master's three, which every dataset honours.
#:
#: Group -> Brand is a **narrowing, not a hierarchy**: a brand can appear under
#: more than one material group, so choosing a group shortens the brand list
#: while choosing a brand implies no group.
MATERIAL_LEVELS: tuple[str, ...] = (
    "material_group_code", "material_brand", "material_code",
)

#: The location levels, which only material stock has. ``company_code`` is the
#: head of this chain and comes from the ordinary sales hierarchy — it is the one
#: level the two surfaces genuinely share, since
#: ``fact_material_stock.company_code`` is the same enterprise code
#: ``dim_company`` holds.
LOCATION_LEVELS: tuple[str, ...] = ("plant_code", "storage_location_key")

#: What the material-stock page offers: both chains. Two chains rather than one,
#: because a stock position is located and classified independently — no material
#: belongs to a plant and no plant implies a material group.
STOCK_LEVELS: tuple[str, ...] = (*LOCATION_LEVELS, *MATERIAL_LEVELS)

STOCK_LEVEL_PARENTS: dict[str, str | None] = {
    level: FILTER_PARENTS[level] for level in STOCK_LEVELS
}

#: Human labels for the two declared chains, published by ``/levels`` so no
#: client has to invent one. Declared once and read by both the ``material`` and
#: ``stock`` sections of that response.
LEVEL_LABELS: dict[str, str] = {
    "plant_code": "Plant",
    "storage_location_key": "Storage Location",
    "material_group_code": "Material Group",
    "material_brand": "Material Brand",
    "material_code": "Material",
}

#: Entity types searchable from the header search box.
SEARCHABLE: tuple[EntityType, ...] = (
    EntityType.REGION, EntityType.ZONE, EntityType.AREA, EntityType.UNIT,
    EntityType.TERRITORY, EntityType.SUB_TERRITORY,
    EntityType.CUSTOMER, EntityType.COMPANY, EntityType.BUSINESS_UNIT,
    EntityType.SALES_LINE, EntityType.SALES_FORCE, EntityType.MATERIAL,
)

#: Where a search result opens in the app.
ROUTE_BY_TYPE: dict[EntityType, str] = {
    EntityType.MATERIAL: "/materials",
    EntityType.CUSTOMER: "/customers",
}


@router.get("/levels")
def levels() -> dict[str, Any]:
    """The hierarchy, so the frontend never hardcodes it."""
    return {
        "levels": [
            {
                "level": binding.code_field,
                "label": binding.code_field.replace("_code", "").replace("_", " ").title(),
                "parent": binding.parent_code_field,
                "depth": LEVEL_DEPTH[binding.code_field],
            }
            for binding in LEVEL_BINDINGS
        ],
        "independent": [
            {"level": "customer_code", "label": "Customer"},
            {"level": "sales_force_code", "label": "Sales Force"},
        ],
        # The item chain, which every dataset honours since revision 0022. Its
        # own section rather than part of ``stock``, because it is no longer
        # stock's alone: a sales report is narrowed by Material Group ->
        # Material Brand -> Material exactly as a stock report is.
        "material": [
            {"level": level, "parent": FILTER_PARENTS[level],
             "label": LEVEL_LABELS[level]}
            for level in MATERIAL_LEVELS
        ],
        # Published for the same reason ``levels`` is: so a client can discover
        # the chain rather than infer it. The web frontend declares this one
        # alongside the sales hierarchy it also declares, so the two stay
        # consistent with each other; this endpoint is what any other client,
        # and anyone reading the API, can check that declaration against.
        "stock": [
            {"level": level, "parent": parent, "label": LEVEL_LABELS[level]}
            for level, parent in STOCK_LEVEL_PARENTS.items()
        ],
        # The whole filter hierarchy in one map, including the edges that are
        # neither organisational nor stock — customer and sales force. This is
        # the declaration the bidirectional filter behaviour is driven by:
        # a parent narrows its child's options, and a child selects its parents.
        "parents": FILTER_PARENTS,
    }


#: Every level a caller may state a selection at.
#:
#: The two chains that are not organisational are here in full, because the
#: filter space intersects across all of them: a material group narrows the
#: companies, and those companies narrow the plants.
SELECTION_LEVELS: tuple[str, ...] = tuple(sorted(SPACE_LEVELS))


def filter_selection(
    company_code: list[str] | None = Query(None),
    bu_code: list[str] | None = Query(None),
    sales_line_code: list[str] | None = Query(None),
    zone_code: list[str] | None = Query(None),
    region_code: list[str] | None = Query(None),
    area_code: list[str] | None = Query(None),
    unit_code: list[str] | None = Query(None),
    territory_code: list[str] | None = Query(None),
    sub_territory_code: list[str] | None = Query(None),
    customer_code: list[str] | None = Query(None),
    sales_force_code: list[str] | None = Query(None),
    plant_code: list[str] | None = Query(None),
    storage_location_key: list[str] | None = Query(None),
    material_group_code: list[str] | None = Query(None),
    material_brand: list[str] | None = Query(None),
    material_code: list[str] | None = Query(None),
) -> dict[str, list[str]]:
    """The whole filter bar, as the selection the options are computed from.

    Every level is a **repeated** parameter rather than a single value, because
    the bar is multi-select: ``?territory_code=T1&territory_code=T5`` is two
    territories, not a malformed one. Only these named levels exist — there is
    no free-text filter and no way to pass a predicate — so a crafted query
    string can narrow a list but never widen one.
    """
    supplied = {
        "company_code": company_code,
        "bu_code": bu_code,
        "sales_line_code": sales_line_code,
        "zone_code": zone_code,
        "region_code": region_code,
        "area_code": area_code,
        "unit_code": unit_code,
        "territory_code": territory_code,
        "sub_territory_code": sub_territory_code,
        "customer_code": customer_code,
        "sales_force_code": sales_force_code,
        "plant_code": plant_code,
        "storage_location_key": storage_location_key,
        "material_group_code": material_group_code,
        "material_brand": material_brand,
        "material_code": material_code,
    }
    return {level: [v for v in values if v]
            for level, values in supplied.items() if values}


def _needs_customers(levels: Sequence[str],
                     selection: dict[str, list[str]]) -> bool:
    """Whether this request has to read ``dim_customer`` at all.

    It is the one master here big enough to be worth skipping — thousands of
    rows against 267 sub-territories — and it only matters when a customer is
    being asked about or has been chosen.
    """
    wanted = set(levels) | {level for level, values in selection.items() if values}
    return bool(wanted & set(ATTACHED_LEVELS))


@router.get("/options/{level}")
def options(
    level: str,
    parent_code: str | None = Query(
        None, description="A single ancestor to narrow by, for callers that do "
                          "not send the whole selection. Folded into the "
                          "selection under `parent_level`, or under this "
                          "level's declared parent when that is omitted."),
    parent_level: str | None = Query(
        None, description="Which level `parent_code` belongs to. It need not be "
                          "the immediate parent — a company narrows territories "
                          "just as a unit does."),
    search: str | None = Query(None, max_length=100),
    limit: int = Query(200, ge=1, le=MAX_OPTIONS),
    selection: dict[str, list[str]] = Depends(filter_selection),
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """Filter options for one level, intersected against the whole selection.

    **A level is never gated on its parent being chosen.** The options are every
    value the master data still allows given everything else selected, so
    Territory is offered under a Company alone — which the old immediate-parent
    cascade could not do, because it asked ``dim_territory`` for the rows whose
    ``unit_code`` was a company code and got nothing.

    ``parent_code`` is kept for callers that narrow by a single ancestor. It is
    merged into the selection rather than handled separately, so both routes
    produce the same answer.
    """
    if level not in SPACE_LEVELS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown level '{level}'.")

    if parent_code:
        owner = parent_level or FILTER_PARENTS.get(level)
        if owner is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"'{level}' has no parent to narrow by; name one with "
                "`parent_level`.")
        if owner not in SPACE_LEVELS:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"Unknown level '{owner}'.")
        # A level the caller also sent explicitly wins nothing: both are
        # constraints and both apply, so they are unioned into one selection.
        selection = {**selection,
                     owner: sorted({*selection.get(owner, []), parent_code})}

    try:
        space = FilterSpace(session,
                            with_customers=_needs_customers([level], selection))
        permissions = PermissionFilter(session, user, space.index)
        answer = space.options_for(level, selection, permissions, search, limit)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, f"master-data options for {level}") from exc

    return {**answer.as_dict(), "parent_code": parent_code}


class FilterOptionsRequest(BaseModel):
    """One filter-bar state in, every control's options out."""

    model_config = ConfigDict(extra="forbid")

    #: Level -> the codes chosen there. A level absent or empty is "All" and
    #: restricts nothing.
    selection: dict[str, list[str]] = Field(default_factory=dict)
    #: The levels to return options for — the controls this page draws.
    levels: list[str] = Field(default_factory=list)
    #: The levels the user has just changed. They are never pruned, which is
    #: what makes the pruning well defined: with Company changed while an
    #: incompatible Territory is still selected, either could be dropped, and
    #: only the user's last action says which one they meant.
    changed: list[str] = Field(default_factory=list)
    #: Optional per-level search text, applied to the code and the label.
    search: dict[str, str] = Field(default_factory=dict)
    limit: int = Field(200, ge=1, le=MAX_OPTIONS)


@router.post("/filter-options")
def filter_options(
    body: FilterOptionsRequest,
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """The whole filter bar in one request: what stays selected, and what each
    control may now offer.

    One round trip per user action rather than one per control. That is not only
    cheaper — it is what keeps the bar consistent, because every control's
    options and the pruned selection are computed from the *same* state, so a
    control can never draw a list that disagrees with the filters beside it.

    Levels this endpoint cannot answer for are reported in ``unsupported``
    rather than refused. ``batch_code`` is transaction data with no master list
    and ``expiry_status`` is derived from a date against today; a caller may
    legitimately hold both in the same bar, and 422-ing the whole request
    because one of fifteen levels has no master would take the other fourteen
    down with it.
    """
    selection = {level: [value for value in values if value]
                 for level, values in body.selection.items() if values}
    known = {level: values for level, values in selection.items()
             if level in SPACE_LEVELS}
    # A filter with no master list still belongs to the caller's state and is
    # handed back untouched — it is not this endpoint's to prune.
    passthrough = {level: values for level, values in selection.items()
                   if level not in SPACE_LEVELS}

    requested = [level for level in body.levels if level in SPACE_LEVELS]
    unsupported = [level for level in body.levels if level not in SPACE_LEVELS]

    try:
        space = FilterSpace(
            session,
            with_customers=_needs_customers(requested, known),
        )
        permissions = PermissionFilter(session, user, space.index)
        kept, removed = space.prune(known, anchor=body.changed)
        answers = {
            level: space.options_for(level, kept, permissions,
                                     body.search.get(level), body.limit).as_dict()
            for level in requested
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "master-data filter options") from exc

    return {
        "selection": {**kept, **passthrough},
        "removed": removed,
        "options": answers,
        "unsupported": unsupported,
    }


def _single_parent(session: Session, model, child_column, child_code: str,
                   parent_column) -> str | None:
    """The parent of one child code, or ``None`` when it is not unambiguous.

    A parent is only returned when **every** master row for this child agrees on
    it. That is what stops the child -> parent direction inventing a selection:
    a material brand, for instance, can appear under more than one material
    group, so a brand alone does not determine a group and this returns nothing
    rather than picking one. Selecting a parent that does not actually contain
    the child would silently narrow a report to the wrong slice.
    """
    rows = session.execute(
        select(parent_column).where(child_column == child_code).distinct()
    ).all()
    values = {row[0] for row in rows if row[0] is not None}
    return values.pop() if len(values) == 1 else None


#: Where a *transaction*-derived parent comes from, per page.
#:
#: The two stock chains do not meet in any master: ``dim_material`` states a
#: group and a brand and no plant, because a material is not *located* anywhere
#: until somebody stocks it. Where a material is held is recorded by the stock
#: position and nowhere else, so deriving Material -> Storage Location -> Plant
#: -> Company means reading the fact table.
#:
#: Declared per page rather than globally, because the answer differs by page: a
#: material's plant is a fact about stock, and a sales report has no plant at all
#: to resolve. A page whose source is not named here resolves master parents only,
#: which is every page except Material Stock.
TRANSACTION_PARENT_SOURCES: dict[str, tuple[str, tuple[str, ...]]] = {
    # source -> (view, the parent levels that view can supply, shallowest first)
    "material_stock": ("vw_material_stock_detail",
                       ("company_code", "plant_code", "storage_location_key")),
}

#: The levels a transaction-derived parent may be resolved *from*.
#:
#: The item levels only. Asking the stock view where a plant is would be
#: circular — a plant is already one of the answers — and the location chain
#: resolves through its own masters perfectly well.
TRANSACTION_PARENT_CHILDREN: frozenset[str] = frozenset(MATERIAL_LEVELS)

#: The column on the view that each item level filters by.
_TRANSACTION_CHILD_COLUMN: dict[str, str] = {
    "material_code": "material_code",
    "material_brand": "material_brand",
    "material_group_code": "material_group_code",
}


def _transaction_ancestors(session: Session, source: str, level: str,
                           codes: Sequence[str]) -> dict[str, set[str]]:
    """Parent values the page's own transaction data implies for these codes.

    One ``DISTINCT`` over the page's view, not one query per level: the three
    location columns come back together because they are on the same row, and a
    material stocked in four places yields all four in a single pass.

    **Every value returned is a real combination somebody recorded.** This is
    the difference between deriving a parent and guessing one — a material is
    not assigned to a plant by this function, it is *found* at the plants where
    stock of it actually sits.
    """
    entry = TRANSACTION_PARENT_SOURCES.get(source)
    column = _TRANSACTION_CHILD_COLUMN.get(level)
    if entry is None or column is None or not codes:
        return {}

    view_name, parent_levels = entry
    table = queries.view(session, view_name)
    if column not in table.c:
        return {}

    available = [name for name in parent_levels if name in table.c]
    if not available:
        return {}

    statement = (
        select(*[table.c[name] for name in available])
        .where(table.c[column].in_(list(codes)))
        .distinct()
    )
    if "is_void" in table.c:
        statement = statement.where(table.c["is_void"] == False)  # noqa: E712

    found: dict[str, set[str]] = {name: set() for name in available}
    for row in session.execute(statement):
        for name, value in zip(available, row):
            if value is not None and str(value) != "":
                found[name].add(str(value))
    return {name: values for name, values in found.items() if values}


def _master_ancestors(session: Session, level: str, code: str) -> dict[str, str]:
    """Every parent level one code implies, read from master data alone."""
    from ..database.models import DimMaterial, DimPlant, DimStorageLocation
    from ..database.models_warehouse import DimCustomer, DimSalesForce

    index = MasterDataIndex(session)
    chain: dict[str, str] = {}

    if level in BINDING_BY_LEVEL:
        chain = dict(index.ancestors_of(level, code))

    elif level == "customer_code":
        parent = _single_parent(session, DimCustomer, DimCustomer.customer_code,
                                code, DimCustomer.sub_territory_code)
        if parent:
            chain = dict(index.ancestors_of("sub_territory_code", parent))

    elif level == "sales_force_code":
        parent = _single_parent(session, DimSalesForce,
                                DimSalesForce.sales_force_code, code,
                                DimSalesForce.territory_code)
        if parent:
            chain = dict(index.ancestors_of("territory_code", parent))

    elif level == "material_code":
        # Both classifications come off the material's own row, so each is
        # exact. The group is read directly rather than through the brand,
        # because a brand does not determine a group.
        row = session.execute(
            select(DimMaterial.material_brand, DimMaterial.material_group_code)
            .where(DimMaterial.material_code == code)
        ).first()
        if row:
            chain = {"material_brand": row[0], "material_group_code": row[1]}

    elif level == "material_brand":
        group = _single_parent(session, DimMaterial, DimMaterial.material_brand,
                               code, DimMaterial.material_group_code)
        if group:
            chain = {"material_group_code": group}

    elif level == "storage_location_key":
        plant = _single_parent(session, DimStorageLocation,
                               DimStorageLocation.storage_location_key, code,
                               DimStorageLocation.plant_code)
        if plant:
            chain = {"plant_code": plant}
            company = _single_parent(session, DimPlant, DimPlant.plant_code,
                                     plant, DimPlant.company_code)
            if company:
                chain["company_code"] = company

    elif level == "plant_code":
        company = _single_parent(session, DimPlant, DimPlant.plant_code, code,
                                 DimPlant.company_code)
        if company:
            chain = {"company_code": company}

    chain.pop(level, None)
    return {parent: value for parent, value in chain.items() if value is not None}


def resolve_ancestor_values(session: Session, permissions: PermissionFilter,
                            level: str, codes: Sequence[str],
                            source: str | None = None) -> dict[str, list[str]]:
    """Every parent level implied by one *or several* selected codes.

    The child -> parent half of the filter system, in its general form: a list
    in, a list per parent level out. Selecting three materials that sit in two
    plants selects both plants, which is what a filter bar has to show for the
    selection to be honest about what it is filtering on.

    Two sources of truth, and they are kept distinct on purpose:

    * **Masters**, through :func:`_master_ancestors`. Exact and page-independent:
      a material's brand and group are its own columns, a storage location's
      plant is the Storage Location Master's.
    * **The page's own transaction data**, through :func:`_transaction_ancestors`,
      and only for the levels and pages :data:`TRANSACTION_PARENT_SOURCES` names.
      This is how a material reaches a plant at all — no master places it
      anywhere, and the stock position is the only record of where it is held.

    The second was once forbidden outright: a parent used to be selected only
    where a master stated it unambiguously. That rule protected against
    *inventing* a relationship, and this does not invent one — every pair it
    returns is a combination the business actually recorded. What it drops is
    the unambiguity requirement, which multi-value selection makes unnecessary:
    a material held at three plants no longer has to resolve to one plant or to
    none, it resolves to three.

    The result excludes ``level`` itself and anything the caller may not see.
    """
    merged: dict[str, set[str]] = {}
    for code in codes:
        for parent, value in _master_ancestors(session, level, code).items():
            merged.setdefault(parent, set()).add(value)

    if level in TRANSACTION_PARENT_CHILDREN and source:
        for parent, values in _transaction_ancestors(
                session, source, level, codes).items():
            merged.setdefault(parent, set()).update(values)

    merged.pop(level, None)
    # Scope is re-applied to the answer, not assumed from the question. An
    # organisational level is checked value by value — a derived parent the
    # caller may not see is dropped, not the whole level — and the rest carry no
    # scope of their own, in the same way the options endpoints above treat them.
    resolved: dict[str, list[str]] = {}
    for parent, values in merged.items():
        allowed = sorted(
            value for value in values
            if parent not in BINDING_BY_LEVEL
            or permissions.is_within_scope(parent, value)
        )
        if allowed:
            resolved[parent] = allowed
    return resolved


def resolve_ancestors(session: Session, permissions: PermissionFilter,
                      level: str, code: str) -> dict[str, str]:
    """One code in, one parent value per level out.

    The single-selection form, kept because every page except Material Stock
    still selects one value per level and reads one back. A level whose parents
    are genuinely several under this code resolves to none of them here rather
    than to an arbitrary one — the same refusal this function has always made.
    """
    return {
        parent: values[0]
        for parent, values in resolve_ancestor_values(
            session, permissions, level, [code]).items()
        if len(values) == 1
    }


@router.get("/ancestors/{level}")
def ancestors(
    level: str,
    code: list[str] = Query(..., min_length=1,
                            description="The selected code(s) at this level. "
                                        "Repeat the parameter to resolve the "
                                        "parents of a multiple selection."),
    source: str | None = Query(
        None, description="The page's transaction source — 'material_stock' for "
                          "the Material Stock page. Where a page names one, "
                          "parents that only its transaction data can supply are "
                          "resolved too: a material has no plant in any master, "
                          "so where it is held comes from the stock position."),
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """The parent levels implied by a selection — the whole chain, one call.

    Selecting a customer implies a sub-territory, territory, unit, area, region,
    zone, sales line, business unit and company. Answering that in **one**
    request is the point: the browser sets every one of those filters together
    and issues a single report query, rather than walking the hierarchy a level
    at a time and re-fetching the page after each step. The same holds for a
    multiple selection — three materials resolve their plants in one request,
    not three.

    ``ancestors`` maps each parent level to **a list**, because a selection can
    genuinely imply several: three materials stocked across two plants imply
    both. ``ancestor`` is the same answer keyed to a single value where a level
    has exactly one, for the callers that select one value per level.
    """
    if level not in FILTER_PARENTS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown level '{level}'.")
    if source is not None and source not in TRANSACTION_PARENT_SOURCES:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Unknown source '{source}'. "
            f"Choose one of: {', '.join(sorted(TRANSACTION_PARENT_SOURCES))}.")
    try:
        index = MasterDataIndex(session)
        permissions = PermissionFilter(session, user, index)
        resolved = resolve_ancestor_values(session, permissions, level, code,
                                           source)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, f"ancestors for {level}") from exc

    return {
        "level": level,
        "codes": code,
        "source": source,
        "ancestors": resolved,
        # The single-value view of the same answer, for the pages that hold one
        # value per level. A level with several parents is absent rather than
        # arbitrarily narrowed to one of them.
        "ancestor": {parent: values[0] for parent, values in resolved.items()
                     if len(values) == 1},
    }


@router.get("/search")
def search(
    q: str = Query(..., min_length=2, max_length=100),
    limit: int = Query(20, ge=1, le=50),
    session: Session = Depends(get_session),
    user: UserContext = Depends(get_current_user),
) -> dict[str, Any]:
    """Global search across the master data the caller is allowed to see."""
    try:
        index = MasterDataIndex(session)
        permissions = PermissionFilter(session, user, index)
        resolver = EntityResolver(session)
        # raise_on_ambiguous is off: the search box wants every candidate, it is
        # the chat agent that must stop and ask.
        candidates = resolver.candidates(q.strip())
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "global search") from exc

    results: list[dict[str, Any]] = []
    for entry in candidates:
        if entry.entity_type not in SEARCHABLE:
            continue
        level = LEVEL_BY_ENTITY.get(entry.entity_type)
        if level and not permissions.is_within_scope(level, entry.code):
            continue
        results.append({
            "entity_type": entry.entity_type.value,
            "type_label": entry.entity_type.value.replace("_", " ").title(),
            "code": entry.code,
            "label": entry.label,
            "route": ROUTE_BY_TYPE.get(entry.entity_type, "/performance"),
            "filter_level": level or f"{entry.entity_type.value}_code",
        })
        if len(results) >= limit:
            break

    return {"query": q, "count": len(results), "results": results}
