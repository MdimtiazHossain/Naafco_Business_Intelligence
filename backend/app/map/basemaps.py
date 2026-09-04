"""The basemaps a design may name, read from configuration rather than code.

MapLibre draws business data over a *style*: a JSON document naming tile
sources, fonts and the layers that turn them into a map. Which style is the
deployment's decision — OpenFreeMap by default, because it serves the
OpenStreetMap basemap with no key, no token and no per-view billing — and it is
read from settings here, once, so changing provider is an environment change
and never an edit to the renderer.

Two basemaps can exist. ``standard`` always does, with a light and a dark style
so the map follows the application theme. ``satellite`` exists only when a
deployment configures a provider for it: none ships by default, because no free
satellite provider has terms this platform can promise to keep, and a Satellite
button that fails is worse than no button.

A configured URL is either a style document or a raster tile template — the
latter recognised by ``{z}``/``{x}``/``{y}`` in it — and the catalogue says
which, so the browser wraps a template in the one-source style MapLibre needs
rather than being handed a URL it cannot parse.

**Attribution is never dropped.** An OpenFreeMap style carries its own credit
inside its TileJSON and MapLibre renders it; ``MAP_ATTRIBUTION`` is *added* to
that for a provider whose tiles state none, and nothing here can remove what a
style declares.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..config import Settings, get_settings

logger = logging.getLogger(__name__)

STANDARD = "standard"
SATELLITE = "satellite"

#: A MapLibre style document, or a raster ``{z}/{x}/{y}`` tile template.
KIND_STYLE = "style"
KIND_RASTER = "raster"

#: What a design that names no basemap, or one the deployment no longer
#: configures, is drawn on.
DEFAULT_BASEMAP = STANDARD


@dataclass(frozen=True)
class Basemap:
    """One basemap the map may draw business data over."""

    key: str
    label: str
    style_url: str
    #: The style for the dark theme, or ``None`` to draw the same one in both.
    style_url_dark: str | None
    kind: str
    #: Extra credit for the attribution control; the style's own stays.
    attribution: str | None
    #: The glyph source a raster basemap draws labels with; a style document
    #: carries its own and ignores this.
    glyphs_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "style_url": self.style_url,
            "style_url_dark": self.style_url_dark,
            "kind": self.kind,
            "attribution": self.attribution,
            "glyphs_url": self.glyphs_url,
        }


def classify(url: str) -> str:
    """Whether a configured URL is a style document or a raster template."""
    return (KIND_RASTER
            if "{z}" in url and "{x}" in url and "{y}" in url
            else KIND_STYLE)


def catalogue(settings: Settings | None = None) -> tuple[Basemap, ...]:
    """Every basemap the deployment offers, the standard one first."""
    settings = settings or get_settings()
    kind = classify(settings.map_style_url)
    dark: str | None = settings.map_style_url_dark or None
    if dark is not None and classify(dark) != kind:
        # One basemap is one kind. A style document in the light theme and a
        # raster template in the dark one would make the browser rebuild the
        # map on every theme switch; the light URL is the one a design is read
        # against, so the mismatched dark one is set aside and said so.
        logger.warning(
            "MAP_STYLE_URL_DARK is a %s and MAP_STYLE_URL a %s; the dark theme "
            "will draw the light basemap.", classify(dark), kind,
        )
        dark = None
    glyphs = settings.map_glyphs_url or None
    basemaps = [Basemap(
        key=STANDARD, label="Map", style_url=settings.map_style_url,
        style_url_dark=dark, kind=kind,
        attribution=settings.map_attribution or None, glyphs_url=glyphs,
    )]
    if settings.map_style_url_satellite:
        basemaps.append(Basemap(
            key=SATELLITE, label="Satellite",
            style_url=settings.map_style_url_satellite, style_url_dark=None,
            kind=classify(settings.map_style_url_satellite),
            attribution=settings.map_attribution_satellite or None,
            glyphs_url=glyphs,
        ))
    return tuple(basemaps)


def resolve(key: str | None, settings: Settings | None = None
            ) -> tuple[Basemap, str | None]:
    """The basemap a design named, or the standard one with a note saying why.

    A design outlives the configuration it was saved under: one that named
    ``satellite`` must still open after the satellite provider is withdrawn.
    Falling back silently would hide that the design no longer draws what it
    says, so the substitution is returned beside the basemap for the page to
    show.
    """
    available = {basemap.key: basemap for basemap in catalogue(settings)}
    if key and key in available:
        return available[key], None
    standard = available[STANDARD]
    if not key:
        return standard, None
    return standard, (
        f"The '{key}' basemap is not configured for this deployment; the "
        f"standard basemap is drawn instead."
    )


def default_view(settings: Settings | None = None) -> dict[str, float]:
    """Where the map opens before any layer has answered."""
    settings = settings or get_settings()
    return {
        "latitude": settings.map_default_latitude,
        "longitude": settings.map_default_longitude,
        "zoom": settings.map_default_zoom,
    }


__all__ = [
    "Basemap",
    "DEFAULT_BASEMAP",
    "KIND_RASTER",
    "KIND_STYLE",
    "SATELLITE",
    "STANDARD",
    "catalogue",
    "classify",
    "default_view",
    "resolve",
]
