"""One entity by name: where it sits in the hierarchy, and where it is.

The card the map shows for a selected point reads "Dhanmondi ST-01 · Dhaka
Zone · Dhaka · Mirpur" — the entity's name and the chain above it. The figures
on that card are the point's own properties, already on the map; what the
browser cannot know is the *names* of the levels above, because a feature
carries only its parent's code. This module answers that from the masters.

**The chain is walked, never assumed.** Each step reads the parent code the
entity's own row states and looks that code up at the parent level, so a
territory whose master names a unit the unit master lacks stops there and says
so (``known: false``) rather than inventing a region for it — the same rule
:func:`app.map.geo.derive_parents` applies when it refuses to place such a
code. No figure is computed here and nothing is scoped: a name is master
data, offered to every reader the way the filter options are.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database.models_map import MapEntityLocation
from . import geo
from .levels import MapLevel, get_level


def _row_at(session: Session, level: MapLevel, code: str):
    return session.execute(
        select(level.model).where(getattr(level.model, level.code_field) == code)
    ).scalar_one_or_none()


def describe(session: Session, level_key: str, code: str) -> dict[str, Any] | None:
    """The entity, its ancestors top-down, and its coordinate if it has one.

    Raises :class:`ValueError` for a level the map does not draw; returns
    ``None`` for a code the level's master does not hold.
    """
    level = get_level(level_key)
    row = _row_at(session, level, code)
    if row is None:
        return None

    ancestors: list[dict[str, Any]] = []
    current_level, current_row = level, row
    while current_level.parent and current_level.parent_code_field:
        parent_code = getattr(current_row, current_level.parent_code_field)
        if not parent_code:
            break
        parent_level = get_level(current_level.parent)
        parent_row = _row_at(session, parent_level, str(parent_code))
        ancestors.append({
            "level": parent_level.key,
            "label": parent_level.label,
            "code": str(parent_code),
            "name": (getattr(parent_row, parent_level.name_field, None)
                     if parent_row is not None else None) or str(parent_code),
            "known": parent_row is not None,
        })
        if parent_row is None:
            break
        current_level, current_row = parent_level, parent_row
    ancestors.reverse()

    location = session.execute(
        select(MapEntityLocation).where(
            MapEntityLocation.entity_type == level.key,
            MapEntityLocation.entity_code == code,
        )
    ).scalar_one_or_none()

    return {
        "level": level.key,
        "label": level.label,
        "code": str(code),
        "name": getattr(row, level.name_field, None) or str(code),
        "ancestors": ancestors,
        "location": geo.location_payload(location) if location is not None else None,
    }


__all__ = ["describe"]
