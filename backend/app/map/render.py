"""Rendering a marker definition to SVG.

This is the single source of truth for what a marker looks like. The designer
preview, the library thumbnail, the map legend and the marker the map draws all
come from this function, so they cannot disagree — a class of bug that is
otherwise guaranteed once two implementations exist.

Everything emitted here is constructed from validated values. Text is
XML-escaped on the way out even though :mod:`app.map.schemas` has already
rejected markup, because defence that depends on a single upstream check is
defence that breaks the first time someone adds a second entry point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from .icons import ICON_VIEWBOX, get_icon
from .schemas import LabelConfig, MarkerDefinition
from .shapes import get_shape, points_to_path

#: Padding around the marker body so a stroke, shadow or badge is not clipped.
CANVAS_MARGIN = 10.0
#: Rough width per character at font-size 1, used to size the label box.
_CHAR_WIDTH = 0.58

_FONT_WEIGHTS = {"normal": "400", "medium": "500", "semibold": "600", "bold": "700"}
_ANCHORS = {"left": "start", "center": "middle", "right": "end"}

#: Sample values used when a preview has no real entity behind it.
SAMPLE_CONTEXT: dict[str, str] = {
    "Name": "Sample", "Code": "T001", "Type": "Territory", "Level": "Territory",
}


@dataclass
class RenderedMarker:
    """A rendered marker and the geometry a map needs to place it."""

    svg: str
    width: float
    height: float
    #: Pixel offset from the top-left of the *image* to the point on the map.
    #: This is what an image-based marker needs.
    anchor_x: float
    anchor_y: float
    #: The same point in the *path's* own origin-centred coordinates. A vector
    #: symbol is positioned by its path, not by an image box, so it needs this
    #: one instead — ``(0, 0)`` for a circle, ``(0, +size/2)`` for a pin's tip.
    path_anchor_x: float
    path_anchor_y: float
    #: True when the design is a plain vector body with no decoration, so a map
    #: renderer can use its cheap native vector path instead of an image.
    vector_only: bool
    #: The body path in marker-local coordinates, when ``vector_only``.
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "svg": self.svg,
            "width": self.width,
            "height": self.height,
            "anchor": {"x": self.anchor_x, "y": self.anchor_y},
            "path_anchor": {"x": self.path_anchor_x, "y": self.path_anchor_y},
            "vector_only": self.vector_only,
            "path": self.path,
        }


def resolve_label(config: LabelConfig, context: dict[str, str] | None = None) -> str:
    """Substitute ``{Name}`` / ``{Code}`` / ``{Type}`` / ``{Level}``.

    Substitution happens **after** validation and before escaping, so a value
    coming from the warehouse (a customer name, say) is treated as data even
    though the template was authored by an administrator.
    """
    values = {**SAMPLE_CONTEXT, **(context or {})}
    text = config.template
    for key, value in values.items():
        text = text.replace(f"{{{key}}}", str(value))
    return text


def _body_path(definition: MarkerDefinition) -> str:
    """The shape's outline in origin-centred coordinates."""
    shape = definition.shape
    if shape.kind == "builtin" and shape.builtin:
        return get_shape(shape.builtin).to_path(shape.size)
    if shape.kind == "polygon":
        return points_to_path([(x, y) for x, y in shape.points], close=shape.closed)
    if shape.kind == "path" and shape.path:
        return shape.path
    return ""


def _body_anchor(definition: MarkerDefinition) -> tuple[float, float]:
    """Where the marker touches the map, in origin-centred coordinates."""
    shape = definition.shape
    if shape.kind == "builtin" and shape.builtin:
        fraction_x, fraction_y = get_shape(shape.builtin).anchor
        # The shape box spans -size/2 .. +size/2 in both axes.
        return (
            (fraction_x - 0.5) * shape.size,
            (fraction_y - 0.5) * shape.size,
        )
    return (0.0, 0.0)


def _label_extent(definition: MarkerDefinition) -> tuple[float, float, float, float]:
    """``(min_x, min_y, max_x, max_y)`` the label occupies, or zeros."""
    label = definition.label
    if not label.enabled:
        return (0, 0, 0, 0)
    text = resolve_label(label)
    width = max(len(text), 1) * label.font_size * _CHAR_WIDTH
    height = label.font_size * 1.3
    x, y = _label_origin(definition)
    half = width / 2
    if label.align == "left":
        return (x, y - height, x + width, y)
    if label.align == "right":
        return (x - width, y - height, x, y)
    return (x - half, y - height, x + half, y)


def _label_origin(definition: MarkerDefinition) -> tuple[float, float]:
    label = definition.label
    half = definition.shape.size / 2
    gap = label.font_size * 0.55
    positions = {
        "inside": (0.0, label.font_size * 0.35),
        "above": (0.0, -half - gap),
        "below": (0.0, half + label.font_size + gap * 0.4),
        "left": (-half - gap, label.font_size * 0.35),
        "right": (half + gap, label.font_size * 0.35),
    }
    x, y = positions[label.position]
    return (x + label.offset_x, y + label.offset_y)


def _badge_centre(definition: MarkerDefinition) -> tuple[float, float]:
    half = definition.shape.size / 2
    offset = definition.badge.size * 0.35
    corners = {
        "top-left": (-half + offset, -half + offset),
        "top-right": (half - offset, -half + offset),
        "bottom-left": (-half + offset, half - offset),
        "bottom-right": (half - offset, half - offset),
    }
    return corners[definition.badge.position]


def is_vector_only(definition: MarkerDefinition) -> bool:
    """True when the marker is a bare shape a map can draw as a native symbol."""
    return not (
        definition.icon.enabled
        or definition.label.enabled
        or definition.badge.enabled
        or definition.background.enabled
        or definition.shape.shadow.enabled
        or definition.shape.kind == "image"
    )


def render(definition: MarkerDefinition, *, context: dict[str, str] | None = None,
           asset_markup: str | None = None) -> RenderedMarker:
    """Render one marker definition to a standalone SVG document.

    ``asset_markup`` is the **already sanitised** content of an uploaded image
    for a ``CUSTOM_IMAGE`` design. This function never sanitises: it requires
    that the caller has, so there is one place where that responsibility sits.
    """
    shape = definition.shape
    half = shape.size / 2

    # --- extent -----------------------------------------------------------
    min_x, min_y = -half, -half
    max_x, max_y = half, half
    if definition.shape.kind == "polygon" and shape.points:
        xs = [p[0] for p in shape.points]
        ys = [p[1] for p in shape.points]
        min_x, max_x = min(min_x, *xs), max(max_x, *xs)
        min_y, max_y = min(min_y, *ys), max(max_y, *ys)
    label_box = _label_extent(definition)
    if definition.label.enabled:
        min_x, min_y = min(min_x, label_box[0]), min(min_y, label_box[1])
        max_x, max_y = max(max_x, label_box[2]), max(max_y, label_box[3])
    if definition.badge.enabled:
        bx, by = _badge_centre(definition)
        radius = definition.badge.size / 2 + definition.shape.stroke_width
        min_x, min_y = min(min_x, bx - radius), min(min_y, by - radius)
        max_x, max_y = max(max_x, bx + radius), max(max_y, by + radius)

    margin = CANVAS_MARGIN + shape.stroke_width
    min_x, min_y = min_x - margin, min_y - margin
    max_x, max_y = max_x + margin, max_y + margin
    width, height = max_x - min_x, max_y - min_y

    anchor_dx, anchor_dy = _body_anchor(definition)
    anchor_x = anchor_dx - min_x
    anchor_y = anchor_dy - min_y

    # --- body -------------------------------------------------------------
    parts: list[str] = []
    defs: list[str] = []

    if definition.shape.shadow.enabled:
        shadow = definition.shape.shadow
        defs.append(
            f'<filter id="mk-shadow" x="-50%" y="-50%" width="200%" height="200%">'
            f'<feDropShadow dx="{_n(shadow.offset_x)}" dy="{_n(shadow.offset_y)}" '
            f'stdDeviation="{_n(shadow.blur / 2)}" '
            f'flood-color="{escape(shadow.colour)}"/></filter>'
        )

    if definition.background.enabled:
        bg = definition.background
        pad = bg.padding
        parts.append(
            f'<rect x="{_n(min_x + margin - pad)}" y="{_n(min_y + margin - pad)}" '
            f'width="{_n(width - 2 * (margin - pad))}" '
            f'height="{_n(height - 2 * (margin - pad))}" '
            f'rx="{_n(bg.corner_radius)}" fill="{escape(bg.fill)}" '
            f'stroke="{escape(bg.stroke)}" stroke-width="{_n(bg.stroke_width)}" '
            f'opacity="{_n(bg.opacity)}"/>'
        )

    transform = f"rotate({_n(shape.rotation)})" if shape.rotation else ""
    filter_attr = ' filter="url(#mk-shadow)"' if definition.shape.shadow.enabled else ""

    if shape.kind == "image":
        # An uploaded asset is inlined, scaled into the shape's box. It has
        # already been sanitised; nothing here can reintroduce script.
        inner = asset_markup or ""
        parts.append(
            f'<g transform="translate({_n(-half)} {_n(-half)})"{filter_attr}>'
            f'<svg x="0" y="0" width="{_n(shape.size)}" height="{_n(shape.size)}" '
            f'opacity="{_n(shape.opacity)}" preserveAspectRatio="xMidYMid meet">'
            f'{inner}</svg></g>'
        )
    else:
        path = _body_path(definition)
        if path:
            attrs = (
                f'd="{escape(path)}" fill="{escape(shape.fill)}" '
                f'fill-opacity="{_n(shape.opacity)}" stroke="{escape(shape.stroke)}" '
                f'stroke-width="{_n(shape.stroke_width)}" stroke-linejoin="round"'
            )
            group = f' transform={quoteattr(transform)}' if transform else ""
            parts.append(f'<g{group}{filter_attr}><path {attrs}/></g>')

    # --- icon -------------------------------------------------------------
    if definition.icon.enabled:
        icon = definition.icon
        if icon.source == "library" and icon.name:
            glyph = get_icon(icon.name)
            scale = icon.size / ICON_VIEWBOX
            tx = icon.offset_x - icon.size / 2
            ty = icon.offset_y - icon.size / 2
            stroke = (f'fill="none" stroke="{escape(icon.colour)}" stroke-width="2" '
                      f'stroke-linecap="round" stroke-linejoin="round"'
                      if glyph.stroked else f'fill="{escape(icon.colour)}"')
            parts.append(
                f'<g transform="translate({_n(tx)} {_n(ty)}) scale({_n(scale)})" '
                f'opacity="{_n(icon.opacity)}"><path d="{escape(glyph.path)}" '
                f'{stroke}/></g>'
            )
        elif icon.source == "asset" and asset_markup:
            tx = icon.offset_x - icon.size / 2
            ty = icon.offset_y - icon.size / 2
            parts.append(
                f'<svg x="{_n(tx)}" y="{_n(ty)}" width="{_n(icon.size)}" '
                f'height="{_n(icon.size)}" opacity="{_n(icon.opacity)}" '
                f'preserveAspectRatio="xMidYMid meet">{asset_markup}</svg>'
            )

    # --- label ------------------------------------------------------------
    if definition.label.enabled:
        label = definition.label
        text = escape(resolve_label(label, context))
        lx, ly = _label_origin(definition)
        common = (
            f'x="{_n(lx)}" y="{_n(ly)}" '
            f'font-family="Segoe UI, Roboto, Helvetica, Arial, sans-serif" '
            f'font-size="{_n(label.font_size)}" '
            f'font-weight="{_FONT_WEIGHTS[label.font_weight]}" '
            f'text-anchor="{_ANCHORS[label.align]}"'
        )
        if label.halo:
            # Drawn twice: a thick stroke underneath keeps the text legible over
            # any basemap without needing a solid plate behind it.
            parts.append(
                f'<text {common} fill="none" stroke="{escape(label.halo_colour)}" '
                f'stroke-width="3" stroke-linejoin="round" opacity="0.65">{text}</text>'
            )
        parts.append(f'<text {common} fill="{escape(label.colour)}">{text}</text>')

    # --- badge ------------------------------------------------------------
    if definition.badge.enabled:
        badge = definition.badge
        bx, by = _badge_centre(definition)
        parts.append(
            f'<circle cx="{_n(bx)}" cy="{_n(by)}" r="{_n(badge.size / 2)}" '
            f'fill="{escape(badge.fill)}" stroke="{escape(badge.stroke)}" '
            f'stroke-width="1.5"/>'
        )
        if badge.text:
            parts.append(
                f'<text x="{_n(bx)}" y="{_n(by + badge.size * 0.32)}" '
                f'font-family="Segoe UI, Roboto, Helvetica, Arial, sans-serif" '
                f'font-size="{_n(badge.size * 0.8)}" font-weight="700" '
                f'text-anchor="middle" fill="{escape(badge.stroke)}">'
                f'{escape(badge.text)}</text>'
            )

    defs_markup = f"<defs>{''.join(defs)}</defs>" if defs else ""
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{_n(min_x)} {_n(min_y)} {_n(width)} {_n(height)}" '
        f'width="{_n(width)}" height="{_n(height)}" role="img">'
        f'{defs_markup}{"".join(parts)}</svg>'
    )

    vector = is_vector_only(definition)
    return RenderedMarker(
        svg=svg, width=width, height=height,
        anchor_x=anchor_x, anchor_y=anchor_y,
        path_anchor_x=anchor_dx, path_anchor_y=anchor_dy,
        vector_only=vector,
        path=_body_path(definition) if vector else None,
    )


def _n(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


__all__ = ["render", "RenderedMarker", "resolve_label", "is_vector_only",
           "SAMPLE_CONTEXT", "CANVAS_MARGIN"]
