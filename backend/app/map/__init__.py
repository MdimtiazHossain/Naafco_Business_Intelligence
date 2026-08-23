"""Business-map configuration.

This package owns *how the map looks*, not the map itself. It stores marker and
shape designs, resolves which design an entity should be drawn with, renders
that design to SVG, and adapts the result for a map renderer.

The separation matters: the renderer is reached through one adapter
(:mod:`app.map.adapters`), so the Shape Builder and the stored designs know
nothing about any particular mapping SDK. The front-end consumes
``GET /api/map/marker-config`` and draws what it is given; marker definitions are
never duplicated in front-end source.

The front-end draws with MapLibre GL JS over an OpenFreeMap basemap and reads
administrative geometry from files served alongside it. Nothing in this package
knows that — it emits an SVG, a size and an anchor, which is what MapLibre's
``addImage`` takes and equally what a legend's ``<img>`` takes.
"""

from .entities import ENTITY_TYPES, ENTITY_TYPE_BY_KEY, EntityType, get_entity_type

__all__ = ["ENTITY_TYPES", "ENTITY_TYPE_BY_KEY", "EntityType", "get_entity_type"]
