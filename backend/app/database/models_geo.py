"""Administrative geography: divisions, districts, upazilas and their boundaries.

This is a **second, independent hierarchy**. The Phase 1 dimensions describe how
the company is organised — company → … → sub-territory — and are the workbook's
contract. Bangladesh's administrative geography is not the company's to define:
a district exists whether or not anyone sells there, and its boundary is a fact
about the country. Modelling it separately is what keeps the two from
contaminating each other; nothing here has a foreign key into the
organisational chain, and nothing there has one into this.

The two are related *spatially*, not structurally, and that relationship is
computed rather than stored: a territory falls inside whichever upazila polygon
contains its coordinate. See :mod:`app.map.areas`.

``division → district → upazila`` follows the same shape as every other
dimension in this codebase — surrogate key, official code with a UNIQUE
constraint, foreign key onto the parent's code, timestamps, soft delete.

Boundaries live in their own table for the same reason coordinates do: the
geometry has its own provenance, its own lifecycle (imported, simplified,
re-imported) and its own size. A polygon of several hundred vertices has no
business being loaded every time someone reads a district's name.
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

from .models import CODE, SURROGATE_PK, Base, SoftDeleteMixin, TimestampMixin
from .models_warehouse import JSON_TYPE


class DimCountry(Base, TimestampMixin, SoftDeleteMixin):
    """The country itself — one row, and the root of the administrative tree.

    A single-row table earns its place because the map draws the national
    outline as a *layer like any other*: it needs a code to key a boundary on,
    a name to label, and a style row of its own. Special-casing the country in
    the level registry instead would mean one branch in every query that walks
    levels, which is how a hierarchy stops being uniform.
    """

    __tablename__ = "dim_country"

    country_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                            autoincrement=True)
    country_code: Mapped[str] = mapped_column(CODE, nullable=False, unique=True)
    country_name: Mapped[str] = mapped_column(Text, nullable=False)
    country_name_bn: Mapped[str | None] = mapped_column(Text)
    #: ISO 3166 codes where the source publishes them. Reference only.
    iso2: Mapped[str | None] = mapped_column(String(2))
    iso3: Mapped[str | None] = mapped_column(String(3))

    divisions: Mapped[list["DimDivision"]] = relationship(back_populates="country")

    __table_args__ = (Index("ix_dim_country_country_code", "country_code"),)


class DimDivision(Base, TimestampMixin, SoftDeleteMixin):
    """Top of the administrative hierarchy below the country. Bangladesh has eight."""

    __tablename__ = "dim_division"

    division_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                             autoincrement=True)
    division_code: Mapped[str] = mapped_column(CODE, nullable=False, unique=True)
    division_name: Mapped[str] = mapped_column(Text, nullable=False)
    #: Bengali name, where the source carries one. Bangla is preserved as-is.
    division_name_bn: Mapped[str | None] = mapped_column(Text)
    #: Nullable, unlike every other parent link here: divisions loaded before
    #: the country layer existed have no country row to point at, and a
    #: migration may not invent one for them. A division file supplies it.
    country_code: Mapped[str | None] = mapped_column(
        CODE, ForeignKey("dim_country.country_code", ondelete="RESTRICT",
                         onupdate="CASCADE"),
    )

    country: Mapped["DimCountry | None"] = relationship(back_populates="divisions")
    districts: Mapped[list["DimDistrict"]] = relationship(back_populates="division")

    __table_args__ = (
        Index("ix_dim_division_division_code", "division_code"),
        Index("ix_dim_division_country_code", "country_code"),
    )


class DimDistrict(Base, TimestampMixin, SoftDeleteMixin):
    """A district (zila) within a division."""

    __tablename__ = "dim_district"

    district_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                             autoincrement=True)
    district_code: Mapped[str] = mapped_column(CODE, nullable=False, unique=True)
    district_name: Mapped[str] = mapped_column(Text, nullable=False)
    district_name_bn: Mapped[str | None] = mapped_column(Text)
    division_code: Mapped[str] = mapped_column(
        CODE, ForeignKey("dim_division.division_code", ondelete="RESTRICT",
                         onupdate="CASCADE"),
        nullable=False,
    )

    division: Mapped[DimDivision] = relationship(back_populates="districts")
    upazilas: Mapped[list["DimUpazila"]] = relationship(back_populates="district")

    __table_args__ = (
        Index("ix_dim_district_district_code", "district_code"),
        Index("ix_dim_district_division_code", "division_code"),
    )


class DimUpazila(Base, TimestampMixin, SoftDeleteMixin):
    """An upazila (sub-district) within a district.

    The level the map draws. ``latitude`` / ``longitude`` are the administrative
    centre and are kept here rather than only in ``map_entity_locations``
    because, unlike a business entity's position, they are part of what the
    boundary source published — a property of the upazila, not a placement
    someone made.
    """

    __tablename__ = "dim_upazila"

    upazila_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                            autoincrement=True)
    upazila_code: Mapped[str] = mapped_column(CODE, nullable=False, unique=True)
    upazila_name: Mapped[str] = mapped_column(Text, nullable=False)
    upazila_name_bn: Mapped[str | None] = mapped_column(Text)
    district_code: Mapped[str] = mapped_column(
        CODE, ForeignKey("dim_district.district_code", ondelete="RESTRICT",
                         onupdate="CASCADE"),
        nullable=False,
    )
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    district: Mapped[DimDistrict] = relationship(back_populates="upazilas")

    __table_args__ = (
        Index("ix_dim_upazila_upazila_code", "upazila_code"),
        Index("ix_dim_upazila_district_code", "district_code"),
    )


# ===========================================================================
# Boundaries
# ===========================================================================


class BoundarySource:
    """Where a boundary came from. Provenance decides how much to trust it."""

    IMPORT = "IMPORT"
    MANUAL = "MANUAL"
    DERIVED = "DERIVED"

    ALL = (IMPORT, MANUAL, DERIVED)


class MapAreaBoundary(Base):
    """The polygon for one administrative area.

    ``geometry`` is a GeoJSON ``Polygon`` or ``MultiPolygon`` — the format the
    source files arrive in and the format both renderers consume, so it crosses
    the whole system without being converted.

    Everything alongside it exists so the geometry itself can stay unread:

    * ``bbox_*`` answers "is this area on screen?" without parsing a thousand
      vertices, which is what makes viewport filtering cheap;
    * ``centroid_*`` places a label or a fallback marker;
    * ``point_count`` and ``source_point_count`` say how much simplification was
      applied, so it is visible rather than silent;
    * ``generation`` changes whenever the geometry does, and is what the ETag on
      the areas endpoint is built from.
    """

    __tablename__ = "map_area_boundaries"

    boundary_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                             autoincrement=True)
    #: ``upazila`` / ``district`` / ``division`` — the same vocabulary the map's
    #: entity types use, so one table serves every administrative level.
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_code: Mapped[str] = mapped_column(CODE, nullable=False)
    #: GeoJSON geometry object: ``{"type": "Polygon", "coordinates": [...]}``.
    geometry: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False)

    bbox_north: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_south: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_east: Mapped[float] = mapped_column(Float, nullable=False)
    bbox_west: Mapped[float] = mapped_column(Float, nullable=False)
    centroid_latitude: Mapped[float] = mapped_column(Float, nullable=False)
    centroid_longitude: Mapped[float] = mapped_column(Float, nullable=False)

    point_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Vertices in the file before simplification, when it was simplified.
    source_point_count: Mapped[int | None] = mapped_column(Integer)
    #: Douglas–Peucker tolerance in degrees, or NULL when stored verbatim.
    simplify_tolerance: Mapped[float | None] = mapped_column(Float)

    source: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=BoundarySource.IMPORT)
    #: The file it came from, so a reload can be traced.
    source_file: Mapped[str | None] = mapped_column(Text)
    imported_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("entity_type", "entity_code",
                         name="uq_map_area_boundary"),
        Index("ix_map_area_boundaries_entity_type", "entity_type"),
        # The viewport query filters on the box before it touches geometry.
        Index("ix_map_area_boundaries_bbox", "entity_type", "bbox_south",
              "bbox_north"),
    )


# ===========================================================================
# How an area is drawn
# ===========================================================================


class MapAreaStyle(Base):
    """How one administrative level is painted.

    A polygon's appearance is not a marker's. A marker has a shape, an icon, an
    anchor and a badge; an area has a fill, an opacity and a stroke. Sharing one
    table would mean half the columns were always null, so they are separate —
    but the *principle* is the same one the Marker Designer already establishes:
    appearance is configuration in the database, never a constant in the code.

    ``rules`` is declared and deliberately not evaluated. The specification asks
    for the architecture to be ready for stock-based colouring — red at zero,
    blue at 1–10, green above — without those rules being implemented yet. It
    exists so that adding them later is a change to the resolver rather than a
    migration, exactly as ``map_marker_assignments.condition`` already does for
    markers.
    """

    __tablename__ = "map_area_styles"

    style_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                          autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False,
                                             unique=True)
    #: CSS hex, validated on write. Never a named colour: "blue" renders
    #: differently across engines and cannot be interpolated.
    fill_color: Mapped[str] = mapped_column(String(16), nullable=False)
    fill_opacity: Mapped[float] = mapped_column(Float, nullable=False)
    stroke_color: Mapped[str] = mapped_column(String(16), nullable=False)
    stroke_opacity: Mapped[float] = mapped_column(Float, nullable=False,
                                                  default=1.0)
    stroke_width: Mapped[float] = mapped_column(Float, nullable=False)
    #: Applied on hover and on the selected area. Optional: when null the base
    #: fill is reused at a higher opacity, so a style is usable half-configured.
    hover_fill_color: Mapped[str | None] = mapped_column(String(16))
    hover_fill_opacity: Mapped[float | None] = mapped_column(Float)
    selected_fill_color: Mapped[str | None] = mapped_column(String(16))
    selected_fill_opacity: Mapped[float | None] = mapped_column(Float)
    #: Draw order when several area layers overlap; higher is on top.
    z_index: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: Future value-based colouring. Nothing reads this yet.
    rules: Mapped[dict | None] = mapped_column(JSON_TYPE)
    is_system_default: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                    default=False)
    updated_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index("ix_map_area_styles_entity_type", "entity_type"),
    )


# ===========================================================================
# Administrative reference points
# ===========================================================================


class AdminPointKind:
    """Which published layer a reference point came from.

    Kept apart rather than merged because the two answer different questions:
    a capital is *the seat* of an administrative unit, while an admin point is
    a labelling position for the unit as a whole. Collapsing them would lose
    which one a coordinate is, and no property in either file restores it.
    """

    CAPITAL = "capital"
    POINT = "point"

    ALL = (CAPITAL, POINT)


class MapAdminPoint(Base):
    """One published administrative point — a capital or a label position.

    Deliberately **not** ``map_entity_locations``. That table holds where a
    *business* entity was placed, is editable through the map UI, and carries a
    placement source and precision because somebody chose the position. These
    are geographic reference data: they arrive with the boundary files, are
    replaced wholesale by a re-import, and nobody edits them. Mixing the two
    would make "who put this here?" unanswerable for both.

    The ancestor codes are stored as plain columns with no foreign keys. The
    point files include level-4 features that this schema has no dimension for,
    and a constraint would reject them — but a level-4 point is still a real
    place worth drawing. The codes are for filtering and joining; they are not
    claims that a matching dimension row exists.
    """

    __tablename__ = "map_admin_points"

    point_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                          autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 0 = country, 1 = division, 2 = district, 3 = upazila, 4 = below it.
    #: Stored as the file states it; nothing here re-derives a level.
    admin_level: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_bn: Mapped[str | None] = mapped_column(Text)

    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)

    country_code: Mapped[str | None] = mapped_column(CODE)
    division_code: Mapped[str | None] = mapped_column(CODE)
    district_code: Mapped[str | None] = mapped_column(CODE)
    upazila_code: Mapped[str | None] = mapped_column(CODE)

    source_file: Mapped[str | None] = mapped_column(Text)
    imported_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        # The natural key: the same place appears once per kind. The published
        # files carry no stable point id, so identity is the kind, the level and
        # the position — which is exactly what a re-import must match on.
        UniqueConstraint("kind", "admin_level", "latitude", "longitude",
                         name="uq_map_admin_point"),
        Index("ix_map_admin_points_kind_level", "kind", "admin_level"),
        # Viewport filtering, the same shape the boundary index serves.
        Index("ix_map_admin_points_bbox", "kind", "latitude", "longitude"),
    )


#: Administrative table -> model, parents first, as the importer loads them.
GEO_MODEL_BY_TABLE: dict[str, type[Base]] = {
    "dim_country": DimCountry,
    "dim_division": DimDivision,
    "dim_district": DimDistrict,
    "dim_upazila": DimUpazila,
}


__all__ = [
    "DimCountry",
    "DimDivision",
    "DimDistrict",
    "DimUpazila",
    "MapAreaBoundary",
    "MapAreaStyle",
    "MapAdminPoint",
    "AdminPointKind",
    "BoundarySource",
    "GEO_MODEL_BY_TABLE",
]
