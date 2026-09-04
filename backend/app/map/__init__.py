"""The business map, rebuilt after ``0033_remove_map``.

One map, many layers. Each layer names a business level — zone, region, area,
unit, territory, sub-territory or customer — and the same renderer draws all
of them, so there is no per-level code anywhere in this package.

* :mod:`levels` — the levels a layer may name, derived from the organisational
  chain rather than restated, and the view modes each can honour;
* :mod:`metrics` — the measures a layer may draw, declared once;
* :mod:`styles` — how a figure becomes a colour and a size, declared once and
  overridable per layer;
* :mod:`basemaps` — the basemaps a design may name, from configuration;
* :mod:`designs` — composing a map: designs, layers and the rules that keep
  a saved design drawable; :mod:`errors` is what it refuses with;
* :mod:`geo` — coordinates: validating, storing, deriving and bounding them;
* :mod:`data` — one level's entities with their measures and positions, as
  GeoJSON, through ``get_map_layer`` and the permission filter;
* :mod:`entities` — one entity by name: its ancestry and its coordinate, for
  the card the map shows when a point is selected.

Aggregation, scope and permission are **not** here. A map figure is produced by
the same tools every report calls, which is what makes a region's sales on the
map equal a region's sales on the Sales page by construction.
"""

from . import basemaps, data, designs, entities, errors, geo, levels, metrics, styles

__all__ = ["basemaps", "data", "designs", "entities", "errors", "geo", "levels",
           "metrics", "styles"]
