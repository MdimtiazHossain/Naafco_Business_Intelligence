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
contains its coordinate.

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

    A single-row table earns its place because the country is a *level like any
    other*: it needs a code to key children on and a name to label. Special-
    casing it in the level registry instead would mean one branch in every query
    that walks levels, which is how a hierarchy stops being uniform.
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

    ``latitude`` / ``longitude`` are the administrative centre, and they sit on
    the master rather than in a separate placement table because, unlike a
    business entity's position, they are part of what the source published — a
    property of the upazila, not a placement someone made. That is why all 507
    survived ``0033_remove_map`` when every other coordinate in the platform did
    not.
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
    "GEO_MODEL_BY_TABLE",
]
