"""Coordinates: storing them, deriving them, and bounding them.

The organisational hierarchy has no coordinates of its own — ``Master Data.xlsx``
carries a free-text ``Location`` and an ``HQ`` town, not a position. So the map
needs somewhere to put them and a way to acquire them without demanding that
someone place all nine levels by hand.

That is what :func:`derive_parents` does. Place the *territories* — the level
where a real address exists — and every level above is the centroid of its
children. A derived centroid is marked ``DERIVED`` and never overwrites a
coordinate a person set, so hand-placing a region's true head office later simply
wins.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database.models_map import GeoPrecision, GeoSource, MapEntityLocation
from ..etl.mapping import BINDING_BY_LEVEL, LEVEL_BINDINGS
from .entities import ENTITY_TYPE_BY_KEY, get_entity_type

#: Web Mercator clamps near the poles; nothing business-related sits there.
MAX_LATITUDE = 85.05112878


class InvalidCoordinate(ValueError):
    """A latitude/longitude that cannot be a place on Earth."""


def validate(latitude: float, longitude: float) -> tuple[float, float]:
    """Bound-check a coordinate. Raises :class:`InvalidCoordinate`.

    Rejecting (0, 0) is deliberate: it is in the Atlantic, and it is what a
    spreadsheet produces when the columns were left empty and coerced to zero.
    Silently plotting a whole region there is worse than refusing the row.
    """
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError) as exc:
        raise InvalidCoordinate("Latitude and longitude must be numbers.") from exc

    if not -90 <= lat <= 90:
        raise InvalidCoordinate(
            f"Latitude {lat} is outside -90 to 90."
        )
    if not -180 <= lon <= 180:
        raise InvalidCoordinate(
            f"Longitude {lon} is outside -180 to 180."
        )
    if lat == 0 and lon == 0:
        raise InvalidCoordinate(
            "Latitude and longitude are both zero, which is a point in the "
            "Atlantic Ocean. Leave the columns empty rather than entering 0."
        )
    return (round(lat, 6), round(lon, 6))


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def to_mercator(latitude: float, longitude: float) -> tuple[float, float]:
    """WGS-84 -> normalised Web Mercator, both axes in ``0..1``.

    Web Mercator is the projection MapLibre draws in, so clustering computed
    here groups the points a viewer sees as adjacent. Clustering in degrees
    instead would produce cells that are visibly taller than they are wide the
    further north the data sits.
    """
    lat = max(-MAX_LATITUDE, min(MAX_LATITUDE, latitude))
    x = (longitude + 180.0) / 360.0
    sin_lat = math.sin(math.radians(lat))
    y = 0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)
    # At the clamp latitude the logarithm lands a few parts in 10^12 outside the
    # unit square. That is harmless for drawing but would give a cluster cell
    # index of -1, isolating a point in a cell of its own.
    return (min(1.0, max(0.0, x)), min(1.0, max(0.0, y)))


@dataclass
class Bounds:
    """A rectangle enclosing a set of points."""

    north: float
    south: float
    east: float
    west: float

    @property
    def centre(self) -> tuple[float, float]:
        return ((self.north + self.south) / 2, (self.east + self.west) / 2)

    def padded(self, fraction: float = 0.12) -> "Bounds":
        """Grow the box so markers near an edge are not clipped."""
        lat_pad = max((self.north - self.south) * fraction, 0.01)
        lon_pad = max((self.east - self.west) * fraction, 0.01)
        return Bounds(
            north=min(90.0, self.north + lat_pad),
            south=max(-90.0, self.south - lat_pad),
            east=min(180.0, self.east + lon_pad),
            west=max(-180.0, self.west - lon_pad),
        )

    def to_dict(self) -> dict[str, float]:
        latitude, longitude = self.centre
        return {
            "north": self.north, "south": self.south,
            "east": self.east, "west": self.west,
            "centre": {"latitude": latitude, "longitude": longitude},
        }


def bounds_of(points: Iterable[tuple[float, float]]) -> Bounds | None:
    """The enclosing rectangle, or ``None`` when there is nothing to enclose."""
    latitudes: list[float] = []
    longitudes: list[float] = []
    for latitude, longitude in points:
        latitudes.append(latitude)
        longitudes.append(longitude)
    if not latitudes:
        return None
    return Bounds(north=max(latitudes), south=min(latitudes),
                  east=max(longitudes), west=min(longitudes))


def centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    """The average position of a set of points.

    Averaged in 3-D and projected back, not averaged as plain degrees: a plain
    average is wrong across the antimeridian, and being right there costs four
    lines. Latitudes average correctly either way.
    """
    if not points:
        raise InvalidCoordinate("Cannot take the centroid of no points.")
    x = y = z = 0.0
    for latitude, longitude in points:
        lat_r = math.radians(latitude)
        lon_r = math.radians(longitude)
        x += math.cos(lat_r) * math.cos(lon_r)
        y += math.cos(lat_r) * math.sin(lon_r)
        z += math.sin(lat_r)
    count = len(points)
    x, y, z = x / count, y / count, z / count
    longitude = math.atan2(y, x)
    hypotenuse = math.sqrt(x * x + y * y)
    latitude = math.atan2(z, hypotenuse)
    return (round(math.degrees(latitude), 6), round(math.degrees(longitude), 6))


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def location_payload(row: MapEntityLocation) -> dict[str, Any]:
    return {
        "location_id": row.location_id,
        "entity_type": row.entity_type,
        "entity_code": row.entity_code,
        "latitude": row.latitude,
        "longitude": row.longitude,
        "source": row.source,
        "precision": row.precision,
        "label": row.label,
        "derived_from": row.derived_from,
        "updated_by": row.updated_by,
        "updated_at": row.updated_at,
    }


def upsert_location(session: Session, *, entity_type: str, entity_code: str,
                    latitude: float, longitude: float,
                    source: str = GeoSource.UPLOAD,
                    precision: str = GeoPrecision.APPROXIMATE,
                    label: str | None = None,
                    actor: str | None = None) -> MapEntityLocation:
    """Create or move one entity's coordinate."""
    get_entity_type(entity_type)
    lat, lon = validate(latitude, longitude)

    row = session.execute(
        select(MapEntityLocation).where(
            MapEntityLocation.entity_type == entity_type,
            MapEntityLocation.entity_code == entity_code,
        )
    ).scalars().first()

    if row is None:
        row = MapEntityLocation(
            entity_type=entity_type, entity_code=entity_code,
            latitude=lat, longitude=lon, source=source, precision=precision,
            label=label, updated_by=actor,
        )
        session.add(row)
    else:
        row.latitude, row.longitude = lat, lon
        row.source, row.precision = source, precision
        row.updated_by = actor
        row.derived_from = None
        if label:
            row.label = label
    session.flush()
    return row


def locations_for(session: Session, entity_type: str
                  ) -> dict[str, MapEntityLocation]:
    """``{entity_code: location}`` for one entity type."""
    rows = session.execute(
        select(MapEntityLocation)
        .where(MapEntityLocation.entity_type == entity_type)
    ).scalars().all()
    return {row.entity_code: row for row in rows}


def coverage(session: Session) -> list[dict[str, Any]]:
    """How many entities of each type have a coordinate.

    The map is useless without coordinates, and "nothing is showing" is a very
    unhelpful symptom. This is what the UI uses to say *why*.
    """
    from sqlalchemy import func as sa_func

    placed = dict(session.execute(
        select(MapEntityLocation.entity_type, sa_func.count())
        .group_by(MapEntityLocation.entity_type)
    ).all())
    derived = dict(session.execute(
        select(MapEntityLocation.entity_type, sa_func.count())
        .where(MapEntityLocation.source == GeoSource.DERIVED)
        .group_by(MapEntityLocation.entity_type)
    ).all())

    result = []
    for entity in ENTITY_TYPE_BY_KEY.values():
        total = _entity_total(session, entity.key)
        result.append({
            "entity_type": entity.key,
            "label": entity.label,
            "total": total,
            "placed": placed.get(entity.key, 0),
            "derived": derived.get(entity.key, 0),
            "missing": max(0, total - placed.get(entity.key, 0)),
        })
    return result


def _entity_total(session: Session, entity_type: str) -> int:
    """How many entities of a type exist in the master data."""
    from sqlalchemy import func as sa_func

    binding = BINDING_BY_LEVEL.get(f"{entity_type}_code")
    model = None
    if binding is not None:
        model = binding.model
    else:
        from ..upload.registry import MASTER_MODEL_BY_TABLE

        entity = ENTITY_TYPE_BY_KEY.get(entity_type)
        model = MASTER_MODEL_BY_TABLE.get(entity.table) if entity else None
    if model is None:
        return 0
    return session.execute(
        select(sa_func.count()).select_from(model.__table__)
    ).scalar_one()


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def derive_parents(session: Session, *, actor: str | None = None) -> dict[str, int]:
    """Give every organisational level above the placed ones a centroid.

    Walks the hierarchy from the deepest level upward. A level's coordinate is
    the centroid of the child coordinates beneath it, so placing territories is
    enough to draw units, areas, regions, zones and everything above.

    A coordinate somebody set by hand is never overwritten — only ``DERIVED``
    rows are recomputed — so this is safe to re-run whenever locations change.
    """
    created: dict[str, int] = {}

    # Deepest first: each pass can consume what the previous one produced.
    for binding in reversed(LEVEL_BINDINGS):
        parent_field = binding.parent_code_field
        if parent_field is None:
            continue
        child_level = binding.code_field.removesuffix("_code")
        parent_level = parent_field.removesuffix("_code")

        child_locations = locations_for(session, child_level)
        if not child_locations:
            continue

        # child code -> parent code, straight from the master dimension.
        pairs = session.execute(
            select(getattr(binding.model, binding.code_field),
                   getattr(binding.model, parent_field))
        ).all()

        grouped: dict[str, list[tuple[float, float]]] = {}
        for child_code, parent_code in pairs:
            location = child_locations.get(child_code)
            if location is None or parent_code is None:
                continue
            grouped.setdefault(parent_code, []).append(
                (location.latitude, location.longitude)
            )

        existing = locations_for(session, parent_level)
        count = 0
        for parent_code, points in grouped.items():
            current = existing.get(parent_code)
            if current is not None and current.source in GeoSource.AUTHORITATIVE:
                continue                      # a person placed this; leave it
            latitude, longitude = centroid(points)
            if current is None:
                current = MapEntityLocation(
                    entity_type=parent_level, entity_code=parent_code,
                    latitude=latitude, longitude=longitude,
                )
                session.add(current)
            else:
                current.latitude, current.longitude = latitude, longitude
            current.source = GeoSource.DERIVED
            current.precision = GeoPrecision.CENTROID
            current.derived_from = len(points)
            current.updated_by = actor
            count += 1
        if count:
            created[parent_level] = count
        session.flush()

    return created


__all__ = [
    "validate",
    "InvalidCoordinate",
    "to_mercator",
    "Bounds",
    "bounds_of",
    "centroid",
    "upsert_location",
    "locations_for",
    "location_payload",
    "coverage",
    "derive_parents",
    "MAX_LATITUDE",
]
