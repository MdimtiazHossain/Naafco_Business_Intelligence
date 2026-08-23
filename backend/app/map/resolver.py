"""Which design draws which entity, and the cache in front of the answer.

The priority the specification asks for is implemented as an ordering, not a
chain of conditionals::

    specific entity configuration   (entity_type + entity_code)
            ↓
    entity type configuration       (entity_type, entity_code IS NULL)
            ↓
    system default                  (is_system_default for the entity type)
            ↓
    built-in fallback               (a plain circle, so the map is never blank)

Only ``ACTIVE`` designs are ever served. A design that is deactivated stops
appearing on the map without its assignment having to be deleted, which is what
makes "deactivate" reversible.

The cache is keyed on a generation counter rather than on time. An administrator
who saves a design expects to see it, so a write bumps the generation and every
cached answer becomes unreachable at once — no stale window, no per-key
invalidation to get wrong.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database.models_map import (
    MapMarkerAsset,
    MapMarkerAssignment,
    MapMarkerDesign,
    MarkerDesignStatus,
)
from .adapters import adapt
from .entities import ENTITY_TYPES, get_entity_type
from .render import RenderedMarker, render
from .schemas import MarkerDefinition, parse_definition

SOURCE_ENTITY = "entity"
SOURCE_ENTITY_TYPE = "entity_type"
SOURCE_SYSTEM_DEFAULT = "system_default"
SOURCE_FALLBACK = "fallback"

#: Drawn when nothing at all has been configured. Deliberately unremarkable: a
#: map with no configuration should look plain, not broken.
FALLBACK_DEFINITION: dict[str, Any] = {
    "shape": {
        "kind": "builtin", "builtin": "circle", "fill": "#64748B",
        "stroke": "#FFFFFF", "stroke_width": 2, "size": 28,
    }
}


@dataclass
class ResolvedMarker:
    """One entity type's (or one entity's) effective marker."""

    entity_type: str
    entity_code: str | None
    design_id: int | None
    design_name: str
    source: str
    definition: MarkerDefinition
    rendered: RenderedMarker

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "entity_code": self.entity_code,
            "design_id": self.design_id,
            "design_name": self.design_name,
            "source": self.source,
            "marker": adapt(self.definition, self.rendered),
            "preview_svg": self.rendered.svg,
            "anchor": {"x": self.rendered.anchor_x, "y": self.rendered.anchor_y},
            "size": {"width": self.rendered.width, "height": self.rendered.height},
        }


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class _MarkerCache:
    """A tiny process-local cache with generation-based invalidation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 1
        self._entries: dict[tuple, Any] = {}

    @property
    def generation(self) -> int:
        return self._generation

    def get(self, key: tuple) -> Any | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            generation, value = entry
            return value if generation == self._generation else None

    def put(self, key: tuple, value: Any) -> None:
        with self._lock:
            # Bounded so a wide range of entity codes cannot grow it without
            # limit; markers are cheap to recompute.
            if len(self._entries) > 512:
                self._entries.clear()
            self._entries[key] = (self._generation, value)

    def invalidate(self) -> int:
        """Bump the generation. Every cached answer becomes unreachable."""
        with self._lock:
            self._generation += 1
            self._entries.clear()
            return self._generation


CACHE = _MarkerCache()


def invalidate_cache() -> int:
    """Called by every write path in the marker API."""
    return CACHE.invalidate()


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _asset_markup(session: Session, design: MapMarkerDesign) -> str | None:
    if design.asset_id is None:
        return None
    asset = session.get(MapMarkerAsset, design.asset_id)
    # Only SVG can be inlined; a raster asset is already a data URI.
    if asset is None:
        return None
    if asset.media_type == "image/svg+xml":
        return asset.content
    return (f'<image href="{asset.content}" x="0" y="0" width="100%" '
            f'height="100%" preserveAspectRatio="xMidYMid meet"/>')


def _render_design(session: Session, design: MapMarkerDesign,
                   context: dict[str, str] | None) -> tuple[MarkerDefinition,
                                                            RenderedMarker]:
    definition = parse_definition(design.definition)
    rendered = render(definition, context=context,
                      asset_markup=_asset_markup(session, design))
    return definition, rendered


def _fallback(entity_type: str, entity_code: str | None,
              context: dict[str, str] | None) -> ResolvedMarker:
    definition = parse_definition(FALLBACK_DEFINITION)
    return ResolvedMarker(
        entity_type=entity_type, entity_code=entity_code, design_id=None,
        design_name="System default", source=SOURCE_FALLBACK,
        definition=definition, rendered=render(definition, context=context),
    )


def resolve(session: Session, entity_type: str, entity_code: str | None = None,
            context: dict[str, str] | None = None) -> ResolvedMarker:
    """The effective marker for one entity type, or one specific entity."""
    get_entity_type(entity_type)                     # validates the key

    cache_key = ("resolve", entity_type, entity_code,
                 tuple(sorted((context or {}).items())))
    cached = CACHE.get(cache_key)
    if cached is not None:
        return cached

    assignment_rows = session.execute(
        select(MapMarkerAssignment, MapMarkerDesign)
        .join(MapMarkerDesign,
              MapMarkerDesign.design_id == MapMarkerAssignment.design_id)
        .where(
            MapMarkerAssignment.entity_type == entity_type,
            MapMarkerAssignment.is_active.is_(True),
            MapMarkerDesign.status == MarkerDesignStatus.ACTIVE,
        )
    ).all()

    specific = [
        (a, d) for a, d in assignment_rows
        if entity_code is not None and a.entity_code == entity_code
    ]
    type_level = [(a, d) for a, d in assignment_rows if a.entity_code is None]

    chosen: MapMarkerDesign | None = None
    source = SOURCE_FALLBACK
    if specific:
        chosen = max(specific, key=lambda pair: pair[0].priority)[1]
        source = SOURCE_ENTITY
    elif type_level:
        chosen = max(type_level, key=lambda pair: pair[0].priority)[1]
        source = SOURCE_ENTITY_TYPE
    else:
        chosen = session.execute(
            select(MapMarkerDesign).where(
                MapMarkerDesign.entity_type == entity_type,
                MapMarkerDesign.is_system_default.is_(True),
                MapMarkerDesign.status == MarkerDesignStatus.ACTIVE,
            ).order_by(MapMarkerDesign.design_id)
        ).scalars().first()
        if chosen is not None:
            source = SOURCE_SYSTEM_DEFAULT

    if chosen is None:
        result = _fallback(entity_type, entity_code, context)
    else:
        definition, rendered = _render_design(session, chosen, context)
        result = ResolvedMarker(
            entity_type=entity_type, entity_code=entity_code,
            design_id=chosen.design_id, design_name=chosen.name, source=source,
            definition=definition, rendered=rendered,
        )

    CACHE.put(cache_key, result)
    return result


def resolve_all(session: Session,
                context: dict[str, str] | None = None) -> list[ResolvedMarker]:
    """The effective marker for every entity type — what the map loads once."""
    return [resolve(session, entity.key, None, context) for entity in ENTITY_TYPES]


def legend(session: Session) -> list[dict[str, Any]]:
    """The map legend, built from the same resolution the map itself uses.

    Because it shares :func:`resolve`, a design change is reflected in the
    legend automatically — there is no second place where legend artwork lives.
    """
    entries = []
    for entity in ENTITY_TYPES:
        resolved = resolve(session, entity.key, None,
                           {"Name": entity.label, "Type": entity.label,
                            "Level": entity.label, "Code": entity.key.upper()})
        entries.append({
            "entity_type": entity.key,
            "label": entity.label,
            "group": entity.group,
            "promoted": entity.promoted,
            **resolved.to_dict(),
        })
    return entries


__all__ = [
    "ResolvedMarker",
    "resolve",
    "resolve_all",
    "legend",
    "invalidate_cache",
    "CACHE",
    "FALLBACK_DEFINITION",
    "SOURCE_ENTITY",
    "SOURCE_ENTITY_TYPE",
    "SOURCE_SYSTEM_DEFAULT",
    "SOURCE_FALLBACK",
]
