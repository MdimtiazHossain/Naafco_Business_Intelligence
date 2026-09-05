"""Composing the map: designs, their layers, and the rules that keep a saved
design drawable.

A *design* is the thing a reader picks from the settings drawer; its *layers*
say which business levels are drawn, in what order, aggregated at which level
and measured by which metric; and a layer's *point configuration* is how its
points are labelled and what its tooltip shows. The database is the source of
truth for all three — the frontend carries no design of its own — so an
administrator can create a new map without a developer.

**Everything a design names is checked against the registries, at write
time.** A level against :mod:`app.map.levels`, a metric against
:mod:`app.map.metrics` (and against the level, because a customer's customer
count is one), a view mode against what the level can honour, a basemap
against what the deployment configures and a style override against
:mod:`app.map.styles`. A saved design that the renderer would have to refuse is
a design somebody will open and find blank; refusing it here, by name, is what
keeps the settings screen honest.

**A design inherits where it says nothing.** A layer with no metric draws the
design's ``default_metric``, no colour metric means Achievement %, no size
metric means Sales Amount and no tooltip fields means the standard six — so
re-pointing a whole map is one edit, and the payload always carries the
*effective* value beside the stored one so the editor can show which is which.

**The system default is protected from deletion and deactivation, and from
nothing else.** The page must open with something; the seeded design is what
guarantees that. Its layers, metrics and name are ordinary editable data.
Every other design is ordinary data throughout, and deleting one is a real
delete — a design is configuration about how figures are shown, not a figure,
and the audit trail keeps who removed it and what it was called.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..ai.permission_filter import UserContext
from ..config import Settings
from ..database.models_map import (
    DesignPurpose,
    LayerViewMode,
    MapDesign,
    MapLayer,
    MapPointConfiguration,
)
from . import basemaps, levels, metrics, styles
from .errors import (
    DesignInactive,
    DesignNameTaken,
    DesignNotFound,
    DesignProtected,
    InvalidDesign,
    InvalidLayer,
)

#: What a tooltip shows when a layer names nothing: the six figures the
#: specification pictures, in its order. Declared here rather than in the
#: browser so "do not hard-code the tooltip" holds on both sides.
DEFAULT_TOOLTIP_FIELDS: tuple[str, ...] = (
    "net_sales", "target_amount", "achievement", "growth", "volume",
    "customer_count",
)

#: Non-metric properties a label or a tooltip may show. Every metric key is
#: allowed too — a point labelled by its sales figure is a legitimate design.
POINT_FIELDS: tuple[str, ...] = ("name", "code")

MAX_ZOOM = 22
MAX_NAME_LENGTH = 128
MAX_DESCRIPTION_LENGTH = 512

#: The fields :func:`update_design` accepts. Layers are replaced through
#: :func:`replace_layers`, and the flags through their own functions, because
#: each of those is an act with its own audit entry.
DESIGN_FIELDS: tuple[str, ...] = ("name", "description", "basemap", "default_metric")


@dataclass(frozen=True)
class LayerSpec:
    """One layer as a caller describes it. Order in the list is display order."""

    point_level: str
    layer_name: str | None = None
    view_mode: str = LayerViewMode.POINT
    metric: str | None = None
    color_metric: str | None = None
    size_metric: str | None = None
    is_visible: bool = True
    min_zoom: int = 0
    cluster_at: int | None = None
    label_field: str = "name"
    show_label: bool = False
    label_min_zoom: int = 8
    tooltip_fields: tuple[str, ...] | None = None
    style_config: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class DesignSpec:
    """A design as a caller describes it, layers included."""

    name: str
    description: str | None = None
    basemap: str = basemaps.DEFAULT_BASEMAP
    default_metric: str = metrics.DEFAULT_METRIC
    #: Which map this design composes. Fixed at creation and never edited —
    #: see :func:`update_design`.
    purpose: str = DesignPurpose.DEFAULT
    layers: tuple[LayerSpec, ...] = ()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _metric_at(level: levels.MapLevel, key: str, field: str) -> None:
    metric = metrics.METRIC_BY_KEY.get(key)
    if metric is None:
        raise InvalidLayer(level.key, field, (
            f"{level.label}: there is no metric called \"{key}\". "
            f"Choose one of: {', '.join(metrics.METRIC_KEYS)}."
        ))
    if not metric.available_at(level.key):
        raise InvalidLayer(level.key, field, (
            f"{level.label}: {metric.label} has no meaning at this level and "
            f"cannot be drawn there."
        ))


def _zoom(level: levels.MapLevel, value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_ZOOM:
        raise InvalidLayer(level.key, field, (
            f"{level.label}: {field} must be a whole number between 0 and "
            f"{MAX_ZOOM}."
        ))
    return value


def validate_layer(spec: LayerSpec, *, design_metric: str) -> levels.MapLevel:
    """Check one layer against every registry it names. Returns its level."""
    try:
        level = levels.get_level(spec.point_level)
    except ValueError:
        raise InvalidLayer(spec.point_level, "point_level", (
            f"There is no map level called \"{spec.point_level}\". Choose one "
            f"of: {', '.join(levels.LEVEL_KEYS)}."
        )) from None

    if spec.view_mode not in levels.view_modes_for(level):
        if spec.view_mode in LayerViewMode.ALL:
            reason = (
                f"{level.label} has no boundary source, so a layer at this "
                f"level can only be drawn as points."
            )
        else:
            reason = (
                f"{level.label}: \"{spec.view_mode}\" is not a view mode. "
                f"Choose one of: {', '.join(LayerViewMode.ALL)}."
            )
        raise InvalidLayer(level.key, "view_mode", reason)

    if spec.layer_name is not None and not spec.layer_name.strip():
        raise InvalidLayer(level.key, "layer_name",
                           f"{level.label}: the layer name cannot be blank.")
    if spec.layer_name is not None and len(spec.layer_name) > MAX_NAME_LENGTH:
        raise InvalidLayer(level.key, "layer_name",
                           f"{level.label}: the layer name is too long.")

    # A layer that names no metric draws the design's default, so the default
    # has to be drawable here too — otherwise the layer is saved and blank.
    _metric_at(level, spec.metric or design_metric,
               "metric" if spec.metric else "default_metric")
    _metric_at(level, spec.color_metric or metrics.DEFAULT_COLOR_METRIC, "color_metric")
    _metric_at(level, spec.size_metric or metrics.DEFAULT_SIZE_METRIC, "size_metric")

    _zoom(level, spec.min_zoom, "min_zoom")
    _zoom(level, spec.label_min_zoom, "label_min_zoom")
    if spec.cluster_at is not None and (
        isinstance(spec.cluster_at, bool) or not isinstance(spec.cluster_at, int)
        or spec.cluster_at < 1
    ):
        raise InvalidLayer(level.key, "cluster_at", (
            f"{level.label}: cluster_at must be a whole number of points, at "
            f"least 1, or empty to never cluster."
        ))

    allowed_fields = (*POINT_FIELDS, *metrics.METRIC_KEYS)
    if spec.label_field not in allowed_fields:
        raise InvalidLayer(level.key, "label_field", (
            f"{level.label}: a label can show {', '.join(POINT_FIELDS)} or a "
            f"metric, not \"{spec.label_field}\"."
        ))
    if spec.tooltip_fields is not None:
        if not spec.tooltip_fields:
            raise InvalidLayer(level.key, "tooltip_fields", (
                f"{level.label}: list at least one tooltip field, or leave it "
                f"empty to show the standard set."
            ))
        for name in spec.tooltip_fields:
            if name not in ("code", *metrics.METRIC_KEYS):
                raise InvalidLayer(level.key, "tooltip_fields", (
                    f"{level.label}: a tooltip can show code or a metric, not "
                    f"\"{name}\"."
                ))
            if name in metrics.METRIC_BY_KEY:
                _metric_at(level, name, "tooltip_fields")

    try:
        styles.effective_style(spec.style_config)
    except ValueError as exc:
        raise InvalidLayer(level.key, "style_config",
                           f"{level.label}: {exc}") from exc
    return level


def validate_layers(specs: Sequence[LayerSpec], *, design_metric: str) -> None:
    """Check a whole layer list: each layer, and one layer per level."""
    if not specs:
        raise InvalidDesign("A map design needs at least one layer.")
    seen: set[str] = set()
    for spec in specs:
        level = validate_layer(spec, design_metric=design_metric)
        if level.key in seen:
            raise InvalidLayer(level.key, "point_level", (
                f"{level.label} appears twice. A design draws each level "
                f"once; give the layer a different level or remove one."
            ))
        seen.add(level.key)


def _validate_name(session: Session, name: str, *,
                   exclude_design_id: int | None = None) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise InvalidDesign("A map design needs a name.")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise InvalidDesign(f"A design name is at most {MAX_NAME_LENGTH} characters.")
    statement = select(MapDesign.design_id).where(
        func.lower(MapDesign.name) == cleaned.lower()
    )
    if exclude_design_id is not None:
        statement = statement.where(MapDesign.design_id != exclude_design_id)
    if session.execute(statement).first() is not None:
        raise DesignNameTaken(cleaned)
    return cleaned


def _validate_description(description: str | None) -> str | None:
    if description is None:
        return None
    cleaned = description.strip()
    if len(cleaned) > MAX_DESCRIPTION_LENGTH:
        raise InvalidDesign(
            f"A design description is at most {MAX_DESCRIPTION_LENGTH} characters."
        )
    return cleaned or None


def _validate_basemap(key: str, settings: Settings | None) -> str:
    available = [basemap.key for basemap in basemaps.catalogue(settings)]
    if key not in available:
        raise InvalidDesign(
            f"There is no basemap called \"{key}\" in this deployment. "
            f"Available: {', '.join(available)}."
        )
    return key


def _validate_default_metric(key: str) -> str:
    if key not in metrics.METRIC_BY_KEY:
        raise InvalidDesign(
            f"There is no metric called \"{key}\". Choose one of: "
            f"{', '.join(metrics.METRIC_KEYS)}."
        )
    return key


def _validate_purpose(key: str) -> str:
    if key not in DesignPurpose.ALL:
        raise InvalidDesign(
            f"There is no map called \"{key}\". Choose one of: "
            f"{', '.join(DesignPurpose.ALL)}."
        )
    return key


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _with_layers():
    return select(MapDesign).options(
        selectinload(MapDesign.layers).selectinload(MapLayer.point_config)
    )


def list_designs(session: Session, *, include_inactive: bool = False,
                 purpose: str = DesignPurpose.DEFAULT) -> list[MapDesign]:
    """Every design a reader may pick from: the default first, then by name.

    Filtered to one purpose, and defaulting to ``analysis`` rather than to
    everything: the two tabs are different maps, and offering a demarcation
    design in the analysis tab's dropdown would let a reader pick a map that
    draws no figures from the screen whose whole subject is figures.
    """
    statement = _with_layers().where(MapDesign.purpose == _validate_purpose(purpose))
    if not include_inactive:
        statement = statement.where(MapDesign.is_active.is_(True))
    statement = statement.order_by(MapDesign.is_default.desc(), MapDesign.name)
    return list(session.execute(statement).scalars().unique().all())


def get_design(session: Session, design_id: int) -> MapDesign:
    design = session.execute(
        _with_layers().where(MapDesign.design_id == design_id)
    ).scalars().unique().one_or_none()
    if design is None:
        raise DesignNotFound(design_id)
    return design


def system_default(session: Session,
                   purpose: str = DesignPurpose.DEFAULT) -> MapDesign | None:
    return session.execute(
        _with_layers().where(MapDesign.is_system_default.is_(True),
                             MapDesign.purpose == purpose)
        .order_by(MapDesign.design_id)
    ).scalars().unique().first()


def default_design(session: Session,
                   purpose: str = DesignPurpose.DEFAULT) -> MapDesign | None:
    """The design the named map opens with.

    The one marked default, if it is active; else the system default; else
    whichever active design sorts first. ``None`` only when no design of that
    purpose exists at all, which the seeds prevent.

    Scoped to one purpose all the way down, because each tab has to have
    something to open on: an analysis reader must never be handed the
    demarcation design because it happened to sort first.
    """
    purpose = _validate_purpose(purpose)
    marked = session.execute(
        _with_layers().where(MapDesign.is_default.is_(True),
                             MapDesign.is_active.is_(True),
                             MapDesign.purpose == purpose)
        .order_by(MapDesign.design_id)
    ).scalars().unique().first()
    if marked is not None:
        return marked
    fallback = system_default(session, purpose)
    if fallback is not None:
        return fallback
    return session.execute(
        _with_layers().where(MapDesign.is_active.is_(True),
                             MapDesign.purpose == purpose)
        .order_by(MapDesign.name)
    ).scalars().unique().first()


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _apply_layer(layer: MapLayer, spec: LayerSpec, level: levels.MapLevel,
                 order: int) -> None:
    layer.layer_name = (spec.layer_name or level.label).strip()
    layer.point_level = level.key
    layer.view_mode = spec.view_mode
    layer.metric = spec.metric
    layer.color_metric = spec.color_metric
    layer.size_metric = spec.size_metric
    layer.is_visible = bool(spec.is_visible)
    layer.display_order = order
    layer.min_zoom = spec.min_zoom
    layer.cluster_at = spec.cluster_at
    config = layer.point_config
    if config is None:
        config = MapPointConfiguration()
        layer.point_config = config
    config.label_field = spec.label_field
    config.show_label = bool(spec.show_label)
    config.label_min_zoom = spec.label_min_zoom
    config.tooltip_fields = (
        list(dict.fromkeys(spec.tooltip_fields)) if spec.tooltip_fields else None
    )
    config.style_config = dict(spec.style_config) if spec.style_config else None


def _set_layers(session: Session, design: MapDesign,
                specs: Sequence[LayerSpec]) -> None:
    """Make the design's layers match ``specs``, in that order.

    Matched by level rather than replaced wholesale, so a layer that survives
    keeps its id: the editor may hold one open while the list is saved, and a
    URL may name one.
    """
    validate_layers(specs, design_metric=design.default_metric)
    existing = {layer.point_level: layer for layer in design.layers}
    kept: list[MapLayer] = []
    for order, spec in enumerate(specs, start=1):
        level = levels.get_level(spec.point_level)
        layer = existing.pop(level.key, None)
        if layer is None:
            # Appended, never constructed with ``design=``: the back-reference
            # would add it to the collection a second time, and a layer that
            # is in the list twice is drawn twice and counted twice.
            layer = MapLayer()
            design.layers.append(layer)
        _apply_layer(layer, spec, level, order)
        kept.append(layer)
    for dropped in existing.values():
        design.layers.remove(dropped)
        session.delete(dropped)
    # The relationship is ordered by display_order on load; keep the in-memory
    # list in the same order so a payload built before the next load agrees.
    design.layers.sort(key=lambda layer: layer.display_order)
    session.flush()


def create_design(session: Session, user: UserContext, spec: DesignSpec, *,
                  settings: Settings | None = None) -> MapDesign:
    """A new, active, non-default design with the layers given."""
    name = _validate_name(session, spec.name)
    design = MapDesign(
        name=name,
        description=_validate_description(spec.description),
        basemap=_validate_basemap(spec.basemap, settings),
        default_metric=_validate_default_metric(spec.default_metric),
        purpose=_validate_purpose(spec.purpose),
        is_default=False,
        is_active=True,
        is_system_default=False,
        created_by=user.username,
    )
    session.add(design)
    _set_layers(session, design, spec.layers)
    return design


def update_design(session: Session, user: UserContext, design_id: int,
                  changes: Mapping[str, Any], *,
                  settings: Settings | None = None) -> MapDesign:
    """Change the named fields and nothing else.

    ``changes`` carries only what the caller set — "leave the description
    alone" and "clear the description" both travel as JSON ``null`` and mean
    opposite things, so presence is what decides, not value.

    ``purpose`` is deliberately not among ``DESIGN_FIELDS`` and so is refused
    by name like any other unknown field. Moving a design between the two maps
    would change what its settings *mean*: a demarcation layer's metrics are
    unread and left at their defaults, and the same layer on the analysis tab
    would suddenly draw by them. Duplicating into the other purpose is the
    honest way to do that, and it leaves the original where readers expect it.
    """
    design = get_design(session, design_id)
    unknown = sorted(set(changes) - set(DESIGN_FIELDS))
    if unknown:
        raise InvalidDesign(
            f"A design has no field called {', '.join(unknown)}. "
            f"Editable: {', '.join(DESIGN_FIELDS)}."
        )
    if "name" in changes:
        design.name = _validate_name(session, changes["name"],
                                     exclude_design_id=design.design_id)
    if "description" in changes:
        design.description = _validate_description(changes["description"])
    if "basemap" in changes:
        design.basemap = _validate_basemap(changes["basemap"], settings)
    if "default_metric" in changes:
        # Every layer that inherits the design's metric has to be able to draw
        # the new one, or the edit saves a blank layer.
        new_metric = _validate_default_metric(changes["default_metric"])
        for layer in design.layers:
            validate_layer(_spec_of(layer), design_metric=new_metric)
        design.default_metric = new_metric
    session.flush()
    return design


def replace_layers(session: Session, user: UserContext, design_id: int,
                   specs: Sequence[LayerSpec]) -> MapDesign:
    """The whole ordered layer list, as it should be from now on."""
    design = get_design(session, design_id)
    _set_layers(session, design, specs)
    return design


def _copy_name(session: Session, name: str) -> str:
    """``"X Copy"``, then ``"X Copy 2"`` and so on, never a name in use."""
    base = f"{name} Copy"
    candidate = base
    counter = 2
    while True:
        taken = session.execute(
            select(MapDesign.design_id)
            .where(func.lower(MapDesign.name) == candidate.lower())
        ).first()
        if taken is None and len(candidate) <= MAX_NAME_LENGTH:
            return candidate
        candidate = f"{base} {counter}"
        counter += 1


def _spec_of(layer: MapLayer) -> LayerSpec:
    config = layer.point_config
    return LayerSpec(
        point_level=layer.point_level,
        layer_name=layer.layer_name,
        view_mode=layer.view_mode,
        metric=layer.metric,
        color_metric=layer.color_metric,
        size_metric=layer.size_metric,
        is_visible=layer.is_visible,
        min_zoom=layer.min_zoom,
        cluster_at=layer.cluster_at,
        label_field=(config.label_field if config and config.label_field else "name"),
        show_label=bool(config.show_label) if config else False,
        label_min_zoom=config.label_min_zoom if config else 8,
        tooltip_fields=(tuple(config.tooltip_fields)
                        if config and config.tooltip_fields else None),
        style_config=(dict(config.style_config)
                      if config and config.style_config else None),
    )


def duplicate_design(session: Session, user: UserContext, design_id: int, *,
                     name: str | None = None,
                     settings: Settings | None = None) -> MapDesign:
    """An independent copy: every layer and configuration, none of the flags.

    The copy is active, not the default and never system-protected, whatever
    the original was — which is how a protected design gets edited: duplicate
    it and change the copy.
    """
    source = get_design(session, design_id)
    spec = DesignSpec(
        name=name.strip() if name and name.strip() else _copy_name(session, source.name),
        description=source.description,
        basemap=source.basemap,
        default_metric=source.default_metric,
        # The copy composes the same map as its original: duplicating the
        # demarcation design to edit it must not land the copy in the analysis
        # tab, where its unread metrics would suddenly decide how it draws.
        purpose=source.purpose,
        layers=tuple(_spec_of(layer) for layer in source.layers),
    )
    return create_design(session, user, spec, settings=settings)


def set_default(session: Session, user: UserContext, design_id: int) -> MapDesign:
    """Make this the design its own map opens with. Exactly one per purpose is.

    Scoped to the design's purpose: promoting a demarcation design must not
    clear the analysis map's default and leave that tab with nothing to open
    on. There is one default per map, not one per table.
    """
    design = get_design(session, design_id)
    if not design.is_active:
        raise DesignInactive(design.name)
    for other in session.execute(
        select(MapDesign).where(MapDesign.is_default.is_(True),
                                MapDesign.purpose == design.purpose)
    ).scalars().all():
        other.is_default = False
    design.is_default = True
    session.flush()
    return design


def _hand_default_to_system(session: Session, leaving: MapDesign) -> None:
    """When a default goes, its own map's system design takes over.

    Within the purpose, for the same reason :func:`set_default` is: the
    analysis map's fallback is no use to the demarcation tab, and handing the
    flag across would take it away from the tab that still needs it.
    """
    if not leaving.is_default:
        return
    leaving.is_default = False
    fallback = system_default(session, leaving.purpose)
    if fallback is not None and fallback.design_id != leaving.design_id:
        fallback.is_default = True


def set_active(session: Session, user: UserContext, design_id: int,
               active: bool) -> MapDesign:
    """Hide a design from readers, or offer it again. Reversible."""
    design = get_design(session, design_id)
    if not active and design.is_system_default:
        raise DesignProtected(design.name, "deactivated")
    if not active:
        _hand_default_to_system(session, design)
    design.is_active = bool(active)
    session.flush()
    return design


def delete_design(session: Session, user: UserContext, design_id: int
                  ) -> dict[str, Any]:
    """Remove a custom design, its layers and their configurations.

    The system default is refused by name. If the design being removed was the
    default, the default returns to the system design first, so the map never
    opens on a design that no longer exists.
    """
    design = get_design(session, design_id)
    if design.is_system_default:
        raise DesignProtected(design.name, "deleted")
    _hand_default_to_system(session, design)
    name = design.name
    purpose = design.purpose
    layer_count = len(design.layers)
    session.delete(design)
    session.flush()
    # The fallback the *caller* now opens on, which is their own map's — read
    # before the delete, because the design is gone by the time we ask.
    fallback = default_design(session, purpose)
    return {
        "deleted_design_id": design_id,
        "name": name,
        "layers_removed": layer_count,
        "default_design_id": fallback.design_id if fallback else None,
    }


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


def layer_to_dict(layer: MapLayer, design: MapDesign) -> dict[str, Any]:
    """A layer with every inherited value resolved beside the stored one."""
    level = levels.get_level(layer.point_level)
    config = layer.point_config
    effective_metric = layer.metric or design.default_metric
    color_metric = layer.color_metric or metrics.DEFAULT_COLOR_METRIC
    size_metric = layer.size_metric or metrics.DEFAULT_SIZE_METRIC
    tooltip = list(config.tooltip_fields) if config and config.tooltip_fields else None
    style_config = dict(config.style_config) if config and config.style_config else None
    return {
        "layer_id": layer.layer_id,
        "layer_name": layer.layer_name,
        "point_level": level.key,
        "level_label": level.label,
        "parent_level": level.parent,
        "view_mode": layer.view_mode,
        "view_modes": list(levels.view_modes_for(level)),
        "boundary_available": level.boundary_available,
        "metric": layer.metric,
        "effective_metric": effective_metric,
        "color_metric": layer.color_metric,
        "effective_color_metric": color_metric,
        "color_mode": styles.color_mode(metrics.get_metric(color_metric)),
        "size_metric": layer.size_metric,
        "effective_size_metric": size_metric,
        "is_visible": layer.is_visible,
        "display_order": layer.display_order,
        "min_zoom": layer.min_zoom,
        "cluster_at": layer.cluster_at,
        "label_field": (config.label_field if config and config.label_field else "name"),
        "show_label": bool(config.show_label) if config else False,
        "label_min_zoom": config.label_min_zoom if config else 8,
        "tooltip_fields": tooltip or [
            key for key in DEFAULT_TOOLTIP_FIELDS
            if metrics.get_metric(key).available_at(level.key)
        ],
        "tooltip_inherited": tooltip is None,
        "style_config": style_config,
        "style": styles.effective_style(style_config),
    }


def design_to_dict(design: MapDesign, *, settings: Settings | None = None,
                   include_layers: bool = True) -> dict[str, Any]:
    basemap, note = basemaps.resolve(design.basemap, settings)
    payload: dict[str, Any] = {
        "design_id": design.design_id,
        "name": design.name,
        "description": design.description,
        "purpose": design.purpose,
        "basemap": design.basemap,
        "basemap_resolved": basemap.to_dict(),
        "basemap_note": note,
        "default_metric": design.default_metric,
        "is_default": design.is_default,
        "is_active": design.is_active,
        "is_system_default": design.is_system_default,
        "created_by": design.created_by,
        "created_at": design.created_at.isoformat() if design.created_at else None,
        "updated_at": design.updated_at.isoformat() if design.updated_at else None,
        "layer_count": len(design.layers),
    }
    if include_layers:
        payload["layers"] = [
            layer_to_dict(layer, design)
            for layer in sorted(design.layers, key=lambda l: l.display_order)
        ]
    return payload


__all__ = [
    "DEFAULT_TOOLTIP_FIELDS",
    "DESIGN_FIELDS",
    "DesignSpec",
    "LayerSpec",
    "MAX_ZOOM",
    "POINT_FIELDS",
    "create_design",
    "default_design",
    "delete_design",
    "design_to_dict",
    "duplicate_design",
    "get_design",
    "layer_to_dict",
    "list_designs",
    "replace_layers",
    "set_active",
    "set_default",
    "system_default",
    "update_design",
    "validate_layer",
    "validate_layers",
]
