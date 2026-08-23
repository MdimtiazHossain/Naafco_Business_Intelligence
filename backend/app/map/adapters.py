"""Map-renderer adapters.

The Shape Builder, the stored definitions and :mod:`app.map.render` know nothing
about any mapping SDK. This module is the only place that does, so replacing the
renderer is one new adapter rather than a change to the designer.

There is currently **one** adapter, and that is the point of the MapLibre
rebuild. The previous implementation carried two — a Google Maps one emitting
``google.maps.Symbol``/``Icon`` object graphs, and this renderer-neutral one for
the fallback canvas renderer — which meant every marker had two payload shapes to
keep in step and every endpoint a ``renderer`` parameter to thread through. One
engine needs one payload.

The payload is the rendered SVG, its size and its anchor. MapLibre consumes it
through ``addImage`` and draws the whole layer as one symbol layer on the GPU;
the same three fields serve a legend's ``<img>`` and the designer's preview, so
what an administrator designs is exactly what the map draws.

The payload never contains a URL to this or any other server, so a map can draw
its entire marker set from one configuration response with no follow-up requests.
"""

from __future__ import annotations

import base64
from typing import Any, Protocol

from .render import RenderedMarker
from .schemas import MarkerDefinition

#: The only adapter key. Kept as a named constant rather than inlined so that a
#: second renderer, if one is ever needed, has an obvious place to register.
GENERIC_SVG = "generic"

ADAPTERS: tuple[str, ...] = (GENERIC_SVG,)


class MarkerAdapter(Protocol):
    """What every renderer adapter must provide."""

    key: str

    def marker(self, definition: MarkerDefinition,
               rendered: RenderedMarker) -> dict[str, Any]:
        """Renderer-specific payload for one marker."""


def _data_uri(svg: str) -> str:
    """An SVG as a base64 data URI.

    Base64 rather than percent-encoding because the payload travels through JSON
    and then into an image URL; base64 has no characters either layer needs to
    escape, so it cannot be corrupted in transit.
    """
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


class GenericSvgAdapter:
    """Renderer-neutral payload: the SVG, its size and its anchor.

    Enough for MapLibre's ``addImage``, a canvas renderer, or a plain ``<img>``
    in a legend. ``path`` and ``vector_only`` are still emitted because the
    designer's own preview uses them to show whether a shape stayed a pure
    vector, which is what keeps a marker cheap to draw.
    """

    key = GENERIC_SVG

    def marker(self, definition: MarkerDefinition,
               rendered: RenderedMarker) -> dict[str, Any]:
        return {
            "kind": "svg",
            "svg": rendered.svg,
            "data_uri": _data_uri(rendered.svg),
            "size": {"width": rendered.width, "height": rendered.height},
            "anchor": {"x": rendered.anchor_x, "y": rendered.anchor_y},
            "vector_only": rendered.vector_only,
            "path": rendered.path,
        }


_ADAPTER_BY_KEY: dict[str, MarkerAdapter] = {
    GENERIC_SVG: GenericSvgAdapter(),
}


def get_adapter(key: str | None) -> MarkerAdapter:
    """Look up an adapter, defaulting to the renderer-neutral one."""
    if key is None:
        return _ADAPTER_BY_KEY[GENERIC_SVG]
    adapter = _ADAPTER_BY_KEY.get(key)
    if adapter is None:
        raise ValueError(
            f"Unknown map renderer {key!r}. Supported: {', '.join(ADAPTERS)}."
        )
    return adapter


def adapt(definition: MarkerDefinition, rendered: RenderedMarker,
          renderer: str | None = None) -> dict[str, Any]:
    """Render one marker for a named renderer."""
    return get_adapter(renderer).marker(definition, rendered)


__all__ = [
    "MarkerAdapter",
    "GenericSvgAdapter",
    "ADAPTERS",
    "GENERIC_SVG",
    "get_adapter",
    "adapt",
]
