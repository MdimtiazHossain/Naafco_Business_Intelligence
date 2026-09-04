"""Coordinates: validating, storing, deriving and bounding them.

The organisational hierarchy has no coordinates of its own — ``Master Data.xlsx``
carries a free-text ``Location`` and an ``HQ`` town, and on every row loaded so
far both are empty. The customers are where a real address exists, and they are
what somebody actually placed: 846 uploaded customer coordinates and 256
uploaded sales-force coordinates were the whole authoritative content of the
table ``0033`` dropped, and every level above them was a centroid.

That is what :func:`derive_parents` does. Place the customers, and each
sub-territory is the centroid of its placed customers, each territory the
centroid of its sub-territories, and so on to the zone. A derived centroid is
marked ``DERIVED`` / ``CENTROID`` with how many children stood behind it, never
overwrites a coordinate a person set, and is recomputed rather than edited —
so hand-placing a region's true head office later simply wins.

Nothing here invents a position. A row without a coordinate stays without one
and is reported as missing by :func:`coverage`; a coordinate that cannot be a
place on Earth is refused by :func:`validate`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from sqlalchemy import func as sa_func, select
from sqlalchemy.orm import Session

from ..database.models_map import GeoPrecision, GeoSource, MapEntityLocation
from ..etl.mapping import LEVEL_BINDINGS
from .levels import LEVEL_BY_KEY, MAP_LEVELS, get_level

logger = logging.getLogger(__name__)

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

    if math.isnan(lat) or math.isnan(lon):
        raise InvalidCoordinate("Latitude and longitude must be numbers.")
    if not -90 <= lat <= 90:
        raise InvalidCoordinate(f"Latitude {lat} is outside -90 to 90.")
    if not -180 <= lon <= 180:
        raise InvalidCoordinate(f"Longitude {lon} is outside -180 to 180.")
    if lat == 0 and lon == 0:
        raise InvalidCoordinate(
            "Latitude and longitude are both zero, which is a point in the "
            "Atlantic Ocean. Leave the columns empty rather than entering 0."
        )
    return (round(lat, 6), round(lon, 6))


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


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

    def to_dict(self) -> dict[str, Any]:
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


def entity_exists(session: Session, entity_type: str, entity_code: str) -> bool:
    """Whether the master data holds a record with this code at this level."""
    level = get_level(entity_type)
    column = getattr(level.model, level.code_field)
    return bool(session.execute(
        select(sa_func.count()).select_from(level.model.__table__)
        .where(column == entity_code)
    ).scalar_one())


def upsert_location(session: Session, *, entity_type: str, entity_code: str,
                    latitude: float, longitude: float,
                    source: str = GeoSource.UPLOAD,
                    precision: str = GeoPrecision.APPROXIMATE,
                    label: str | None = None,
                    actor: str | None = None) -> MapEntityLocation:
    """Create or move one entity's coordinate."""
    get_level(entity_type)
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
    """``{entity_code: location}`` for one level."""
    rows = session.execute(
        select(MapEntityLocation)
        .where(MapEntityLocation.entity_type == entity_type)
    ).scalars().all()
    return {row.entity_code: row for row in rows}


def coverage(session: Session) -> list[dict[str, Any]]:
    """How many entities of each level have a coordinate, and how.

    The map is useless without coordinates, and "nothing is showing" is a very
    unhelpful symptom. This is what the settings screen uses to say *why*, and
    it separates placed from derived because a region drawn at the centroid of
    sixteen customers is a different claim from a region somebody placed.
    """
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
    for level in MAP_LEVELS:
        total = session.execute(
            select(sa_func.count()).select_from(level.model.__table__)
        ).scalar_one()
        result.append({
            "entity_type": level.key,
            "label": level.label,
            "total": total,
            "placed": placed.get(level.key, 0),
            "derived": derived.get(level.key, 0),
            "missing": max(0, total - placed.get(level.key, 0)),
        })
    return result


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def _codes_at(session: Session, level_key: str) -> set[str]:
    """Every code the master data holds at one level."""
    level = LEVEL_BY_KEY[level_key]
    column = getattr(level.model, level.code_field)
    return {value for (value,) in session.execute(select(column)).all() if value}


def _write_centroids(session: Session, level_key: str,
                     grouped: Mapping[str, list[tuple[float, float]]],
                     actor: str | None) -> int:
    """Store one level's centroids, leaving anything a person placed alone.

    A parent code the master does not hold at this level gets no row. The
    deployment's sales-force master names sub-territory codes in its
    ``territory_code`` column, and grouping on that column produced 111
    "territories" that were nothing of the kind: a centroid written for a code
    that is not an entity is a position for something that does not exist,
    which is the invention this module refuses. Such codes are logged and
    skipped, and the rows they point at are placed when the master is
    corrected.
    """
    known = _codes_at(session, level_key)
    unknown = sorted(code for code in grouped if code not in known)
    if unknown:
        logger.warning(
            "map derivation: %d %s code(s) named as a parent do not exist in "
            "the %s master and were not placed (first: %s)",
            len(unknown), level_key, level_key, ", ".join(unknown[:5]),
        )
    existing = locations_for(session, level_key)
    count = 0
    for code, points in grouped.items():
        if code not in known:
            continue
        current = existing.get(code)
        if current is not None and current.source in GeoSource.AUTHORITATIVE:
            continue                          # a person placed this; leave it
        latitude, longitude = centroid(points)
        if current is None:
            current = MapEntityLocation(
                entity_type=level_key, entity_code=code,
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
    session.flush()
    return count


def _derive_from_business(session: Session, level_key: str,
                          actor: str | None) -> int:
    """Place a business level's parent at the centroid of its placed members.

    Customer and sales force are not rungs of ``LEVEL_BINDINGS`` — they are
    business levels attached to the chain through their own dimension's parent
    column — so the generic pass below cannot reach them, and without this the
    whole cascade starts from nothing.

    **The result is an approximation and is recorded as one.** A sub-territory
    holding forty customers of which sixteen are placed is centred on those
    sixteen: a real centroid of real points, but not the sub-territory's
    location. ``derived_from`` carries how many stood behind it, which is what
    lets the map say "approximate" and a reader judge how much to trust it.
    """
    level = LEVEL_BY_KEY[level_key]
    placed = locations_for(session, level_key)
    if not placed or level.parent is None or level.parent_code_field is None:
        return 0

    code_column = getattr(level.model, level.code_field)
    parent_column = getattr(level.model, level.parent_code_field)
    pairs = session.execute(
        select(code_column, parent_column)
        .where(parent_column.isnot(None), parent_column != "")
    ).all()

    grouped: dict[str, list[tuple[float, float]]] = {}
    for code, parent_code in pairs:
        location = placed.get(code)
        if location is None:
            continue
        grouped.setdefault(parent_code, []).append(
            (location.latitude, location.longitude)
        )
    return _write_centroids(session, level.parent, grouped, actor)


def _prune_orphaned_centroids(session: Session) -> int:
    """Remove derived rows whose entity the master data no longer holds.

    A derived centroid is recomputed, never edited, so one that points at a
    code with no master record is not history — it is a computation whose
    input has gone. Only ``DERIVED`` rows are touched: a coordinate a person
    placed is theirs to remove.
    """
    removed = 0
    for level in MAP_LEVELS:
        rows = session.execute(
            select(MapEntityLocation).where(
                MapEntityLocation.entity_type == level.key,
                MapEntityLocation.source == GeoSource.DERIVED,
            )
        ).scalars().all()
        if not rows:
            continue
        known = _codes_at(session, level.key)
        for row in rows:
            if row.entity_code not in known:
                session.delete(row)
                removed += 1
    if removed:
        session.flush()
        logger.info("map derivation: removed %d orphaned centroid(s)", removed)
    return removed


def derive_parents(session: Session, *, actor: str | None = None) -> dict[str, int]:
    """Give every organisational level above the placed ones a centroid.

    Seeds the chain from the business levels, then walks the hierarchy from the
    deepest organisational level upward so that each pass can consume what the
    previous one produced. Placing customers is therefore enough to draw
    sub-territories, territories, units, areas, regions, zones and everything
    above.

    A coordinate somebody set by hand is never overwritten — only ``DERIVED``
    rows are recomputed — so this is safe to re-run whenever locations change.
    Returns how many rows were derived at each level.
    """
    created: dict[str, int] = {}
    _prune_orphaned_centroids(session)

    # Sub-territory from customers and territory from the sales force. The two
    # seeds can both write a territory: a territory's sales-force centroid is
    # then replaced by the centroid of its sub-territories in the pass below,
    # which is the deliberate order — a sub-territory is the finer placement.
    for business in ("customer", "sales_force"):
        seeded = _derive_from_business(session, business, actor)
        if seeded:
            parent = LEVEL_BY_KEY[business].parent or business
            created[parent] = created.get(parent, 0) + seeded

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

        count = _write_centroids(session, parent_level, grouped, actor)
        if count:
            created[parent_level] = count

    return created


# ---------------------------------------------------------------------------
# Restoring an export
# ---------------------------------------------------------------------------


@dataclass
class RestoreReport:
    """What :func:`restore_locations` did, and what it declined to do."""

    #: Authoritative rows written, by level.
    restored: dict[str, int] = field(default_factory=dict)
    #: Derived rows in the export, skipped by level: they are recomputed.
    skipped_derived: dict[str, int] = field(default_factory=dict)
    #: Rows whose code the master data no longer holds — ``(level, code)``.
    unknown: list[tuple[str, str]] = field(default_factory=list)
    #: Rows that failed :func:`validate` — ``(level, code, reason)``.
    invalid: list[tuple[str, str, str]] = field(default_factory=list)
    #: What :func:`derive_parents` produced afterwards, by level.
    derived: dict[str, int] = field(default_factory=dict)

    @property
    def restored_total(self) -> int:
        return sum(self.restored.values())


def restore_locations(session: Session, records: Iterable[Mapping[str, Any]],
                      *, actor: str | None = None,
                      derive: bool = True) -> RestoreReport:
    """Put an exported ``map_entity_locations`` file back, uploaded rows only.

    A row is restored with the source, precision and label it was exported
    with, through the same :func:`validate` an upload passes — so a coordinate
    that would be refused today is refused here too rather than slipping back
    in because it was once accepted. ``DERIVED`` rows are **not** restored:
    they were centroids of the rows being restored, and recomputing them is
    both cheaper and more honest than trusting a snapshot that may predate a
    customer's re-mapping.

    A code the master data no longer holds is reported, not written. A
    coordinate for a customer that does not exist would never draw, and
    writing it would leave the table claiming coverage it does not have.
    """
    report = RestoreReport()
    for record in records:
        level_key = str(record.get("entity_type") or "").strip()
        code = str(record.get("entity_code") or "").strip()
        source = str(record.get("source") or GeoSource.UPLOAD).strip()
        if not level_key or not code:
            continue
        if level_key not in LEVEL_BY_KEY:
            report.unknown.append((level_key, code))
            continue
        if source not in GeoSource.AUTHORITATIVE:
            report.skipped_derived[level_key] = (
                report.skipped_derived.get(level_key, 0) + 1
            )
            continue
        if not entity_exists(session, level_key, code):
            report.unknown.append((level_key, code))
            continue
        try:
            upsert_location(
                session, entity_type=level_key, entity_code=code,
                latitude=record.get("latitude"), longitude=record.get("longitude"),
                source=source,
                precision=str(record.get("precision") or GeoPrecision.APPROXIMATE),
                label=(str(record["label"]).strip() or None)
                if record.get("label") is not None else None,
                actor=actor,
            )
        except InvalidCoordinate as exc:
            report.invalid.append((level_key, code, str(exc)))
            continue
        report.restored[level_key] = report.restored.get(level_key, 0) + 1

    if derive and report.restored_total:
        report.derived = derive_parents(session, actor=actor)
    return report


__all__ = [
    "Bounds",
    "InvalidCoordinate",
    "MAX_LATITUDE",
    "RestoreReport",
    "bounds_of",
    "centroid",
    "coverage",
    "derive_parents",
    "entity_exists",
    "location_payload",
    "locations_for",
    "restore_locations",
    "upsert_location",
    "validate",
]
