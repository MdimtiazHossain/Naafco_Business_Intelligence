"""Every placed coordinate, by level, with no figure attached to any of it.

This is what the Area Demarcation tab draws. It is a different question from
:mod:`app.map.data`'s, and the difference is the whole point: that module
answers "what did this level *do* in this period, and where is it", and this
one answers only "where is it". No fact table is read, no period applies, and
no metric is computed — so nothing here can disagree with a report, because
nothing here states a figure.

**Why this is not a tool.** ``get_map_layer`` exists in ``ai/tools.py`` because
the analysis map aggregates sales and targets, and every path to a figure in
this platform goes through the tool layer so a map and a report cannot come to
different numbers. This read touches ``map_entity_locations`` and the master
dimensions and nothing else. Putting it behind the tool registry would suggest
it is a query surface that could drift from a report; it cannot, because it
computes nothing to drift.

**Why this is not scoped, which is the decision worth arguing about.** The
precedent is ``GET /api/map/entities/{level}/{code}``, which returns a name, an
ancestry and a coordinate to any reader holding the ``map`` section, unscoped,
as the filter options are. Three reasons to follow it here:

1. **A scoped demarcation map cannot do its job.** You judge where a boundary
   should fall by looking at what is on *both* sides of it. A regional manager
   shown only their own region's points has no way to see where their edge
   meets the next one, which is the single question this tab exists to answer.
2. **No figure is disclosed.** Sales, targets, achievement and growth are all
   absent. What travels is a code, a name, a coordinate and how that coordinate
   was arrived at.
3. **That is already readable.** The same reader can fetch any one entity's
   name and coordinate from the entities endpoint, and every customer code and
   name from the filter options.

The honest caveat, recorded rather than glossed: what changes is *bulk*, not
kind. A national customer map is a different artefact from 846 individual
lookups even though every row in it was already fetchable one at a time. Reason
1 is what settles it — a demarcation tool that cannot show the neighbouring
territory is not a demarcation tool — and if that trade is ever judged wrong,
the fix is to scope this module, not to hide the tab.

**A level with no coordinates says so.** An empty layer carries a note naming
what to load, because a blank map and a map of nothing look identical and only
one of them is a data problem. The deployment has no customer coordinates at
all and ``data/dev.db`` has 846, so this is the ordinary case, not an edge one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database.models_map import GeoSource, MapEntityLocation
from . import geo
from .levels import MAP_LEVELS, MapLevel, get_level


@dataclass
class LocationLayer:
    """One level's placed coordinates, and what is missing from them."""

    level: str
    label: str
    #: GeoJSON point features, one per placed entity. Longitude first.
    features: list[dict[str, Any]] = field(default_factory=list)
    #: How many entities the master holds at this level, placed or not.
    total: int = 0
    #: How many of the placed coordinates are centroids rather than positions
    #: somebody stated. Reported separately because the renderer draws them
    #: differently and a reader has to be able to count them.
    derived: int = 0
    bounds: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def placed(self) -> int:
        return len(self.features)

    @property
    def missing(self) -> int:
        return max(0, self.total - self.placed)

    def feature_collection(self) -> dict[str, Any]:
        return {"type": "FeatureCollection", "features": self.features}

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "label": self.label,
            "features": self.feature_collection(),
            "total": self.total,
            "placed": self.placed,
            "derived": self.derived,
            "missing": self.missing,
            "bounds": self.bounds,
            "notes": self.notes,
        }


@dataclass
class DemarcationData:
    """Several levels of coordinates, in the order asked for."""

    layers: list[LocationLayer] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not any(layer.features for layer in self.layers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "empty": self.empty,
            "layers": [layer.to_dict() for layer in self.layers],
        }


def _master_rows(session: Session, level: MapLevel
                 ) -> tuple[dict[str, tuple[str | None, str | None]], int]:
    """``{code: (name, parent code)}`` for a level, and how many rows it has.

    One statement per level rather than one per entity: a customer layer is
    2,091 rows in the master and asking for each one's name individually is the
    shape of query that makes a page take a minute.
    """
    code = getattr(level.model, level.code_field)
    name = getattr(level.model, level.name_field, None)
    parent = (getattr(level.model, level.parent_code_field, None)
              if level.parent_code_field else None)

    columns = [code]
    columns.append(name if name is not None else code)
    if parent is not None:
        columns.append(parent)

    rows: dict[str, tuple[str | None, str | None]] = {}
    for row in session.execute(select(*columns)).all():
        row_code = str(row[0])
        row_name = str(row[1]) if row[1] is not None else None
        row_parent = str(row[2]) if parent is not None and row[2] else None
        rows[row_code] = (row_name, row_parent)
    return rows, len(rows)


def layer_locations(session: Session, level_key: str) -> LocationLayer:
    """Every placed coordinate at one level, as GeoJSON points.

    Raises :class:`ValueError` for a level the map does not draw.
    """
    level = get_level(level_key)
    layer = LocationLayer(level=level.key, label=level.label)

    master, layer.total = _master_rows(session, level)
    placed = geo.locations_for(session, level.key)

    points: list[tuple[float, float]] = []
    for code, location in sorted(placed.items()):
        name, parent_code = master.get(code, (None, None))
        if location.source == GeoSource.DERIVED:
            layer.derived += 1
        layer.features.append({
            "type": "Feature",
            "id": f"{level.key}:{code}",
            # GeoJSON is longitude first, stated once here rather than in the
            # renderer that would otherwise get it the wrong way round.
            "geometry": {"type": "Point",
                         "coordinates": [location.longitude, location.latitude]},
            "properties": {
                "code": code,
                # The coordinate's own label first: it is what whoever placed
                # the point called it, and a master renamed since should not
                # silently relabel somebody's pin.
                "name": location.label or name or code,
                "level": level.key,
                "parent_level": level.parent,
                "parent_code": parent_code,
                "source": location.source,
                "precision": location.precision,
                "derived_from": location.derived_from,
            },
        })
        points.append((location.latitude, location.longitude))

    bounds = geo.bounds_of(points)
    layer.bounds = bounds.to_dict() if bounds else None

    if layer.total == 0:
        layer.notes.append(
            f"No {level.label.lower()} records exist yet, so there is nothing "
            f"to place."
        )
    elif layer.placed == 0:
        layer.notes.append(
            f"None of the {layer.total} {level.label.lower()} records has a "
            f"coordinate. Load them through the Upload Centre's Map Locations "
            f"file, or place them one at a time in Data Management."
        )
    elif layer.missing:
        layer.notes.append(
            f"{layer.missing} of {layer.total} {level.label.lower()} "
            f"{'record has' if layer.missing == 1 else 'records have'} no "
            f"coordinate and cannot be drawn."
        )
    return layer


def demarcation_data(session: Session,
                     level_keys: Sequence[str]) -> DemarcationData:
    """Several levels at once, in the order asked for.

    **One call for every level**, which is the opposite of what the analysis
    map does — and deliberately, because the cost profile is the opposite.
    There, each layer is three aggregates over ``fact_sales`` and
    ``fact_target``, so one request per layer lets the first paint while the
    rest are still running. Here every layer is an index scan over a table with
    a four-figure row count, and splitting it would buy a round trip per level
    to save nothing.
    """
    data = DemarcationData()
    for level_key in level_keys:
        data.layers.append(layer_locations(session, level_key))
    return data


def drawable_levels(session: Session) -> list[str]:
    """Every level that has at least one coordinate, in hierarchy order.

    Derived from :data:`app.map.levels.MAP_LEVELS` and the coordinates that
    exist, never from a written-down list: a level added to the organisational
    chain becomes drawable here with no second place to keep in step.
    """
    placed = {
        row[0] for row in session.execute(
            select(MapEntityLocation.entity_type).distinct()
        ).all()
    }
    return [level.key for level in MAP_LEVELS if level.key in placed]


__all__ = [
    "DemarcationData",
    "LocationLayer",
    "demarcation_data",
    "drawable_levels",
    "layer_locations",
]
