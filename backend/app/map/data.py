"""The map's data: what to plot, where, and how big.

One function, :func:`map_data`, answers the whole question: for a level, a
metric, a period and the caller's filters, return every entity that has a
coordinate, with its measures, its marker and its position.

Three things it deliberately does *not* reimplement:

* **aggregation** — it calls :mod:`app.ai.queries`, the same code the dashboard
  and the AI agent use, so a region's sales on the map equals a region's sales
  everywhere else by construction;
* **authorisation** — filters go through :class:`PermissionFilter` exactly as
  every report does, so the map shows a regional manager their region and
  nothing else;
* **appearance** — markers come from :mod:`app.map.resolver`, so the map draws
  whatever the Marker Designer says it should.

Clustering is done here rather than in the browser because the browser should
never receive ten thousand points in order to draw fifty.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ..ai import queries as q
from ..etl.transforms import achievement_percent
from ..ai.permission_filter import PermissionFilter, UserContext
from ..ai.schemas import GroupBy, ScopeFilters
from ..database.models_map import MapEntityLocation
from .entities import ENTITY_TYPE_BY_KEY, get_entity_type
from .geo import Bounds, bounds_of, centroid, locations_for, to_mercator
from .resolver import resolve

#: Map level -> the ``GroupBy`` the query layer aggregates with. Only levels
#: that are both an entity type and a groupable dimension can be drawn.
GROUP_BY_LEVEL: dict[str, GroupBy] = {
    "company": GroupBy.COMPANY,
    "bu": GroupBy.BUSINESS_UNIT,
    "sales_line": GroupBy.SALES_LINE,
    "zone": GroupBy.ZONE,
    "region": GroupBy.REGION,
    "area": GroupBy.AREA,
    "unit": GroupBy.UNIT,
    "territory": GroupBy.TERRITORY,
    "sub_territory": GroupBy.SUB_TERRITORY,
    "customer": GroupBy.CUSTOMER,
    "sales_force": GroupBy.SALES_FORCE,
}

#: The organisational drill path, shallowest first.
DRILL_PATH: tuple[str, ...] = (
    "zone", "region", "area", "unit", "territory", "sub_territory",
)


#: Map level -> the ``ScopeFilters`` field that narrows a query to one of them.
#:
#: **Derived, never written down.** The chain is already stated twice in this
#: codebase and both statements are load-bearing: :data:`GROUP_BY_LEVEL` says
#: which dimension a level aggregates by, ``queries.GROUP_COLUMNS`` says which
#: column that dimension is keyed on, and ``queries.FILTER_COLUMNS`` says which
#: filter field carries that column. Inverting the last of those and walking the
#: three is what makes ``bu -> business_unit_codes`` correct without anybody
#: remembering that this one level is not simply ``f"{level}_codes"`` — the trap
#: a hand-written copy of this map would fall into the first time it was edited.
FILTER_FIELD_BY_LEVEL: dict[str, str] = {}
for _level, _group_by in GROUP_BY_LEVEL.items():
    _columns = q.GROUP_COLUMNS.get(_group_by)
    if not _columns:
        continue
    for _field, _column in q.FILTER_COLUMNS.items():
        if _column == _columns[0]:
            FILTER_FIELD_BY_LEVEL[_level] = _field
            break
del _level, _group_by, _columns, _field, _column


def filter_field_for(level: str) -> str:
    """The ``ScopeFilters`` field that narrows to one entity of ``level``.

    Raises :class:`ValueError` for anything the map cannot aggregate, so a
    hand-typed level is a 422 rather than a filter silently doing nothing.
    """
    try:
        return FILTER_FIELD_BY_LEVEL[level]
    except KeyError:
        raise ValueError(
            f"Unknown level {level!r}. Supported: "
            + ", ".join(sorted(FILTER_FIELD_BY_LEVEL))
        ) from None


#: The one metric that is derived rather than selected.
#:
#: Kept as a constant because three places have to agree on it — the spec, the
#: branch in :func:`map_data`, and the frontend's mode registry — and a literal
#: repeated three times is how they drift apart.
ACHIEVEMENT_KEY = "achievement"


@dataclass(frozen=True)
class MetricSpec:
    """One thing the map can be coloured and sized by."""

    key: str
    label: str
    measures: Any
    #: Field of the aggregate row carrying the value.
    field: str
    unit: str
    #: True when a larger value is worse, so the colour ramp is inverted.
    inverse: bool = False
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "unit": self.unit,
            "inverse": self.inverse, "description": self.description,
        }


METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("net_sales", "Net Sales", q.SALES_MEASURES, "net_sales", "currency",
               description="Sales value after discount."),
    MetricSpec("quantity", "Quantity Sold", q.SALES_MEASURES, "quantity", "quantity"),
    # No Gross Profit metric. A sales report states quantity, volume and net
    # sales; `gross_sales` and `gross_profit` are still stored on `fact_sales`
    # and still derived by the ETL, but they left the reporting surface with
    # `SALES_MEASURES.sums`, and the map is part of that surface.
    #
    # It was also broken while it lasted: `aggregate_by` selects only the
    # columns the measure set names, so `row["gross_profit"]` came back absent
    # and `_numeric` turned it into 0.0 — every area drew as zero while the
    # ordering underneath reflected the real figure. A metric that cannot be
    # computed is removed, not rendered as nothing.
    #
    # No Collection or Outstanding metric. Both datasets left this platform in
    # revision 0020, so neither has a measure set to aggregate or a view to read.
    # No stock metric on the map. Material stock is located by plant and storage
    # location — each its own master — and carries no region, territory or customer
    # code, so there is no geography to plot it against. The old metric worked
    # only because the previous stock fact hung off the sales hierarchy.
    MetricSpec("target_amount", "Target", q.TARGET_MEASURES, "target_amount",
               "currency"),
    # Volume is summed in the warehouse like quantity and net sales, and carries
    # no unit anywhere (0022) — so it is plotted as a bare magnitude, exactly as
    # quantity is. It reads the same measure set; only the field differs.
    MetricSpec("volume", "Sales Volume", q.SALES_MEASURES, "volume", "quantity",
               description="Total volume the upload stated."),
    # Achievement is actual over target, and is the one metric here that is not
    # a column: it is derived in ``_achievement_points`` from two independently
    # aggregated sides. Declared with the sales measures because the sales side
    # is what it ranks and sorts by.
    MetricSpec(ACHIEVEMENT_KEY, "Achievement %", q.SALES_MEASURES, "net_sales",
               "percent",
               description="Net sales against target, as a percentage."),
)

METRIC_BY_KEY: dict[str, MetricSpec] = {m.key: m for m in METRICS}

#: Above this many points, the response is clustered rather than listed.
CLUSTER_THRESHOLD = 300
#: Slippy-map tile size. The world is ``TILE_SIZE * 2**zoom`` pixels across.
TILE_SIZE = 256
#: How close two markers may be, in screen pixels, before they merge. Roughly
#: one marker's width — closer than this and they would overlap anyway.
CLUSTER_PIXELS = 64
#: Zoom assumed when the client does not say. 7 is a country-level view.
DEFAULT_ZOOM = 7
MIN_ZOOM, MAX_ZOOM = 1, 20


def cluster_grid(zoom: int) -> float:
    """Cluster cell size in normalised Mercator units, for a given zoom.

    Clustering has to be zoom-aware. A fixed cell is wrong at both ends: at
    country zoom it leaves overlapping pins, and at street zoom it merges
    genuinely distinct places. Deriving the cell from the pixel size the user is
    actually looking at is the only way it can be right at every zoom.
    """
    bounded = max(MIN_ZOOM, min(MAX_ZOOM, int(zoom)))
    return CLUSTER_PIXELS / (TILE_SIZE * (2 ** bounded))


def get_metric(key: str) -> MetricSpec:
    metric = METRIC_BY_KEY.get(key)
    if metric is None:
        raise ValueError(
            f"Unknown metric {key!r}. Supported: {', '.join(METRIC_BY_KEY)}."
        )
    return metric


def drill_levels() -> list[dict[str, Any]]:
    """The levels the map can show, in drill order."""
    levels = []
    for index, key in enumerate(DRILL_PATH):
        entity = ENTITY_TYPE_BY_KEY[key]
        levels.append({
            "key": key,
            "label": entity.label,
            "depth": index,
            "child": DRILL_PATH[index + 1] if index + 1 < len(DRILL_PATH) else None,
            "parent": DRILL_PATH[index - 1] if index else None,
        })
    return levels


@dataclass
class MapPoint:
    """One plottable entity."""

    code: str
    label: str
    latitude: float
    longitude: float
    value: float
    measures: dict[str, Any]
    location_source: str
    location_precision: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "value": self.value,
            "measures": self.measures,
            "location": {
                "source": self.location_source,
                "precision": self.location_precision,
            },
        }


@dataclass
class MapResult:
    level: str
    metric: str
    points: list[MapPoint] = field(default_factory=list)
    clusters: list[dict[str, Any]] = field(default_factory=list)
    #: Entities with data but no coordinate — the honest reason for a sparse map.
    unplaced: list[dict[str, Any]] = field(default_factory=list)
    bounds: Bounds | None = None
    total_value: float = 0.0
    clustered: bool = False
    #: The zoom the clusters were computed for; a client that zooms past this
    #: should ask again rather than reuse them.
    zoom: int = DEFAULT_ZOOM


def _numeric(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _cluster(points: list[MapPoint], zoom: int = DEFAULT_ZOOM) -> list[dict[str, Any]]:
    """Grid-cluster points in projected space, at the given zoom.

    Clustering in Mercator rather than in degrees matters: a degree of longitude
    is a different distance at different latitudes, so a degree grid produces
    clusters that are stretched near the poles and cramped at the equator. In
    projected space the cells are square on screen, which is where the user is
    looking.
    """
    grid = cluster_grid(zoom)
    cells: dict[tuple[int, int], list[MapPoint]] = {}
    for point in points:
        x, y = to_mercator(point.latitude, point.longitude)
        key = (int(x / grid), int(y / grid))
        cells.setdefault(key, []).append(point)

    clusters = []
    for members in cells.values():
        latitude, longitude = centroid(
            [(p.latitude, p.longitude) for p in members]
        )
        clusters.append({
            "latitude": latitude,
            "longitude": longitude,
            "count": len(members),
            "value": sum(p.value for p in members),
            # Enough to label the cluster without shipping every member.
            "sample": [p.label for p in members[:5]],
            "codes": [p.code for p in members] if len(members) <= 50 else [],
        })
    clusters.sort(key=lambda c: -c["value"])
    return clusters


def map_data(session: Session, user: UserContext, *, level: str, metric: str,
             date_from: dt.date, date_to: dt.date,
             filters: ScopeFilters | None = None,
             limit: int = 2000, cluster: bool | None = None,
             zoom: int = DEFAULT_ZOOM) -> MapResult:
    """Everything the map needs to draw one level."""
    get_entity_type(level)
    if level not in GROUP_BY_LEVEL:
        raise ValueError(f"'{level}' cannot be plotted; it has no fact dimension.")
    spec = get_metric(metric)

    # Authorisation, exactly as every other report does it. A scoped user's
    # filters are narrowed here — the map never queries outside the scope and
    # then trims, because a trimmed result has already been read.
    permissions = PermissionFilter(session, user)
    scoped = permissions.enforce(filters or ScopeFilters())

    rows, _ = q.aggregate_by(
        session, spec.measures, scoped, date_from, date_to,
        GROUP_BY_LEVEL[level], limit=limit, sort_field=spec.field,
    )

    # Achievement needs the target beside each sales row. The two sides are
    # aggregated *independently* and joined on the group's own code — never a
    # raw-to-raw join, which a code with two targets and three sales lines would
    # turn into six inflated combinations. This is the same rule the executive
    # brand table follows, for the same reason.
    targets: dict[str, float] = {}
    if metric == ACHIEVEMENT_KEY:
        target_rows, _ = q.aggregate_by(
            session, q.TARGET_MEASURES, scoped, date_from, date_to,
            GROUP_BY_LEVEL[level], limit=q.MAX_ROWS, sort_field="target_amount",
        )
        targets = {
            str(row.get("code")): _numeric(row.get("target_amount"))
            for row in target_rows
            if row.get("code") not in (None, "(unassigned)")
        }

    locations = locations_for(session, level)
    result = MapResult(level=level, metric=metric)

    for row in rows:
        code = row.get("code")
        if code in (None, "(unassigned)"):
            # Real data, but not attributable to a place. Counted in the total
            # and reported as unplaced rather than dropped or invented.
            result.unplaced.append({
                "code": "(unassigned)",
                "label": row.get("label") or "(not assigned at this level)",
                "value": _numeric(row.get(spec.field)),
                "reason": "not_assigned",
            })
            result.total_value += _numeric(row.get(spec.field))
            continue

        location = locations.get(code)
        value = _numeric(row.get(spec.field))
        result.total_value += value

        if metric == ACHIEVEMENT_KEY:
            target = targets.get(str(code), 0.0)
            achieved = achievement_percent(value, target)
            # A target of zero or none makes achievement unanswerable, not zero.
            # The row is real and is reported — as unplaced with its reason, so
            # it appears in "Not on the map" rather than being silently dropped
            # or, worse, drawn as if it had achieved nothing.
            if achieved is None:
                result.unplaced.append({
                    "code": code,
                    "label": row.get("label") or code,
                    "value": value,
                    "reason": "no_target",
                })
                continue
            # Both sides travel with the point, which is what lets a tooltip
            # state sales *and* target without asking the server again.
            row["net_sales"] = value
            row["target_amount"] = target
            row["achievement_percent"] = float(achieved)
            value = float(achieved)

        if location is None:
            result.unplaced.append({
                "code": code,
                "label": row.get("label") or code,
                "value": value,
                "reason": "no_coordinate",
            })
            continue

        result.points.append(MapPoint(
            code=code,
            label=row.get("label") or code,
            latitude=location.latitude,
            longitude=location.longitude,
            value=value,
            measures={k: v for k, v in row.items() if k not in ("code", "label")},
            location_source=location.source,
            location_precision=location.precision,
        ))

    result.bounds = bounds_of([(p.latitude, p.longitude) for p in result.points])

    should_cluster = (
        cluster if cluster is not None else len(result.points) > CLUSTER_THRESHOLD
    )
    if should_cluster and result.points:
        result.clusters = _cluster(result.points, zoom)
        result.clustered = True
        result.zoom = zoom

    return result


def result_payload(session: Session, result: MapResult) -> dict[str, Any]:
    """The map response, including the marker the level is drawn with."""
    marker = resolve(session, result.level).to_dict()
    values = [p.value for p in result.points]

    payload: dict[str, Any] = {
        "level": result.level,
        "metric": result.metric,
        "metric_label": METRIC_BY_KEY[result.metric].label,
        "marker": marker,
        "clustered": result.clustered,
        "totals": {
            "plotted": len(result.points),
            "unplaced": len(result.unplaced),
            "total_value": result.total_value,
            "min_value": min(values) if values else 0.0,
            "max_value": max(values) if values else 0.0,
        },
        "bounds": result.bounds.padded().to_dict() if result.bounds else None,
        "unplaced": result.unplaced[:100],
    }
    if result.clustered:
        payload["clusters"] = result.clusters
        payload["cluster_zoom"] = result.zoom
        # Points are still sent when the set is small enough to be useful at
        # high zoom; beyond that the clusters are the answer.
        payload["points"] = [p.to_dict() for p in result.points[:CLUSTER_THRESHOLD]]
        payload["points_truncated"] = len(result.points) > CLUSTER_THRESHOLD
    else:
        payload["points"] = [p.to_dict() for p in result.points]
        payload["points_truncated"] = False
    return payload


__all__ = [
    "MetricSpec",
    "METRICS",
    "METRIC_BY_KEY",
    "GROUP_BY_LEVEL",
    "FILTER_FIELD_BY_LEVEL",
    "filter_field_for",
    "DRILL_PATH",
    "CLUSTER_THRESHOLD",
    "CLUSTER_PIXELS",
    "DEFAULT_ZOOM",
    "cluster_grid",
    "MapPoint",
    "MapResult",
    "map_data",
    "result_payload",
    "drill_levels",
    "get_metric",
]
