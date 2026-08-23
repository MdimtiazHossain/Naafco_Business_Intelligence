"""GeoJSON polygon maths: bounds, centroids, containment, simplification.

Pure functions over GeoJSON geometry objects, with no database and no
dependency on a geospatial library. That last point is deliberate: adding GEOS
or Shapely to deploy one map layer would be a large operational cost for four
algorithms that are each a dozen lines, and it would put a compiled dependency
between this project and every environment it runs in.

Everything here works on the two geometry types administrative boundaries
actually come in — ``Polygon`` and ``MultiPolygon`` — and treats a polygon as
GeoJSON defines it: a list of linear rings, the first being the outer ring and
any others being holes.

Coordinates are ``[longitude, latitude]``, which is GeoJSON's order and the
reverse of how humans say it. Every function here keeps that order; converting
to the ``(lat, lon)`` the rest of the map uses happens at the boundary of this
module, once, rather than being re-decided at each call site.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterator

POLYGON = "Polygon"
MULTI_POLYGON = "MultiPolygon"
SUPPORTED_TYPES = (POLYGON, MULTI_POLYGON)

#: Line types, which :func:`simplify` and :func:`round_coordinates` also accept.
#:
#: The database stores polygons only — :func:`validate` still refuses anything
#: else — but the published boundary release ships an administrative *line*
#: layer alongside the polygons, and the browser bundle is built from the same
#: two reducers. Handling lines here is what stops a second Douglas–Peucker
#: existing in the build script.
LINE_STRING = "LineString"
MULTI_LINE_STRING = "MultiLineString"


class InvalidGeometry(ValueError):
    """A geometry this module cannot work with, with a reason a person can act on."""


@dataclass(frozen=True)
class BoundingBox:
    """The rectangle enclosing a geometry, in degrees."""

    west: float
    south: float
    east: float
    north: float

    def intersects(self, other: "BoundingBox") -> bool:
        """Do the two boxes overlap?

        The whole point of storing a box: this comparison decides whether an
        area is on screen, and it costs four float comparisons instead of
        walking several hundred vertices.
        """
        return not (
            other.west > self.east
            or other.east < self.west
            or other.south > self.north
            or other.north < self.south
        )

    def to_dict(self) -> dict[str, float]:
        return {"north": self.north, "south": self.south,
                "east": self.east, "west": self.west}


def rings(geometry: dict[str, Any]) -> Iterator[list[list[float]]]:
    """Every linear ring in the geometry, outer and holes alike."""
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if kind == POLYGON:
        for ring in coordinates or []:
            yield ring
    elif kind == MULTI_POLYGON:
        for polygon in coordinates or []:
            for ring in polygon or []:
                yield ring
    else:
        raise InvalidGeometry(
            f"Unsupported geometry type {kind!r}. An administrative boundary "
            f"must be a {POLYGON} or a {MULTI_POLYGON}."
        )


def polygons(geometry: dict[str, Any]) -> Iterator[list[list[list[float]]]]:
    """Each polygon as its list of rings — outer ring first, then holes."""
    kind = geometry.get("type")
    if kind == POLYGON:
        yield geometry.get("coordinates") or []
    elif kind == MULTI_POLYGON:
        for polygon in geometry.get("coordinates") or []:
            yield polygon
    else:
        raise InvalidGeometry(f"Unsupported geometry type {kind!r}.")


def validate(geometry: Any) -> dict[str, Any]:
    """Check a geometry is one this module can draw, and return it.

    Checks structure rather than topology: a self-intersecting ring still
    renders, so refusing it would reject usable published boundaries for a
    purity this map does not need. What is refused is anything that cannot be
    drawn at all — a wrong type, or a ring with fewer than three points.
    """
    if not isinstance(geometry, dict):
        raise InvalidGeometry("Geometry must be a GeoJSON object.")
    kind = geometry.get("type")
    if kind not in SUPPORTED_TYPES:
        raise InvalidGeometry(
            f"Geometry type {kind!r} cannot be drawn as an area. Supported: "
            f"{', '.join(SUPPORTED_TYPES)}."
        )
    found = False
    for ring in rings(geometry):
        found = True
        if not isinstance(ring, list) or len(ring) < 3:
            raise InvalidGeometry(
                "A boundary ring needs at least three points; one has "
                f"{len(ring) if isinstance(ring, list) else 0}."
            )
        for point in ring:
            if (not isinstance(point, (list, tuple)) or len(point) < 2):
                raise InvalidGeometry(
                    "Each boundary point must be a [longitude, latitude] pair."
                )
            lon, lat = float(point[0]), float(point[1])
            if not -180 <= lon <= 180 or not -90 <= lat <= 90:
                raise InvalidGeometry(
                    f"Point ({lon}, {lat}) is not on Earth. GeoJSON orders "
                    "coordinates as [longitude, latitude] — a swapped pair is "
                    "the usual cause."
                )
    if not found:
        raise InvalidGeometry("The geometry has no rings.")
    return geometry


def bounds(geometry: dict[str, Any]) -> BoundingBox:
    west = south = math.inf
    east = north = -math.inf
    for ring in rings(geometry):
        for lon, lat, *_ in ring:
            west = min(west, lon)
            east = max(east, lon)
            south = min(south, lat)
            north = max(north, lat)
    if west is math.inf:
        raise InvalidGeometry("The geometry has no points to bound.")
    return BoundingBox(west=west, south=south, east=east, north=north)


def count_points(geometry: dict[str, Any]) -> int:
    return sum(len(ring) for ring in rings(geometry))


def _ring_area_and_centroid(ring: list[list[float]]) -> tuple[float, float, float]:
    """Signed area and centroid of one ring, by the shoelace formula.

    Signed, because the sign is what lets a hole subtract itself from the
    polygon it sits in rather than being added to it.
    """
    area2 = 0.0
    cx = cy = 0.0
    for index in range(len(ring)):
        x0, y0 = ring[index][0], ring[index][1]
        x1, y1 = ring[(index + 1) % len(ring)][0], ring[(index + 1) % len(ring)][1]
        cross = x0 * y1 - x1 * y0
        area2 += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if area2 == 0:
        return (0.0, 0.0, 0.0)
    return (area2 / 2.0, cx / (3.0 * area2), cy / (3.0 * area2))


def centroid(geometry: dict[str, Any]) -> tuple[float, float]:
    """Area-weighted centroid, as ``(latitude, longitude)``.

    Area-weighted rather than the mean of the vertices: a coastline with
    thousands of points along one edge and a straight line along the other
    would drag a vertex-mean towards the detailed side, putting the label
    outside the shape. Holes carry negative area and pull the centroid the
    right way.

    Falls back to the bounding-box centre for a degenerate geometry — one whose
    rings enclose no area — because a label somewhere sensible beats none.
    """
    total_area = 0.0
    x_sum = y_sum = 0.0
    for polygon in polygons(geometry):
        for index, ring in enumerate(polygon):
            area, cx, cy = _ring_area_and_centroid(ring)
            # The outer ring's winding decides the sign convention for this
            # polygon; holes come back with the opposite sign and subtract.
            if index > 0:
                area = -abs(area)
            else:
                area = abs(area)
            total_area += area
            x_sum += cx * area
            y_sum += cy * area

    if abs(total_area) < 1e-12:
        box = bounds(geometry)
        return ((box.north + box.south) / 2, (box.east + box.west) / 2)
    return (y_sum / total_area, x_sum / total_area)


def contains(geometry: dict[str, Any], latitude: float, longitude: float) -> bool:
    """Is the point inside the geometry?

    Ray casting: count how many edges a ray eastward from the point crosses.
    Odd means inside. Holes fall out of the algorithm for free — a point in a
    hole crosses the outer ring once and the hole ring once, an even count.

    This is what relates the two hierarchies. A territory is "in" an upazila
    because its coordinate falls inside that upazila's boundary — a real
    geographic fact, computed from data that already exists, rather than a
    mapping table somebody would have to maintain.
    """
    inside = False
    for polygon in polygons(geometry):
        for ring in polygon:
            if _ring_contains(ring, latitude, longitude):
                inside = not inside
    return inside


def _ring_contains(ring: list[list[float]], latitude: float,
                   longitude: float) -> bool:
    inside = False
    count = len(ring)
    for index in range(count):
        x0, y0 = ring[index][0], ring[index][1]
        x1, y1 = ring[(index + 1) % count][0], ring[(index + 1) % count][1]
        # Does the edge straddle the ray's latitude? The asymmetric comparison
        # is what stops a vertex exactly on the ray being counted twice.
        if (y0 > latitude) != (y1 > latitude):
            crossing = x0 + (latitude - y0) / (y1 - y0) * (x1 - x0)
            if longitude < crossing:
                inside = not inside
    return inside


# ---------------------------------------------------------------------------
# Simplification
# ---------------------------------------------------------------------------


def simplify(geometry: dict[str, Any], tolerance: float) -> dict[str, Any]:
    """Douglas–Peucker, applied ring by ring.

    Published administrative boundaries are surveyed to a precision no screen
    can show: an upazila outline can carry several thousand vertices, of which
    a few hundred are visually indistinguishable at country zoom. Dropping the
    rest is the difference between a map that renders and one that stutters.

    A ring is never reduced below four points, and the closing point is always
    restored, so a simplified ring is still a ring. A tolerance of zero or less
    returns the geometry untouched.
    """
    if tolerance <= 0:
        return geometry

    def reduce_ring(ring: list[list[float]]) -> list[list[float]]:
        closed = len(ring) > 1 and ring[0][:2] == ring[-1][:2]
        working = ring[:-1] if closed else ring[:]
        if len(working) <= 4:
            return ring
        kept = _douglas_peucker(working, tolerance)
        if len(kept) < 4:
            kept = working[:: max(1, len(working) // 4)][:4]
        return [*kept, kept[0]] if closed else kept

    def reduce_line(line: list[list[float]]) -> list[list[float]]:
        # A line has no closing point to restore and no minimum length to hold
        # to — two points is a valid line — so it goes straight to the reducer.
        return _douglas_peucker(line, tolerance) if len(line) > 2 else line

    kind = geometry["type"]
    if kind == POLYGON:
        coordinates = [reduce_ring(ring) for ring in geometry["coordinates"]]
    elif kind == LINE_STRING:
        coordinates = reduce_line(geometry["coordinates"])
    elif kind == MULTI_LINE_STRING:
        coordinates = [reduce_line(line) for line in geometry["coordinates"]]
    else:
        coordinates = [
            [reduce_ring(ring) for ring in polygon]
            for polygon in geometry["coordinates"]
        ]
    return {"type": kind, "coordinates": coordinates}


def _douglas_peucker(points: list[list[float]], tolerance: float,
                     ) -> list[list[float]]:
    """Iterative Douglas–Peucker.

    Iterative rather than recursive because a ring of several thousand points
    in a near-straight line recurses to that depth, and Python's default limit
    is a thousand — a crash on exactly the detailed coastlines this exists for.
    """
    if len(points) < 3:
        return points

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]

    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        furthest = -1.0
        index = start
        for i in range(start + 1, end):
            distance = _perpendicular_distance(points[i], points[start], points[end])
            if distance > furthest:
                furthest = distance
                index = i
        if furthest > tolerance:
            keep[index] = True
            stack.append((start, index))
            stack.append((index, end))

    return [point for point, kept in zip(points, keep) if kept]


def _perpendicular_distance(point: list[float], start: list[float],
                            end: list[float]) -> float:
    """Distance from ``point`` to the segment ``start``–``end``, in degrees.

    Degrees, not metres: the tolerance is a screen-resolution judgement and the
    error from treating degrees as flat is irrelevant at the scale of one
    upazila. Converting to metres would cost a trigonometric call per vertex on
    a hot loop, to move a point by less than a pixel.
    """
    x0, y0 = point[0], point[1]
    x1, y1 = start[0], start[1]
    x2, y2 = end[0], end[1]

    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x0 - x1, y0 - y1)

    t = ((x0 - x1) * dx + (y0 - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(x0 - (x1 + t * dx), y0 - (y1 + t * dy))


def round_coordinates(geometry: dict[str, Any], places: int = 5,
                      ) -> dict[str, Any]:
    """Round every vertex.

    Five decimal places is about a metre — finer than any screen this renders
    on — and it typically halves the JSON payload, because the published files
    carry twelve or more.
    """
    def reduce_ring(ring: list[list[float]]) -> list[list[float]]:
        return [[round(p[0], places), round(p[1], places)] for p in ring]

    kind = geometry["type"]
    if kind == POLYGON:
        coordinates = [reduce_ring(r) for r in geometry["coordinates"]]
    elif kind == LINE_STRING:
        coordinates = reduce_ring(geometry["coordinates"])
    elif kind == MULTI_LINE_STRING:
        coordinates = [reduce_ring(line) for line in geometry["coordinates"]]
    elif kind == "Point":
        point = geometry["coordinates"]
        coordinates = [round(point[0], places), round(point[1], places)]
    else:
        coordinates = [
            [reduce_ring(r) for r in polygon]
            for polygon in geometry["coordinates"]
        ]
    return {"type": kind, "coordinates": coordinates}


__all__ = [
    "POLYGON",
    "MULTI_POLYGON",
    "LINE_STRING",
    "MULTI_LINE_STRING",
    "SUPPORTED_TYPES",
    "BoundingBox",
    "InvalidGeometry",
    "validate",
    "bounds",
    "centroid",
    "contains",
    "count_points",
    "simplify",
    "round_coordinates",
    "rings",
    "polygons",
]
