"""The business map's HTTP surface.

One reader for what the map *can* draw (``GET /config``), one for what it
*does* draw (``GET /data``), one for a selected entity's name and ancestry
(``GET /entities/{level}/{code}``), and the endpoints that compose a design.
Everything here is thin: the levels, metrics, styles and basemaps are declared
in :mod:`app.map` and published as they are, the composition rules live in
:mod:`app.map.designs`, and a figure is never computed in this module — the
data endpoint hands the question to ``get_map_layer`` through the same tool
layer every report uses, so a number on the map is the number on the report.

Two sections guard it. ``map`` is what lets a reader open the map, and every
read here requires it; ``map_settings`` is what lets somebody change what the
map draws for everyone, and every write requires that instead — CREATE for a
new or duplicated design, EDIT for changing one, DELETE for removing one.

**A refusal is answered by name.** A design the map cannot draw as described is
a 409 carrying ``error_code`` and the reason, because the reason is the whole
value to the person editing it; something that is not there is a 404. An
inactive design is a 404 to a reader who may not compose the map — it has been
taken off the shelf, and "there is a design you cannot see" would be a stranger
answer than "there is no such design".
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..ai.schemas import ScopeFilters
from ..auth import audit
from ..auth.permissions import FORBIDDEN_MESSAGE, can, require_action, require_section
from ..config import get_settings
from ..database.models_ai import AuditAction
from ..database.models_map import DesignPurpose
from ..map import basemaps, designs, entities, geo, levels, metrics, styles
from ..map import data as map_data
from ..map import locations as map_locations
from ..map.errors import DesignNotFound, MapError
from ..security.sections import Action, SectionKey
from .deps import get_session, internal_error
from .routes_dashboard import date_range_params, scope_filters, tool_context

router = APIRouter(prefix="/api/map", tags=["map"])

_VIEW = require_section(SectionKey.MAP)
_CREATE = require_action(SectionKey.MAP_SETTINGS, Action.CREATE)
_EDIT = require_action(SectionKey.MAP_SETTINGS, Action.EDIT)
_DELETE = require_action(SectionKey.MAP_SETTINGS, Action.DELETE)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LayerRequest(BaseModel):
    """One layer as the editor sends it. ``extra="forbid"`` as every schema is.

    The bounds here are the cheap, shape-level ones; what a level, a metric or
    a view mode *means* is checked by :mod:`app.map.designs` against the
    registries, and refused by name.
    """

    model_config = ConfigDict(extra="forbid")

    point_level: str = Field(min_length=1, max_length=32)
    layer_name: str | None = Field(default=None, max_length=designs.MAX_NAME_LENGTH)
    view_mode: str = Field(default="point", max_length=16)
    metric: str | None = Field(default=None, max_length=32)
    color_metric: str | None = Field(default=None, max_length=32)
    size_metric: str | None = Field(default=None, max_length=32)
    is_visible: bool = True
    min_zoom: int = Field(default=0, ge=0, le=designs.MAX_ZOOM)
    cluster_at: int | None = Field(default=None, ge=1)
    label_field: str = Field(default="name", max_length=64)
    show_label: bool = False
    label_min_zoom: int = Field(default=8, ge=0, le=designs.MAX_ZOOM)
    tooltip_fields: list[str] | None = None
    style_config: dict[str, Any] | None = None

    def to_spec(self) -> designs.LayerSpec:
        return designs.LayerSpec(
            point_level=self.point_level,
            layer_name=self.layer_name,
            view_mode=self.view_mode,
            metric=self.metric,
            color_metric=self.color_metric,
            size_metric=self.size_metric,
            is_visible=self.is_visible,
            min_zoom=self.min_zoom,
            cluster_at=self.cluster_at,
            label_field=self.label_field,
            show_label=self.show_label,
            label_min_zoom=self.label_min_zoom,
            tooltip_fields=(tuple(self.tooltip_fields)
                            if self.tooltip_fields is not None else None),
            style_config=self.style_config,
        )


class DesignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=designs.MAX_NAME_LENGTH)
    description: str | None = Field(default=None,
                                    max_length=designs.MAX_DESCRIPTION_LENGTH)
    basemap: str = Field(default=basemaps.DEFAULT_BASEMAP, max_length=32)
    default_metric: str = Field(default=metrics.DEFAULT_METRIC, max_length=32)
    #: Which map this design composes. Settable at creation and never
    #: afterwards — see :func:`app.map.designs.update_design`.
    purpose: str = Field(default=DesignPurpose.DEFAULT, max_length=16)
    layers: list[LayerRequest] = Field(min_length=1)

    def to_spec(self) -> designs.DesignSpec:
        return designs.DesignSpec(
            name=self.name,
            description=self.description,
            basemap=self.basemap,
            default_metric=self.default_metric,
            purpose=self.purpose,
            layers=tuple(layer.to_spec() for layer in self.layers),
        )


class DesignUpdateRequest(BaseModel):
    """Only the fields sent are changed.

    Every field is optional and ``model_fields_set`` decides what applies —
    "leave the description alone" and "clear the description" both travel as
    JSON ``null`` and mean opposite things. Layers may ride along so the design
    editor saves in one call; the settings drawer's toggles use the layer
    endpoint instead.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1,
                             max_length=designs.MAX_NAME_LENGTH)
    description: str | None = Field(default=None,
                                    max_length=designs.MAX_DESCRIPTION_LENGTH)
    basemap: str | None = Field(default=None, max_length=32)
    default_metric: str | None = Field(default=None, max_length=32)
    layers: list[LayerRequest] | None = Field(default=None, min_length=1)


class LayersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layers: list[LayerRequest] = Field(min_length=1)


class DuplicateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1,
                             max_length=designs.MAX_NAME_LENGTH)


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _status_for(error_code: str) -> int:
    """404 for something that is not there, 409 for something that says no."""
    return (status.HTTP_404_NOT_FOUND if error_code.endswith("_NOT_FOUND")
            else status.HTTP_409_CONFLICT)


def _refusal(exc: MapError) -> HTTPException:
    return HTTPException(
        status_code=_status_for(exc.code),
        detail={"error_code": exc.code, "message": exc.user_message},
    )


def _read(work: Callable[[], Any]) -> Any:
    try:
        return work()
    except MapError as exc:
        raise _refusal(exc) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "map") from exc


def _commit(session: Session, user: UserContext, http_request: Request, *,
            audit_action: str, describe: Callable[[Any], dict[str, Any]],
            work: Callable[[], Any]) -> Any:
    """Run one write, audit it and commit — or surface a clean refusal.

    ``describe`` turns the result into the audit detail, which also names the
    design the entry is about; it runs before the commit so the entry and the
    act it describes live or die together.
    """
    try:
        result = work()
        detail = describe(result)
        audit.record(
            session, action=audit_action, user_id=user.user_id,
            username=user.username,
            resource=f"map_design:{detail.get('design_id')}",
            ip_address=audit.client_ip(http_request), detail=detail,
        )
        session.commit()
        return result
    except MapError as exc:
        session.rollback()
        raise _refusal(exc) from exc
    except HTTPException:
        session.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        raise internal_error(exc, "map") from exc


def _may_compose(session: Session, user: UserContext) -> bool:
    return can(session, user, SectionKey.MAP_SETTINGS, Action.VIEW)


def _design_detail(design) -> dict[str, Any]:
    return {
        "design_id": design.design_id,
        "name": design.name,
        "layers": [layer.point_level for layer in design.layers],
    }


# ---------------------------------------------------------------------------
# What the map can draw
# ---------------------------------------------------------------------------


@router.get("/config")
def map_config(session: Session = Depends(get_session),
               _: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    """Everything the page needs before it asks for data.

    The basemaps the deployment offers, where the map opens, the levels a
    layer may draw and the view modes each can honour, the metrics and the
    style defaults, and how many entities of each level actually have a
    coordinate — so "nothing is showing" can be answered with "162 of 267
    sub-territories are placed" rather than a blank map.
    """
    settings = get_settings()
    try:
        return {
            "basemaps": [basemap.to_dict() for basemap in basemaps.catalogue(settings)],
            "default_basemap": basemaps.DEFAULT_BASEMAP,
            "view": basemaps.default_view(settings),
            "levels": levels.catalogue(),
            "promoted_levels": list(levels.PROMOTED_LEVELS),
            "view_modes": [dict(mode) for mode in levels.VIEW_MODES],
            "metrics": metrics.catalogue(),
            # Shapes travel with their geometry, so the browser rasterises what
            # it is sent rather than keeping a catalogue of its own — the rule
            # 0033's removal recorded, read as strictly as it can be.
            "shapes": styles.shape_catalogue(),
            "purposes": list(DesignPurpose.ALL),
            "defaults": {
                "metric": metrics.DEFAULT_METRIC,
                "color_metric": metrics.DEFAULT_COLOR_METRIC,
                "size_metric": metrics.DEFAULT_SIZE_METRIC,
                # Shape and point colour are deliberately *not* here: they are
                # style, and ``style`` below already carries them. "defaults"
                # is what a layer inherits when it names no metric or tooltip,
                # and a second copy of a value is a second thing to keep true.
                "tooltip_fields": list(designs.DEFAULT_TOOLTIP_FIELDS),
            },
            "style": styles.default_style(),
            "coverage": geo.coverage(session),
        }
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "map config") from exc


# ---------------------------------------------------------------------------
# What the map draws
# ---------------------------------------------------------------------------


def _design_for_reader(session: Session, user: UserContext,
                       design_id: int | None,
                       purpose: str = DesignPurpose.DEFAULT):
    """The design to draw: the one asked for, or the one the page opens with.

    A design of the wrong purpose is a 404 rather than a 409: from this
    endpoint's point of view there is no such design, and saying "that design
    exists but belongs to the other map" would be an error message about the
    caller's mistake rather than about the resource. The analysis endpoints
    therefore cannot be handed a demarcation design and made to draw figures
    over a composition that named none.
    """
    if design_id is None:
        design = designs.default_design(session, purpose)
        if design is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, {
                "error_code": "MAP_DESIGN_NOT_FOUND",
                "message": "No map design is configured.",
            })
        return design
    design = designs.get_design(session, design_id)
    if design.purpose != purpose:
        raise DesignNotFound(design_id)
    if not design.is_active and not _may_compose(session, user):
        raise DesignNotFound(design_id)
    return design


def _rank_metric(requested: str, layer, design, level_key: str
                 ) -> tuple[str, str | None]:
    """The metric a layer is ranked by, and a note when it is not the one asked.

    A customer's customer count is one, so a map ranked by Customer Count
    ranks its customer layer by the layer's own metric instead — and says so,
    because a ranking headed by a metric it was not built on is a lie.
    """
    if metrics.get_metric(requested).available_at(level_key):
        return requested, None
    fallback = layer.metric or design.default_metric
    if not metrics.get_metric(fallback).available_at(level_key):
        fallback = metrics.DEFAULT_METRIC
    level = levels.get_level(level_key)
    return fallback, (
        f"{metrics.get_metric(requested).label} has no meaning at "
        f"{level.label.lower()} level; this layer is ranked by "
        f"{metrics.get_metric(fallback).label} instead."
    )


@router.get("/data")
def map_data_endpoint(
    request: Request,
    design_id: int | None = Query(
        None, description="The design to draw. Defaults to the design the "
                          "map opens with."),
    layer_levels: list[str] | None = Query(
        None, alias="levels",
        description="Which of the design's layers to draw, repeated. Defaults "
                    "to the layers the design shows; a reader toggling a layer "
                    "sends the list, and nothing is saved."),
    metric: str | None = Query(
        None, max_length=32,
        description="The figure to rank and headline by. Defaults to the "
                    "design's default metric."),
    rank_limit: int = Query(5, ge=1, le=50,
                            description="How many entities each of Top and "
                                        "Bottom lists per layer."),
    date_range=Depends(date_range_params),
    filters: ScopeFilters = Depends(scope_filters),
    session: Session = Depends(get_session),
    user: UserContext = Depends(_VIEW),
) -> dict[str, Any]:
    """Every requested layer of one design, for one period and filter set.

    The figures come from ``get_map_layer`` through the same tool layer every
    report uses, under the caller's own data scope: a regional manager's map
    is their region, a filter outside it is refused rather than narrowed, and
    a user with no scope is refused rather than shown an empty map. Each layer
    carries its GeoJSON features, the entities with data but no coordinate,
    the extents and class breaks its legend needs, and a Top / Bottom ranking
    by the requested metric.
    """
    settings = get_settings()

    def work() -> dict[str, Any]:
        design = _design_for_reader(session, user, design_id)
        by_level = {layer.point_level: layer for layer in design.layers}
        if layer_levels:
            unknown = [level for level in layer_levels if level not in by_level]
            if unknown:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"'{unknown[0]}' is not a layer of the design "
                    f'"{design.name}". Its layers: {", ".join(by_level)}.',
                )
            requested = list(dict.fromkeys(layer_levels))
        else:
            requested = [layer.point_level for layer in design.layers
                         if layer.is_visible]
        metric_key = metric or design.default_metric
        if metric_key not in metrics.METRIC_BY_KEY:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f'There is no metric called "{metric_key}". Choose one of: '
                f"{', '.join(metrics.METRIC_KEYS)}.",
            )

        ctx = tool_context(session, user)
        result = map_data.map_data(
            ctx, requested, date_from=date_range.date_from,
            date_to=date_range.date_to, filters=filters,
            compare_from=date_range.compare_from,
            compare_to=date_range.compare_to,
        )
        layers_payload = []
        for layer_data in result.layers:
            layer = by_level[layer_data.level]
            rank_metric, rank_note = _rank_metric(metric_key, layer, design,
                                                  layer_data.level)
            payload = layer_data.to_dict()
            payload["layer"] = designs.layer_to_dict(layer, design)
            payload["ranking"] = layer_data.ranking(rank_metric, rank_limit)
            if rank_note:
                payload["notes"].append(rank_note)
            layers_payload.append(payload)
        return {
            "period": date_range.model_dump(mode="json"),
            "filters": {key: value for key, value
                        in filters.model_dump(mode="json").items() if value},
            "design": designs.design_to_dict(design, settings=settings),
            "metric": metric_key,
            "levels": requested,
            "empty": result.empty,
            "layers": layers_payload,
        }

    try:
        payload = work()
    except MapError as exc:
        raise _refusal(exc) from exc
    except HTTPException:
        raise
    except ValueError as exc:
        # An unknown level or metric reaching the data layer: the request was
        # readable and wrong, not a fault.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "map data") from exc

    audit.record(
        session, action=AuditAction.VIEW_REPORT, user_id=user.user_id,
        username=user.username, resource="map",
        ip_address=audit.client_ip(request),
        detail={"period": date_range.label,
                "design_id": payload["design"]["design_id"],
                "levels": payload["levels"]},
    )
    session.commit()
    return payload


@router.get("/locations")
def map_locations_endpoint(
    request: Request,
    design_id: int | None = Query(
        None, description="The demarcation design to draw. Defaults to the "
                          "one the Area Demarcation tab opens with."),
    layer_levels: list[str] | None = Query(
        None, alias="levels",
        description="Which of the design's layers to draw, repeated. Defaults "
                    "to the layers the design shows."),
    session: Session = Depends(get_session),
    user: UserContext = Depends(_VIEW),
) -> dict[str, Any]:
    """Every placed coordinate of the requested layers, and no figure at all.

    What the Area Demarcation tab draws. Each layer carries its GeoJSON points
    and the configuration that says how to draw them — shape, colour, the zoom
    it appears at, the count it clusters above — so the renderer reads one
    object per layer and decides nothing itself.

    **Unscoped, deliberately**, and :mod:`app.map.locations` carries the
    reasoning at length: you judge a boundary by seeing both sides of it, so a
    demarcation map clipped to the reader's own region cannot answer the
    question it exists for. No figure is disclosed — a code, a name, a
    coordinate and its provenance — and the same rows are already readable one
    at a time from ``/api/map/entities/{level}/{code}``.

    Viewing is audited like every other report read, so the bulk disclosure is
    on the record even though it is permitted.
    """
    settings = get_settings()

    def work() -> dict[str, Any]:
        design = _design_for_reader(session, user, design_id,
                                    DesignPurpose.DEMARCATION)
        by_level = {layer.point_level: layer for layer in design.layers}
        if layer_levels:
            unknown = [level for level in layer_levels if level not in by_level]
            if unknown:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"'{unknown[0]}' is not a layer of the design "
                    f'"{design.name}". Its layers: {", ".join(by_level)}.',
                )
            requested = list(dict.fromkeys(layer_levels))
        else:
            requested = [layer.point_level for layer in design.layers
                         if layer.is_visible]

        result = map_locations.demarcation_data(session, requested)
        layers_payload = []
        for layer_data in result.layers:
            payload = layer_data.to_dict()
            payload["layer"] = designs.layer_to_dict(by_level[layer_data.level],
                                                     design)
            layers_payload.append(payload)
        return {
            "design": designs.design_to_dict(design, settings=settings),
            "levels": requested,
            "empty": result.empty,
            "layers": layers_payload,
        }

    try:
        payload = work()
    except MapError as exc:
        raise _refusal(exc) from exc
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise internal_error(exc, "map locations") from exc

    audit.record(
        session, action=AuditAction.VIEW_REPORT, user_id=user.user_id,
        username=user.username, resource="map",
        ip_address=audit.client_ip(request),
        detail={"view": "demarcation",
                "design_id": payload["design"]["design_id"],
                "levels": payload["levels"]},
    )
    session.commit()
    return payload


@router.get("/entities/{level}/{code}")
def map_entity(level: str, code: str, session: Session = Depends(get_session),
               _: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    """One entity's name, ancestry and coordinate, for the selected-entity card.

    Names only — the figures are the point's own properties, already on the
    map — so this is master data offered to every reader, as the filter
    options are, and it is not scoped.
    """
    def work() -> dict[str, Any]:
        try:
            described = entities.describe(session, level, code)
        except ValueError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        if described is None:
            label = levels.get_level(level).label
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"There is no {label.lower()} with the code '{code}'.",
            )
        return described
    return _read(work)


# ---------------------------------------------------------------------------
# Designs
# ---------------------------------------------------------------------------


@router.get("/designs")
def list_designs(
    include_inactive: bool = Query(
        False, description="Also list designs taken off the shelf. Needs the "
                           "Map Settings section."),
    purpose: str = Query(
        DesignPurpose.DEFAULT,
        description="Which map's designs to list: analysis (figures) or "
                    "demarcation (coordinates only)."),
    session: Session = Depends(get_session),
    user: UserContext = Depends(_VIEW),
) -> dict[str, Any]:
    """Every design a reader may pick from, with its layers, the default first.

    One purpose at a time, defaulting to ``analysis``. The two tabs are
    different maps and each has its own default, so a combined list would let
    a reader choose a design the screen in front of them cannot draw.
    """
    def work() -> dict[str, Any]:
        if include_inactive and not _may_compose(session, user):
            raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN_MESSAGE)
        settings = get_settings()
        rows = designs.list_designs(session, include_inactive=include_inactive,
                                    purpose=purpose)
        default = designs.default_design(session, purpose)
        return {
            "purpose": purpose,
            "designs": [designs.design_to_dict(design, settings=settings)
                        for design in rows],
            "default_design_id": default.design_id if default else None,
        }
    return _read(work)


@router.get("/designs/{design_id}")
def get_design(design_id: int, session: Session = Depends(get_session),
               user: UserContext = Depends(_VIEW)) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        design = designs.get_design(session, design_id)
        if not design.is_active and not _may_compose(session, user):
            raise DesignNotFound(design_id)
        return designs.design_to_dict(design, settings=get_settings())
    return _read(work)


@router.post("/designs", status_code=status.HTTP_201_CREATED)
def create_design(body: DesignRequest, http_request: Request,
                  session: Session = Depends(get_session),
                  user: UserContext = Depends(_CREATE)) -> dict[str, Any]:
    settings = get_settings()
    design = _commit(
        session, user, http_request,
        audit_action=AuditAction.MAP_DESIGN_CREATED, describe=_design_detail,
        work=lambda: designs.create_design(session, user, body.to_spec(),
                                          settings=settings),
    )
    return designs.design_to_dict(design, settings=settings)


@router.put("/designs/{design_id}")
def update_design(design_id: int, body: DesignUpdateRequest, http_request: Request,
                  session: Session = Depends(get_session),
                  user: UserContext = Depends(_EDIT)) -> dict[str, Any]:
    """Change the fields sent, and the layers if they were sent."""
    settings = get_settings()
    sent = set(body.model_fields_set)
    changes = {name: getattr(body, name) for name in sent if name != "layers"}

    def work():
        design = designs.update_design(session, user, design_id, changes,
                                       settings=settings)
        if "layers" in sent and body.layers is not None:
            design = designs.replace_layers(
                session, user, design_id,
                [layer.to_spec() for layer in body.layers],
            )
        return design

    design = _commit(
        session, user, http_request,
        audit_action=AuditAction.MAP_DESIGN_UPDATED,
        describe=lambda d: {**_design_detail(d), "fields": sorted(sent)},
        work=work,
    )
    return designs.design_to_dict(design, settings=settings)


@router.put("/designs/{design_id}/layers")
def replace_layers(design_id: int, body: LayersRequest, http_request: Request,
                   session: Session = Depends(get_session),
                   user: UserContext = Depends(_EDIT)) -> dict[str, Any]:
    """The whole ordered layer list — one call for a toggle, a reorder or an edit."""
    settings = get_settings()
    design = _commit(
        session, user, http_request,
        audit_action=AuditAction.MAP_LAYERS_UPDATED,
        describe=lambda d: {
            **_design_detail(d),
            "visible": [layer.point_level for layer in d.layers if layer.is_visible],
        },
        work=lambda: designs.replace_layers(
            session, user, design_id, [layer.to_spec() for layer in body.layers],
        ),
    )
    return designs.design_to_dict(design, settings=settings)


@router.post("/designs/{design_id}/duplicate", status_code=status.HTTP_201_CREATED)
def duplicate_design(design_id: int, http_request: Request,
                     body: DuplicateRequest | None = None,
                     session: Session = Depends(get_session),
                     user: UserContext = Depends(_CREATE)) -> dict[str, Any]:
    settings = get_settings()
    design = _commit(
        session, user, http_request,
        audit_action=AuditAction.MAP_DESIGN_DUPLICATED,
        describe=lambda d: {**_design_detail(d), "source_design_id": design_id},
        work=lambda: designs.duplicate_design(
            session, user, design_id, name=body.name if body else None,
            settings=settings,
        ),
    )
    return designs.design_to_dict(design, settings=settings)


@router.post("/designs/{design_id}/default")
def set_default(design_id: int, http_request: Request,
                session: Session = Depends(get_session),
                user: UserContext = Depends(_EDIT)) -> dict[str, Any]:
    design = _commit(
        session, user, http_request,
        audit_action=AuditAction.MAP_DESIGN_DEFAULT_SET, describe=_design_detail,
        work=lambda: designs.set_default(session, user, design_id),
    )
    return designs.design_to_dict(design, settings=get_settings())


@router.post("/designs/{design_id}/activate")
def activate_design(design_id: int, http_request: Request,
                    session: Session = Depends(get_session),
                    user: UserContext = Depends(_EDIT)) -> dict[str, Any]:
    design = _commit(
        session, user, http_request,
        audit_action=AuditAction.MAP_DESIGN_ACTIVATED, describe=_design_detail,
        work=lambda: designs.set_active(session, user, design_id, True),
    )
    return designs.design_to_dict(design, settings=get_settings())


@router.post("/designs/{design_id}/deactivate")
def deactivate_design(design_id: int, http_request: Request,
                      session: Session = Depends(get_session),
                      user: UserContext = Depends(_EDIT)) -> dict[str, Any]:
    design = _commit(
        session, user, http_request,
        audit_action=AuditAction.MAP_DESIGN_DEACTIVATED, describe=_design_detail,
        work=lambda: designs.set_active(session, user, design_id, False),
    )
    return designs.design_to_dict(design, settings=get_settings())


@router.delete("/designs/{design_id}")
def delete_design(design_id: int, http_request: Request,
                  session: Session = Depends(get_session),
                  user: UserContext = Depends(_DELETE)) -> dict[str, Any]:
    """Remove a custom design. The system default refuses, by name.

    A design is configuration about how figures are shown, not a figure, so
    this is a real delete; the audit entry keeps who removed it and what it
    was called. If it was the default, the default has already returned to the
    system design by the time the row goes.
    """
    return _commit(
        session, user, http_request,
        audit_action=AuditAction.MAP_DESIGN_DELETED,
        describe=lambda result: {"design_id": result["deleted_design_id"],
                                 "name": result["name"],
                                 "layers_removed": result["layers_removed"]},
        work=lambda: designs.delete_design(session, user, design_id),
    )
