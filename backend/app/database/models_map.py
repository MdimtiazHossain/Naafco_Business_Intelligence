"""The business map: where entities are, and how a map is composed.

Four tables, rebuilt from nothing after ``0033_remove_map`` and deliberately
fewer than the eleven that revision dropped. The marker library, the boundary
store and the administrative points are gone; what remains is the two things a
configuration-driven map cannot do without.

``map_entity_locations``
    The position of one business entity. A **separate table** rather than
    latitude/longitude columns on the Phase 1 dimensions, for three reasons that
    have not changed: the master dimensions are the workbook's contract and are
    not extended by a later phase; one uniform table covers every entity type
    including ones added later; and a coordinate has its own provenance and
    lifecycle (uploaded, derived, re-derived) that does not belong in a
    slowly-changing master record.

``map_designs`` / ``map_layers`` / ``map_point_configurations``
    Composition. A design is the thing a reader picks from the settings drawer;
    its layers say *which* business levels are drawn, in what order, aggregated
    at which level and measured by which metric; and a layer's point
    configuration is how its points are labelled and what the tooltip shows.
    The database is the source of truth for all three — the frontend carries no
    design of its own — so an administrator can create a new map without a
    developer.

No column here holds a figure. Sales, targets, achievement and growth are
computed at request time through the same query layer every report uses, and a
design only names *which* of them to draw.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .models import SURROGATE_PK, Base, TimestampMixin
from .models_warehouse import FK_TYPE, JSON_TYPE


# ===========================================================================
# Where things are
# ===========================================================================


class GeoSource:
    """How a coordinate was obtained. Kept because provenance decides trust.

    An uploaded coordinate is authoritative. A derived one is a centroid of the
    children below it — useful for drawing a region without anyone having to
    place it by hand, but it must not be presented as a surveyed position, and it
    must be recomputed rather than edited.
    """

    UPLOAD = "UPLOAD"
    MANUAL = "MANUAL"
    DERIVED = "DERIVED"
    GEOCODED = "GEOCODED"

    ALL = (UPLOAD, MANUAL, DERIVED, GEOCODED)
    #: Sources a user set deliberately; a derived value never overwrites these.
    AUTHORITATIVE = (UPLOAD, MANUAL, GEOCODED)


class GeoPrecision:
    EXACT = "EXACT"
    APPROXIMATE = "APPROXIMATE"
    CENTROID = "CENTROID"

    ALL = (EXACT, APPROXIMATE, CENTROID)


class MapEntityLocation(Base):
    """The position of one business entity on the map."""

    __tablename__ = "map_entity_locations"

    location_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                             autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_code: Mapped[str] = mapped_column(String(64), nullable=False)
    #: WGS-84 degrees. Stored as float: a marker does not need survey precision,
    #: and a float survives JSON round-trips without a decimal-string dance.
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=GeoSource.UPLOAD)
    precision: Mapped[str] = mapped_column(String(16), nullable=False,
                                           default=GeoPrecision.APPROXIMATE)
    #: Cached name, so the map can label a point without joining every dimension.
    label: Mapped[str | None] = mapped_column(Text)
    #: How many child locations a DERIVED centroid was computed from.
    derived_from: Mapped[int | None] = mapped_column(Integer)
    updated_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("entity_type", "entity_code",
                         name="uq_map_entity_location"),
        Index("ix_map_entity_locations_entity_type", "entity_type"),
    )


# ===========================================================================
# How a map is composed
# ===========================================================================


class LayerViewMode:
    """How a layer draws its entities.

    ``POINT`` is the only mode a business level can honour today: no source
    states a polygon for a zone, a region or a territory, and this platform does
    not draw one it was not given. ``BOUNDARY`` and ``BOTH`` are declared so a
    design can ask for them, and the renderer answers "no boundary source for
    this level" rather than inventing an outline — the same rule as a figure
    that cannot be computed reading ``n/a`` rather than zero.
    """

    POINT = "point"
    BOUNDARY = "boundary"
    BOTH = "both"

    ALL = (POINT, BOUNDARY, BOTH)


class DesignPurpose:
    """Which map a design composes.

    The two tabs of the Business Map page are different maps, not two views of
    one: ``ANALYSIS`` draws figures — sized and coloured by a metric, over a
    period, inside the reader's data scope — and ``DEMARCATION`` draws
    coordinates and nothing else, so an area can be judged by eye.

    They are told apart by a column rather than by convention because a design
    is *offered* to a reader: without this, an analysis reader could pick the
    demarcation design from the same dropdown and get a map that is neither.
    Everything else about the two is genuinely shared — validation, ordering,
    permissions, the audit actions, per-layer zoom and clustering — which is why
    this is one table with a discriminator and not two.
    """

    ANALYSIS = "analysis"
    DEMARCATION = "demarcation"

    ALL = (ANALYSIS, DEMARCATION)
    DEFAULT = ANALYSIS


class MapDesign(Base, TimestampMixin):
    """One saved map: a name, a basemap, a default metric and its layers.

    ``is_system_default`` marks the design seeded by the migration. It cannot be
    deleted, because the page has to have something to open with, and every
    other design is ordinary, editable, deletable data. ``is_default`` is which
    design the page opens with — a system design is the default until an
    administrator promotes another one.

    ``is_default`` is unique **within a purpose**, not across the table: each
    tab has to have a design to open on, so making a demarcation design the
    default must not leave the analysis map with none.
    """

    __tablename__ = "map_designs"

    design_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                           autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512))
    #: Which map this design composes — see :class:`DesignPurpose`. Carries a
    #: server default so every design written before ``0035`` is an analysis
    #: design without the migration having to update a single row.
    purpose: Mapped[str] = mapped_column(String(16), nullable=False,
                                         default=DesignPurpose.DEFAULT,
                                         server_default=DesignPurpose.DEFAULT)
    #: A named basemap style, resolved against the configured providers.
    basemap: Mapped[str] = mapped_column(String(32), nullable=False,
                                         default="standard")
    #: The metric a layer inherits when it names none of its own, so a design
    #: can be re-pointed at Achievement % in one edit rather than seven.
    default_metric: Mapped[str] = mapped_column(String(32), nullable=False,
                                                default="net_sales")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                             default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                            default=True)
    is_system_default: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                    default=False)
    created_by: Mapped[str | None] = mapped_column(String(64))

    layers: Mapped[list["MapLayer"]] = relationship(
        back_populates="design", cascade="all, delete-orphan",
        order_by="MapLayer.display_order",
    )

    __table_args__ = (
        UniqueConstraint("name", name="uq_map_designs_name"),
        Index("ix_map_designs_active", "is_active"),
        Index("ix_map_designs_default", "is_default"),
        Index("ix_map_designs_purpose", "purpose"),
    )


class MapLayer(Base, TimestampMixin):
    """One business level inside a design, and how it is measured.

    One layer per level per design (``uq_map_layers_design_level``): a second
    layer for the same level would be two answers to one question, and the
    renderer would draw both. ``min_zoom`` and ``cluster_at`` are what let six
    layers coexist on one map — a layer that only appears past a zoom, and
    clusters above a point count, does not become the single blue mass that
    made the previous design draw one level at a time.
    """

    __tablename__ = "map_layers"

    layer_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                          autoincrement=True)
    design_id: Mapped[int] = mapped_column(
        FK_TYPE, ForeignKey("map_designs.design_id", ondelete="CASCADE"),
        nullable=False,
    )
    layer_name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Which business level this layer aggregates and draws. Validated in the
    #: service against the level registry rather than by a CHECK, so adding a
    #: level stays a code change in one place instead of a migration.
    point_level: Mapped[str] = mapped_column(String(32), nullable=False)
    view_mode: Mapped[str] = mapped_column(String(16), nullable=False,
                                           default=LayerViewMode.POINT)
    #: NULL means "inherit the design's default_metric".
    metric: Mapped[str | None] = mapped_column(String(32))
    color_metric: Mapped[str | None] = mapped_column(String(32))
    size_metric: Mapped[str | None] = mapped_column(String(32))
    is_visible: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                             default=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False,
                                               default=0)
    min_zoom: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cluster_at: Mapped[int | None] = mapped_column(Integer)
    configuration_json: Mapped[dict | None] = mapped_column(JSON_TYPE)

    design: Mapped[MapDesign] = relationship(back_populates="layers")
    point_config: Mapped["MapPointConfiguration | None"] = relationship(
        back_populates="layer", cascade="all, delete-orphan", uselist=False,
    )

    __table_args__ = (
        UniqueConstraint("design_id", "point_level",
                         name="uq_map_layers_design_level"),
        Index("ix_map_layers_design", "design_id"),
        Index("ix_map_layers_order", "design_id", "display_order"),
    )


class MapPointConfiguration(Base, TimestampMixin):
    """How one layer's points are labelled and described.

    Separate from the layer because it is edited by a different person for a
    different reason: a layer is composition ("draw territories, sized by
    sales"), and this is presentation ("label them by name, show these five
    fields on hover"). One row per layer, created with it.
    """

    __tablename__ = "map_point_configurations"

    config_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                           autoincrement=True)
    layer_id: Mapped[int] = mapped_column(
        FK_TYPE, ForeignKey("map_layers.layer_id", ondelete="CASCADE"),
        nullable=False,
    )
    label_field: Mapped[str | None] = mapped_column(String(64))
    show_label: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                             default=False)
    #: Above this zoom the label appears; below it the point is a dot. The
    #: answer to "too many labels" that does not require choosing between
    #: legible and informative.
    label_min_zoom: Mapped[int] = mapped_column(Integer, nullable=False,
                                                default=8)
    tooltip_fields: Mapped[list | None] = mapped_column(JSON_TYPE)
    style_config: Mapped[dict | None] = mapped_column(JSON_TYPE)

    layer: Mapped[MapLayer] = relationship(back_populates="point_config")

    __table_args__ = (
        UniqueConstraint("layer_id", name="uq_map_point_config_layer"),
    )


__all__ = [
    "DesignPurpose",
    "GeoPrecision",
    "GeoSource",
    "LayerViewMode",
    "MapDesign",
    "MapEntityLocation",
    "MapLayer",
    "MapPointConfiguration",
]
