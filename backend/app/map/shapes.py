"""The built-in shape catalogue.

Every shape is a **vector generator**: given a size it returns points (or an SVG
path) in a coordinate system centred on ``(0, 0)``, with ``+y`` downward as SVG
expects. Keeping geometry on the server means the designer preview, the map
legend and the rendered marker are produced by one function and cannot disagree.

Anchor points matter. A circle is anchored at its centre; a pin is anchored at
its *tip*, because a pin that floats above the place it marks is wrong. Each
shape declares its own anchor so the map adapter does not have to guess.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

Point = tuple[float, float]


@dataclass(frozen=True)
class BuiltinShape:
    """One parameterised shape."""

    key: str
    label: str
    #: ``(size) -> [(x, y), ...]`` in a box of ``size`` centred on the origin.
    points: Callable[[float], list[Point]] | None
    #: ``(size) -> "M … Z"`` for shapes a polygon cannot express.
    path: Callable[[float], str] | None = None
    #: Where the marker touches the map, as a fraction of the bounding box.
    #: ``(0.5, 0.5)`` is the centre; ``(0.5, 1.0)`` is the bottom edge.
    anchor: tuple[float, float] = (0.5, 0.5)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "anchor": list(self.anchor),
            "description": self.description,
            # A sample outline at a fixed size, so the shape picker can draw
            # every option without reimplementing the geometry.
            "sample": [list(p) for p in self.outline(40.0)],
            "sample_path": self.to_path(40.0),
        }

    def outline(self, size: float) -> list[Point]:
        """The shape's points at ``size``. Empty for path-only shapes."""
        return self.points(size) if self.points else []

    def to_path(self, size: float) -> str:
        """The shape as an SVG path string."""
        if self.path:
            return self.path(size)
        return points_to_path(self.outline(size))


def points_to_path(points: list[Point], close: bool = True) -> str:
    """``[(x, y), …]`` -> ``"M x y L x y … Z"`` with bounded precision."""
    if not points:
        return ""
    parts = [f"M {_n(points[0][0])} {_n(points[0][1])}"]
    parts += [f"L {_n(x)} {_n(y)}" for x, y in points[1:]]
    if close:
        parts.append("Z")
    return " ".join(parts)


def _n(value: float) -> str:
    """Three decimals is well under a pixel at any sane marker size."""
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def _regular_polygon(sides: int, rotation_deg: float = -90.0):
    """A regular polygon inscribed in a circle of radius ``size / 2``."""

    def build(size: float) -> list[Point]:
        radius = size / 2
        offset = math.radians(rotation_deg)
        return [
            (radius * math.cos(offset + 2 * math.pi * i / sides),
             radius * math.sin(offset + 2 * math.pi * i / sides))
            for i in range(sides)
        ]

    return build


def star_points(size: float, points: int = 5, inner_ratio: float = 0.4) -> list[Point]:
    """A star, alternating between an outer and an inner radius.

    Exposed by name because the Shape Builder offers "Create Star" and must
    produce exactly the geometry the built-in star uses.
    """
    outer = size / 2
    inner = outer * inner_ratio
    result: list[Point] = []
    for index in range(points * 2):
        radius = outer if index % 2 == 0 else inner
        angle = math.radians(-90) + math.pi * index / points
        result.append((radius * math.cos(angle), radius * math.sin(angle)))
    return result


def _circle_path(size: float) -> str:
    """Two arcs, because an SVG path has no circle primitive."""
    r = size / 2
    return (f"M {_n(-r)} 0 A {_n(r)} {_n(r)} 0 1 0 {_n(r)} 0 "
            f"A {_n(r)} {_n(r)} 0 1 0 {_n(-r)} 0 Z")


def _rounded_square_path(size: float) -> str:
    half = size / 2
    radius = size * 0.22
    return (
        f"M {_n(-half + radius)} {_n(-half)} "
        f"L {_n(half - radius)} {_n(-half)} "
        f"Q {_n(half)} {_n(-half)} {_n(half)} {_n(-half + radius)} "
        f"L {_n(half)} {_n(half - radius)} "
        f"Q {_n(half)} {_n(half)} {_n(half - radius)} {_n(half)} "
        f"L {_n(-half + radius)} {_n(half)} "
        f"Q {_n(-half)} {_n(half)} {_n(-half)} {_n(half - radius)} "
        f"L {_n(-half)} {_n(-half + radius)} "
        f"Q {_n(-half)} {_n(-half)} {_n(-half + radius)} {_n(-half)} Z"
    )


def _pin_path(size: float) -> str:
    """A teardrop pin whose tip sits at the bottom of the bounding box."""
    half = size / 2
    r = size * 0.34
    cy = -half + r
    tip = half
    return (
        f"M 0 {_n(tip)} "
        f"C {_n(-r * 0.9)} {_n(cy + r * 1.15)} {_n(-r)} {_n(cy + r * 0.55)} "
        f"{_n(-r)} {_n(cy)} "
        f"A {_n(r)} {_n(r)} 0 1 1 {_n(r)} {_n(cy)} "
        f"C {_n(r)} {_n(cy + r * 0.55)} {_n(r * 0.9)} {_n(cy + r * 1.15)} "
        f"0 {_n(tip)} Z"
    )


def _flag_points(size: float) -> list[Point]:
    half = size / 2
    return [
        (-half * 0.55, -half), (half * 0.85, -half * 0.62),
        (-half * 0.55, -half * 0.24), (-half * 0.55, half),
        (-half * 0.8, half), (-half * 0.8, -half),
    ]


def _arrow_points(size: float) -> list[Point]:
    half = size / 2
    return [
        (0, -half), (half, half * 0.35), (half * 0.3, half * 0.35),
        (half * 0.3, half), (-half * 0.3, half), (-half * 0.3, half * 0.35),
        (-half, half * 0.35),
    ]


def _cross_points(size: float) -> list[Point]:
    half = size / 2
    arm = size * 0.18
    return [
        (-arm, -half), (arm, -half), (arm, -arm), (half, -arm), (half, arm),
        (arm, arm), (arm, half), (-arm, half), (-arm, arm), (-half, arm),
        (-half, -arm), (-arm, -arm),
    ]


#: The catalogue, in the order the spec lists it.
BUILTIN_SHAPES: tuple[BuiltinShape, ...] = (
    BuiltinShape("circle", "Circle", None, _circle_path,
                 description="The neutral default; reads clearly at any zoom."),
    BuiltinShape("square", "Square",
                 lambda s: [(-s / 2, -s / 2), (s / 2, -s / 2), (s / 2, s / 2),
                            (-s / 2, s / 2)]),
    BuiltinShape("rounded_square", "Rounded Square", None, _rounded_square_path),
    BuiltinShape("triangle", "Triangle", _regular_polygon(3)),
    BuiltinShape("diamond", "Diamond", _regular_polygon(4, rotation_deg=-90)),
    BuiltinShape("pentagon", "Pentagon", _regular_polygon(5)),
    BuiltinShape("hexagon", "Hexagon", _regular_polygon(6)),
    BuiltinShape("star", "Star", star_points),
    BuiltinShape("pin", "Pin", None, _pin_path, anchor=(0.5, 1.0),
                 description="Anchored at the tip, so it points at the location."),
    BuiltinShape("flag", "Flag", _flag_points, anchor=(0.28, 1.0),
                 description="Anchored at the foot of the pole."),
    BuiltinShape("arrow", "Arrow", _arrow_points),
    BuiltinShape("cross", "Cross", _cross_points),
)

SHAPE_BY_KEY: dict[str, BuiltinShape] = {s.key: s for s in BUILTIN_SHAPES}
SHAPE_KEYS: tuple[str, ...] = tuple(SHAPE_BY_KEY)


def get_shape(key: str) -> BuiltinShape:
    shape = SHAPE_BY_KEY.get(key)
    if shape is None:
        raise ValueError(
            f"Unknown shape {key!r}. Supported: {', '.join(SHAPE_KEYS)}."
        )
    return shape


def catalogue() -> list[dict[str, Any]]:
    return [shape.to_dict() for shape in BUILTIN_SHAPES]


__all__ = [
    "BuiltinShape",
    "BUILTIN_SHAPES",
    "SHAPE_BY_KEY",
    "SHAPE_KEYS",
    "Point",
    "get_shape",
    "catalogue",
    "points_to_path",
    "star_points",
]
