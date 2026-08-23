"""Marker and shape designs for the business map.

Four tables, each with one job:

``map_marker_designs``
    The design itself — a name, an entity type, and a validated definition
    document. The definition is JSON rather than thirty columns because it is a
    *document* whose shape is owned by :mod:`app.map.schemas`: a Pydantic model
    validates every field before it is stored, so the flexibility costs nothing
    in safety and adding a property needs no migration.

``map_marker_design_versions``
    Point-in-time copies. Editing an **active** design writes the previous
    definition here first, so a change to a design the map is using is
    recoverable rather than destructive.

``map_marker_assignments``
    Which design draws which entity. A row with ``entity_code = NULL`` applies to
    the whole entity type; a row with a code applies to that one entity and wins.
    That two-row shape is what makes the documented priority — specific entity →
    entity type → system default — a lookup rather than a special case.

``map_marker_assets``
    Uploaded SVG / PNG / WebP. SVG is stored as *sanitised text*, never as the
    bytes that arrived, so what is served can never be what was uploaded if the
    upload contained anything executable.
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
from sqlalchemy.orm import Mapped, mapped_column

from .models import SURROGATE_PK, Base
from .models_warehouse import FK_TYPE, JSON_TYPE


class MarkerDesignType:
    """How a design produces its geometry."""

    #: One of the built-in shapes, parameterised (circle, star, pin, …).
    BUILTIN_SHAPE = "BUILTIN_SHAPE"
    #: An uploaded SVG / PNG / WebP asset.
    CUSTOM_IMAGE = "CUSTOM_IMAGE"
    #: A polygon or path drawn point-by-point in the Shape Builder.
    SHAPE_BUILDER = "SHAPE_BUILDER"

    ALL = (BUILTIN_SHAPE, CUSTOM_IMAGE, SHAPE_BUILDER)


class MarkerDesignStatus:
    """A design's lifecycle.

    ``DRAFT`` is deliberately distinct from ``INACTIVE``: a draft has never been
    used, while an inactive design has been retired and may still be referenced
    by history. Only an ``ACTIVE`` design is ever served to the map.
    """

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"

    ALL = (DRAFT, ACTIVE, INACTIVE)


class MapMarkerDesign(Base):
    """One marker design."""

    __tablename__ = "map_marker_designs"

    design_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                           autoincrement=True)
    #: Stable external identifier, used by export/import so a design keeps its
    #: identity across environments where surrogate keys differ.
    design_uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    design_type: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=MarkerDesignStatus.DRAFT)
    #: The validated marker definition. See ``app.map.schemas.MarkerDefinition``.
    definition: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False)
    #: Uploaded asset backing a CUSTOM_IMAGE design.
    asset_id: Mapped[int | None] = mapped_column(
        FK_TYPE, ForeignKey("map_marker_assets.asset_id", ondelete="RESTRICT")
    )
    #: Incremented every time an active design's definition changes.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: True for the design the system falls back to for an entity type when
    #: nothing has been assigned. Seeded, never deleted through the API.
    is_system_default: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                    default=False)

    created_by: Mapped[str | None] = mapped_column(String(64))
    updated_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_map_marker_designs_entity_type", "entity_type"),
        Index("ix_map_marker_designs_status", "status"),
        Index("ix_map_marker_designs_entity_status", "entity_type", "status"),
    )


class MapMarkerDesignVersion(Base):
    """A previous definition of a design, kept so an edit is never destructive."""

    __tablename__ = "map_marker_design_versions"

    version_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                            autoincrement=True)
    design_id: Mapped[int] = mapped_column(
        FK_TYPE, ForeignKey("map_marker_designs.design_id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    design_type: Mapped[str] = mapped_column(String(24), nullable=False)
    definition: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False)
    asset_id: Mapped[int | None] = mapped_column(FK_TYPE)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Why this version was superseded, e.g. "edited while active".
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("design_id", "version", name="uq_marker_design_version"),
        Index("ix_map_marker_design_versions_design_id", "design_id"),
    )


class MapMarkerAssignment(Base):
    """Which design draws which entity.

    ``entity_code IS NULL`` means "every entity of this type". A row carrying a
    code overrides it for that entity alone. Both are held in one table so the
    resolver reads one query and orders by specificity.
    """

    __tablename__ = "map_marker_assignments"

    assignment_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                               autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: NULL = the whole entity type.
    entity_code: Mapped[str | None] = mapped_column(String(64))
    design_id: Mapped[int] = mapped_column(
        FK_TYPE, ForeignKey("map_marker_designs.design_id", ondelete="CASCADE"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Reserved for the conditional-styling model (outstanding > X → red marker).
    #: Nothing evaluates it in this phase; it exists so adding rules later is a
    #: resolver change rather than a migration.
    condition: Mapped[dict | None] = mapped_column(JSON_TYPE)
    #: Higher wins among rows of equal specificity.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("entity_type", "entity_code",
                         name="uq_map_marker_assignment_entity"),
        Index("ix_map_marker_assignments_entity_type", "entity_type"),
        Index("ix_map_marker_assignments_design_id", "design_id"),
    )


class MapMarkerAsset(Base):
    """An uploaded marker image.

    For SVG, ``content`` holds the **sanitised** markup and is what is served —
    the uploaded bytes are never stored, so a payload that failed sanitisation
    cannot later be recovered and served by mistake. Raster uploads (PNG/WebP)
    are stored as a base64 data URI in the same column, having been checked for
    a valid magic header and decodable dimensions.
    """

    __tablename__ = "map_marker_assets"

    asset_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                          autoincrement=True)
    asset_uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Size of the stored (sanitised) content, not of the upload.
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: SHA-256 of the stored content, so a duplicate upload is recognisable.
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: What the sanitiser removed, kept so an administrator can see why their
    #: file looks different from the one they uploaded.
    sanitised_report: Mapped[dict | None] = mapped_column(JSON_TYPE)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    uploaded_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_map_marker_assets_checksum", "checksum"),
    )


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
    """The position of one business entity on the map.

    Deliberately a **separate table** rather than latitude/longitude columns on
    the Phase 1 dimensions. Three reasons: the master dimensions are the
    workbook's contract and are not extended by a later phase; one uniform table
    covers every entity type including ones added later; and a coordinate has its
    own provenance and lifecycle (uploaded, derived, re-derived) that does not
    belong in a slowly-changing master record.
    """

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


__all__ = [
    "MapMarkerDesign",
    "MapMarkerDesignVersion",
    "MapMarkerAssignment",
    "MapMarkerAsset",
    "MapEntityLocation",
    "MarkerDesignType",
    "MarkerDesignStatus",
    "GeoSource",
    "GeoPrecision",
]
