"""The administrative area layer: polygons, their metric, and how they filter.

An area is not a marker. A marker says *where* something is; an area says what
region of the map a value belongs to, and it is drawn as the shape of that
region. So this module produces a GeoJSON ``FeatureCollection`` rather than a
list of points.

Since the MapLibre rebuild the frontend reads boundary *geometry* from files
served with it (``frontend/public/geo/``, built by
``scripts/build_map_geojson.py``) and calls this layer with ``geometry=false``
for the metric alone, joining the two on the P-code both sides carry. The
geometry path here is still complete and still exercised — it is what any other
client gets, and what ``/areas/{level}/{code}`` answers with — but the busy path
is now properties-only, which is why the viewport and simplification work below
is skipped rather than merely cheap when geometry is not asked for.

Four things it has to get right:

**The metric is real or honestly absent.** Stock is asked of the data warehouse
first. Only when it cannot attribute stock to an area does the configured
default apply, and every feature carries ``stock_source`` saying which happened.
Nothing here hardcodes a number.

**The two hierarchies meet spatially.** A territory has no district column and a
district has no territory column, because neither is true — they are different
ways of dividing the same country. What *is* true is that a territory's
coordinate falls inside exactly one upazila, so filtering areas by territory is
a point-in-polygon test over data that already exists, not a mapping table
somebody has to maintain.

**Only what is needed is loaded.** Geometry is the expensive part of an area, so
the viewport filter is applied to the stored bounding box before any geometry is
read, and a request that names no viewport is capped.

**Appearance comes from the database.** Fill, opacity, stroke and width are read
from ``map_area_styles``; this module never contains a colour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..ai import queries as q
from ..ai.permission_filter import UserContext
from ..ai.schemas import ScopeFilters
from ..config import get_settings
from ..database.models_geo import (
    DimCountry,
    DimDistrict,
    DimDivision,
    DimUpazila,
    MapAdminPoint,
    MapAreaBoundary,
    MapAreaStyle,
)
from ..database.models_map import MapEntityLocation
from . import geometry as geo

# ---------------------------------------------------------------------------
# The levels
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdminLevel:
    """One administrative level the map can draw as areas."""

    key: str
    label: str
    model: Any
    code_field: str
    name_field: str
    parent_key: str | None
    parent_field: str | None
    depth: int
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "code_field": self.code_field,
            "name_field": self.name_field,
            "parent": self.parent_key,
            "parent_field": self.parent_field,
            "depth": self.depth,
            "description": self.description,
        }


ADMIN_LEVELS: tuple[AdminLevel, ...] = (
    AdminLevel(
        key="country", label="Bangladesh", model=DimCountry,
        code_field="country_code", name_field="country_name",
        parent_key=None, parent_field=None, depth=0,
        description="The national outline.",
    ),
    AdminLevel(
        key="division", label="Division", model=DimDivision,
        code_field="division_code", name_field="division_name",
        parent_key="country", parent_field="country_code", depth=1,
        description="The largest administrative unit within the country.",
    ),
    AdminLevel(
        key="district", label="District", model=DimDistrict,
        code_field="district_code", name_field="district_name",
        parent_key="division", parent_field="division_code", depth=2,
        description="A zila within a division.",
    ),
    AdminLevel(
        key="upazila", label="Upazila", model=DimUpazila,
        code_field="upazila_code", name_field="upazila_name",
        parent_key="district", parent_field="district_code", depth=3,
        description="A sub-district within a district. The level the map draws.",
    ),
)

LEVEL_BY_KEY: dict[str, AdminLevel] = {level.key: level for level in ADMIN_LEVELS}

#: The level the layer shows unless told otherwise.
DEFAULT_LEVEL = "upazila"

#: Layer key the frontend switches on. Namespaced so it can never collide with
#: an organisational layer of the same name.
LAYER_KEY = "admin_area"


class UnknownLevel(ValueError):
    """Asked for an administrative level that does not exist."""


def get_level(key: str) -> AdminLevel:
    level = LEVEL_BY_KEY.get(key)
    if level is None:
        raise UnknownLevel(
            f"Unknown administrative level '{key}'. Available: "
            f"{', '.join(LEVEL_BY_KEY)}."
        )
    return level


# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------

#: Used only when the style row is missing — a database that skipped the seed.
#: Not the documented default; that lives in migration ``0010``, which is the
#: single place the blue is written down.
_FALLBACK_STYLE: dict[str, Any] = {
    "fill_color": "#2563EB",
    "fill_opacity": 0.20,
    "stroke_color": "#2563EB",
    "stroke_opacity": 1.0,
    "stroke_width": 1.5,
    "hover_fill_opacity": 0.35,
    "selected_fill_opacity": 0.45,
    "z_index": 1,
}


def style_for(session: Session, level_key: str) -> dict[str, Any]:
    """The configured appearance of one level, as the renderers consume it."""
    row = session.execute(
        select(MapAreaStyle).where(MapAreaStyle.entity_type == level_key)
    ).scalar_one_or_none()
    if row is None:
        return {"entity_type": level_key, "configured": False, **_FALLBACK_STYLE}
    return {
        "entity_type": row.entity_type,
        "configured": True,
        "fill_color": row.fill_color,
        "fill_opacity": row.fill_opacity,
        "stroke_color": row.stroke_color,
        "stroke_opacity": row.stroke_opacity,
        "stroke_width": row.stroke_width,
        # Hover and selection fall back to the base fill lifted, so a style
        # configured with only the four documented properties still responds to
        # the pointer instead of looking inert.
        "hover_fill_color": row.hover_fill_color or row.fill_color,
        "hover_fill_opacity": (
            row.hover_fill_opacity
            if row.hover_fill_opacity is not None
            else min(1.0, row.fill_opacity + 0.15)
        ),
        "selected_fill_color": row.selected_fill_color or row.fill_color,
        "selected_fill_opacity": (
            row.selected_fill_opacity
            if row.selected_fill_opacity is not None
            else min(1.0, row.fill_opacity + 0.25)
        ),
        "z_index": row.z_index,
        "rules": row.rules,
        "is_system_default": row.is_system_default,
    }


def all_styles(session: Session) -> dict[str, dict[str, Any]]:
    return {level.key: style_for(session, level.key) for level in ADMIN_LEVELS}


# ---------------------------------------------------------------------------
# The metric
# ---------------------------------------------------------------------------

#: Metrics an area can report. Stock is the one the layer is specified around;
#: the others are listed because the popup shows whatever the data supports and
#: this is where "what does the data support" is answered.
AREA_METRICS: tuple[str, ...] = ("stock", "net_sales")

SOURCE_MEASURED = "measured"
SOURCE_DEFAULT = "default"


@dataclass
class AreaMetrics:
    """One area's figures, and where each came from."""

    stock: float
    stock_source: str
    extra: dict[str, float] = field(default_factory=dict)


def stock_by_area(session: Session, level_key: str, user: UserContext,
                  filters: ScopeFilters | None = None,
                  date_from: Any = None, date_to: Any = None,
                  ) -> dict[str, float]:
    """No stock is attributable to a map area under the material stock model.

    This used to sum closing stock per warehouse and place each warehouse in the
    upazila its coordinates fell inside. Material stock is held by **plant and
    storage location**, each its own master: a position carries no warehouse
    code, and neither master records a coordinate, so there is nothing to place
    on a map.

    Kept as an empty result rather than deleted so the map keeps rendering — every
    area falls back to the configured default, and the API goes on reporting per
    feature whether a figure was measured or defaulted. Restoring a real
    attribution needs a plant-to-geography mapping that does not exist yet, and
    inventing one would put stock in places nobody could verify.
    """
    return {}


# ---------------------------------------------------------------------------
# Territory ↔ area
# ---------------------------------------------------------------------------


def areas_for_territories(session: Session, territory_codes: Iterable[str],
                          level_key: str = "upazila") -> set[str]:
    """Which areas the given territories fall inside.

    The specification asks for a territory filter to show "the relevant
    Upazila(s) associated with the selected territory according to the existing
    geographic relationship". The existing relationship is the coordinate the
    map already holds for that territory: whichever polygon contains it.

    A territory with no coordinate matches nothing. That is reported rather than
    hidden — the same rule the rest of the map follows for unplaced entities.
    """
    codes = [str(code) for code in territory_codes if code]
    if not codes:
        return set()

    placements = session.execute(
        select(MapEntityLocation.entity_code, MapEntityLocation.latitude,
               MapEntityLocation.longitude)
        .where(MapEntityLocation.entity_type == "territory",
               MapEntityLocation.entity_code.in_(codes))
    ).all()
    if not placements:
        return set()

    boundaries = session.execute(
        select(MapAreaBoundary.entity_code, MapAreaBoundary.geometry,
               MapAreaBoundary.bbox_north, MapAreaBoundary.bbox_south,
               MapAreaBoundary.bbox_east, MapAreaBoundary.bbox_west)
        .where(MapAreaBoundary.entity_type == level_key)
    ).all()

    matched: set[str] = set()
    for _code, latitude, longitude in placements:
        for (area_code, shape, north, south, east, west) in boundaries:
            if not (south <= latitude <= north and west <= longitude <= east):
                continue
            if geo.contains(shape, latitude, longitude):
                matched.add(area_code)
                break
    return matched


def unplaced_territories(session: Session,
                         territory_codes: Iterable[str]) -> list[str]:
    """Territories the filter could not resolve, because they have no coordinate."""
    codes = {str(code) for code in territory_codes if code}
    if not codes:
        return []
    placed = {
        row[0] for row in session.execute(
            select(MapEntityLocation.entity_code).where(
                MapEntityLocation.entity_type == "territory",
                MapEntityLocation.entity_code.in_(sorted(codes)),
            )
        )
    }
    return sorted(codes - placed)


# ---------------------------------------------------------------------------
# The query
# ---------------------------------------------------------------------------


@dataclass
class AreaFilters:
    """What narrows the area layer."""

    division_code: str | None = None
    district_code: str | None = None
    upazila_code: str | None = None
    #: Organisational territories; resolved to areas spatially.
    territory_codes: tuple[str, ...] = ()
    #: Viewport, as ``(north, south, east, west)``.
    viewport: geo.BoundingBox | None = None

    @property
    def any_set(self) -> bool:
        return bool(self.division_code or self.district_code
                    or self.upazila_code or self.territory_codes)


@dataclass
class AreaResult:
    features: list[dict[str, Any]] = field(default_factory=list)
    style: dict[str, Any] = field(default_factory=dict)
    level: str = DEFAULT_LEVEL
    total: int = 0
    truncated: bool = False
    #: Territories the caller filtered on that have no coordinate to resolve.
    unresolved_territories: list[str] = field(default_factory=list)
    #: True when no boundary has ever been imported, so the UI can say so
    #: instead of rendering an empty map that looks like a filter mistake.
    boundaries_loaded: bool = True
    bounds: dict[str, float] | None = None

    def to_geojson(self) -> dict[str, Any]:
        return {"type": "FeatureCollection", "features": self.features}


def _level_rows(session: Session, level: AdminLevel,
                filters: AreaFilters) -> list[Any]:
    """The administrative records in scope, with their parents joined.

    One query with the ancestry attached, because every feature reports its
    district and division and doing that per row would be a lookup per area.
    """
    model = level.model
    if level.key == "upazila":
        statement = (
            select(DimUpazila, DimDistrict, DimDivision)
            .join(DimDistrict, DimDistrict.district_code == DimUpazila.district_code)
            .join(DimDivision, DimDivision.division_code == DimDistrict.division_code)
            .where(DimUpazila.is_deleted.is_(False))
        )
        if filters.district_code:
            statement = statement.where(
                DimUpazila.district_code == filters.district_code)
        if filters.division_code:
            statement = statement.where(
                DimDistrict.division_code == filters.division_code)
        if filters.upazila_code:
            statement = statement.where(
                DimUpazila.upazila_code == filters.upazila_code)
        return session.execute(statement).all()

    if level.key == "district":
        statement = (
            select(DimDistrict, DimDivision)
            .join(DimDivision, DimDivision.division_code == DimDistrict.division_code)
            .where(DimDistrict.is_deleted.is_(False))
        )
        if filters.division_code:
            statement = statement.where(
                DimDistrict.division_code == filters.division_code)
        if filters.district_code:
            statement = statement.where(
                DimDistrict.district_code == filters.district_code)
        return session.execute(statement).all()

    if level.key == "division":
        statement = select(DimDivision).where(DimDivision.is_deleted.is_(False))
        if filters.division_code:
            statement = statement.where(
                DimDivision.division_code == filters.division_code)
        return [(row,) for row in session.execute(statement).scalars()]

    # Country. The administrative filters below it do not apply: narrowing to a
    # district cannot make the national outline a different shape, and dropping
    # it would remove the frame the other layers are read against.
    statement = select(DimCountry).where(DimCountry.is_deleted.is_(False))
    return [(row,) for row in session.execute(statement).scalars()]


def _properties(level: AdminLevel, row: tuple) -> dict[str, Any]:
    """The identity of one area, flattened for the popup."""
    if level.key == "upazila":
        upazila, district, division = row
        return {
            "upazila_id": upazila.upazila_id,
            "upazila_code": upazila.upazila_code,
            "upazila_name": upazila.upazila_name,
            "upazila_name_bn": upazila.upazila_name_bn,
            "district_id": district.district_id,
            "district_code": district.district_code,
            "district_name": district.district_name,
            "division_id": division.division_id,
            "division_code": division.division_code,
            "division_name": division.division_name,
            "latitude": upazila.latitude,
            "longitude": upazila.longitude,
        }
    if level.key == "district":
        district, division = row
        return {
            "district_id": district.district_id,
            "district_code": district.district_code,
            "district_name": district.district_name,
            "division_id": division.division_id,
            "division_code": division.division_code,
            "division_name": division.division_name,
        }
    if level.key == "division":
        (division,) = row
        return {
            "division_id": division.division_id,
            "division_code": division.division_code,
            "division_name": division.division_name,
            "division_name_bn": division.division_name_bn,
        }
    (country,) = row
    return {
        "country_id": country.country_id,
        "country_code": country.country_code,
        "country_name": country.country_name,
        "country_name_bn": country.country_name_bn,
        "iso3": country.iso3,
    }


def area_features(session: Session, user: UserContext, *,
                  level_key: str = DEFAULT_LEVEL,
                  filters: AreaFilters | None = None,
                  business_filters: ScopeFilters | None = None,
                  date_from: Any = None, date_to: Any = None,
                  include_geometry: bool = True,
                  simplify_tolerance: float = 0.0) -> AreaResult:
    """Build the area layer for one request."""
    level = get_level(level_key)
    filters = filters or AreaFilters()
    settings = get_settings()

    result = AreaResult(level=level.key, style=style_for(session, level.key))

    total_boundaries = session.execute(
        select(func.count()).select_from(MapAreaBoundary.__table__)
        .where(MapAreaBoundary.entity_type == level.key)
    ).scalar_one()
    result.boundaries_loaded = bool(total_boundaries)

    rows = _level_rows(session, level, filters)
    codes = [_code_of(level, row) for row in rows]

    # A territory filter narrows to whatever the territories sit inside. Applied
    # after the administrative filters so the two intersect rather than one
    # overriding the other.
    if filters.territory_codes:
        spatial = areas_for_territories(session, filters.territory_codes, level.key)
        rows = [row for row in rows if _code_of(level, row) in spatial]
        codes = [_code_of(level, row) for row in rows]
        result.unresolved_territories = unplaced_territories(
            session, filters.territory_codes)

    if not codes:
        return result

    boundaries = _boundaries_for(session, level.key, codes, filters.viewport)

    measured = stock_by_area(session, level.key, user, business_filters,
                             date_from, date_to)

    drawn = [row for row in rows if _code_of(level, row) in boundaries]
    result.total = len(drawn)
    limit = settings.map_area_max_features
    if len(drawn) > limit:
        result.truncated = True
        drawn = drawn[:limit]

    north = east = -90.0
    south, west = 90.0, 180.0

    for row in drawn:
        code = _code_of(level, row)
        boundary = boundaries[code]
        properties = _properties(level, row)

        if code in measured:
            properties["stock"] = round(measured[code], 4)
            properties["stock_source"] = SOURCE_MEASURED
        else:
            # The configured placeholder, and labelled as such — so a reader can
            # always tell a real figure from a stand-in.
            properties["stock"] = settings.map_area_default_stock
            properties["stock_source"] = SOURCE_DEFAULT

        properties["centroid_latitude"] = boundary.centroid_latitude
        properties["centroid_longitude"] = boundary.centroid_longitude
        properties["point_count"] = boundary.point_count

        shape: dict[str, Any] | None = None
        if include_geometry:
            shape = boundary.geometry
            if simplify_tolerance > 0:
                shape = geo.simplify(shape, simplify_tolerance)

        result.features.append({
            "type": "Feature",
            "id": code,
            "properties": properties,
            "geometry": shape,
            "bbox": [boundary.bbox_west, boundary.bbox_south,
                     boundary.bbox_east, boundary.bbox_north],
        })

        north = max(north, boundary.bbox_north)
        south = min(south, boundary.bbox_south)
        east = max(east, boundary.bbox_east)
        west = min(west, boundary.bbox_west)

    if result.features:
        result.bounds = {"north": north, "south": south, "east": east,
                         "west": west}
    return result


def _code_of(level: AdminLevel, row: tuple) -> str:
    return getattr(row[0], level.code_field)


def _boundaries_for(session: Session, level_key: str, codes: list[str],
                    viewport: geo.BoundingBox | None,
                    ) -> dict[str, MapAreaBoundary]:
    """Boundaries for these codes, filtered to the viewport before geometry loads.

    The viewport predicate is on the stored box, so an area off screen is never
    fetched — which is what keeps a country-wide layer from being a country-wide
    payload.
    """
    statement = select(MapAreaBoundary).where(
        MapAreaBoundary.entity_type == level_key,
        MapAreaBoundary.entity_code.in_(codes),
    )
    if viewport is not None:
        statement = statement.where(and_(
            MapAreaBoundary.bbox_south <= viewport.north,
            MapAreaBoundary.bbox_north >= viewport.south,
            MapAreaBoundary.bbox_west <= viewport.east,
            MapAreaBoundary.bbox_east >= viewport.west,
        ))
    return {row.entity_code: row for row in session.execute(statement).scalars()}


# ---------------------------------------------------------------------------
# One area, in detail
# ---------------------------------------------------------------------------


def area_detail(session: Session, user: UserContext, level_key: str, code: str,
                *, business_filters: ScopeFilters | None = None,
                date_from: Any = None, date_to: Any = None,
                ) -> dict[str, Any] | None:
    """Everything the click popup shows for one area.

    Optional metrics are included only when the warehouse can attribute them.
    An absent metric is left out entirely rather than reported as zero: "no
    sales data for this upazila" and "zero sales in this upazila" are different
    statements, and showing the second when the first is true is a lie.
    """
    level = get_level(level_key)
    filters = AreaFilters(**{f"{level.key}_code": code})
    result = area_features(session, user, level_key=level_key, filters=filters,
                           business_filters=business_filters,
                           date_from=date_from, date_to=date_to,
                           include_geometry=False)
    if not result.features:
        return None

    feature = result.features[0]
    properties = dict(feature["properties"])

    if level.key == "upazila":
        properties.update(_optional_metrics(session, code))

    return {
        "level": level.key,
        "code": code,
        "properties": properties,
        "bbox": feature["bbox"],
        "style": result.style,
    }


def _optional_metrics(session: Session, upazila_code: str) -> dict[str, Any]:
    """Counts the data can support for one upazila. Currently none.

    Warehouses used to be counted here: they were placed on the map, so each one
    could be located inside a boundary. The dimension is gone (revision 0020),
    and nothing that replaced it carries a coordinate — a plant and a storage
    location are named by the material masters and placed nowhere.

    Customers and sales force are still not counted either, for the reason they
    never were: nothing gives them a coordinate or a district, so any count
    would be invented. Every one of them appears here the moment it is placed,
    which is why this returns an empty mapping rather than being deleted.
    """
    return {}


# ---------------------------------------------------------------------------
# Coverage and cache validity
# ---------------------------------------------------------------------------


def coverage(session: Session) -> list[dict[str, Any]]:
    """How many areas exist per level, and how many have a boundary."""
    rows = []
    for level in ADMIN_LEVELS:
        total = session.execute(
            select(func.count()).select_from(level.model.__table__)
            .where(level.model.is_deleted.is_(False))
        ).scalar_one()
        drawn = session.execute(
            select(func.count()).select_from(MapAreaBoundary.__table__)
            .where(MapAreaBoundary.entity_type == level.key)
        ).scalar_one()
        rows.append({
            "level": level.key,
            "label": level.label,
            "total": total,
            "with_boundary": drawn,
            "missing_boundary": max(0, total - drawn),
        })
    return rows


def generation(session: Session, level_key: str) -> str:
    """A token that changes whenever this level's boundaries change.

    Boundaries are imported rarely and read constantly, which is exactly the
    shape an ETag is for: the browser keeps the megabyte it already has and the
    server answers 304 until an import moves the token.
    """
    row = session.execute(
        select(func.count(), func.max(MapAreaBoundary.updated_at))
        .where(MapAreaBoundary.entity_type == level_key)
    ).one()
    count, latest = row[0], row[1]
    return f"{level_key}-{count}-{latest or 'never'}"


def levels_payload() -> list[dict[str, Any]]:
    return [level.to_dict() for level in ADMIN_LEVELS]


# ---------------------------------------------------------------------------
# Administrative reference points
# ---------------------------------------------------------------------------

#: The point layers the map can draw, and the level each one goes down to by
#: default. Capitals stop at the upazila seat; the point layer's level 4 is
#: below anything this schema names, so it is available but not implied.
ADMIN_POINT_KINDS: tuple[dict[str, Any], ...] = (
    {"key": "capital", "label": "Administrative Capitals", "max_level": 3},
    {"key": "point", "label": "Administrative Points", "max_level": 4},
)


def admin_points(session: Session, *, kind: str,
                 max_level: int | None = None,
                 viewport: geo.BoundingBox | None = None,
                 limit: int = 2000) -> tuple[list[dict[str, Any]], bool]:
    """Published reference points of one kind, filtered before they are loaded.

    A point is small, but there are thousands of them — the point layer alone
    has 5,777 — so the same discipline the boundary layer follows applies here:
    the viewport predicate is part of the query rather than a filter applied to
    everything after loading it all. Returns the rows and whether the limit cut
    the result short, because a silently truncated layer looks like missing
    geography rather than a capped request.
    """
    statement = select(MapAdminPoint).where(MapAdminPoint.kind == kind)
    if max_level is not None:
        statement = statement.where(MapAdminPoint.admin_level <= max_level)
    if viewport is not None:
        statement = statement.where(
            MapAdminPoint.latitude <= viewport.north,
            MapAdminPoint.latitude >= viewport.south,
            MapAdminPoint.longitude <= viewport.east,
            MapAdminPoint.longitude >= viewport.west,
        )
    # Coarser levels first, so a truncated result keeps the points that matter
    # at the zoom a large result implies: losing a country label to keep a ward
    # would be exactly the wrong trade.
    statement = statement.order_by(MapAdminPoint.admin_level,
                                   MapAdminPoint.name).limit(limit + 1)
    rows = list(session.execute(statement).scalars())
    truncated = len(rows) > limit
    return [
        {
            "id": row.point_id,
            "kind": row.kind,
            "admin_level": row.admin_level,
            "name": row.name,
            "name_bn": row.name_bn,
            "latitude": row.latitude,
            "longitude": row.longitude,
            "country_code": row.country_code,
            "division_code": row.division_code,
            "district_code": row.district_code,
            "upazila_code": row.upazila_code,
        }
        for row in rows[:limit]
    ], truncated


def admin_point_generation(session: Session, kind: str) -> str:
    """ETag token for a point layer, on the same principle as ``generation``."""
    row = session.execute(
        select(func.count(), func.max(MapAdminPoint.updated_at))
        .where(MapAdminPoint.kind == kind)
    ).one()
    return f"{kind}-{row[0]}-{row[1] or 'never'}"


__all__ = [
    "AdminLevel",
    "ADMIN_LEVELS",
    "LEVEL_BY_KEY",
    "DEFAULT_LEVEL",
    "LAYER_KEY",
    "AREA_METRICS",
    "SOURCE_MEASURED",
    "SOURCE_DEFAULT",
    "AreaFilters",
    "AreaResult",
    "UnknownLevel",
    "get_level",
    "style_for",
    "all_styles",
    "area_features",
    "area_detail",
    "areas_for_territories",
    "unplaced_territories",
    "stock_by_area",
    "coverage",
    "generation",
    "levels_payload",
    "ADMIN_POINT_KINDS",
    "admin_points",
    "admin_point_generation",
]
