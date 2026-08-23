"""Assembling the multi-level map view.

One request, every level. Given a hierarchy filter, this returns the selected
entity, its ancestors, its descendants and the customers, sales force and
warehouses beneath it — each carrying its own type so the Marker Designer
decides how it is drawn.

Two ideas keep the behaviour predictable:

**Filter is not layer visibility.** The filter decides which entities *belong*
in the answer; ``layers`` decides which of those types are returned for drawing.
Turning the customer layer off does not narrow the scope, and turning it back on
needs no re-filtering — the counts reported are always the counts of the scope,
not of what happens to be visible.

**Membership is not a business metric.** A customer belongs to a territory
because of where it trades, which does not change when the period or product
filter changes. So the entity set is resolved without those filters, while the
metric attached to each entity honours them.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ..ai import queries as q
from ..ai.permission_filter import UserContext
from ..ai.schemas import ScopeFilters
from ..database.models_map import MapEntityLocation
from .data import MetricSpec, _cluster, get_metric
from .geo import bounds_of
from .hierarchy import (
    ALL_TYPES,
    BUSINESS_TYPES,
    ORG_CHAIN,
    BusinessEntity,
    OrgScope,
    code_field,
    resolve_business_entities,
    resolve_org_scope,
)
from .resolver import resolve as resolve_marker

#: Types drawn by default. Every level is *available*; showing all nine at once
#: is rarely what an operator wants, so the shallow organisational levels are
#: off until asked for.
#:
#: **Every name here must be in** :data:`hierarchy.ALL_TYPES`. The endpoint
#: falls back to this list when the caller names no layers and then rejects any
#: layer it does not recognise, so a stale name here does not degrade to a
#: missing layer — it makes the default request fail outright. ``warehouse`` was
#: left behind when revision 0020 removed the dimension, and did exactly that.
DEFAULT_LAYERS: tuple[str, ...] = (
    "region", "area", "unit", "territory", "sub_territory",
    "customer", "sales_force",
)

#: Beyond this many points of one type, that type is clustered.
CLUSTER_AT = 150


@dataclass
class MapEntity:
    """One point on the map, whatever level it belongs to."""

    type: str
    code: str
    name: str
    parent_type: str | None
    parent_code: str | None
    latitude: float | None = None
    longitude: float | None = None
    value: float | None = None
    location_source: str | None = None

    @property
    def placed(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "id": self.code,
            "code": self.code,
            "name": self.name,
            "parent_type": self.parent_type,
            "parent_id": self.parent_code,
            "parent_code": self.parent_code,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "value": self.value,
            "location_source": self.location_source,
        }


@dataclass
class EntityView:
    entities: list[MapEntity] = field(default_factory=list)
    #: Every type in scope -> how many entities, whether drawn or not.
    counts: dict[str, int] = field(default_factory=dict)
    placed_counts: dict[str, int] = field(default_factory=dict)
    unplaced: list[dict[str, Any]] = field(default_factory=list)
    clusters: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    diagnostics: dict[str, Any] | None = None


def _metric_by_entity(session: Session, spec: MetricSpec, scope: OrgScope,
                      level: str, filters: ScopeFilters,
                      date_from: dt.date, date_to: dt.date) -> dict[str, float]:
    """Aggregate the chosen metric per entity of one type, in one query."""
    table = q.view(session, spec.measures.view_name)
    column_name = code_field(level) if level in ORG_CHAIN else f"{level}_code"
    if column_name not in table.c or spec.field not in table.c:
        return {}

    conditions = q.filter_conditions(table, filters, date_from, date_to)
    codes = scope.of(level) if level in ORG_CHAIN else None
    if codes:
        conditions.append(table.c[column_name].in_(sorted(codes)))

    statement = (
        select(table.c[column_name].label("code"),
               func.sum(table.c[spec.field]).label("value"))
        .group_by(table.c[column_name])
    )
    if conditions:
        statement = statement.where(and_(*conditions))

    return {
        row.code: float(row.value or 0)
        for row in session.execute(statement)
        if row.code is not None
    }


def build_entity_view(
    session: Session,
    user: UserContext,
    *,
    filters: dict[str, str | None],
    layers: Iterable[str] | None = None,
    metric: str = "net_sales",
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    business_filters: ScopeFilters | None = None,
    zoom: int = 7,
    diagnostics: bool = False,
    limit: int = 5000,
) -> EntityView:
    """Resolve a hierarchy filter into every entity that belongs under it."""
    requested_layers = [t for t in (layers or DEFAULT_LAYERS) if t in ALL_TYPES]
    spec = get_metric(metric)

    scope = resolve_org_scope(session, filters)
    business = resolve_business_entities(
        session, scope,
        types=[t for t in BUSINESS_TYPES if t in requested_layers] or BUSINESS_TYPES,
        limit=limit,
    )

    view = EntityView()

    # --- counts describe the scope, not the visible layers -----------------
    for level in ORG_CHAIN:
        view.counts[level] = len(scope.of(level))
    for entity_type, items in business.items():
        view.counts[entity_type] = len(items)

    if scope.empty:
        if diagnostics:
            view.diagnostics = _diagnostics(filters, scope, business, view)
        return view

    # --- coordinates, one query per type -----------------------------------
    locations = _locations(session, requested_layers)

    # --- metrics, one query per organisational level actually drawn --------
    metric_values: dict[str, dict[str, float]] = {}
    if date_from and date_to:
        for level in requested_layers:
            if level in ORG_CHAIN:
                metric_values[level] = _metric_by_entity(
                    session, spec, scope, level,
                    business_filters or ScopeFilters(), date_from, date_to,
                )

    # --- organisational entities -------------------------------------------
    for node in scope.nodes:
        if node.type not in requested_layers:
            continue
        view.entities.append(_to_entity(
            node.type, node.code, node.name, node.parent_type, node.parent_code,
            locations, metric_values.get(node.type, {}),
        ))

    # --- business entities --------------------------------------------------
    for entity_type, items in business.items():
        if entity_type not in requested_layers:
            continue
        for item in items:
            view.entities.append(_to_entity(
                item.type, item.code, item.name, item.parent_type, item.parent_code,
                locations, {},
            ))

    # --- placement and clustering ------------------------------------------
    for entity in view.entities:
        if entity.placed:
            view.placed_counts[entity.type] = view.placed_counts.get(entity.type, 0) + 1
        else:
            view.unplaced.append({
                "type": entity.type, "code": entity.code, "name": entity.name,
                "reason": "no_coordinate",
            })

    view.clusters = _cluster_dense_layers(view.entities, zoom)

    if diagnostics:
        view.diagnostics = _diagnostics(filters, scope, business, view)
    return view


def _to_entity(entity_type: str, code: str, name: str, parent_type: str | None,
               parent_code: str | None, locations: dict[tuple[str, str], Any],
               metrics: dict[str, float]) -> MapEntity:
    location = locations.get((entity_type, code))
    return MapEntity(
        type=entity_type, code=code, name=name,
        parent_type=parent_type, parent_code=parent_code,
        latitude=location.latitude if location else None,
        longitude=location.longitude if location else None,
        location_source=location.source if location else None,
        value=metrics.get(code),
    )


def _locations(session: Session, types: Iterable[str]) -> dict[tuple[str, str], Any]:
    """Every coordinate for the requested types, in one query."""
    wanted = sorted(set(types))
    if not wanted:
        return {}
    rows = session.execute(
        select(MapEntityLocation)
        .where(MapEntityLocation.entity_type.in_(wanted))
    ).scalars().all()
    return {(row.entity_type, row.entity_code): row for row in rows}


def _cluster_dense_layers(entities: list[MapEntity],
                          zoom: int) -> dict[str, list[dict[str, Any]]]:
    """Cluster only the layers dense enough to need it.

    Clustering per type rather than globally keeps a handful of territories
    drawn as territories even when thousands of customers behind them collapse
    into groups — mixing them would hide the level the operator filtered on.
    """
    from .data import MapPoint

    by_type: dict[str, list[MapPoint]] = {}
    for entity in entities:
        if not entity.placed:
            continue
        by_type.setdefault(entity.type, []).append(MapPoint(
            code=entity.code, label=entity.name,
            latitude=entity.latitude, longitude=entity.longitude,
            value=entity.value or 0, measures={},
            location_source=entity.location_source or "",
            location_precision="",
        ))
    return {
        entity_type: _cluster(points, zoom)
        for entity_type, points in by_type.items()
        if len(points) > CLUSTER_AT
    }


def _diagnostics(filters: dict[str, str | None], scope: OrgScope,
                 business: dict[str, list[BusinessEntity]],
                 view: EntityView) -> dict[str, Any]:
    """What the filter resolved to. Development aid, never shown to normal users."""
    return {
        "selected_filters": {k: v for k, v in (filters or {}).items() if v},
        "selected_level": scope.selected_level,
        "selected_code": scope.selected_code,
        "resolved_ancestors": {
            level: sorted(scope.of(level))
            for level in ORG_CHAIN
            if scope.selected_level
            and ORG_CHAIN.index(level) < ORG_CHAIN.index(scope.selected_level)
            and scope.of(level)
        },
        "resolved_descendants": {
            level: sorted(scope.of(level))
            for level in ORG_CHAIN
            if scope.selected_level
            and ORG_CHAIN.index(level) > ORG_CHAIN.index(scope.selected_level)
            and scope.of(level)
        },
        "resolved_business_codes": {
            entity_type: sorted(item.code for item in items)[:50]
            for entity_type, items in business.items()
        },
        "entity_counts": dict(view.counts),
        "placed_counts": dict(view.placed_counts),
        "scope_empty": scope.empty,
    }


def view_payload(session: Session, view: EntityView, *,
                 layers: Iterable[str]) -> dict[str, Any]:
    """The API response, including the marker each type is drawn with."""
    drawn = [t for t in layers if t in ALL_TYPES]
    markers = {
        entity_type: resolve_marker(session, entity_type).to_dict()
        for entity_type in drawn
    }
    placed = [(e.latitude, e.longitude) for e in view.entities if e.placed]
    bounds = bounds_of(placed)

    return {
        "entities": [entity.to_dict() for entity in view.entities],
        "markers": markers,
        "layers": drawn,
        "counts": view.counts,
        "placed_counts": view.placed_counts,
        "totals": {
            "entities": len(view.entities),
            "placed": len(placed),
            "unplaced": len(view.unplaced),
        },
        "clusters": view.clusters,
        "unplaced": view.unplaced[:100],
        "bounds": bounds.padded().to_dict() if bounds else None,
        **({"diagnostics": view.diagnostics} if view.diagnostics else {}),
    }


__all__ = [
    "MapEntity",
    "EntityView",
    "build_entity_view",
    "view_payload",
    "DEFAULT_LAYERS",
    "CLUSTER_AT",
]
