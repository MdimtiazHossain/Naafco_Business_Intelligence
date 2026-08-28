"""SQLAlchemy models for the master-data dimensions.

Every table follows the same pattern:

* ``BIGSERIAL`` surrogate key, used only for internal joins
* the **official business code** kept as ``VARCHAR`` with a UNIQUE constraint —
  never replaced by the surrogate key, never converted to an integer
* a foreign key onto the parent dimension's business code (hierarchy tables)
* ``created_at`` / ``updated_at`` audit timestamps
* ``is_deleted`` / ``deleted_at`` / ``deleted_by`` — master data is retired,
  never destroyed, because facts keep referencing it

Column names mirror ``master_data.schema`` exactly so that DB, Python, API and
frontend share one vocabulary.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    false,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for the whole warehouse."""


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SoftDeleteMixin:
    """Master data is retired, not destroyed.

    A dimension row is referenced by fact rows that must keep meaning years
    later, so deleting one would orphan history. Instead the record is flagged:
    it disappears from the management tables and can be restored, while every
    transaction that ever pointed at it still resolves.

    The three columns are the standard set — who, when, and whether — rather
    than a bare flag, because "this was deleted" is not enough to answer for
    later: an administrator needs to know by whom and when.
    """

    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[str | None] = mapped_column(String(64))


CODE = String(64)
PHONE = String(32)
MONEY = Numeric(18, 4)

#: Surrogate-key type. BIGSERIAL on PostgreSQL; SQLite only auto-increments a
#: column declared exactly ``INTEGER PRIMARY KEY``, so the test database gets the
#: narrower type via a dialect variant.
SURROGATE_PK = BigInteger().with_variant(Integer, "sqlite")


class DimCompany(Base, TimestampMixin, SoftDeleteMixin):
    """Top of the organisational hierarchy."""

    __tablename__ = "dim_company"

    company_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    company_code: Mapped[str] = mapped_column(CODE, nullable=False, unique=True)
    company_name: Mapped[str] = mapped_column(Text, nullable=False)
    company_head_id: Mapped[str | None] = mapped_column(CODE)
    company_head_name: Mapped[str | None] = mapped_column(Text)

    business_units: Mapped[list["DimBusinessUnit"]] = relationship(back_populates="company")

    __table_args__ = (Index("ix_dim_company_company_code", "company_code"),)


class DimBusinessUnit(Base, TimestampMixin, SoftDeleteMixin):
    """Business unit within a company."""

    __tablename__ = "dim_business_unit"

    business_unit_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    bu_code: Mapped[str] = mapped_column(CODE, nullable=False, unique=True)
    bu_name: Mapped[str] = mapped_column(Text, nullable=False)
    company_code: Mapped[str] = mapped_column(
        CODE, ForeignKey("dim_company.company_code", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
    )
    bu_head_id: Mapped[str | None] = mapped_column(CODE)
    bu_head_name: Mapped[str | None] = mapped_column(Text)

    company: Mapped[DimCompany] = relationship(back_populates="business_units")
    sales_lines: Mapped[list["DimSalesLine"]] = relationship(back_populates="business_unit")

    __table_args__ = (
        Index("ix_dim_business_unit_bu_code", "bu_code"),
        Index("ix_dim_business_unit_company_code", "company_code"),
    )


class DimSalesLine(Base, TimestampMixin, SoftDeleteMixin):
    """Sales line within a business unit."""

    __tablename__ = "dim_sales_line"

    sales_line_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    sales_line_code: Mapped[str] = mapped_column(CODE, nullable=False, unique=True)
    sales_line_name: Mapped[str] = mapped_column(Text, nullable=False)
    bu_code: Mapped[str] = mapped_column(
        CODE, ForeignKey("dim_business_unit.bu_code", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
    )
    sales_line_head_id: Mapped[str | None] = mapped_column(CODE)
    sales_line_head_name: Mapped[str | None] = mapped_column(Text)

    business_unit: Mapped[DimBusinessUnit] = relationship(back_populates="sales_lines")
    zones: Mapped[list["DimZone"]] = relationship(back_populates="sales_line")

    __table_args__ = (
        Index("ix_dim_sales_line_sales_line_code", "sales_line_code"),
        Index("ix_dim_sales_line_bu_code", "bu_code"),
    )


class DimZone(Base, TimestampMixin, SoftDeleteMixin):
    """Zone within a sales line."""

    __tablename__ = "dim_zone"

    zone_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    zone_code: Mapped[str] = mapped_column(CODE, nullable=False, unique=True)
    zone_name: Mapped[str] = mapped_column(Text, nullable=False)
    sales_line_code: Mapped[str] = mapped_column(
        CODE, ForeignKey("dim_sales_line.sales_line_code", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
    )
    zone_head_id: Mapped[str | None] = mapped_column(CODE)
    zone_head_name: Mapped[str | None] = mapped_column(Text)

    sales_line: Mapped[DimSalesLine] = relationship(back_populates="zones")
    regions: Mapped[list["DimRegion"]] = relationship(back_populates="zone")

    __table_args__ = (
        Index("ix_dim_zone_zone_code", "zone_code"),
        Index("ix_dim_zone_sales_line_code", "sales_line_code"),
    )


class DimRegion(Base, TimestampMixin, SoftDeleteMixin):
    """Region within a zone."""

    __tablename__ = "dim_region"

    region_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    region_code: Mapped[str] = mapped_column(CODE, nullable=False, unique=True)
    region_name: Mapped[str] = mapped_column(Text, nullable=False)
    zone_code: Mapped[str] = mapped_column(
        CODE, ForeignKey("dim_zone.zone_code", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
    )
    region_head_id: Mapped[str | None] = mapped_column(CODE)
    region_head_name: Mapped[str | None] = mapped_column(Text)
    region_hq: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)

    zone: Mapped[DimZone] = relationship(back_populates="regions")
    areas: Mapped[list["DimArea"]] = relationship(back_populates="region")

    __table_args__ = (
        Index("ix_dim_region_region_code", "region_code"),
        Index("ix_dim_region_zone_code", "zone_code"),
    )


class DimArea(Base, TimestampMixin, SoftDeleteMixin):
    """Area within a region."""

    __tablename__ = "dim_area"

    area_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    area_code: Mapped[str] = mapped_column(CODE, nullable=False, unique=True)
    area_name: Mapped[str] = mapped_column(Text, nullable=False)
    region_code: Mapped[str] = mapped_column(
        CODE, ForeignKey("dim_region.region_code", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
    )
    area_head_id: Mapped[str | None] = mapped_column(CODE)
    area_head_name: Mapped[str | None] = mapped_column(Text)
    area_hq: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)

    region: Mapped[DimRegion] = relationship(back_populates="areas")
    units: Mapped[list["DimUnit"]] = relationship(back_populates="area")

    __table_args__ = (
        Index("ix_dim_area_area_code", "area_code"),
        Index("ix_dim_area_region_code", "region_code"),
    )


class DimUnit(Base, TimestampMixin, SoftDeleteMixin):
    """Unit within an area."""

    __tablename__ = "dim_unit"

    unit_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    unit_code: Mapped[str] = mapped_column(CODE, nullable=False, unique=True)
    unit_name: Mapped[str] = mapped_column(Text, nullable=False)
    area_code: Mapped[str] = mapped_column(
        CODE, ForeignKey("dim_area.area_code", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
    )
    unit_head_id: Mapped[str | None] = mapped_column(CODE)
    unit_head_name: Mapped[str | None] = mapped_column(Text)
    unit_hq: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)

    area: Mapped[DimArea] = relationship(back_populates="units")
    territories: Mapped[list["DimTerritory"]] = relationship(back_populates="unit")

    __table_args__ = (
        Index("ix_dim_unit_unit_code", "unit_code"),
        Index("ix_dim_unit_area_code", "area_code"),
    )


class DimTerritory(Base, TimestampMixin, SoftDeleteMixin):
    """Territory within a unit."""

    __tablename__ = "dim_territory"

    territory_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    territory_code: Mapped[str] = mapped_column(CODE, nullable=False, unique=True)
    territory_name: Mapped[str] = mapped_column(Text, nullable=False)
    unit_code: Mapped[str] = mapped_column(
        CODE, ForeignKey("dim_unit.unit_code", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
    )
    territory_head_id: Mapped[str | None] = mapped_column(CODE)
    territory_head_name: Mapped[str | None] = mapped_column(Text)
    territory_head_phone: Mapped[str | None] = mapped_column(PHONE)
    territory_hq: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)

    unit: Mapped[DimUnit] = relationship(back_populates="territories")
    sub_territories: Mapped[list["DimSubTerritory"]] = relationship(back_populates="territory")

    __table_args__ = (
        Index("ix_dim_territory_territory_code", "territory_code"),
        Index("ix_dim_territory_unit_code", "unit_code"),
    )


class DimSubTerritory(Base, TimestampMixin, SoftDeleteMixin):
    """Sub-territory within a territory: the lowest hierarchy level."""

    __tablename__ = "dim_sub_territory"

    sub_territory_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    sub_territory_code: Mapped[str] = mapped_column(CODE, nullable=False, unique=True)
    sub_territory_name: Mapped[str] = mapped_column(Text, nullable=False)
    territory_code: Mapped[str] = mapped_column(
        CODE, ForeignKey("dim_territory.territory_code", ondelete="RESTRICT", onupdate="CASCADE"),
        nullable=False,
    )
    sub_territory_head_id: Mapped[str | None] = mapped_column(CODE)
    sub_territory_head_name: Mapped[str | None] = mapped_column(Text)
    sub_territory_head_phone: Mapped[str | None] = mapped_column(PHONE)
    sub_territory_hq: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)

    territory: Mapped[DimTerritory] = relationship(back_populates="sub_territories")

    __table_args__ = (
        Index("ix_dim_sub_territory_sub_territory_code", "sub_territory_code"),
        Index("ix_dim_sub_territory_territory_code", "territory_code"),
    )


class DimPlant(Base, TimestampMixin, SoftDeleteMixin):
    """Plant Master: one row per Company + Plant.

    A plant code identifies a plant *within* a company, not globally, which is
    why the natural key is the pair and ``plant_key`` carries it joined — the
    same device ``DimStorageLocation`` and the stock fact use, so an upsert can
    conflict on one column.

    Deliberately **not** linked to ``dim_company`` by a foreign key: this master
    arrives from its own SAP-side extract, and a constraint would make the upload
    fail on any company code the Company Master has not received yet. The
    relationship is checked by the upload validator, where it can say something
    useful about the row.
    """

    __tablename__ = "dim_plant"

    plant_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    #: ``company|plant`` — the composite natural key, stored joined.
    plant_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    company_code: Mapped[str] = mapped_column(CODE, nullable=False)
    plant_code: Mapped[str] = mapped_column(CODE, nullable=False)
    plant_name: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_dim_plant_key", "plant_key"),
        Index("ix_dim_plant_company", "company_code"),
        Index("ix_dim_plant_code", "plant_code"),
    )


class DimStorageLocation(Base, TimestampMixin, SoftDeleteMixin):
    """Storage Location Master: one row per Plant + Storage Location.

    A storage location belongs to a plant and its code is unique only within
    one, so the natural key is the pair.

    **No plant name here.** That is an attribute of the plant and is reached
    through ``plant_code``; storing it a second time would let a renamed plant
    keep its old name on every storage location beneath it. The same rule the
    fact tables follow — codes, and the dimensions own the names.

    Not a database foreign key to :class:`DimPlant`, for the reason the rest of
    this module gives: the masters load independently and in any order, and a
    constraint would make a storage-location upload fail on a plant the Plant
    Master has not received yet. The upload validator checks the pair exists and
    rejects the row with a message naming the missing plant.
    """

    __tablename__ = "dim_storage_location"

    storage_location_id: Mapped[int] = mapped_column(
        SURROGATE_PK, primary_key=True, autoincrement=True)
    #: ``plant|storage_location`` — the composite natural key, stored joined.
    storage_location_key: Mapped[str] = mapped_column(
        String(160), nullable=False, unique=True)
    plant_code: Mapped[str] = mapped_column(CODE, nullable=False)
    storage_location_code: Mapped[str] = mapped_column(CODE, nullable=False)
    storage_location_name: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_dim_storage_location_key", "storage_location_key"),
        Index("ix_dim_storage_location_plant", "plant_code"),
        Index("ix_dim_storage_location_code", "storage_location_code"),
    )


class DimMaterial(Base, TimestampMixin, SoftDeleteMixin):
    """Material Master: Material Group -> Material Brand -> Material.

    **The one product dimension.** Since revision 0022 there is no SKU master
    beside it: ``material_code`` identifies the goods everywhere — on a sale, on
    a target and on a stock position alike — and every report that names an item
    reaches this table through that code. The Product Master it replaced held a
    second, parallel identity for the same goods, and the two could disagree; one
    master cannot.

    ``material_code`` **is** the identity — one row per material, unique and
    required. Unlike the column revision 0018 added to the table this replaced,
    it tolerates nothing: that nullability existed purely for rows loaded before
    the Material Code was specified, and those rows are not carried forward.

    **Group and brand are code/name pairs on the material, not two more
    tables.** The source states them per material and states no attribute of a
    group or a brand beyond its own name, so a separate table would hold nothing
    the name does not already say. If a group ever acquires an attribute of its
    own — a shelf-life policy, an owner — that is when it earns a table.

    **A column exists here only where the source states it.** Seven columns
    identify a material: a company, a group, a brand, a code and a description.
    Revision 0027 added two more — ``conversion_factor`` and ``transfer_price``
    — because the Material Master extract grew columns for them, and both are
    nullable so a file that does not carry them leaves them unset rather than
    guessed. There is still no pack size, no unit of measure and no second
    identifier, because the source states none. A column the source does not
    fill can only be filled by a guess.

    **``material_code`` stays unique, and ``company_code`` is an attribute of the
    material rather than half of its key** (`0023_material_company`). The
    distinction is load-bearing: every fact resolves its item by material code
    alone, and ``fact_target`` states no company of its own — it derives one from
    its territory later in the pipeline — so a composite key would leave a target
    unable to name an item at all. A material belongs to one company; a company
    has many materials.

    A file naming the same material under two companies is therefore a conflict
    the upload **reports**, not something this model can hold. Which of the two is
    right is a question about the business, and guessing either would silently
    halve every report filtered to one of them.
    """

    __tablename__ = "dim_material"

    material_id: Mapped[int] = mapped_column(
        SURROGATE_PK, primary_key=True, autoincrement=True)
    #: The company this material belongs to. First in the master's stated column
    #: order, and the head of the item filter chain: Company -> Group -> Brand ->
    #: Material.
    #:
    #: Nullable, and only because revision 0023 added it to 405 rows that predate
    #: it — a material whose company the data does not state keeps NULL rather
    #: than a guess, and the upload requires the column on every new row.
    #:
    #: Deliberately **not** a database foreign key to ``dim_company``, for the
    #: reason every material-side reference in this module is not: this master
    #: arrives from its own SAP-side extract and a constraint would fail the whole
    #: upload on a company the Company Master has not received yet. The upload
    #: validator checks it row by row, where it can name the offending row.
    company_code: Mapped[str | None] = mapped_column(CODE)
    material_code: Mapped[str] = mapped_column(CODE, nullable=False, unique=True)
    material_description: Mapped[str] = mapped_column(Text, nullable=False)
    material_group_code: Mapped[str] = mapped_column(CODE, nullable=False)
    material_group_name: Mapped[str] = mapped_column(Text, nullable=False)
    material_brand_code: Mapped[str] = mapped_column(CODE, nullable=False)
    material_brand: Mapped[str] = mapped_column(Text, nullable=False)

    #: How many volume units make one saleable unit, and what one of those units
    #: transfers at. The two inputs of the Target Management module's central
    #: calculation: ``quantity = target_volume / conversion_factor`` and
    #: ``value = quantity * transfer_price``.
    #:
    #: Both are **nullable, and stay NULL until a Material Master file states
    #: them.** This is the one honest option. A conversion factor is a property
    #: of the pack the goods ship in, and a transfer price is a commercial
    #: decision; neither can be derived from anything else this schema holds, and
    #: a default of 1.0 would not read as "unknown" — it would read as "one
    #: volume unit per saleable unit", which is a claim about the goods.
    #:
    #: A material missing either yields no quantity and no value, and the
    #: reporting surface renders ``n/a``. That is the suppress-rather-than-guess
    #: invariant applied to a derived figure: a ratio with a missing divisor is
    #: not zero.
    #:
    #: ``conversion_factor`` carries six decimal places rather than the four the
    #: money columns use, because it is a *divisor* — a 100 ml pack of a
    #: litre-based material is 0.1, but a 5 g sachet of a kilogram-based one is
    #: 0.005, and rounding a divisor is how a rounding error becomes a
    #: multiplication error.
    conversion_factor: Mapped[float | None] = mapped_column(Numeric(18, 6))
    transfer_price: Mapped[float | None] = mapped_column(MONEY)

    __table_args__ = (
        Index("ix_dim_material_code", "material_code"),
        Index("ix_dim_material_company", "company_code"),
        Index("ix_dim_material_group", "material_group_code"),
        Index("ix_dim_material_brand", "material_brand_code"),
        # The filter engine's central question is "which materials does this
        # company have", asked on every dropdown, so the pair is indexed as a
        # pair rather than left to two separate lookups.
        Index("ix_dim_material_company_code", "company_code", "material_code"),
    )


#: How Company + Plant become one key. Used by the master upload, the stock ETL
#: and the reporting join, so all three agree on spelling and separator.
def plant_key(company: str | None, plant: str | None) -> str:
    """``C001|P100`` — the Plant Master's composite identity."""
    return "|".join((value or "").strip() for value in (company, plant))


#: How Plant + Storage Location become one key.
def storage_location_key(plant: str | None, storage_location: str | None) -> str:
    """``P100|SL01`` — the Storage Location Master's composite identity."""
    return "|".join((value or "").strip() for value in (plant, storage_location))


#: Table name -> model, in import order (parents first).
MODEL_BY_TABLE: dict[str, type[Base]] = {
    "dim_company": DimCompany,
    "dim_business_unit": DimBusinessUnit,
    "dim_sales_line": DimSalesLine,
    "dim_zone": DimZone,
    "dim_region": DimRegion,
    "dim_area": DimArea,
    "dim_unit": DimUnit,
    "dim_territory": DimTerritory,
    "dim_sub_territory": DimSubTerritory,
    # The material side. Independent of the organisational chain above it, so
    # the order here is only about the material masters among themselves:
    # a storage location names a plant, and a stock position names all three,
    # so nothing is referenced before it exists.
    "dim_plant": DimPlant,
    "dim_storage_location": DimStorageLocation,
    "dim_material": DimMaterial,
}

__all__ = ["Base", "TimestampMixin", "SoftDeleteMixin", "MODEL_BY_TABLE",
           "plant_key", "storage_location_key",
           *[m.__name__ for m in MODEL_BY_TABLE.values()]]
