"""Marker and shape design API.

Two audiences, deliberately separated:

* the **designer** endpoints (``/marker-designs``, ``/marker-assets``,
  ``/assignments``) are gated on the ``map_settings`` section — a permission in
  its own right, so a user without it gets ``403`` whether they click a menu or
  type the URL;
* the **consumption** endpoints (``/marker-config``, ``/legend``) are gated on
  the ordinary ``dashboard`` section, because every user who can see the map
  needs the markers it is drawn with. They are read-only and return no
  authoring detail.

That split is what lets the map front-end read its configuration from the
database without every viewer being a map administrator.
"""

from __future__ import annotations

import hashlib
import json
import datetime as dt
import logging
import os
import re
from types import SimpleNamespace
from typing import Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..ai.schemas import ScopeFilters
from ..auth import audit
from ..auth.permissions import require_any_section, require_section
from ..config import get_settings
from ..database.models_ai import AuditAction
from ..database.models_map import (
    GeoPrecision,
    GeoSource,
    MapEntityLocation,
    MapMarkerAsset,
    MapMarkerAssignment,
    MapMarkerDesign,
    MapMarkerDesignVersion,
    MarkerDesignStatus,
    MarkerDesignType,
)
from ..database.models_geo import MapAreaStyle
from ..map import adapters, assets, entities, geo, icons, resolver, service, shapes
from ..map import areas as area_service
from ..map import data as map_data_service
from ..map import entity_view, geometry as geometry_util, hierarchy
from ..map.render import render
from ..map.schemas import LABEL_VARIABLES, MarkerDefinition, parse_definition
from ..security.sections import SectionKey
from .deps import get_session, internal_error
from .routes_dashboard import date_range_params, run, scope_filters, tool_context

logger = logging.getLogger("app.api.map")

router = APIRouter(prefix="/api/map", tags=["map"])

#: Authoring requires the Map Settings section.
DesignerDep = Depends(require_section(SectionKey.MAP_SETTINGS))
#: Marker appearance is needed by the map, the dashboard legend and the
#: designer alike, and carries no business figures — so any of the three does.
ViewerDep = Depends(require_any_section(
    SectionKey.MAP, SectionKey.DASHBOARD, SectionKey.MAP_SETTINGS,
))


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class DesignCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    entity_type: str
    design_type: str = MarkerDesignType.BUILTIN_SHAPE
    description: str | None = Field(None, max_length=500)
    definition: dict[str, Any] = Field(default_factory=dict)
    asset_id: int | None = None
    status: str = MarkerDesignStatus.DRAFT


class DesignUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=120)
    entity_type: str | None = None
    design_type: str | None = None
    description: str | None = Field(None, max_length=500)
    definition: dict[str, Any] | None = None
    asset_id: int | None = None


class DuplicateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=120)


class AssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: NULL assigns to the whole entity type; a code assigns to one entity.
    entity_code: str | None = Field(None, max_length=64)
    priority: int = Field(0, ge=0, le=1000)


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any] = Field(default_factory=dict)
    asset_id: int | None = None
    #: Sample values for the label placeholders.
    context: dict[str, str] = Field(default_factory=dict)


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    designs: list[dict[str, Any]]
    #: Replace a design whose ``design_uuid`` already exists, rather than skipping.
    overwrite: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _design_or_404(session: Session, design_id: int) -> MapMarkerDesign:
    design = session.get(MapMarkerDesign, design_id)
    if design is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Marker design not found.")
    return design


def _validated(payload: dict[str, Any] | None) -> MarkerDefinition:
    try:
        return parse_definition(payload)
    except Exception as exc:  # noqa: BLE001 - pydantic message is user-safe
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"The marker definition is not valid: {_first_error(exc)}",
        ) from exc


def _first_error(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            first = errors()[0]
            location = ".".join(str(p) for p in first.get("loc", ()))
            return f"{location}: {first.get('msg', 'invalid value')}"
        except (IndexError, KeyError, TypeError):  # pragma: no cover
            pass
    return str(exc)[:200]


def _check_entity(entity_type: str) -> None:
    try:
        entities.get_entity_type(entity_type)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


def _check_design_type(design_type: str) -> None:
    if design_type not in MarkerDesignType.ALL:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown design type '{design_type}'. Supported: "
            + ", ".join(MarkerDesignType.ALL),
        )


def _audit(session: Session, request: Request, user: UserContext, action: str,
           design: MapMarkerDesign | None, detail: dict[str, Any]) -> None:
    audit.record(
        session, action=action, user_id=user.user_id, username=user.username,
        resource=f"marker-design:{design.design_id}" if design else "marker-design",
        ip_address=audit.client_ip(request), detail=detail,
    )


# ---------------------------------------------------------------------------
# Catalogues
# ---------------------------------------------------------------------------


@router.get("/entity-types")
def entity_types(_: UserContext = DesignerDep) -> dict[str, Any]:
    """What a marker can be assigned to, derived from the real hierarchy."""
    return {"entity_types": entities.catalogue()}


@router.get("/shapes")
def shape_catalogue(_: UserContext = DesignerDep) -> dict[str, Any]:
    """Built-in shapes, each with a sample outline the picker can draw."""
    return {"shapes": shapes.catalogue()}


@router.get("/icons")
def icon_catalogue(_: UserContext = DesignerDep) -> dict[str, Any]:
    """The built-in icon library, grouped by category."""
    return icons.catalogue()


@router.get("/designer-options")
def designer_options(session: Session = Depends(get_session),
                     _: UserContext = DesignerDep) -> dict[str, Any]:
    """Everything the designer needs to render its panels, in one request."""
    return {
        "entity_types": entities.catalogue(),
        "shapes": shapes.catalogue(),
        "icons": icons.catalogue(),
        "design_types": [
            {"key": MarkerDesignType.BUILTIN_SHAPE, "label": "Built-in Shape"},
            {"key": MarkerDesignType.CUSTOM_IMAGE, "label": "Custom SVG / Image"},
            {"key": MarkerDesignType.SHAPE_BUILDER, "label": "Shape Builder"},
        ],
        "statuses": list(MarkerDesignStatus.ALL),
        "label_variables": list(LABEL_VARIABLES),
        "renderers": list(adapters.ADAPTERS),
        "upload": {
            "max_bytes": assets.max_upload_bytes(),
            "extensions": list(assets.ALLOWED_UPLOADS),
        },
        "counts": service.design_counts(session),
    }


# ---------------------------------------------------------------------------
# Designs
# ---------------------------------------------------------------------------


@router.get("/marker-designs")
def list_designs(
    entity_type: str | None = Query(None, max_length=32),
    design_status: str | None = Query(None, alias="status", max_length=16),
    search: str | None = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
    _: UserContext = DesignerDep,
) -> dict[str, Any]:
    """The marker design library."""
    conditions = []
    if entity_type:
        conditions.append(MapMarkerDesign.entity_type == entity_type)
    if design_status:
        conditions.append(MapMarkerDesign.status == design_status.upper())
    if search:
        conditions.append(MapMarkerDesign.name.ilike(f"%{search.strip()}%"))

    total = session.execute(
        select(func.count()).select_from(MapMarkerDesign).where(*conditions)
    ).scalar_one()
    rows = session.execute(
        select(MapMarkerDesign).where(*conditions)
        .order_by(MapMarkerDesign.entity_type, MapMarkerDesign.name)
        .limit(limit).offset(offset)
    ).scalars().all()

    # Which designs are in use, so the library can warn before a delete.
    in_use = {
        design_id for (design_id,) in session.execute(
            select(MapMarkerAssignment.design_id)
            .where(MapMarkerAssignment.is_active.is_(True))
        ).all()
    }
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "designs": [
            {**service.design_payload(design, session=session),
             "in_use": design.design_id in in_use}
            for design in rows
        ],
    }


@router.post("/marker-designs", status_code=status.HTTP_201_CREATED)
def create_design(
    request: DesignCreateRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = DesignerDep,
) -> dict[str, Any]:
    """Create a marker design."""
    _check_entity(request.entity_type)
    _check_design_type(request.design_type)
    if request.status not in MarkerDesignStatus.ALL:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"Unknown status '{request.status}'.")
    definition = _validated(request.definition)

    if request.asset_id is not None and session.get(MapMarkerAsset,
                                                    request.asset_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "The chosen image no longer exists.")

    design = service.create_design(
        session, name=request.name, entity_type=request.entity_type,
        design_type=request.design_type, definition=definition,
        description=request.description, asset_id=request.asset_id,
        status=request.status, actor=user.username,
    )
    _audit(session, http_request, user, AuditAction.MARKER_DESIGN_CREATED, design,
           {"name": design.name, "entity_type": design.entity_type,
            "design_type": design.design_type, "new": design.definition})
    session.commit()
    return service.design_payload(design, session=session)


@router.get("/marker-designs/{design_id}")
def get_design(design_id: int, session: Session = Depends(get_session),
               _: UserContext = DesignerDep) -> dict[str, Any]:
    design = _design_or_404(session, design_id)
    return {
        **service.design_payload(design, session=session),
        "assignments": [
            {"assignment_id": a.assignment_id, "entity_type": a.entity_type,
             "entity_code": a.entity_code, "is_active": a.is_active,
             "priority": a.priority}
            for a in service.assignments_using(session, design)
        ],
    }


@router.put("/marker-designs/{design_id}")
def update_design(
    design_id: int,
    request: DesignUpdateRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = DesignerDep,
) -> dict[str, Any]:
    """Update a design, versioning the previous configuration when it was live."""
    design = _design_or_404(session, design_id)
    if request.entity_type:
        _check_entity(request.entity_type)
    if request.design_type:
        _check_design_type(request.design_type)

    previous = dict(design.definition or {})
    definition = _validated(request.definition) if request.definition is not None else None

    try:
        applied = service.update_design(
            session, design, actor=user.username, definition=definition,
            name=request.name, description=request.description,
            design_type=request.design_type, asset_id=request.asset_id,
            entity_type=request.entity_type,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    _audit(session, http_request, user, AuditAction.MARKER_DESIGN_UPDATED, design,
           {"changes": list(applied), "old": previous, "new": design.definition,
            "version": design.version})
    session.commit()
    return service.design_payload(design, session=session)


@router.delete("/marker-designs/{design_id}")
def delete_design(
    design_id: int,
    http_request: Request,
    confirm: bool = Query(False, description="Required when the design is in use."),
    session: Session = Depends(get_session),
    user: UserContext = DesignerDep,
) -> dict[str, Any]:
    """Delete a design.

    A design the map is actively using cannot be deleted by accident: the call
    is refused unless the caller confirms, and a system default cannot be deleted
    at all, because it is the fallback that keeps the map from going blank.
    """
    design = _design_or_404(session, design_id)
    if design.is_system_default:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This is the system default for its entity type and cannot be "
            "deleted. Assign a different design instead.",
        )

    used_by = service.assignments_using(session, design)
    if used_by and not confirm:
        where = ", ".join(
            a.entity_code or f"all {a.entity_type}" for a in used_by
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This design is in use ({where}). Confirm to delete it — those "
            "entities will fall back to the system default marker.",
        )

    detail = {"name": design.name, "entity_type": design.entity_type,
              "old": design.definition, "assignments_removed": len(used_by)}
    session.delete(design)          # assignments cascade
    session.flush()
    resolver.invalidate_cache()
    _audit(session, http_request, user, AuditAction.MARKER_DESIGN_DELETED, None,
           {"design_id": design_id, **detail})
    session.commit()
    return {"deleted": design_id, "assignments_removed": len(used_by)}


@router.post("/marker-designs/{design_id}/duplicate",
             status_code=status.HTTP_201_CREATED)
def duplicate_design(
    design_id: int,
    http_request: Request,
    request: DuplicateRequest = Body(default_factory=DuplicateRequest),
    session: Session = Depends(get_session),
    user: UserContext = DesignerDep,
) -> dict[str, Any]:
    """Copy a design so it can be modified independently."""
    design = _design_or_404(session, design_id)
    copy = service.duplicate_design(session, design, name=request.name,
                                    actor=user.username)
    _audit(session, http_request, user, AuditAction.MARKER_DESIGN_DUPLICATED, copy,
           {"source_design_id": design_id, "source_name": design.name,
            "new_name": copy.name})
    session.commit()
    return service.design_payload(copy, session=session)


@router.post("/marker-designs/{design_id}/assign")
def assign_design(
    design_id: int,
    http_request: Request,
    request: AssignRequest = Body(default_factory=AssignRequest),
    session: Session = Depends(get_session),
    user: UserContext = DesignerDep,
) -> dict[str, Any]:
    """Assign a design to its entity type, or to one specific entity."""
    design = _design_or_404(session, design_id)
    assignment = service.assign_design(
        session, design, entity_code=request.entity_code, actor=user.username,
        priority=request.priority,
    )
    _audit(session, http_request, user, AuditAction.MARKER_DESIGN_ASSIGNED, design,
           {"entity_type": design.entity_type, "entity_code": request.entity_code,
            "design_name": design.name, "priority": request.priority})
    session.commit()
    return {
        "assignment_id": assignment.assignment_id,
        "entity_type": assignment.entity_type,
        "entity_code": assignment.entity_code,
        "design": service.design_payload(design, session=session),
    }


@router.post("/marker-designs/{design_id}/activate")
def activate_design(design_id: int, http_request: Request,
                    session: Session = Depends(get_session),
                    user: UserContext = DesignerDep) -> dict[str, Any]:
    design = _design_or_404(session, design_id)
    service.set_status(session, design, MarkerDesignStatus.ACTIVE,
                       actor=user.username)
    _audit(session, http_request, user, AuditAction.MARKER_DESIGN_ACTIVATED, design,
           {"name": design.name, "new": MarkerDesignStatus.ACTIVE})
    session.commit()
    return service.design_payload(design, session=session)


@router.post("/marker-designs/{design_id}/deactivate")
def deactivate_design(design_id: int, http_request: Request,
                      session: Session = Depends(get_session),
                      user: UserContext = DesignerDep) -> dict[str, Any]:
    """Retire a design without deleting it.

    A system default cannot be deactivated: it is the last line before the map
    has nothing to draw.
    """
    design = _design_or_404(session, design_id)
    if design.is_system_default:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The system default cannot be deactivated — the map would have no "
            "marker to fall back to.",
        )
    service.set_status(session, design, MarkerDesignStatus.INACTIVE,
                       actor=user.username)
    _audit(session, http_request, user, AuditAction.MARKER_DESIGN_DEACTIVATED,
           design, {"name": design.name, "new": MarkerDesignStatus.INACTIVE})
    session.commit()
    return service.design_payload(design, session=session)


@router.get("/marker-designs/{design_id}/versions")
def design_versions(design_id: int, session: Session = Depends(get_session),
                    _: UserContext = DesignerDep) -> dict[str, Any]:
    """The design's version history, newest first."""
    design = _design_or_404(session, design_id)
    rows = session.execute(
        select(MapMarkerDesignVersion)
        .where(MapMarkerDesignVersion.design_id == design_id)
        .order_by(desc(MapMarkerDesignVersion.version))
    ).scalars().all()
    return {
        "design_id": design_id,
        "current_version": design.version,
        "versions": [
            {
                "version_id": row.version_id,
                "version": row.version,
                "name": row.name,
                "design_type": row.design_type,
                "status": row.status,
                "note": row.note,
                "definition": row.definition,
                "preview_svg": render(parse_definition(row.definition)).svg,
                "created_by": row.created_by,
                "created_at": row.created_at,
            }
            for row in rows
        ],
    }


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


@router.post("/marker-designs/preview")
def preview_design(request: PreviewRequest,
                   session: Session = Depends(get_session),
                   _: UserContext = DesignerDep) -> dict[str, Any]:
    """Render a definition without saving it.

    This is what makes the live preview and "Preview on Map" honest: the browser
    shows exactly what the server would produce for a saved design, so nothing
    changes in appearance at the moment of saving.
    """
    definition = _validated(request.definition)
    markup = None
    if request.asset_id is not None:
        asset = session.get(MapMarkerAsset, request.asset_id)
        if asset is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Image not found.")
        markup = (asset.content if asset.media_type == assets.MEDIA_SVG else
                  f'<image href="{asset.content}" x="0" y="0" width="100%" '
                  f'height="100%" preserveAspectRatio="xMidYMid meet"/>')
    try:
        rendered = render(definition, context=request.context, asset_markup=markup)
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "marker preview") from exc
    return {
        **rendered.to_dict(),
        "marker": adapters.adapt(definition, rendered),
    }


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


@router.post("/marker-assets", status_code=status.HTTP_201_CREATED)
async def upload_asset(
    http_request: Request,
    file: UploadFile = File(..., description="SVG (preferred), PNG or WebP."),
    session: Session = Depends(get_session),
    user: UserContext = DesignerDep,
) -> dict[str, Any]:
    """Upload marker artwork. SVG is sanitised before it is stored."""
    try:
        stored = assets.store_asset(session, file.file, file.filename or "",
                                    file.content_type, user.username)
    except assets.AssetRejected as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "marker asset upload") from exc
    finally:
        await file.close()

    if not stored.reused:
        _audit(session, http_request, user, AuditAction.MARKER_ASSET_UPLOADED, None,
               {"asset_id": stored.asset.asset_id,
                "file_name": stored.asset.file_name,
                "media_type": stored.asset.media_type,
                "sanitised": stored.asset.sanitised_report})
    session.commit()
    return {**assets.asset_payload(stored.asset), "reused": stored.reused}


@router.get("/marker-assets")
def list_assets(limit: int = Query(50, ge=1, le=200),
                session: Session = Depends(get_session),
                _: UserContext = DesignerDep) -> dict[str, Any]:
    rows = session.execute(
        select(MapMarkerAsset).order_by(desc(MapMarkerAsset.asset_id)).limit(limit)
    ).scalars().all()
    return {"assets": [assets.asset_payload(row) for row in rows]}


@router.get("/marker-assets/{asset_id}")
def get_asset(asset_id: int, session: Session = Depends(get_session),
              _: UserContext = DesignerDep) -> dict[str, Any]:
    asset = session.get(MapMarkerAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Image not found.")
    return assets.asset_payload(asset)


# ---------------------------------------------------------------------------
# Assignment maintenance
# ---------------------------------------------------------------------------


@router.get("/assignments")
def list_assignments(session: Session = Depends(get_session),
                     _: UserContext = DesignerDep) -> dict[str, Any]:
    """Every assignment, with the design it points at."""
    rows = session.execute(
        select(MapMarkerAssignment, MapMarkerDesign)
        .join(MapMarkerDesign,
              MapMarkerDesign.design_id == MapMarkerAssignment.design_id)
        .order_by(MapMarkerAssignment.entity_type,
                  MapMarkerAssignment.entity_code)
    ).all()
    return {
        "assignments": [
            {
                "assignment_id": assignment.assignment_id,
                "entity_type": assignment.entity_type,
                "entity_code": assignment.entity_code,
                "scope": "entity" if assignment.entity_code else "entity_type",
                "is_active": assignment.is_active,
                "priority": assignment.priority,
                "design_id": design.design_id,
                "design_name": design.name,
                "design_status": design.status,
            }
            for assignment, design in rows
        ],
    }


@router.post("/assignments/{entity_type}/reset")
def reset_assignments(entity_type: str, http_request: Request,
                      confirm: bool = Query(False),
                      session: Session = Depends(get_session),
                      user: UserContext = DesignerDep) -> dict[str, Any]:
    """Remove custom assignments and restore the system default marker."""
    _check_entity(entity_type)
    if not confirm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Resetting removes the custom marker assignment for this entity type "
            "and restores the system default. Confirm to proceed.",
        )
    removed = service.reset_entity_type(session, entity_type, actor=user.username)
    audit.record(session, action=AuditAction.MARKER_RESET_TO_DEFAULT,
                 user_id=user.user_id, username=user.username,
                 resource=f"marker-assignment:{entity_type}",
                 ip_address=audit.client_ip(http_request),
                 detail={"entity_type": entity_type, "removed": removed})
    session.commit()
    return {"entity_type": entity_type, "assignments_removed": removed}


# ---------------------------------------------------------------------------
# Export / import
# ---------------------------------------------------------------------------


@router.get("/marker-designs-export")
def export_designs(entity_type: str | None = Query(None),
                   session: Session = Depends(get_session),
                   _: UserContext = DesignerDep) -> Response:
    """Marker configuration as JSON, portable between environments.

    Carries designs, their definitions and their assignments — and nothing else.
    No credentials, no API keys, no user data.
    """
    conditions = [MapMarkerDesign.entity_type == entity_type] if entity_type else []
    designs = session.execute(
        select(MapMarkerDesign).where(*conditions)
        .order_by(MapMarkerDesign.entity_type, MapMarkerDesign.name)
    ).scalars().all()
    by_id = {d.design_id: d.design_uuid for d in designs}
    assignments = session.execute(select(MapMarkerAssignment)).scalars().all()

    payload = {
        "format": "marker-designs/v1",
        "designs": [
            {
                "design_uuid": d.design_uuid,
                "name": d.name,
                "description": d.description,
                "entity_type": d.entity_type,
                "design_type": d.design_type,
                "status": d.status,
                "definition": d.definition,
                "is_system_default": d.is_system_default,
            }
            for d in designs
        ],
        "assignments": [
            {
                "entity_type": a.entity_type,
                "entity_code": a.entity_code,
                "design_uuid": by_id[a.design_id],
                "priority": a.priority,
            }
            for a in assignments if a.design_id in by_id
        ],
    }
    body = json.dumps(payload, indent=2, default=str)
    return Response(
        content=body, media_type="application/json",
        headers={"Content-Disposition":
                 'attachment; filename="marker-designs.json"'},
    )


@router.post("/marker-designs-import")
def import_designs(request: ImportRequest, http_request: Request,
                   session: Session = Depends(get_session),
                   user: UserContext = DesignerDep) -> dict[str, Any]:
    """Import marker designs from an exported document.

    Every definition is re-validated on the way in — an exported file is just
    untrusted JSON once it has left this system.
    """
    created = updated = skipped = 0
    problems: list[str] = []

    for index, entry in enumerate(request.designs):
        name = str(entry.get("name") or "").strip()
        entity_type = str(entry.get("entity_type") or "")
        if not name or entity_type not in entities.ENTITY_TYPE_BY_KEY:
            problems.append(f"Design {index + 1}: unknown entity type or missing name.")
            skipped += 1
            continue
        try:
            definition = parse_definition(entry.get("definition"))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"Design {index + 1} ({name}): {_first_error(exc)}")
            skipped += 1
            continue

        design_type = entry.get("design_type") or MarkerDesignType.BUILTIN_SHAPE
        if design_type not in MarkerDesignType.ALL:
            design_type = MarkerDesignType.BUILTIN_SHAPE

        existing = session.execute(
            select(MapMarkerDesign)
            .where(MapMarkerDesign.design_uuid == entry.get("design_uuid"))
        ).scalars().first() if entry.get("design_uuid") else None

        if existing is not None:
            if not request.overwrite:
                skipped += 1
                continue
            service.update_design(session, existing, actor=user.username,
                                  definition=definition, name=name,
                                  design_type=design_type)
            updated += 1
        else:
            service.create_design(
                session, name=name, entity_type=entity_type,
                design_type=design_type, definition=definition,
                description=entry.get("description"),
                status=MarkerDesignStatus.DRAFT, actor=user.username,
            )
            created += 1

    audit.record(session, action=AuditAction.MARKER_CONFIG_IMPORTED,
                 user_id=user.user_id, username=user.username,
                 resource="marker-design", ip_address=audit.client_ip(http_request),
                 detail={"created": created, "updated": updated, "skipped": skipped},
                 success=not problems)
    session.commit()
    resolver.invalidate_cache()
    return {"created": created, "updated": updated, "skipped": skipped,
            "problems": problems[:20]}


# ---------------------------------------------------------------------------
# Consumption — what a map front-end reads
# ---------------------------------------------------------------------------


@router.get("/marker-config")
def marker_config(
    entity_type: str | None = Query(None),
    session: Session = Depends(get_session),
    _: UserContext = ViewerDep,
) -> dict[str, Any]:
    """The resolved marker for every entity type — the map's one config call.

    ``generation`` is the cache generation the answer was computed under. A
    client can send it back as a cheap freshness check: if it has not changed,
    nothing about the markers has.

    There is no ``renderer`` parameter any more. One engine draws this map, and
    the single payload it needs — the rendered SVG, its size and its anchor — is
    also what a legend and the designer's preview read, so a caller had nothing
    to choose between.
    """
    try:
        if entity_type:
            _check_entity(entity_type)
            resolved = [resolver.resolve(session, entity_type)]
        else:
            resolved = resolver.resolve_all(session)
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "marker configuration") from exc

    return {
        "generation": resolver.CACHE.generation,
        "renderer": adapters.GENERIC_SVG,
        "markers": {r.entity_type: r.to_dict() for r in resolved},
    }


@router.get("/marker-config/{entity_type}/{entity_code}")
def marker_for_entity(entity_type: str, entity_code: str,
                      session: Session = Depends(get_session),
                      _: UserContext = ViewerDep) -> dict[str, Any]:
    """The marker for one specific entity, honouring the assignment priority."""
    _check_entity(entity_type)
    resolved = resolver.resolve(session, entity_type, entity_code)
    return {"generation": resolver.CACHE.generation, **resolved.to_dict()}


@router.get("/legend")
def map_legend(session: Session = Depends(get_session),
               _: UserContext = ViewerDep) -> dict[str, Any]:
    """The map legend, built from the same resolution the map uses.

    Because the legend and the map share one resolver, changing a design updates
    both — there is no separate legend artwork to fall out of step.
    """
    try:
        return {
            "generation": resolver.CACHE.generation,
            "entries": resolver.legend(session),
            # Areas are drawn as filled shapes, not markers, so they cannot be
            # rendered by the marker resolver. They get their own entries,
            # carrying the configured colour rather than a copy of it — change
            # the style and the swatch changes with the map.
            "area_entries": [
                {
                    "entity_type": level["key"],
                    "label": f"Administrative Area — {level['label']}",
                    "group": "Administrative",
                    "kind": "area",
                    "style": area_service.style_for(session, level["key"]),
                    "has_boundaries": any(
                        row["level"] == level["key"] and row["with_boundary"]
                        for row in area_service.coverage(session)
                    ),
                }
                for level in area_service.levels_payload()
            ],
        }
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "map legend") from exc


# ---------------------------------------------------------------------------
# The map itself
# ---------------------------------------------------------------------------

#: Viewing the map requires the Business Map section.
MapDep = Depends(require_section(SectionKey.MAP))


class LocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: str
    entity_code: str = Field(min_length=1, max_length=64)
    latitude: float
    longitude: float
    label: str | None = Field(None, max_length=255)
    precision: str = GeoPrecision.APPROXIMATE


class BulkLocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locations: list[LocationRequest]
    #: Recompute parent centroids after loading. Almost always wanted.
    derive_parents: bool = True


@router.get("/config")
def map_config(session: Session = Depends(get_session),
               _: UserContext = MapDep) -> dict[str, Any]:
    """How the map should be rendered, and with what.

    The basemap is named here rather than in the frontend bundle so that a
    deployment can repoint it — at its own tile server, say — without a rebuild.
    No key, token or credential is involved: the map runs on MapLibre GL JS over
    OpenFreeMap, neither of which authenticates, which is why this response is
    the same for every caller and why the frontend no longer carries a fallback
    renderer for deployments that had no key.

    Administrative *geometry* is deliberately absent. It is served with the
    frontend from ``public/geo/`` — boundaries change only when somebody imports
    a new release, so re-sending them per request would be traffic with no answer
    that ever differs. What this describes is how those local polygons should be
    painted, and the levels they come in.
    """
    settings = get_settings()
    coverage = geo.coverage(session)
    placed = sum(row["placed"] for row in coverage)

    return {
        "basemap": {
            "engine": "maplibre-gl",
            "provider": "openfreemap",
            "style_url": settings.map_basemap_style_url,
            "style_url_dark": settings.map_basemap_style_url_dark,
            # Stated rather than assumed by the client: an operator who repoints
            # the style at a self-hosted server needs the attribution to follow.
            "attribution": "© OpenStreetMap contributors, © OpenFreeMap",
        },
        "default_view": {
            # Centred on whatever is actually placed; only falls back to a
            # configured default when nothing has coordinates yet.
            "latitude": settings.map_default_latitude,
            "longitude": settings.map_default_longitude,
            "zoom": settings.map_default_zoom,
        },
        "levels": map_data_service.drill_levels(),
        "metrics": [metric.to_dict() for metric in map_data_service.METRICS],
        "cluster_threshold": map_data_service.CLUSTER_THRESHOLD,
        "coverage": coverage,
        "has_locations": placed > 0,
        "currency": settings.currency_code,
        # The administrative area layer travels with the rest of the map's
        # configuration, so the frontend learns about it — its levels, its
        # colours and whether any boundary exists — in the same round trip.
        "administrative_areas": {
            "layer_key": area_service.LAYER_KEY,
            "levels": area_service.levels_payload(),
            "default_level": area_service.DEFAULT_LEVEL,
            "styles": area_service.all_styles(session),
            "coverage": area_service.coverage(session),
            "default_stock": settings.map_area_default_stock,
            "has_boundaries": any(
                row["with_boundary"] for row in area_service.coverage(session)
            ),
        },
    }


@router.get("/levels")
def map_levels(_: UserContext = MapDep) -> dict[str, Any]:
    """The drill path, shallowest first."""
    return {"levels": map_data_service.drill_levels()}


@router.get("/data")
def map_points(
    request: Request,
    level: str = Query("region", max_length=32),
    metric: str = Query("net_sales", max_length=32),
    cluster: bool | None = Query(None),
    zoom: int = Query(map_data_service.DEFAULT_ZOOM, ge=1, le=20,
                      description="Map zoom, so clusters match what is on screen."),
    limit: int = Query(2000, ge=1, le=5000),
    date_range=Depends(date_range_params),
    filters: ScopeFilters = Depends(scope_filters),
    session: Session = Depends(get_session),
    user: UserContext = MapDep,
) -> dict[str, Any]:
    """Everything needed to draw one level of the map.

    The caller's data scope is applied to the query, not to the result: a
    regional manager's request never reads another region's rows.
    """
    try:
        result = map_data_service.map_data(
            session, user, level=level, metric=metric,
            date_from=date_range.date_from, date_to=date_range.date_to,
            filters=filters, limit=limit, cluster=cluster, zoom=zoom,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "map data") from exc

    audit.record(session, action=AuditAction.VIEW_REPORT, user_id=user.user_id,
                 username=user.username, resource=f"map:{level}",
                 ip_address=audit.client_ip(request),
                 detail={"level": level, "metric": metric,
                         "period": date_range.label})
    session.commit()

    return {
        "period": date_range.model_dump(mode="json"),
        "filters": {k: v for k, v in filters.model_dump(mode="json").items() if v},
        "scope_description": user.describe_scope(),
        **map_data_service.result_payload(session, result),
    }


@router.get("/entity-trend")
def entity_trend(
    request: Request,
    level: str = Query(..., max_length=32,
                       description="Map level of the entity — region, territory, "
                                   "customer, and so on."),
    code: str = Query(..., max_length=64),
    months: int = Query(6, ge=1, le=24),
    date_range=Depends(date_range_params),
    filters: ScopeFilters = Depends(scope_filters),
    session: Session = Depends(get_session),
    user: UserContext = MapDep,
) -> dict[str, Any]:
    """Monthly net sales for one entity the map has drawn.

    What the detail panel's history bars are made of. It exists because the map
    is the one surface where a reader picks a single region or dealer out of a
    picture and immediately wants to know whether it has been like that all
    year — a question the ranking beside it cannot answer, because a ranking is
    one period deep.

    It runs ``get_sales_trend`` through the same ``execute_tool`` path the
    dashboard and every page use, so the caller's role, section and data scope
    are applied to the query exactly as they are everywhere else, and the
    figures are the ones the reports show. Nothing here aggregates anything.

    **There is no target series beside the actuals**, and that is a limit of the
    data rather than of this endpoint. ``fact_target`` records a target month and
    a financial year rather than a date, and no tool on this path groups it by
    month, so a monthly target would have to be invented here — which is the one
    thing this application refuses to do. The bars are actuals; the panel says
    so.
    """
    try:
        field = map_data_service.filter_field_for(level)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    entity_code = code.strip()
    if not entity_code:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "A code is required.")

    # The reader's own filters still apply — a trend for one customer inside a
    # company-filtered page is that customer's sales *in that company*. Only the
    # clicked level is overwritten, because that is what was clicked.
    scoped = filters.model_copy(update={field: [entity_code]})

    # A window of whole months ending with the period on screen, rather than the
    # period itself: the panel asks "has it been like this?", which one month
    # cannot answer. Walked by hand rather than with a relativedelta, because
    # this repository carries no dateutil and month arithmetic is four lines.
    last = date_range.date_to.replace(day=1)
    year, month = last.year, last.month - (months - 1)
    while month <= 0:
        month += 12
        year -= 1
    window = SimpleNamespace(date_from=dt.date(year, month, 1),
                             date_to=date_range.date_to)

    ctx = tool_context(session, user)
    try:
        payload = run(ctx, "get_sales_trend", window, scoped,
                      granularity="month", limit=months)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "map entity trend") from exc

    audit.record(session, action=AuditAction.VIEW_REPORT, user_id=user.user_id,
                 username=user.username, resource=f"map:trend:{level}",
                 ip_address=audit.client_ip(request),
                 detail={"level": level, "code": entity_code, "months": months})
    session.commit()

    return {
        "level": level,
        "code": entity_code,
        "months": months,
        "period": {"date_from": window.date_from.isoformat(),
                   "date_to": window.date_to.isoformat()},
        "scope_description": user.describe_scope(),
        "rows": payload.get("rows", []),
        "notes": payload.get("notes", []),
        "error": payload.get("error"),
    }


@router.get("/entities")
def map_entities(
    request: Request,
    zone_code: str | None = Query(None, max_length=64),
    region_code: str | None = Query(None, max_length=64),
    area_code: str | None = Query(None, max_length=64),
    unit_code: str | None = Query(None, max_length=64),
    territory_code: str | None = Query(None, max_length=64),
    sub_territory_code: str | None = Query(None, max_length=64),
    customer_code: str | None = Query(None, max_length=64),
    company_code: str | None = Query(None, max_length=64),
    bu_code: str | None = Query(None, max_length=64),
    sales_line_code: str | None = Query(None, max_length=64),
    layers: str | None = Query(
        None, description="Comma-separated entity types to draw. Layer visibility "
                          "only — it never changes the filter scope."),
    metric: str = Query("net_sales", max_length=32),
    zoom: int = Query(map_data_service.DEFAULT_ZOOM, ge=1, le=20),
    diagnostics: bool = Query(False, description="Development aid; ignored in "
                                                 "production."),
    limit: int = Query(5000, ge=1, le=20000),
    date_range=Depends(date_range_params),
    business: ScopeFilters = Depends(scope_filters),
    session: Session = Depends(get_session),
    user: UserContext = MapDep,
) -> dict[str, Any]:
    """Every entity belonging under a hierarchy filter, at every level.

    Selecting a territory returns that territory, the ancestors that place it,
    its sub-territories, and the customers, sales force and warehouses beneath
    it — in one call. The front end never walks the hierarchy itself.

    Multiple filters intersect: ``region=REG001&territory=TR004`` returns
    nothing when TR004 does not sit in REG001, rather than silently widening.
    """
    requested = {
        "company": company_code, "bu": bu_code, "sales_line": sales_line_code,
        "zone": zone_code, "region": region_code, "area": area_code,
        "unit": unit_code, "territory": territory_code,
        "sub_territory": sub_territory_code,
    }

    try:
        # Authorisation first: an out-of-scope code is refused before any data
        # is read, and an unfiltered request is narrowed to the caller's scope.
        scoped_filters = hierarchy.apply_data_scope(user, session, requested)
    except Exception as exc:  # noqa: BLE001 - PermissionDeniedError -> 403
        raise internal_error(exc, "map entities") from exc

    chosen_layers = (
        [part.strip() for part in layers.split(",") if part.strip()]
        if layers else list(entity_view.DEFAULT_LAYERS)
    )
    unknown = [name for name in chosen_layers if name not in hierarchy.ALL_TYPES]
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown layer(s) {', '.join(unknown)}. Supported: "
            + ", ".join(hierarchy.ALL_TYPES),
        )

    # Diagnostics are a development aid and are refused outside development, so
    # resolved identifiers can never leak to a normal user in production.
    allow_diagnostics = (
        diagnostics and get_settings().environment.lower() != "production"
    )

    try:
        view = entity_view.build_entity_view(
            session, user,
            filters=scoped_filters,
            layers=chosen_layers,
            metric=metric,
            date_from=date_range.date_from,
            date_to=date_range.date_to,
            business_filters=business,
            zoom=zoom,
            diagnostics=allow_diagnostics,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "map entities") from exc

    audit.record(session, action=AuditAction.VIEW_REPORT, user_id=user.user_id,
                 username=user.username, resource="map:entities",
                 ip_address=audit.client_ip(request),
                 detail={"filters": {k: v for k, v in requested.items() if v},
                         "layers": chosen_layers, "period": date_range.label})
    session.commit()

    if diagnostics and not allow_diagnostics:
        logger.info("diagnostics requested by %s but refused outside development",
                    user.username)

    return {
        "period": date_range.model_dump(mode="json"),
        "filters": {k: v for k, v in requested.items() if v},
        "applied_filters": {k: v for k, v in scoped_filters.items() if v},
        "scope_description": user.describe_scope(),
        "metric": metric,
        "metric_label": map_data_service.METRIC_BY_KEY[metric].label
        if metric in map_data_service.METRIC_BY_KEY else metric,
        **entity_view.view_payload(session, view, layers=chosen_layers),
    }


# ---------------------------------------------------------------------------
# Coordinates
# ---------------------------------------------------------------------------


@router.get("/locations")
def list_locations(
    entity_type: str | None = Query(None, max_length=32),
    limit: int = Query(500, ge=1, le=2000),
    session: Session = Depends(get_session),
    _: UserContext = DesignerDep,
) -> dict[str, Any]:
    """Stored coordinates, and how complete the coverage is."""
    conditions = []
    if entity_type:
        _check_entity(entity_type)
        conditions.append(MapEntityLocation.entity_type == entity_type)
    rows = session.execute(
        select(MapEntityLocation).where(*conditions)
        .order_by(MapEntityLocation.entity_type, MapEntityLocation.entity_code)
        .limit(limit)
    ).scalars().all()
    return {
        "locations": [geo.location_payload(row) for row in rows],
        "coverage": geo.coverage(session),
        "sources": list(GeoSource.ALL),
        "precisions": list(GeoPrecision.ALL),
    }


@router.put("/locations")
def upsert_locations(
    request: BulkLocationRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = DesignerDep,
) -> dict[str, Any]:
    """Place one or many entities, then re-derive the levels above them."""
    saved = 0
    problems: list[str] = []
    for index, item in enumerate(request.locations):
        try:
            _check_entity(item.entity_type)
            geo.upsert_location(
                session, entity_type=item.entity_type, entity_code=item.entity_code,
                latitude=item.latitude, longitude=item.longitude,
                source=GeoSource.MANUAL, precision=item.precision,
                label=item.label, actor=user.username,
            )
            saved += 1
        except (geo.InvalidCoordinate, HTTPException, ValueError) as exc:
            detail = getattr(exc, "detail", None) or str(exc)
            problems.append(f"{item.entity_type} {item.entity_code}: {detail}")

    derived = geo.derive_parents(session, actor=user.username) \
        if request.derive_parents and saved else {}

    audit.record(session, action=AuditAction.MAP_LOCATION_UPDATED,
                 user_id=user.user_id, username=user.username,
                 resource="map-location", ip_address=audit.client_ip(http_request),
                 detail={"saved": saved, "derived": derived,
                         "problems": len(problems)},
                 success=not problems)
    session.commit()
    resolver.invalidate_cache()
    return {"saved": saved, "derived": derived, "problems": problems[:20]}


@router.post("/locations/derive")
def derive_locations(http_request: Request,
                     session: Session = Depends(get_session),
                     user: UserContext = DesignerDep) -> dict[str, Any]:
    """Recompute every parent level's centroid from the levels below it.

    Safe to re-run: a coordinate someone placed by hand is never overwritten.
    """
    try:
        derived = geo.derive_parents(session, actor=user.username)
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "derive locations") from exc
    audit.record(session, action=AuditAction.MAP_LOCATION_UPDATED,
                 user_id=user.user_id, username=user.username,
                 resource="map-location", ip_address=audit.client_ip(http_request),
                 detail={"derived": derived})
    session.commit()
    return {"derived": derived, "coverage": geo.coverage(session)}


@router.delete("/locations/{entity_type}/{entity_code}")
def delete_location(entity_type: str, entity_code: str, http_request: Request,
                    session: Session = Depends(get_session),
                    user: UserContext = DesignerDep) -> dict[str, Any]:
    """Remove one coordinate. The entity itself is untouched."""
    _check_entity(entity_type)
    row = session.execute(
        select(MapEntityLocation).where(
            MapEntityLocation.entity_type == entity_type,
            MapEntityLocation.entity_code == entity_code,
        )
    ).scalars().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No coordinate for that entity.")
    session.delete(row)
    audit.record(session, action=AuditAction.MAP_LOCATION_UPDATED,
                 user_id=user.user_id, username=user.username,
                 resource="map-location", ip_address=audit.client_ip(http_request),
                 detail={"deleted": f"{entity_type}:{entity_code}"})
    session.commit()
    resolver.invalidate_cache()
    return {"deleted": f"{entity_type}:{entity_code}"}


# ---------------------------------------------------------------------------
# Administrative areas
# ---------------------------------------------------------------------------


class AreaStyleRequest(BaseModel):
    """The four documented properties, plus the optional interaction states."""

    model_config = ConfigDict(extra="forbid")

    fill_color: str = Field(..., max_length=16)
    fill_opacity: float = Field(..., ge=0.0, le=1.0)
    stroke_color: str = Field(..., max_length=16)
    stroke_width: float = Field(..., ge=0.0, le=20.0)
    stroke_opacity: float = Field(1.0, ge=0.0, le=1.0)
    hover_fill_color: str | None = Field(None, max_length=16)
    hover_fill_opacity: float | None = Field(None, ge=0.0, le=1.0)
    selected_fill_color: str | None = Field(None, max_length=16)
    selected_fill_opacity: float | None = Field(None, ge=0.0, le=1.0)
    z_index: int = Field(1, ge=0, le=100)


def _hex_colour(value: str | None, field: str) -> str | None:
    """Accept only ``#RGB`` / ``#RRGGBB``.

    The same rule the marker schema applies. A named colour would render
    differently across the places this value is used — a MapLibre paint
    property, the SVG renderer and the legend swatch — and could not be
    interpolated when value-based colouring arrives.
    """
    if value is None:
        return None
    text = value.strip()
    if not re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", text):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{field} must be a hex colour such as #2563EB, not '{value}'.",
        )
    return text.upper()


def _viewport(bbox: str | None) -> geometry_util.BoundingBox | None:
    """Parse ``west,south,east,north`` — the order GeoJSON uses for a bbox."""
    if not bbox:
        return None
    parts = [part.strip() for part in bbox.split(",")]
    if len(parts) != 4:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "bbox must be 'west,south,east,north' in degrees.",
        )
    try:
        west, south, east, north = (float(p) for p in parts)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "bbox values must be numbers.",
        ) from exc
    return geometry_util.BoundingBox(west=west, south=south, east=east,
                                     north=north)


@router.get("/area-levels")
def area_levels(_: UserContext = MapDep) -> dict[str, Any]:
    """The administrative levels, so the frontend never hardcodes them."""
    return {
        "levels": area_service.levels_payload(),
        "default_level": area_service.DEFAULT_LEVEL,
        "layer_key": area_service.LAYER_KEY,
    }


@router.get("/areas")
def map_areas(
    request: Request,
    response: Response,
    level: str = Query(area_service.DEFAULT_LEVEL, max_length=32),
    division_code: str | None = Query(None, max_length=64),
    district_code: str | None = Query(None, max_length=64),
    upazila_code: str | None = Query(None, max_length=64),
    territory_code: str | None = Query(
        None, max_length=512,
        description="Comma-separated territory codes. Areas containing their "
                    "coordinates are returned.",
    ),
    bbox: str | None = Query(None, description="Viewport as west,south,east,north."),
    simplify: float = Query(0.0, ge=0.0, le=1.0,
                            description="Extra Douglas-Peucker tolerance in "
                                        "degrees, on top of the import-time one."),
    geometry: bool = Query(True, description="Set false for properties only."),
    date_range=Depends(date_range_params),
    filters: ScopeFilters = Depends(scope_filters),
    session: Session = Depends(get_session),
    user: UserContext = MapDep,
) -> Any:
    """Administrative areas as a GeoJSON ``FeatureCollection``.

    Cached by ETag rather than by time: boundaries change only when someone
    imports a file, so a browser that already holds them should keep them
    indefinitely and be told the moment they move.
    """
    try:
        area_service.get_level(level)
    except area_service.UnknownLevel as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    # The token covers the boundary set and the request's own shape, so two
    # different viewports never share a cache entry.
    token = area_service.generation(session, level)
    etag = (
        '"' + hashlib.sha256(
            "|".join([
                token, str(division_code), str(district_code), str(upazila_code),
                str(territory_code), str(bbox), str(simplify), str(geometry),
                str(date_range.date_from), str(date_range.date_to),
                user.username,
            ]).encode("utf-8")
        ).hexdigest()[:32] + '"'
    )
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED,
                        headers={"ETag": etag})

    area_filters = area_service.AreaFilters(
        division_code=division_code,
        district_code=district_code,
        upazila_code=upazila_code,
        territory_codes=tuple(
            code.strip() for code in (territory_code or "").split(",")
            if code.strip()
        ),
        viewport=_viewport(bbox),
    )

    try:
        result = area_service.area_features(
            session, user, level_key=level, filters=area_filters,
            business_filters=filters, date_from=date_range.date_from,
            date_to=date_range.date_to, include_geometry=geometry,
            simplify_tolerance=simplify,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "map areas") from exc

    response.headers["ETag"] = etag
    # Must revalidate: the payload depends on the caller's data scope, so a
    # shared cache holding it would be a scope leak.
    response.headers["Cache-Control"] = "private, max-age=0, must-revalidate"

    return {
        "type": "FeatureCollection",
        "level": result.level,
        "layer_key": area_service.LAYER_KEY,
        "features": result.features,
        "style": result.style,
        "total": result.total,
        "returned": len(result.features),
        "truncated": result.truncated,
        "boundaries_loaded": result.boundaries_loaded,
        "unresolved_territories": result.unresolved_territories,
        "bounds": result.bounds,
        "period": date_range.model_dump(mode="json"),
        "metric": "stock",
        "default_stock": get_settings().map_area_default_stock,
    }


@router.get("/admin-points")
def map_admin_points(
    request: Request,
    response: Response,
    kind: str = Query("capital", max_length=16,
                      description="capital or point."),
    max_level: int | None = Query(None, ge=0, le=4,
                                  description="Deepest administrative level to "
                                              "return. Omit for the layer's own."),
    bbox: str | None = Query(None, description="Viewport as west,south,east,north."),
    limit: int = Query(2000, ge=1, le=6000),
    session: Session = Depends(get_session),
    user: UserContext = MapDep,
) -> Any:
    """Published administrative reference points as GeoJSON.

    A separate endpoint from ``/areas`` because it answers a separate question:
    these are points that came with the boundary files, not polygons and not
    business entities. Cached by ETag on the same reasoning — an import is the
    only thing that moves them.
    """
    kinds = {entry["key"]: entry for entry in area_service.ADMIN_POINT_KINDS}
    if kind not in kinds:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown point layer '{kind}'. Choose one of: "
            f"{', '.join(sorted(kinds))}.")

    ceiling = max_level if max_level is not None else kinds[kind]["max_level"]

    token = area_service.admin_point_generation(session, kind)
    etag = (
        '"' + hashlib.sha256(
            "|".join([token, str(ceiling), str(bbox), str(limit)]).encode("utf-8")
        ).hexdigest()[:32] + '"'
    )
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED,
                        headers={"ETag": etag})

    try:
        rows, truncated = area_service.admin_points(
            session, kind=kind, max_level=ceiling, viewport=_viewport(bbox),
            limit=limit)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "map admin points") from exc

    response.headers["ETag"] = etag
    # These carry no business data, so unlike /areas they are not scope-dependent
    # — but the endpoint still requires the Map section, so the cache stays private.
    response.headers["Cache-Control"] = "private, max-age=0, must-revalidate"

    return {
        "type": "FeatureCollection",
        "kind": kind,
        "max_level": ceiling,
        "features": [
            {
                "type": "Feature",
                "id": row["id"],
                "properties": {k: v for k, v in row.items() if k != "id"},
                "geometry": {"type": "Point",
                             "coordinates": [row["longitude"], row["latitude"]]},
            }
            for row in rows
        ],
        "returned": len(rows),
        "truncated": truncated,
        "kinds": list(area_service.ADMIN_POINT_KINDS),
    }


@router.get("/areas/{level}/{code}")
def map_area_detail(
    level: str,
    code: str,
    date_range=Depends(date_range_params),
    filters: ScopeFilters = Depends(scope_filters),
    session: Session = Depends(get_session),
    user: UserContext = MapDep,
) -> dict[str, Any]:
    """One area's detail, for the click popup."""
    try:
        detail = area_service.area_detail(
            session, user, level, code, business_filters=filters,
            date_from=date_range.date_from, date_to=date_range.date_to,
        )
    except area_service.UnknownLevel as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "map area detail") from exc

    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"No {level} with code '{code}'.")
    return detail


@router.get("/area-styles")
def area_styles(session: Session = Depends(get_session),
                _: UserContext = ViewerDep) -> dict[str, Any]:
    """How each administrative level is painted. Readable by any map viewer,
    because it is appearance and carries no business figures."""
    return {"styles": area_service.all_styles(session),
            "coverage": area_service.coverage(session)}


@router.put("/area-styles/{level}")
def set_area_style(
    level: str,
    request: AreaStyleRequest,
    http_request: Request,
    session: Session = Depends(get_session),
    user: UserContext = DesignerDep,
) -> dict[str, Any]:
    """Change how a level is drawn. Authoring, so Map Settings is required."""
    try:
        area_service.get_level(level)
    except area_service.UnknownLevel as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    fill = _hex_colour(request.fill_color, "fill_color")
    stroke = _hex_colour(request.stroke_color, "stroke_color")
    hover = _hex_colour(request.hover_fill_color, "hover_fill_color")
    selected = _hex_colour(request.selected_fill_color, "selected_fill_color")

    row = session.execute(
        select(MapAreaStyle).where(MapAreaStyle.entity_type == level)
    ).scalar_one_or_none()
    before = area_service.style_for(session, level)

    if row is None:
        row = MapAreaStyle(entity_type=level, fill_color=fill or "#2563EB",
                           fill_opacity=request.fill_opacity,
                           stroke_color=stroke or "#2563EB",
                           stroke_width=request.stroke_width)
        session.add(row)

    row.fill_color = fill or row.fill_color
    row.fill_opacity = request.fill_opacity
    row.stroke_color = stroke or row.stroke_color
    row.stroke_opacity = request.stroke_opacity
    row.stroke_width = request.stroke_width
    row.hover_fill_color = hover
    row.hover_fill_opacity = request.hover_fill_opacity
    row.selected_fill_color = selected
    row.selected_fill_opacity = request.selected_fill_opacity
    row.z_index = request.z_index
    # Edited by hand, so it is no longer the shipped default.
    row.is_system_default = False
    row.updated_by = user.username
    session.flush()

    audit.record(session, action=AuditAction.MARKER_DESIGN_UPDATED,
                 user_id=user.user_id, username=user.username,
                 resource=f"area-style:{level}",
                 ip_address=audit.client_ip(http_request),
                 detail={"level": level, "old": before,
                         "new": {"fill_color": row.fill_color,
                                 "fill_opacity": row.fill_opacity,
                                 "stroke_color": row.stroke_color,
                                 "stroke_width": row.stroke_width}})
    session.commit()
    return area_service.style_for(session, level)


__all__ = ["router"]
