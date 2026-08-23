"""Marker-design operations.

Kept out of the route module so that versioning, assignment and the
seeded defaults are testable without an HTTP client, and so the rules that must
hold on every path — bump the version before overwriting an active design,
invalidate the cache after any write — live in one place rather than being
repeated per endpoint.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database.models_map import (
    MapMarkerAssignment,
    MapMarkerDesign,
    MapMarkerDesignVersion,
    MarkerDesignStatus,
    MarkerDesignType,
)
from .entities import ENTITY_TYPES, get_entity_type
from .render import render
from .resolver import invalidate_cache
from .schemas import MarkerDefinition, parse_definition

#: Colours for the seeded defaults, chosen so the hierarchy reads as one family
#: getting lighter as it narrows, with the operational types clearly distinct.
_DEFAULT_STYLE: dict[str, tuple[str, str, float, str | None]] = {
    "company":       ("#1E3A8A", "hexagon", 34, "building"),
    "bu":            ("#1D4ED8", "hexagon", 32, "briefcase"),
    "sales_line":    ("#2563EB", "pentagon", 30, None),
    "zone":          ("#3B82F6", "pentagon", 30, "compass"),
    "region":        ("#0EA5E9", "circle", 28, "globe"),
    "area":          ("#06B6D4", "circle", 26, None),
    "unit":          ("#14B8A6", "rounded_square", 24, None),
    "territory":     ("#10B981", "triangle", 26, "flag"),
    "sub_territory": ("#84CC16", "diamond", 22, None),
    # No warehouse style. Seeding walks ENTITY_TYPES and looks a style up by
    # key, so the entry revision 0020 left here was unreachable — and it
    # implied a placeable type that `entities.EXTRA_ENTITY_TYPES` explicitly
    # says no longer exists.
    "customer":      ("#7C3AED", "pin", 32, "store"),
    "sales_force":   ("#DB2777", "pin", 30, "person"),
}


def design_payload(design: MapMarkerDesign, *, session: Session | None = None,
                   include_preview: bool = True) -> dict[str, Any]:
    """One design as the API returns it, optionally with its rendered preview."""
    payload: dict[str, Any] = {
        "design_id": design.design_id,
        "design_uuid": design.design_uuid,
        "name": design.name,
        "description": design.description,
        "entity_type": design.entity_type,
        "design_type": design.design_type,
        "status": design.status,
        "definition": design.definition,
        "asset_id": design.asset_id,
        "version": design.version,
        "is_system_default": design.is_system_default,
        "created_by": design.created_by,
        "updated_by": design.updated_by,
        "created_at": design.created_at,
        "updated_at": design.updated_at,
    }
    if include_preview:
        from .resolver import _asset_markup

        definition = parse_definition(design.definition)
        markup = _asset_markup(session, design) if session is not None else None
        rendered = render(definition, asset_markup=markup)
        payload["preview"] = {
            "svg": rendered.svg,
            "width": rendered.width,
            "height": rendered.height,
            "vector_only": rendered.vector_only,
        }
    return payload


def snapshot_version(session: Session, design: MapMarkerDesign, *,
                     note: str, actor: str | None) -> MapMarkerDesignVersion:
    """Copy the design's current definition into the version history.

    Called *before* an active design is overwritten, so the configuration the
    map was using is recoverable. A draft is not snapshotted: nothing has ever
    depended on it, and a version list full of drafting noise hides the changes
    that mattered.
    """
    version = MapMarkerDesignVersion(
        design_id=design.design_id,
        version=design.version,
        name=design.name,
        design_type=design.design_type,
        definition=design.definition,
        asset_id=design.asset_id,
        status=design.status,
        note=note,
        created_by=actor,
    )
    session.add(version)
    session.flush()
    return version


def create_design(session: Session, *, name: str, entity_type: str,
                  design_type: str, definition: MarkerDefinition,
                  description: str | None = None, asset_id: int | None = None,
                  status: str = MarkerDesignStatus.DRAFT,
                  actor: str | None = None,
                  is_system_default: bool = False) -> MapMarkerDesign:
    """Create a design. The definition must already be validated."""
    get_entity_type(entity_type)
    design = MapMarkerDesign(
        design_uuid=str(uuid.uuid4()),
        name=name,
        description=description,
        entity_type=entity_type,
        design_type=design_type,
        status=status,
        definition=definition.to_json(),
        asset_id=asset_id,
        version=1,
        is_system_default=is_system_default,
        created_by=actor,
        updated_by=actor,
    )
    session.add(design)
    session.flush()
    invalidate_cache()
    return design


def update_design(session: Session, design: MapMarkerDesign, *,
                  actor: str | None, **changes: Any) -> dict[str, Any]:
    """Apply changes, versioning first when the design is live.

    Returns the applied changes for the audit record. The old definition is
    captured before anything is written so "old value / new value" in the audit
    log is genuinely the old value.
    """
    applied: dict[str, Any] = {}
    definition: MarkerDefinition | None = changes.pop("definition", None)

    definition_changed = (
        definition is not None and definition.to_json() != design.definition
    )
    material = definition_changed or any(
        value is not None and getattr(design, field) != value
        for field, value in changes.items()
        if field in {"design_type", "asset_id"}
    )

    # Only an active design has a configuration worth preserving: it is what the
    # map is drawing right now.
    if material and design.status == MarkerDesignStatus.ACTIVE:
        snapshot_version(session, design, note="superseded by an edit while active",
                         actor=actor)
        design.version += 1
        applied["version"] = design.version

    if definition is not None and definition_changed:
        design.definition = definition.to_json()
        applied["definition"] = True

    for field in ("name", "description", "design_type", "asset_id", "entity_type"):
        value = changes.get(field)
        if value is not None and getattr(design, field) != value:
            if field == "entity_type":
                get_entity_type(value)
            setattr(design, field, value)
            applied[field] = value

    design.updated_by = actor
    design.updated_at = datetime.now(timezone.utc)
    session.flush()
    invalidate_cache()
    return applied


def duplicate_design(session: Session, design: MapMarkerDesign, *,
                     name: str | None = None,
                     actor: str | None = None) -> MapMarkerDesign:
    """Copy a design so it can be modified independently.

    The copy starts as a ``DRAFT`` and is never a system default, so duplicating
    a live design cannot change what the map draws.
    """
    copy = MapMarkerDesign(
        design_uuid=str(uuid.uuid4()),
        name=name or _copy_name(session, design.name),
        description=design.description,
        entity_type=design.entity_type,
        design_type=design.design_type,
        status=MarkerDesignStatus.DRAFT,
        definition=design.definition,
        asset_id=design.asset_id,
        version=1,
        is_system_default=False,
        created_by=actor,
        updated_by=actor,
    )
    session.add(copy)
    session.flush()
    return copy


def _copy_name(session: Session, name: str) -> str:
    """``"Territory Default"`` -> ``"Territory Default (copy)"``, then ``(copy 2)``."""
    base = f"{name} (copy)"
    existing = {
        row for (row,) in session.execute(
            select(MapMarkerDesign.name).where(MapMarkerDesign.name.like(f"{name}%"))
        ).all()
    }
    if base not in existing:
        return base[:120]
    for index in range(2, 100):
        candidate = f"{name} (copy {index})"
        if candidate not in existing:
            return candidate[:120]
    return f"{name} {uuid.uuid4().hex[:6]}"[:120]


def assign_design(session: Session, design: MapMarkerDesign, *,
                  entity_code: str | None, actor: str | None,
                  priority: int = 0) -> MapMarkerAssignment:
    """Point an entity type — or one specific entity — at a design.

    An entity type has at most one type-level assignment and at most one per
    entity code, enforced by a unique constraint; assigning again replaces the
    target rather than stacking rows that would need tie-breaking.
    """
    existing = session.execute(
        select(MapMarkerAssignment).where(
            MapMarkerAssignment.entity_type == design.entity_type,
            MapMarkerAssignment.entity_code.is_(None) if entity_code is None
            else MapMarkerAssignment.entity_code == entity_code,
        )
    ).scalars().first()

    if existing is None:
        existing = MapMarkerAssignment(
            entity_type=design.entity_type,
            entity_code=entity_code,
            design_id=design.design_id,
            is_active=True,
            priority=priority,
            created_by=actor,
        )
        session.add(existing)
    else:
        existing.design_id = design.design_id
        existing.is_active = True
        existing.priority = priority

    # An assignment is a statement of intent to use the design, so a draft is
    # promoted rather than assigned-but-invisible.
    if design.status == MarkerDesignStatus.DRAFT:
        design.status = MarkerDesignStatus.ACTIVE

    session.flush()
    invalidate_cache()
    return existing


def set_status(session: Session, design: MapMarkerDesign, status: str, *,
               actor: str | None) -> None:
    design.status = status
    design.updated_by = actor
    session.flush()
    invalidate_cache()


def assignments_using(session: Session, design: MapMarkerDesign
                      ) -> list[MapMarkerAssignment]:
    """Live assignments pointing at a design — what makes a delete dangerous."""
    return list(session.execute(
        select(MapMarkerAssignment).where(
            MapMarkerAssignment.design_id == design.design_id,
            MapMarkerAssignment.is_active.is_(True),
        )
    ).scalars())


def reset_entity_type(session: Session, entity_type: str, *,
                      actor: str | None) -> int:
    """Remove custom assignments for an entity type, restoring the default.

    The designs themselves are left alone: reset means "stop using my custom
    choice", not "destroy the work". Returns how many assignments were removed.
    """
    get_entity_type(entity_type)
    rows = list(session.execute(
        select(MapMarkerAssignment)
        .where(MapMarkerAssignment.entity_type == entity_type)
    ).scalars())
    for row in rows:
        session.delete(row)
    session.flush()
    invalidate_cache()
    return len(rows)


def ensure_system_defaults(session: Session) -> int:
    """Seed one system-default design per entity type, once.

    Idempotent: a type that already has a default is skipped, so this is safe to
    call on every startup and from tests. Returns how many were created.
    """
    existing = {
        entity_type for (entity_type,) in session.execute(
            select(MapMarkerDesign.entity_type)
            .where(MapMarkerDesign.is_system_default.is_(True))
        ).all()
    }
    created = 0
    for entity in ENTITY_TYPES:
        if entity.key in existing:
            continue
        colour, shape, size, icon = _DEFAULT_STYLE.get(
            entity.key, ("#64748B", "circle", 28, None)
        )
        definition = parse_definition({
            "shape": {
                "kind": "builtin", "builtin": shape, "fill": colour,
                "stroke": "#FFFFFF", "stroke_width": 2, "size": size,
            },
            "icon": ({"enabled": True, "source": "library", "name": icon,
                      "size": size * 0.5, "colour": "#FFFFFF"} if icon else {}),
            "label": {"enabled": False, "template": "{Code}"},
        })
        create_design(
            session,
            name=f"{entity.label} Default",
            description=f"System default marker for {entity.label.lower()}.",
            entity_type=entity.key,
            design_type=MarkerDesignType.BUILTIN_SHAPE,
            definition=definition,
            status=MarkerDesignStatus.ACTIVE,
            actor="system",
            is_system_default=True,
        )
        created += 1
    if created:
        session.flush()
        invalidate_cache()
    return created


def design_counts(session: Session) -> dict[str, int]:
    by_status = dict(session.execute(
        select(MapMarkerDesign.status, func.count())
        .group_by(MapMarkerDesign.status)
    ).all())
    return {
        "total": sum(by_status.values()),
        "active": by_status.get(MarkerDesignStatus.ACTIVE, 0),
        "draft": by_status.get(MarkerDesignStatus.DRAFT, 0),
        "inactive": by_status.get(MarkerDesignStatus.INACTIVE, 0),
        "assignments": session.execute(
            select(func.count()).select_from(MapMarkerAssignment)
        ).scalar_one(),
    }


__all__ = [
    "design_payload",
    "create_design",
    "update_design",
    "duplicate_design",
    "assign_design",
    "set_status",
    "snapshot_version",
    "assignments_using",
    "reset_entity_type",
    "ensure_system_defaults",
    "design_counts",
]
