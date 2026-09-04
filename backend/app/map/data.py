"""One level's entities, their measures and their positions, as GeoJSON.

:func:`layer_data` answers the whole question for one layer: for a level, a
period and the caller's filters, every entity that has sales or a target in
the period, with its measures, its parent and its coordinate. The same
function serves all eleven levels — there is no per-level branch anywhere in
this module, which is what the specification means by a generic renderer.

Three things it deliberately does *not* reimplement:

* **aggregation** — it calls :func:`app.ai.tools.get_map_layer` through
  :func:`execute_tool`, the same registry the dashboard and the assistant
  read, so a region's sales on the map equals a region's sales on the Sales
  page by construction;
* **authorisation** — that tool applies :class:`PermissionFilter` exactly as
  every other tool does, so a regional manager's map is their region and a
  user with no scope is refused rather than shown nothing;
* **coordinates** — :mod:`app.map.geo` says where an entity is and how that
  was known.

**An entity with data and no coordinate is reported, never hidden.** It is
the map's one honest failure mode — "this territory sold 12 lakh and cannot
be drawn" — and ``unplaced`` carries it so the settings screen can say what
to load. The reverse case, a placed entity with nothing in the period, is not
drawn at all: the filters and the period define what is on the map, and a
grey dot for every customer outside the selected region would contradict
them.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy import select

from ..ai.exceptions import ToolExecutionError
from ..ai.schemas import GroupBy, ScopeFilters
from ..ai.tools import ToolContext, execute_tool
from ..etl.mapping import LEVEL_DEPTH
from . import geo
from .levels import MAP_LEVELS, MapLevel, get_level
from .metrics import METRICS, get_metric
from .styles import class_breaks

#: The ``(unassigned)`` group the query layer labels a NULL code with.
UNASSIGNED_CODE = "(unassigned)"


def _group_by_for(level: MapLevel) -> GroupBy:
    """The dimension the query layer groups by for one map level.

    Every level key is the ``GroupBy`` value of the same name except the
    business unit, whose level is spelled ``bu`` after its code column.
    """
    if level.key == "bu":
        return GroupBy.BUSINESS_UNIT
    return GroupBy(level.key)


#: Map level -> the ``GroupBy`` the query layer aggregates with. Derived from
#: the level registry, never restated, and pinned complete by ``test_map_data``.
GROUP_BY_LEVEL: dict[str, GroupBy] = {
    level.key: _group_by_for(level) for level in MAP_LEVELS
}


@dataclass
class LayerData:
    """Everything needed to draw one level."""

    level: str
    label: str
    group_by: str
    #: GeoJSON point features, one per placed entity with data.
    features: list[dict[str, Any]] = field(default_factory=list)
    #: Entities with data in the period and no coordinate: ``{code, label}``.
    unplaced: list[dict[str, str]] = field(default_factory=list)
    #: The figures of rows that carry no code at this level, if any — sales
    #: captured at area level have no territory. They belong to the total and
    #: cannot be drawn, so they are handed back rather than dropped.
    unassigned: dict[str, Any] | None = None
    #: Warnings the tool raised (an unsupported filter, a scope narrower than
    #: the level) — never hidden from the reader.
    notes: list[str] = field(default_factory=list)
    bounds: dict[str, Any] | None = None
    #: Every entity with data at this level, placed or not, with its measures
    #: — the ranking reads these, because an entity that cannot be drawn still
    #: performed. The ``(unassigned)`` group is not an entity and is not here.
    rows: list[dict[str, Any]] = field(default_factory=list)
    #: ``{metric key: {"min", "max"}}`` over the placed features, for the size
    #: scale and its legend. Absent where no placed feature states the metric.
    extents: dict[str, dict[str, float]] = field(default_factory=dict)
    #: ``{metric key: [ascending thresholds]}`` — quantile class breaks over
    #: the placed features, for a sequential colour scale and its legend.
    breaks: dict[str, list[float]] = field(default_factory=dict)

    @property
    def entity_count(self) -> int:
        return len(self.features) + len(self.unplaced)

    def feature_collection(self) -> dict[str, Any]:
        return {"type": "FeatureCollection", "features": self.features}

    def ranking(self, metric_key: str, limit: int = 5) -> dict[str, Any]:
        """The best and the worst entities by one metric.

        Ordered highest first for every metric, because every metric here is
        "larger is better" — shortfall is negative when short and growth is
        signed. An entity whose figure is absent is not ranked at all: a
        territory with no target has no achievement, and listing it last would
        read as the worst achievement in the region. ``bottom`` is the worst of
        what ``top`` did not already show, so the two lists never overlap and
        a level with five entities has a top five and no bottom.
        """
        metric = get_metric(metric_key)
        ranked = sorted(
            (row for row in self.rows if row.get(metric.field) is not None),
            key=lambda row: row[metric.field], reverse=True,
        )
        top = ranked[:limit]
        bottom = ranked[limit:][-limit:][::-1]
        return {
            "metric": metric.key,
            "field": metric.field,
            "top": top,
            "bottom": bottom,
            "ranked_count": len(ranked),
            "unranked_count": len(self.rows) - len(ranked),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "label": self.label,
            "group_by": self.group_by,
            "entity_count": self.entity_count,
            "placed_count": len(self.features),
            "features": self.feature_collection(),
            "unplaced": self.unplaced,
            "unassigned": self.unassigned,
            "notes": self.notes,
            "bounds": self.bounds,
            "extents": self.extents,
            "breaks": self.breaks,
        }


@dataclass
class MapData:
    """Several layers over one period and one set of filters."""

    date_from: dt.date
    date_to: dt.date
    compare_from: dt.date | None
    compare_to: dt.date | None
    layers: list[LayerData] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not any(layer.features for layer in self.layers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date_from": self.date_from.isoformat(),
            "date_to": self.date_to.isoformat(),
            "compare_from": self.compare_from.isoformat() if self.compare_from else None,
            "compare_to": self.compare_to.isoformat() if self.compare_to else None,
            "empty": self.empty,
            "layers": [layer.to_dict() for layer in self.layers],
        }


def _parent_codes(ctx: ToolContext, level: MapLevel) -> dict[str, str | None]:
    """``{code: parent code}`` from the level's own master row."""
    if level.parent_code_field is None:
        return {}
    code = getattr(level.model, level.code_field)
    parent = getattr(level.model, level.parent_code_field)
    return {
        str(row_code): (str(row_parent) if row_parent else None)
        for row_code, row_parent in ctx.session.execute(select(code, parent)).all()
    }


def _scope_note(ctx: ToolContext, level: MapLevel) -> str | None:
    """Say so when a level sits above the caller's data scope.

    A regional manager drawing the zone layer sees their zone's point carrying
    only their region's figures — the same number the Performance page shows
    them at zone level, and correct for what they may see, but not the zone's
    total. A figure that is neither labelled as partial nor is the whole is the
    kind of number this platform does not put on a screen unexplained.
    """
    if ctx.user.is_unrestricted or level.depth is None:
        return None
    deepest = ctx.user.scope_levels()
    if not deepest:
        return None
    scope_depth = LEVEL_DEPTH.get(deepest[0])
    if scope_depth is None or level.depth >= scope_depth:
        return None
    return (
        f"{level.label} figures cover only your data scope "
        f"({ctx.user.describe_scope()}), not the whole {level.label.lower()}."
    )


def layer_data(ctx: ToolContext, level_key: str, *, date_from: dt.date,
               date_to: dt.date, filters: ScopeFilters | None = None,
               compare_from: dt.date | None = None,
               compare_to: dt.date | None = None) -> LayerData:
    """Everything needed to draw one level for one period.

    Raises :class:`ValueError` for a level the map does not draw, and lets a
    :class:`PermissionDeniedError` from the tool propagate — a user with no
    scope is refused, not shown an empty map.
    """
    level = get_level(level_key)
    filters = filters or ScopeFilters()
    arguments: dict[str, Any] = {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "filters": filters.model_dump(mode="json"),
        "group_by": GROUP_BY_LEVEL[level.key].value,
    }
    if compare_from is not None and compare_to is not None:
        arguments["compare_from"] = compare_from.isoformat()
        arguments["compare_to"] = compare_to.isoformat()

    invocation = execute_tool(ctx, "get_map_layer", arguments)
    result = invocation.result
    if result is None:
        raise ToolExecutionError(invocation.error_message or "map layer failed")

    layer = LayerData(level=level.key, label=level.label,
                      group_by=GROUP_BY_LEVEL[level.key].value,
                      notes=list(result.notes))
    scope_note = _scope_note(ctx, level)
    if scope_note:
        layer.notes.append(scope_note)
    if not result.rows:
        return layer

    locations = geo.locations_for(ctx.session, level.key)
    parents = _parent_codes(ctx, level)
    fields = [metric.field for metric in METRICS]
    points: list[tuple[float, float]] = []

    for row in result.rows:
        code = str(row["code"])
        if code == UNASSIGNED_CODE:
            layer.unassigned = {name: row.get(name) for name in fields}
            layer.unassigned["label"] = row.get("label")
            continue
        location = locations.get(code)
        entry: dict[str, Any] = {
            "code": code,
            "name": row.get("label") or (location.label if location else None) or code,
            "placed": location is not None,
            "parent_code": parents.get(code),
        }
        for name in fields:
            entry[name] = row.get(name)
        layer.rows.append(entry)
        if location is None:
            layer.unplaced.append({"code": code, "label": row.get("label") or code})
            continue
        properties: dict[str, Any] = {
            "code": code,
            "name": row.get("label") or location.label or code,
            "level": level.key,
            "parent_level": level.parent,
            "parent_code": parents.get(code),
            "location_source": location.source,
            "location_precision": location.precision,
            "derived_from": location.derived_from,
        }
        for name in fields:
            properties[name] = row.get(name)
        layer.features.append({
            "type": "Feature",
            "id": f"{level.key}:{code}",
            # GeoJSON is longitude first. Stated here once rather than in every
            # renderer that would otherwise get it the wrong way round.
            "geometry": {"type": "Point",
                         "coordinates": [location.longitude, location.latitude]},
            "properties": properties,
        })
        points.append((location.latitude, location.longitude))

    bounds = geo.bounds_of(points)
    layer.bounds = bounds.to_dict() if bounds else None
    # Extents and breaks describe what is *drawn*: the legend beside the map
    # explains the points on it, and an unplaced entity has no point.
    for metric in METRICS:
        if not metric.available_at(level.key):
            continue
        values = [
            feature["properties"][metric.field] for feature in layer.features
            if feature["properties"].get(metric.field) is not None
        ]
        if values:
            layer.extents[metric.key] = {"min": min(values), "max": max(values)}
            layer.breaks[metric.key] = class_breaks(values)
    if layer.unplaced:
        layer.notes.append(
            f"{len(layer.unplaced)} {level.label.lower()} "
            f"{'entity has' if len(layer.unplaced) == 1 else 'entities have'} "
            f"data in this period but no coordinate, and cannot be drawn."
        )
    return layer


def map_data(ctx: ToolContext, levels: Sequence[str], *, date_from: dt.date,
             date_to: dt.date, filters: ScopeFilters | None = None,
             compare_from: dt.date | None = None,
             compare_to: dt.date | None = None) -> MapData:
    """Several layers at once, in the order asked for."""
    data = MapData(date_from=date_from, date_to=date_to,
                   compare_from=compare_from, compare_to=compare_to)
    for level_key in levels:
        data.layers.append(layer_data(
            ctx, level_key, date_from=date_from, date_to=date_to, filters=filters,
            compare_from=compare_from, compare_to=compare_to,
        ))
    return data


__all__ = [
    "GROUP_BY_LEVEL",
    "LayerData",
    "MapData",
    "UNASSIGNED_CODE",
    "layer_data",
    "map_data",
]
