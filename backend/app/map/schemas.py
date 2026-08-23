"""The marker definition contract.

A design's ``definition`` column is JSON, and this is what makes that safe: every
field is a Pydantic model with ``extra="forbid"``, colours must match a hex
pattern, sizes and opacities are bounded, and every free choice is an enum. A
definition that reaches the database has already been proved to be a drawing,
not a payload.

Two rules the model exists to enforce:

* **No arbitrary markup.** Label text is a template over a fixed set of
  placeholders (``{Name}``, ``{Code}``, ``{Type}``, ``{Level}``); anything
  outside that set is rejected, and the renderer XML-escapes what it emits. There
  is no path by which a design carries HTML or script into a page.
* **Bounded geometry.** A Shape Builder polygon has a point limit and a
  coordinate range, so a design cannot become a denial-of-service against the
  renderer or the browser.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .icons import ICON_KEYS
from .shapes import SHAPE_KEYS

#: ``#rgb``, ``#rrggbb`` or ``#rrggbbaa``. Named colours and ``rgb()`` are
#: excluded deliberately: one representation means one thing to validate.
HEX_COLOUR = r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$"

Colour = Annotated[str, Field(pattern=HEX_COLOUR)]

#: The only placeholders a label may contain.
LABEL_VARIABLES: tuple[str, ...] = ("{Name}", "{Code}", "{Type}", "{Level}")
_PLACEHOLDER = re.compile(r"\{([A-Za-z]+)\}")
#: Printable text, spaces and the placeholder braces. No angle brackets, no
#: ampersands, no quotes — the renderer escapes anyway, but a label has no
#: legitimate need for them and rejecting early gives a clearer error.
_LABEL_SAFE = re.compile(r"^[\w\s{}\-.,:/#()&'ঀ-৿]*$", re.UNICODE)

MIN_SIZE, MAX_SIZE = 8.0, 128.0
MAX_POLYGON_POINTS = 200
COORDINATE_LIMIT = 512.0


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


class ShadowConfig(Base):
    enabled: bool = False
    colour: Colour = "#00000040"
    blur: float = Field(3.0, ge=0, le=24)
    offset_x: float = Field(0.0, ge=-24, le=24)
    offset_y: float = Field(2.0, ge=-24, le=24)


class ShapeConfig(Base):
    """The marker's body."""

    #: ``builtin`` uses the shape catalogue; ``polygon`` and ``path`` come from
    #: the Shape Builder; ``image`` defers to an uploaded asset.
    kind: Literal["builtin", "polygon", "path", "image"] = "builtin"
    #: Defaults to a circle so a brand-new design is valid and drawable the
    #: moment the designer opens, rather than failing validation until the user
    #: happens to pick a shape.
    builtin: str | None = "circle"
    #: Shape Builder output, in the same origin-centred space as the catalogue.
    points: list[tuple[float, float]] = Field(default_factory=list)
    #: An SVG path ``d``. Validated for allowed commands only.
    path: str | None = None
    closed: bool = True

    fill: Colour = "#2563EB"
    stroke: Colour = "#FFFFFF"
    stroke_width: float = Field(2.0, ge=0, le=16)
    opacity: float = Field(1.0, ge=0, le=1)
    rotation: float = Field(0.0, ge=-360, le=360)
    size: float = Field(36.0, ge=MIN_SIZE, le=MAX_SIZE)
    shadow: ShadowConfig = Field(default_factory=ShadowConfig)

    @field_validator("builtin")
    @classmethod
    def _known_shape(cls, value: str | None) -> str | None:
        if value is not None and value not in SHAPE_KEYS:
            raise ValueError(
                f"Unknown shape '{value}'. Supported: {', '.join(SHAPE_KEYS)}."
            )
        return value

    @field_validator("points")
    @classmethod
    def _bounded_points(cls, value: list[tuple[float, float]]):
        if len(value) > MAX_POLYGON_POINTS:
            raise ValueError(
                f"A shape may have at most {MAX_POLYGON_POINTS} points; got {len(value)}."
            )
        for x, y in value:
            if abs(x) > COORDINATE_LIMIT or abs(y) > COORDINATE_LIMIT:
                raise ValueError(
                    f"Point ({x}, {y}) is outside the ±{COORDINATE_LIMIT:g} design area."
                )
        return value

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str | None) -> str | None:
        """An SVG path may contain path commands and numbers — nothing else."""
        if value is None:
            return None
        if len(value) > 20_000:
            raise ValueError("The path is too long for a marker.")
        if not re.fullmatch(r"[MmLlHhVvCcSsQqTtAaZz0-9eE ,.\-+\t\r\n]*", value):
            raise ValueError(
                "The path contains characters that are not SVG path commands or "
                "numbers."
            )
        return value.strip()

    @model_validator(mode="after")
    def _geometry_present(self) -> "ShapeConfig":
        if self.kind == "builtin" and not self.builtin:
            raise ValueError("A built-in shape must name which shape to draw.")
        if self.kind == "polygon" and len(self.points) < 3:
            raise ValueError("A polygon needs at least three points.")
        if self.kind == "path" and not self.path:
            raise ValueError("A path shape must carry a path definition.")
        return self


# ---------------------------------------------------------------------------
# Decoration
# ---------------------------------------------------------------------------


class IconConfig(Base):
    """A glyph drawn inside the marker."""

    enabled: bool = False
    #: ``library`` names a built-in icon; ``asset`` points at an upload.
    source: Literal["library", "asset"] = "library"
    name: str | None = None
    asset_id: int | None = None
    size: float = Field(16.0, ge=4, le=96)
    colour: Colour = "#FFFFFF"
    offset_x: float = Field(0.0, ge=-64, le=64)
    offset_y: float = Field(0.0, ge=-64, le=64)
    opacity: float = Field(1.0, ge=0, le=1)

    @field_validator("name")
    @classmethod
    def _known_icon(cls, value: str | None) -> str | None:
        if value is not None and value not in ICON_KEYS:
            raise ValueError(f"Unknown icon '{value}'.")
        return value

    @model_validator(mode="after")
    def _source_present(self) -> "IconConfig":
        if not self.enabled:
            return self
        if self.source == "library" and not self.name:
            raise ValueError("Choose an icon from the library.")
        if self.source == "asset" and self.asset_id is None:
            raise ValueError("Choose an uploaded icon.")
        return self


class LabelConfig(Base):
    """Optional text inside or beside the marker."""

    enabled: bool = False
    #: A template over :data:`LABEL_VARIABLES`, e.g. ``"{Code}"``.
    template: str = Field("{Code}", max_length=64)
    font_size: float = Field(11.0, ge=6, le=48)
    font_weight: Literal["normal", "medium", "semibold", "bold"] = "semibold"
    colour: Colour = "#FFFFFF"
    align: Literal["left", "center", "right"] = "center"
    position: Literal["inside", "above", "below", "left", "right"] = "below"
    offset_x: float = Field(0.0, ge=-64, le=64)
    offset_y: float = Field(0.0, ge=-64, le=64)
    #: Drawn behind the text so a label stays readable over any basemap.
    halo: bool = True
    halo_colour: Colour = "#000000"

    @field_validator("template")
    @classmethod
    def _known_variables(cls, value: str) -> str:
        if not _LABEL_SAFE.match(value):
            raise ValueError(
                "The label may contain letters, numbers, spaces, simple "
                "punctuation and the supported variables only."
            )
        allowed = {v.strip("{}") for v in LABEL_VARIABLES}
        unknown = [f"{{{n}}}" for n in _PLACEHOLDER.findall(value) if n not in allowed]
        if unknown:
            raise ValueError(
                f"Unknown label variable(s) {', '.join(unknown)}. Supported: "
                + ", ".join(LABEL_VARIABLES)
            )
        return value


class BadgeConfig(Base):
    """A small status dot or pill on the marker.

    ``kind`` names *what the badge means*, not what it currently says. Nothing in
    this phase evaluates the business condition — the map front-end supplies a
    value and the renderer draws it — but naming the meaning here is what lets
    conditional styling be added later without a schema change.
    """

    enabled: bool = False
    kind: Literal["active", "inactive", "alert", "outstanding", "low_stock",
                  "performance", "custom"] = "active"
    position: Literal["top-left", "top-right", "bottom-left", "bottom-right"] = \
        "top-right"
    fill: Colour = "#16A34A"
    stroke: Colour = "#FFFFFF"
    size: float = Field(10.0, ge=4, le=32)
    #: Optional short text, e.g. a count. Same escaping rules as a label.
    text: str | None = Field(None, max_length=4)

    @field_validator("text")
    @classmethod
    def _safe_text(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[\w!?+%.\-]{0,4}", value):
            raise ValueError("Badge text may be up to 4 letters, digits or +!?%.-")
        return value


class BackgroundConfig(Base):
    """An optional plate behind the whole marker."""

    enabled: bool = False
    fill: Colour = "#FFFFFF"
    stroke: Colour = "#CBD5E1"
    stroke_width: float = Field(1.0, ge=0, le=8)
    corner_radius: float = Field(6.0, ge=0, le=32)
    padding: float = Field(4.0, ge=0, le=32)
    opacity: float = Field(1.0, ge=0, le=1)


class MarkerDefinition(Base):
    """A complete marker: body, glyph, label, badge and plate."""

    shape: ShapeConfig = Field(default_factory=ShapeConfig)
    icon: IconConfig = Field(default_factory=IconConfig)
    label: LabelConfig = Field(default_factory=LabelConfig)
    badge: BadgeConfig = Field(default_factory=BadgeConfig)
    background: BackgroundConfig = Field(default_factory=BackgroundConfig)
    #: Free-text note kept with the design; never rendered into the SVG.
    notes: str | None = Field(None, max_length=500)

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def parse_definition(payload: dict[str, Any] | None) -> MarkerDefinition:
    """Validate a stored or submitted definition."""
    return MarkerDefinition.model_validate(payload or {})


__all__ = [
    "MarkerDefinition",
    "ShapeConfig",
    "IconConfig",
    "LabelConfig",
    "BadgeConfig",
    "BackgroundConfig",
    "ShadowConfig",
    "LABEL_VARIABLES",
    "HEX_COLOUR",
    "MIN_SIZE",
    "MAX_SIZE",
    "MAX_POLYGON_POINTS",
    "parse_definition",
]
