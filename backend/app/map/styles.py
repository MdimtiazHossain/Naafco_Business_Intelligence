"""How a figure becomes a colour and a size — declared once, overridable per layer.

The renderer must not be the only thing that knows a marker's design (the rule
``0033``'s removal recorded), so the defaults a layer draws with live here, are
published through ``GET /api/map/config``, and a layer's ``style_config`` may
override them field by field. The browser turns what it is sent into MapLibre
expressions; it invents no threshold and no colour of its own.

Three colour modes, chosen by the **metric** rather than by the layer:

* **bands** — Achievement %. Four bands with business meaning attached: good,
  medium, low, critical. The thresholds are configuration, not a constant:
  90 / 70 / 50 is the specification's starting point, and a business that
  chases at 80 changes three numbers here or on one layer, never in the
  renderer.
* **diverging** — a signed metric (growth, shortfall): negative is red,
  positive green, and zero sits in the middle rather than at the foot of a
  ramp.
* **sequential** — every other figure: one ramp from light to dark, with breaks
  the data endpoint computes from each layer's own extent so a legend reads
  "Above 10 Cr" rather than a fraction.

**An entity whose colour metric is absent is drawn neutral, never critical.**
No target means no achievement, not an achievement of nothing — the same
distinction every report in this platform keeps between absent and zero.

**Shapes are declared here too, geometry included.** The Area Demarcation map
draws a different shape per level so a reader can tell a territory from a
customer at a glance, and the catalogue below carries each shape's SVG path
rather than only its name. The browser rasterises what it is sent and holds no
catalogue of its own, which is the rule ``0033``'s removal recorded — the
renderer must not be the only thing that knows a marker's design — read as
strictly as it can be: a shape the server does not declare cannot reach the
renderer, and one it does declare cannot arrive without its geometry.

This is deliberately a small closed set, not the uploadable asset library
``0033`` removed. There is no ``map_marker_designs``, no asset table and no
per-point shape: a shape is chosen per *level*, from these eight.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, Mapping

from .metrics import MapMetric

MODE_BANDS = "bands"
MODE_DIVERGING = "diverging"
MODE_SEQUENTIAL = "sequential"

#: Achievement bands, highest first: at or above 90 is good, at or above 70
#: medium, at or above 50 low, and everything under that critical.
ACHIEVEMENT_THRESHOLDS: tuple[float, float, float] = (90.0, 70.0, 50.0)
BAND_KEYS: tuple[str, ...] = ("good", "medium", "low", "critical")
#: Green for good, amber and orange for the two warnings, red for critical —
#: the palette the specification asks for and the one the stock statuses use.
BAND_COLORS: dict[str, str] = {
    "good": "#16a34a",
    "medium": "#f59e0b",
    "low": "#ea580c",
    "critical": "#dc2626",
}
#: Slate, deliberately outside the band palette, for a figure that is absent.
NO_DATA_COLOR = "#94a3b8"
#: Light to dark blue for a sequential metric; the primary colour, as asked.
SEQUENTIAL_RAMP: tuple[str, ...] = ("#bfdbfe", "#60a5fa", "#2563eb", "#1e3a8a")
DIVERGING_COLORS: dict[str, str] = {
    "negative": "#dc2626",
    "neutral": "#94a3b8",
    "positive": "#16a34a",
}
#: Circle radius in pixels for the smallest and the largest figure on a layer.
RADIUS_RANGE: tuple[float, float] = (4.0, 22.0)
#: How a cluster of points is drawn before the reader zooms into it.
CLUSTER_STYLE: dict[str, str] = {"color": "#1d4ed8", "text_color": "#ffffff"}

#: Every shape a layer may be drawn with, on a 24×24 viewBox centred at
#: (12, 12). The path travels to the browser with the key, so adding a shape is
#: a change to this tuple and nothing else — and a renderer can never be handed
#: a key it has no geometry for.
#:
#: ``circle`` is first and is the default, because it is what the analysis map
#: has always drawn: a layer that names no shape keeps the appearance it had.
SHAPES: tuple[dict[str, str], ...] = (
    {"key": "circle", "label": "Circle",
     "path": "M22 12 A10 10 0 1 1 2 12 A10 10 0 1 1 22 12 Z"},
    {"key": "square", "label": "Square",
     "path": "M3 3 H21 V21 H3 Z"},
    {"key": "triangle", "label": "Triangle",
     "path": "M12 2 L22 20 H2 Z"},
    {"key": "diamond", "label": "Diamond",
     "path": "M12 2 L22 12 L12 22 L2 12 Z"},
    {"key": "hexagon", "label": "Hexagon",
     "path": "M12 2 L20.66 7 V17 L12 22 L3.34 17 V7 Z"},
    {"key": "star", "label": "Star",
     "path": "M12 2 L14.35 8.76 L21.51 8.91 L15.8 13.24 L17.88 20.09 "
             "L12 16 L6.12 20.09 L8.2 13.24 L2.49 8.91 L9.65 8.76 Z"},
    {"key": "cross", "label": "Cross",
     "path": "M9 2 H15 V9 H22 V15 H15 V22 H9 V15 H2 V9 H9 Z"},
    {"key": "pin", "label": "Pin",
     "path": "M12 22 C12 22 21 14.5 21 9 A9 9 0 1 0 3 9 C3 14.5 12 22 12 22 Z"},
)
SHAPE_KEYS: tuple[str, ...] = tuple(shape["key"] for shape in SHAPES)
DEFAULT_SHAPE = SHAPE_KEYS[0]
#: The side of the viewBox every path above is drawn on.
SHAPE_VIEWBOX = 24

#: The flat colour a point takes where no metric decides one — which is every
#: point on the demarcation map. Blue rather than slate: slate is
#: :data:`NO_DATA_COLOR`, and "this level's colour" and "this figure is absent"
#: must not look alike.
DEFAULT_POINT_COLOR = "#2563eb"

#: How a ``DERIVED`` coordinate is drawn: the same shape, hollow.
#:
#: A derived point is the centroid of what is placed below it, not a place
#: anybody surveyed. On a map whose whole purpose is judging where a boundary
#: falls, mistaking a computed average for a real address is the error that
#: matters, so the two are told apart by fill rather than by a legend entry
#: somebody has to go and read. 293 of ``data/dev.db``'s 1,139 coordinates are
#: derived, so this is the common case, not an edge one.
DERIVED_OPACITY = 0.25
DERIVED_STROKE_WIDTH = 1.5

#: The keys a layer's ``style_config`` may override. Anything else is refused
#: rather than stored and ignored: a saved setting that changes nothing is a
#: setting somebody will trust.
OVERRIDABLE: tuple[str, ...] = (
    "thresholds", "band_colors", "no_data_color", "sequential", "diverging",
    "radius", "shape", "point_color",
)

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_RADIUS_MIN, _RADIUS_MAX = 1.0, 60.0


def color_mode(metric: MapMetric) -> str:
    """Which of the three colour modes a metric is drawn in."""
    if metric.key == "achievement":
        return MODE_BANDS
    if metric.signed:
        return MODE_DIVERGING
    return MODE_SEQUENTIAL


def _percent(value: float) -> str:
    return f"{value:g}%"


def bands(thresholds: tuple[float, ...] = ACHIEVEMENT_THRESHOLDS,
          colors: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """The four achievement bands, each with its bounds, label and colour.

    Labels are derived from the thresholds so a changed threshold relabels the
    legend by itself — a legend reading "90% and above" over a band that starts
    at 80 would be the kind of stale list this platform refuses to keep.
    """
    palette = {**BAND_COLORS, **(colors or {})}
    high, mid, low = thresholds
    return [
        {"key": "good", "min": high, "max": None,
         "label": f"{_percent(high)} and above", "color": palette["good"]},
        {"key": "medium", "min": mid, "max": high,
         "label": f"{_percent(mid)} – {_percent(high)}", "color": palette["medium"]},
        {"key": "low", "min": low, "max": mid,
         "label": f"{_percent(low)} – {_percent(mid)}", "color": palette["low"]},
        {"key": "critical", "min": None, "max": low,
         "label": f"Below {_percent(low)}", "color": palette["critical"]},
    ]


#: How many classes a sequential colour scale has: one per ramp colour.
SEQUENTIAL_CLASSES = len(SEQUENTIAL_RAMP)


def class_breaks(values: Iterable[float | None],
                 classes: int = SEQUENTIAL_CLASSES) -> list[float]:
    """Quantile breaks dividing ``values`` into ``classes`` equal-count classes.

    Returns up to ``classes - 1`` ascending thresholds: a value below the first
    is in the lightest class, one at or above the last is in the darkest.
    Equal-count rather than equal-width because a sales figure is heavily
    skewed — a handful of large territories would otherwise take three of the
    four colours and leave every other point in the fourth, which is a map that
    says nothing. Fewer distinct values than classes give fewer breaks, so a
    layer of two entities has two classes rather than four labels over one
    colour. Nothing here is invented: every break is a value some entity has.
    """
    cleaned = sorted(
        float(value) for value in values
        if value is not None and not math.isnan(float(value))
    )
    if not cleaned:
        return []
    distinct = sorted(set(cleaned))
    if len(distinct) <= classes:
        return distinct[1:]
    count = len(cleaned)
    breaks: list[float] = []
    for step in range(1, classes):
        index = min(count - 1, max(0, math.ceil(step * count / classes) - 1))
        candidate = cleaned[index]
        if not breaks or candidate > breaks[-1]:
            breaks.append(candidate)
    return breaks


def default_style() -> dict[str, Any]:
    """The style every layer draws with unless it overrides a field."""
    return {
        "thresholds": list(ACHIEVEMENT_THRESHOLDS),
        "band_colors": dict(BAND_COLORS),
        "bands": bands(),
        "no_data_color": NO_DATA_COLOR,
        "sequential": list(SEQUENTIAL_RAMP),
        "diverging": dict(DIVERGING_COLORS),
        "radius": list(RADIUS_RANGE),
        "cluster": dict(CLUSTER_STYLE),
        "shape": DEFAULT_SHAPE,
        "point_color": DEFAULT_POINT_COLOR,
        "derived_opacity": DERIVED_OPACITY,
        "derived_stroke_width": DERIVED_STROKE_WIDTH,
    }


def shape_catalogue() -> list[dict[str, Any]]:
    """Every shape with its geometry, as ``GET /api/map/config`` publishes it."""
    return [{**shape, "viewbox": SHAPE_VIEWBOX} for shape in SHAPES]


def _hex(value: Any, key: str) -> str:
    if not isinstance(value, str) or not _HEX.match(value):
        raise ValueError(f"style_config.{key} must be a #rrggbb colour, not {value!r}.")
    return value.lower()


def _number(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"style_config.{key} must be a number, not {value!r}.")
    return float(value)


def effective_style(overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    """The defaults with a layer's overrides applied, every override validated.

    Raises :class:`ValueError` naming the offending key, so the layer editor can
    say which field to fix rather than "invalid style".
    """
    style = default_style()
    if not overrides:
        return style
    if not isinstance(overrides, Mapping):
        raise ValueError("style_config must be an object.")

    unknown = sorted(set(overrides) - set(OVERRIDABLE))
    if unknown:
        raise ValueError(
            f"style_config has no field named {', '.join(unknown)}. "
            f"Overridable: {', '.join(OVERRIDABLE)}."
        )

    if "thresholds" in overrides:
        raw = overrides["thresholds"]
        if not isinstance(raw, (list, tuple)) or len(raw) != 3:
            raise ValueError("style_config.thresholds must list exactly three "
                             "percentages, highest first.")
        values = tuple(_number(item, "thresholds") for item in raw)
        if any(value < 0 for value in values):
            raise ValueError("style_config.thresholds must not be negative.")
        if not (values[0] > values[1] > values[2]):
            raise ValueError("style_config.thresholds must be strictly "
                             "descending, e.g. [90, 70, 50].")
        style["thresholds"] = list(values)

    if "band_colors" in overrides:
        raw = overrides["band_colors"]
        if not isinstance(raw, Mapping):
            raise ValueError("style_config.band_colors must map band to colour.")
        unknown_bands = sorted(set(raw) - set(BAND_KEYS))
        if unknown_bands:
            raise ValueError(
                f"style_config.band_colors names no band called "
                f"{', '.join(unknown_bands)}. Bands: {', '.join(BAND_KEYS)}."
            )
        style["band_colors"] = {
            **style["band_colors"],
            **{band: _hex(color, f"band_colors.{band}") for band, color in raw.items()},
        }

    if "no_data_color" in overrides:
        style["no_data_color"] = _hex(overrides["no_data_color"], "no_data_color")

    if "sequential" in overrides:
        raw = overrides["sequential"]
        if not isinstance(raw, (list, tuple)) or not 2 <= len(raw) <= 8:
            raise ValueError("style_config.sequential must list between two "
                             "and eight colours, lightest first.")
        style["sequential"] = [_hex(color, "sequential") for color in raw]

    if "diverging" in overrides:
        raw = overrides["diverging"]
        if not isinstance(raw, Mapping):
            raise ValueError("style_config.diverging must map negative, "
                             "neutral and positive to colours.")
        unknown_sides = sorted(set(raw) - set(DIVERGING_COLORS))
        if unknown_sides:
            raise ValueError(
                f"style_config.diverging names no side called "
                f"{', '.join(unknown_sides)}. Sides: negative, neutral, positive."
            )
        style["diverging"] = {
            **style["diverging"],
            **{side: _hex(color, f"diverging.{side}") for side, color in raw.items()},
        }

    if "radius" in overrides:
        raw = overrides["radius"]
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            raise ValueError("style_config.radius must be [smallest, largest] "
                             "in pixels.")
        low, high = (_number(item, "radius") for item in raw)
        if not (_RADIUS_MIN <= low < high <= _RADIUS_MAX):
            raise ValueError(
                f"style_config.radius must satisfy {_RADIUS_MIN:g} <= smallest "
                f"< largest <= {_RADIUS_MAX:g}."
            )
        style["radius"] = [low, high]

    if "shape" in overrides:
        raw = overrides["shape"]
        if raw not in SHAPE_KEYS:
            raise ValueError(
                f"style_config.shape is not a shape this map draws: {raw!r}. "
                f"Choose one of: {', '.join(SHAPE_KEYS)}."
            )
        style["shape"] = raw

    if "point_color" in overrides:
        style["point_color"] = _hex(overrides["point_color"], "point_color")

    # The bands are derived from the (possibly overridden) thresholds and
    # colours, never stored: a stored band would be a second copy of one rule.
    style["bands"] = bands(tuple(style["thresholds"]), style["band_colors"])
    return style


__all__ = [
    "ACHIEVEMENT_THRESHOLDS",
    "BAND_COLORS",
    "BAND_KEYS",
    "CLUSTER_STYLE",
    "DEFAULT_POINT_COLOR",
    "DEFAULT_SHAPE",
    "DERIVED_OPACITY",
    "DERIVED_STROKE_WIDTH",
    "DIVERGING_COLORS",
    "MODE_BANDS",
    "MODE_DIVERGING",
    "MODE_SEQUENTIAL",
    "NO_DATA_COLOR",
    "OVERRIDABLE",
    "RADIUS_RANGE",
    "SEQUENTIAL_CLASSES",
    "SEQUENTIAL_RAMP",
    "SHAPES",
    "SHAPE_KEYS",
    "SHAPE_VIEWBOX",
    "bands",
    "class_breaks",
    "color_mode",
    "default_style",
    "effective_style",
    "shape_catalogue",
]
