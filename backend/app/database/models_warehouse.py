"""Phase 2 warehouse models: date dimension, ETL control, staging and facts.

Layering (see README):

    source -> stg_* -> validation -> master mapping -> fact_* -> vw_*

The Phase 1 master dimensions in ``models.py`` are the single source of truth and
are **not** redefined here; facts reference their surrogate keys.

Two dimensions are declared *future-ready*: ``dim_customer`` and
``dim_sales_force``. Their source files are not part of ``Master Data.xlsx``, so
they are created empty and registered as ``PENDING_SOURCE_DATA`` in
``etl_master_source_status``. Facts reference them with nullable foreign keys, so
a transaction carrying a customer code is not rejected merely because the
customer master has not arrived yet — the code is preserved on the fact row and
can be back-filled.

``dim_warehouse`` was a third such dimension and is gone (revision 0020). It
never received a source file and never held a row; stock is located by Plant and
Storage Location, each its own master, and a sale states no warehouse. There is
no "warehouse" concept left in this system — the Storage Location Master is not
a rename of it but the thing the business actually has.
"""

from __future__ import annotations

from datetime import date as _date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, declared_attr, mapped_column
from sqlalchemy.types import JSON

from .models import (
    CODE,
    MONEY,
    PHONE,
    SURROGATE_PK,
    Base,
    SoftDeleteMixin,
    TimestampMixin,
)

#: Quantities can be fractional (kg, litres) and negative (returns).
QUANTITY = Numeric(18, 4)
#: Free-text source identifiers.
SOURCE_ID = String(128)
#: Deterministic de-duplication key, see ``etl.datasets``.
BUSINESS_KEY = String(512)

#: JSON that becomes JSONB on PostgreSQL and TEXT-backed JSON on SQLite.
try:  # pragma: no cover - import shape depends on the installed dialect
    from sqlalchemy.dialects.postgresql import JSONB

    JSON_TYPE = JSON().with_variant(JSONB, "postgresql")
except ImportError:  # pragma: no cover
    JSON_TYPE = JSON()

#: Foreign-key column type matching the dimensions' BIGSERIAL surrogate keys.
FK_TYPE = BigInteger().with_variant(Integer, "sqlite")


def fk_column(target: str, *, nullable: bool = True, ondelete: str = "RESTRICT"):
    """A foreign-key column usable from a declarative **mixin**.

    SQLAlchemy refuses to copy a bare ``ForeignKey`` column across the several
    tables that inherit a mixin, because a ``ForeignKey`` object belongs to one
    table. Wrapping it in ``declared_attr`` builds a fresh column per subclass,
    which is exactly what the shared organisational-dimension mixins need.
    """

    @declared_attr
    def _attr(_cls):
        return mapped_column(
            FK_TYPE, ForeignKey(target, ondelete=ondelete), nullable=nullable
        )

    return _attr


# ===========================================================================
# Date dimension
# ===========================================================================


class DimDate(Base):
    """Calendar and financial-year calendar.

    ``date_id`` is the conventional ``YYYYMMDD`` integer so that fact tables
    carry a compact, human-readable date key. Financial-year columns are
    generated from ``Settings.financial_year_start_month``; nothing about the
    financial calendar is hard-coded.
    """

    __tablename__ = "dim_date"

    date_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    full_date: Mapped[_date] = mapped_column(Date, nullable=False, unique=True)
    day: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    month: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    month_name: Mapped[str] = mapped_column(String(16), nullable=False)
    month_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    quarter: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    quarter_name: Mapped[str] = mapped_column(String(8), nullable=False)
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    week: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    week_name: Mapped[str] = mapped_column(String(16), nullable=False)
    financial_year: Mapped[str] = mapped_column(String(16), nullable=False)
    financial_month: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    financial_quarter: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    is_month_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_quarter_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_year_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_financial_year_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_dim_date_full_date", "full_date"),
        Index("ix_dim_date_year_month", "year", "month"),
        Index("ix_dim_date_financial_year", "financial_year"),
    )


# ===========================================================================
# Future-ready dimensions (PENDING_SOURCE_DATA)
# ===========================================================================


class DimCustomer(Base, TimestampMixin, SoftDeleteMixin):
    """Customer dimension. **PENDING_SOURCE_DATA** — no customer master exists yet.

    The structure is declared so that facts can reference it and so the importer
    can be pointed at a customer file the moment one arrives. No customer record
    is ever invented.
    """

    __tablename__ = "dim_customer"

    customer_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    #: Which sub-territory the customer belongs to, in ``dim_sub_territory``.
    #:
    #: This is the **authoritative** Sub-Territory → Customer mapping. Until it
    #: existed the map derived customer membership from the facts — a customer
    #: belonged where it had traded — which works only for customers that have
    #: traded, and is ambiguous for one that has traded in two places. The
    #: master field settles both cases, and the fact-derived rule stays as the
    #: fallback for rows it has not been set on.
    #:
    #: Not a database foreign key, for the same reason ``territory_code`` on
    #: ``dim_sales_force`` is not: this dimension is populated by upload and by
    #: ETL placeholders, and a constraint would make an unknown code fail the
    #: whole import rather than produce a row-level message. It is validated on
    #: every write and every upload instead.
    sub_territory_code: Mapped[str | None] = mapped_column(CODE)
    customer_code: Mapped[str] = mapped_column(CODE, nullable=False, unique=True)
    customer_name: Mapped[str | None] = mapped_column(Text)
    customer_type: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    district: Mapped[str | None] = mapped_column(Text)
    mobile: Mapped[str | None] = mapped_column(PHONE)
    status: Mapped[str | None] = mapped_column(Text)
    #: Set when the transaction stream referenced a code the master lacks.
    is_placeholder: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_dim_customer_customer_code", "customer_code"),
        # The map asks "which customers are in this sub-territory" on every
        # draw, so the lookup that answers it is indexed.
        Index("ix_dim_customer_sub_territory_code", "sub_territory_code"),
        Index("ix_dim_customer_status", "status"),
    )


class DimSalesForce(Base, TimestampMixin, SoftDeleteMixin):
    """Sales-force dimension. **PENDING_SOURCE_DATA**.

    ``territory_code`` is intentionally *not* a foreign key yet: until the real
    sales-force master arrives there is nothing to guarantee its codes match the
    official hierarchy. Mapping it to ``dim_territory`` is a Phase 2.1 task once
    the file exists.
    """

    __tablename__ = "dim_sales_force"

    sales_force_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    sales_force_code: Mapped[str] = mapped_column(CODE, nullable=False, unique=True)
    sales_force_name: Mapped[str | None] = mapped_column(Text)
    designation: Mapped[str | None] = mapped_column(Text)
    employee_id: Mapped[str | None] = mapped_column(CODE)
    territory_code: Mapped[str | None] = mapped_column(CODE)
    status: Mapped[str | None] = mapped_column(Text)
    is_placeholder: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_dim_sales_force_sales_force_code", "sales_force_code"),
        Index("ix_dim_sales_force_territory_code", "territory_code"),
    )


class MasterSourceStatus(Base):
    """Registry of which master dimensions have a real source file behind them.

    Read by the ETL (to decide whether an unknown code is a rejection or a
    tolerated gap) and exposed through ``GET /api/data-quality/master-sources``.
    """

    __tablename__ = "etl_master_source_status"

    table_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_description: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


STATUS_AVAILABLE = "AVAILABLE"
STATUS_PENDING_SOURCE_DATA = "PENDING_SOURCE_DATA"


# ===========================================================================
# ETL control tables
# ===========================================================================


class EtlImportBatch(Base):
    """One import run: the unit of auditability, re-processing and rollback."""

    __tablename__ = "etl_import_batches"

    batch_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    batch_uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)   # EXCEL/CSV/SAP_API/...
    source_system: Mapped[str] = mapped_column(String(32), nullable=False) # DEMO/SAP/SALES_APP/...
    source_file: Mapped[str | None] = mapped_column(Text)
    data_type: Mapped[str] = mapped_column(String(32), nullable=False)     # sales/material_stock/target
    load_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="INCREMENTAL")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    successful_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inserted_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="STARTED")
    error_summary: Mapped[dict | None] = mapped_column(JSON_TYPE)

    __table_args__ = (
        Index("ix_etl_import_batches_data_type", "data_type"),
        Index("ix_etl_import_batches_status", "status"),
        Index("ix_etl_import_batches_started_at", "started_at"),
        Index("ix_etl_import_batches_source_system", "source_system"),
    )


#: Batch lifecycle.
BATCH_STARTED = "STARTED"
BATCH_VALIDATING = "VALIDATING"
BATCH_COMPLETED = "COMPLETED"
BATCH_COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
BATCH_FAILED = "FAILED"


class EtlRejectedRecord(Base):
    """A row that failed validation. Never loaded into a fact table, never lost."""

    __tablename__ = "etl_rejected_records"

    id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("etl_import_batches.batch_id", ondelete="CASCADE"), nullable=False,
    )
    data_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_file: Mapped[str | None] = mapped_column(Text)
    source_row_number: Mapped[int | None] = mapped_column(Integer)
    raw_data: Mapped[dict | None] = mapped_column(JSON_TYPE)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False)
    error_category: Mapped[str] = mapped_column(String(32), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    field_name: Mapped[str | None] = mapped_column(String(64))
    field_value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_etl_rejected_records_batch_id", "batch_id"),
        Index("ix_etl_rejected_records_error_code", "error_code"),
        Index("ix_etl_rejected_records_data_type", "data_type"),
    )


# ===========================================================================
# Staging
# ===========================================================================


class StagingMixin:
    """Audit columns every staging table carries.

    Staging keeps the source shape: every business column is TEXT, exactly as it
    was read, so that a value which fails numeric or date validation is still
    visible for diagnosis. ``raw_data`` holds the complete original row
    including columns the canonical mapping does not use.
    """

    staging_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    import_batch_id = fk_column(
        "etl_import_batches.batch_id", nullable=False, ondelete="CASCADE"
    )
    source_file: Mapped[str | None] = mapped_column(Text)
    source_row_number: Mapped[int | None] = mapped_column(Integer)
    source_system: Mapped[str | None] = mapped_column(String(32))
    raw_data: Mapped[dict | None] = mapped_column(JSON_TYPE)
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    validation_status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    validation_error: Mapped[str | None] = mapped_column(Text)


STAGING_PENDING = "PENDING"
STAGING_VALID = "VALID"
STAGING_REJECTED = "REJECTED"
STAGING_DUPLICATE = "DUPLICATE"


class OrgCodeMixin:
    """The organisational codes a transaction source may carry, as raw text."""

    company_code: Mapped[str | None] = mapped_column(CODE)
    bu_code: Mapped[str | None] = mapped_column(CODE)
    sales_line_code: Mapped[str | None] = mapped_column(CODE)
    zone_code: Mapped[str | None] = mapped_column(CODE)
    region_code: Mapped[str | None] = mapped_column(CODE)
    area_code: Mapped[str | None] = mapped_column(CODE)
    unit_code: Mapped[str | None] = mapped_column(CODE)
    territory_code: Mapped[str | None] = mapped_column(CODE)
    sub_territory_code: Mapped[str | None] = mapped_column(CODE)


class StgSales(Base, StagingMixin, OrgCodeMixin):
    __tablename__ = "stg_sales"

    transaction_date: Mapped[str | None] = mapped_column(String(64))
    invoice_no: Mapped[str | None] = mapped_column(String(128))
    invoice_line_no: Mapped[str | None] = mapped_column(String(64))
    batch_code: Mapped[str | None] = mapped_column(CODE)
    #: The Material Code the source states for the line. Since revision 0022 this
    #: is the only item identifier a sale carries: it resolves against
    #: ``dim_material`` and against nothing else.
    material_code: Mapped[str | None] = mapped_column(CODE)
    customer_code: Mapped[str | None] = mapped_column(CODE)
    sales_force_code: Mapped[str | None] = mapped_column(CODE)
    quantity: Mapped[str | None] = mapped_column(String(64))
    gross_sales: Mapped[str | None] = mapped_column(String(64))
    discount: Mapped[str | None] = mapped_column(String(64))
    net_sales: Mapped[str | None] = mapped_column(String(64))
    cost: Mapped[str | None] = mapped_column(String(64))
    volume: Mapped[str | None] = mapped_column(String(64))
    volume_unit: Mapped[str | None] = mapped_column(String(16))
    source_transaction_id: Mapped[str | None] = mapped_column(SOURCE_ID)

    __table_args__ = (Index("ix_stg_sales_batch", "import_batch_id", "validation_status"),)


class StgMaterialStock(Base, StagingMixin):
    """Material Transaction Data as the file supplied it.

    No ``OrgCodeMixin``: material stock is located by Plant and Storage Location,
    which are its own dimension, not by the sales hierarchy. Carrying region and
    territory columns here would invite a mapping that has no source.
    """

    __tablename__ = "stg_material_stock"

    company_code: Mapped[str | None] = mapped_column(CODE)
    plant_code: Mapped[str | None] = mapped_column(CODE)
    storage_location_code: Mapped[str | None] = mapped_column(CODE)
    material_group_code: Mapped[str | None] = mapped_column(CODE)
    material_brand_code: Mapped[str | None] = mapped_column(CODE)
    material_code: Mapped[str | None] = mapped_column(CODE)
    unrestricted_stock: Mapped[str | None] = mapped_column(String(64))
    quality_inspection_stock: Mapped[str | None] = mapped_column(String(64))
    blocked_stock: Mapped[str | None] = mapped_column(String(64))
    stock_in_transit: Mapped[str | None] = mapped_column(String(64))
    production_date: Mapped[str | None] = mapped_column(String(64))
    shelf_life_expiration_date: Mapped[str | None] = mapped_column(String(64))
    source_transaction_id: Mapped[str | None] = mapped_column(SOURCE_ID)

    __table_args__ = (
        Index("ix_stg_material_stock_batch", "import_batch_id", "validation_status"),
    )


class StgTarget(Base, StagingMixin, OrgCodeMixin):
    """Target rows as the file supplied them.

    ``OrgCodeMixin`` still brings the full set of organisational columns, but a
    Target file states only territory and sub-territory: the dataset spec maps
    those two and reports any other level as an unmapped column. The unused
    columns are left in place because the mixin is shared with four other
    staging tables, and staging is the layer that keeps the source shape rather
    than the layer that enforces it.

    There is no ``target_date``: the file states a month and a financial year,
    and the date they resolve to is derived during validation, so staging — which
    holds what arrived — has nothing to put in such a column.
    """

    __tablename__ = "stg_target"

    target_month: Mapped[str | None] = mapped_column(String(32))
    financial_year: Mapped[str | None] = mapped_column(String(32))
    customer_code: Mapped[str | None] = mapped_column(CODE)
    #: The Material Code the target is set for. The Target file's column is still
    #: headed ``SKU Code`` — that is what the planners call it — and the dataset
    #: spec accepts either heading; what it resolves against is the Material
    #: Master.
    material_code: Mapped[str | None] = mapped_column(CODE)
    sales_force_code: Mapped[str | None] = mapped_column(CODE)
    target_amount: Mapped[str | None] = mapped_column(String(64))
    target_quantity: Mapped[str | None] = mapped_column(String(64))
    target_volume: Mapped[str | None] = mapped_column(String(64))
    source_transaction_id: Mapped[str | None] = mapped_column(SOURCE_ID)

    __table_args__ = (Index("ix_stg_target_batch", "import_batch_id", "validation_status"),)


# ===========================================================================
# Facts
# ===========================================================================


class FactAuditMixin:
    """Provenance carried by every fact row: source system, file, row, batch."""

    source_system: Mapped[str] = mapped_column(String(32), nullable=False)
    source_transaction_id: Mapped[str | None] = mapped_column(SOURCE_ID)
    source_file: Mapped[str | None] = mapped_column(Text)
    source_row_number: Mapped[int | None] = mapped_column(Integer)
    import_batch_id = fk_column("etl_import_batches.batch_id", nullable=False)
    #: Deterministic de-duplication key; see ``etl.datasets.DatasetSpec``.
    business_key: Mapped[str] = mapped_column(BUSINESS_KEY, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class VoidableMixin:
    """A transaction is reversed, never erased.

    Deleting a fact row would silently change last month's reported figures and
    leave nothing to explain why. Voiding keeps the row, its provenance and its
    business key, and removes it from every report at once — the reporting views
    filter on ``is_void``, so a voided sale disappears from the dashboard, the
    map, the exports and the AI agent in the same instant, without any of them
    knowing this feature exists.

    Keeping the row also means the ETL still recognises the business key, so
    re-importing the source transaction updates the voided row rather than
    inserting a duplicate.
    """

    is_void: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[str | None] = mapped_column(String(64))
    #: Why. Required by the API, because an unexplained reversal of an
    #: accounting record is not auditable.
    void_reason: Mapped[str | None] = mapped_column(Text)


class OrgDimensionMixin:
    """Resolved organisational surrogate keys.

    Every level is nullable because a source is not required to carry every
    level; the mapper fills in whatever the hierarchy lets it derive from the
    deepest code supplied. A record with no resolvable organisational level at
    all is rejected, not stored with all-NULL dimensions.
    """

    company_id = fk_column("dim_company.company_id")
    business_unit_id = fk_column("dim_business_unit.business_unit_id")
    sales_line_id = fk_column("dim_sales_line.sales_line_id")
    zone_id = fk_column("dim_zone.zone_id")
    region_id = fk_column("dim_region.region_id")
    area_id = fk_column("dim_area.area_id")
    unit_id = fk_column("dim_unit.unit_id")


class FullOrgDimensionMixin(OrgDimensionMixin):
    """Organisational keys down to sub-territory (not used by stock)."""

    territory_id = fk_column("dim_territory.territory_id")
    sub_territory_id = fk_column("dim_sub_territory.sub_territory_id")


class FactSales(Base, FullOrgDimensionMixin, FactAuditMixin, VoidableMixin):
    """Invoice-line level sales."""

    __tablename__ = "fact_sales"

    sales_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    date_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("dim_date.date_id", ondelete="RESTRICT"), nullable=False
    )
    #: The material sold, resolved against the Material Master. Since revision
    #: 0022 this is the sale's only item key — the SKU dimension it replaced held
    #: a second identity for the same goods, and a sale, a target and a stock
    #: position now all reach the same row of ``dim_material`` by the same code.
    #: The constraint is **named**, unlike most in this schema. Revision 0022
    #: created it inside a SQLite ``batch_alter_table``, which cannot attach an
    #: anonymous constraint to a table it is rewriting; the model spells the same
    #: name so a fresh migration and these models compare equal.
    material_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("dim_material.material_id", ondelete="RESTRICT",
                   name="fk_fact_sales_material_id"),
        nullable=False,
    )
    customer_id: Mapped[int | None] = fk_column("dim_customer.customer_id")
    sales_force_id: Mapped[int | None] = fk_column("dim_sales_force.sales_force_id")

    #: Codes as they arrived, kept for traceability and for back-filling the
    #: PENDING_SOURCE_DATA dimensions once their masters exist.
    #:
    #: ``material_code`` is here for a different reason: the material is resolved,
    #: not pending, but keeping the raw code on the row is what lets a fact state
    #: what the file said without a join, exactly as the stock fact does.
    material_code: Mapped[str] = mapped_column(CODE, nullable=False)
    customer_code: Mapped[str | None] = mapped_column(CODE)
    sales_force_code: Mapped[str | None] = mapped_column(CODE)

    invoice_no: Mapped[str | None] = mapped_column(String(128))
    #: The source ERP's own line identifier, when it supplies one. Part of the
    #: business key in that case; NULL when the source does not number lines.
    invoice_line_no: Mapped[str | None] = mapped_column(String(64))
    #: Manufacturing batch, stored exactly as supplied. The same material on one
    #: invoice from two batches is two legitimate lines, which is why this is
    #: part of the fallback business key.
    batch_code: Mapped[str | None] = mapped_column(CODE)

    quantity: Mapped[float] = mapped_column(QUANTITY, nullable=False, default=0)
    gross_sales: Mapped[float] = mapped_column(MONEY, nullable=False, default=0)
    discount: Mapped[float] = mapped_column(MONEY, nullable=False, default=0)
    net_sales: Mapped[float] = mapped_column(MONEY, nullable=False, default=0)
    cost: Mapped[float | None] = mapped_column(MONEY)
    gross_profit: Mapped[float | None] = mapped_column(MONEY)

    #: The Total Volume the source file stated for the line, stored exactly as
    #: supplied. Never derived and never converted; see ``etl.volume``.
    volume: Mapped[float | None] = mapped_column(QUANTITY)
    #: Historical, unwritten columns. They held the unit and the pack size of a
    #: volume this system used to *compute*; nothing has written either since the
    #: uploaded Total Volume became the volume, and no migration drops them
    #: because the figures they explain are still on the rows they explain.
    volume_unit: Mapped[str | None] = mapped_column(String(16))
    volume_factor: Mapped[float | None] = mapped_column(Numeric(18, 6))

    __table_args__ = (
        UniqueConstraint("business_key", name="uq_fact_sales_business_key"),
        Index("ix_fact_sales_date_id", "date_id"),
        Index("ix_fact_sales_material_id", "material_id"),
        Index("ix_fact_sales_material_code", "material_code"),
        Index("ix_fact_sales_region_id", "region_id"),
        Index("ix_fact_sales_territory_id", "territory_id"),
        Index("ix_fact_sales_batch", "import_batch_id"),
        Index("ix_fact_sales_source_system", "source_system"),
        Index("ix_fact_sales_invoice_no", "invoice_no"),
        # The duplicate report and any "show me this invoice" lookup filter on
        # the invoice and its line; the batch index serves batch-wise reporting.
        Index("ix_fact_sales_invoice_line", "invoice_no", "invoice_line_no"),
        Index("ix_fact_sales_batch_code", "batch_code"),
        Index("ix_fact_sales_volume_unit", "volume_unit"),
        Index("ix_fact_sales_date_region", "date_id", "region_id"),
    )


class FactMaterialStock(Base, FactAuditMixin, VoidableMixin):
    """The current material stock position, by location and material group.

    **A snapshot, not a series.** The Material Transaction Data carries no
    posting date — Production Date and Shelf Life Expiration Date describe the
    goods, not when the position was taken — so this table holds *where stock
    stands now*. Re-uploading the file updates the same rows rather than adding
    a second day's worth, which is what the business key below encodes. Nothing
    here reconstructs a history the source does not supply.

    **No date dimension and no organisational hierarchy.** There is no
    ``date_id``: there is no date to put in it. There is no region or territory:
    stock sits in a Plant and a Storage Location, which are its own dimension,
    and mapping it onto the sales hierarchy would be an invention.

    **Three masters, not one.** A position resolves to a Plant, a Storage
    Location and a Material, each its own dimension since revision 0019. The
    material carries its group and its brand, so stock reports by brand without
    this table storing a brand name it would then have to keep in step.

    **One material dimension, shared with sales and target.** Since revision 0022
    ``dim_material`` is the only item master, so a brand here and a brand on a
    sales report are the same brand of the same goods, reached by the same
    ``material_code``. There is no second product identity left to reconcile.

    The four measures stay four measures. ``unrestricted`` is what can be sold;
    the other three are held for a reason, and adding them together is the
    caller's decision to state explicitly, never this table's.
    """

    __tablename__ = "fact_material_stock"

    material_stock_id: Mapped[int] = mapped_column(
        SURROGATE_PK, primary_key=True, autoincrement=True)

    #: The three master rows this position belongs to. All nullable because a
    #: position whose masters cannot be resolved is *rejected* rather than
    #: stored — these are here for the resolved case, not as a tolerance.
    plant_id: Mapped[int | None] = fk_column("dim_plant.plant_id")
    storage_location_id: Mapped[int | None] = fk_column(
        "dim_storage_location.storage_location_id")
    material_id: Mapped[int | None] = fk_column("dim_material.material_id")
    #: The codes as the file stated them, kept alongside the resolved keys so a
    #: row still says what it came from after a master record is renamed.
    company_code: Mapped[str] = mapped_column(CODE, nullable=False)
    plant_code: Mapped[str] = mapped_column(CODE, nullable=False)
    storage_location_code: Mapped[str] = mapped_column(CODE, nullable=False)
    #: All required on a position. A stock row naming no material, group or
    #: brand cannot be resolved against the Material Master and is rejected long
    #: before here.
    material_code: Mapped[str] = mapped_column(CODE, nullable=False)
    material_group_code: Mapped[str] = mapped_column(CODE, nullable=False)
    material_brand_code: Mapped[str] = mapped_column(CODE, nullable=False)

    unrestricted_stock: Mapped[float] = mapped_column(QUANTITY, nullable=False, default=0)
    quality_inspection_stock: Mapped[float] = mapped_column(QUANTITY, nullable=False, default=0)
    blocked_stock: Mapped[float] = mapped_column(QUANTITY, nullable=False, default=0)
    stock_in_transit: Mapped[float] = mapped_column(QUANTITY, nullable=False, default=0)

    #: Attributes of the goods, not of the reading. Both nullable: a material
    #: group with no shelf life legitimately has neither, and that is reported as
    #: "no expiry date" rather than guessed.
    production_date: Mapped[_date | None] = mapped_column(Date)
    shelf_life_expiration_date: Mapped[_date | None] = mapped_column(Date)

    __table_args__ = (
        UniqueConstraint("business_key", name="uq_fact_material_stock_business_key"),
        Index("ix_fact_material_stock_plant_id", "plant_id"),
        Index("ix_fact_material_stock_storage_id", "storage_location_id"),
        Index("ix_fact_material_stock_material_id", "material_id"),
        Index("ix_fact_material_stock_company", "company_code"),
        Index("ix_fact_material_stock_plant", "plant_code"),
        Index("ix_fact_material_stock_storage", "storage_location_code"),
        Index("ix_fact_material_stock_material", "material_code"),
        Index("ix_fact_material_stock_group", "material_group_code"),
        Index("ix_fact_material_stock_brand", "material_brand_code"),
        Index("ix_fact_material_stock_expiry", "shelf_life_expiration_date"),
        Index("ix_fact_material_stock_batch", "import_batch_id"),
        Index("ix_fact_material_stock_source_system", "source_system"),
    )


class FactTarget(Base, FullOrgDimensionMixin, FactAuditMixin, VoidableMixin):
    """Monthly sales targets by territory, customer, material and sales force.

    A clean fact table: codes and measures only. No territory name, customer name
    or material description is stored here — those are attributes of the
    dimensions the codes resolve to, and duplicating them would let a target row
    go on naming a customer the master has since renamed. The reporting views
    join for them.

    The organisational surrogate keys above territory are **derived**, not
    supplied: a Target file states a territory and the ETL walks the master
    hierarchy up to company, so the region a target belongs to is always the one
    the master says it belongs to.
    """

    __tablename__ = "fact_target"

    target_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    date_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("dim_date.date_id", ondelete="RESTRICT"), nullable=False
    )
    #: The material the target is set for. Nullable in the schema, required by
    #: the ETL.
    #:
    #: Every target loaded under the current structure names a material — the
    #: dataset spec marks it required and a row without one is rejected. The
    #: column stays nullable because target rows loaded before that rule existed
    #: did not have to, and those rows are history: they are not deleted and not
    #: given an invented material to satisfy a constraint added after the fact.
    #: Named for the reason ``FactSales.material_id`` gives.
    material_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("dim_material.material_id",
                   name="fk_fact_target_material_id"),
    )
    customer_id: Mapped[int | None] = fk_column("dim_customer.customer_id")
    sales_force_id: Mapped[int | None] = fk_column("dim_sales_force.sales_force_id")

    #: Codes as they arrived. ``customer_code`` and ``sales_force_code`` are kept
    #: for the same reason as on ``fact_sales``: those two dimensions are
    #: PENDING_SOURCE_DATA, so an unknown code is a deferred mapping rather than a
    #: rejection and the code is what back-fills the link once the master
    #: arrives. ``material_code`` is kept so a target states its own item without
    #: a join, as the sales and stock facts do.
    material_code: Mapped[str | None] = mapped_column(CODE)
    customer_code: Mapped[str | None] = mapped_column(CODE)
    sales_force_code: Mapped[str | None] = mapped_column(CODE)

    #: ``YYYY-MM``, and the financial-year label ``dim_date`` stores.
    #:
    #: Both are derived from the pair the file states and both are redundant
    #: with ``date_id`` — deliberately. They are the period *as the target was
    #: set*, they are what the business key is built from, and keeping them on
    #: the row means a target names its own month without a join.
    target_month: Mapped[str | None] = mapped_column(String(16))
    financial_year: Mapped[str | None] = mapped_column(String(32))

    target_amount: Mapped[float] = mapped_column(MONEY, nullable=False, default=0)
    #: Quantity and volume targets, both optional.
    #:
    #: ``target_amount`` stays the one required measure: a target set in taka
    #: alone is a complete target, and every row loaded before these columns
    #: existed is still valid. NULL means "no target was set for this measure",
    #: which is not the same as a target of zero — a zero target is a real
    #: instruction and must stay distinguishable from an absent one.
    target_quantity: Mapped[float | None] = mapped_column(QUANTITY)
    #: The volume target, exactly as the file stated it.
    #:
    #: Unit-free since revision 0022, like the volume on a sale. The unit used to
    #: be reached through the SKU's ``pack_unit`` in the Product Master; with that
    #: master gone the Material Master states no unit, and asserting one would be
    #: inventing the measurement the source never made. The figure is the number
    #: the planner typed, and it is reported as that.
    target_volume: Mapped[float | None] = mapped_column(QUANTITY)

    __table_args__ = (
        UniqueConstraint("business_key", name="uq_fact_target_business_key"),
        Index("ix_fact_target_date_id", "date_id"),
        Index("ix_fact_target_material_id", "material_id"),
        Index("ix_fact_target_material_code", "material_code"),
        Index("ix_fact_target_customer_id", "customer_id"),
        Index("ix_fact_target_region_id", "region_id"),
        Index("ix_fact_target_territory_id", "territory_id"),
        Index("ix_fact_target_month", "target_month"),
        Index("ix_fact_target_financial_year", "financial_year"),
        Index("ix_fact_target_batch", "import_batch_id"),
        Index("ix_fact_target_source_system", "source_system"),
    )


STAGING_MODEL_BY_DATA_TYPE = {
    "sales": StgSales,
    "material_stock": StgMaterialStock,
    "target": StgTarget,
}

FACT_MODEL_BY_DATA_TYPE = {
    "sales": FactSales,
    "material_stock": FactMaterialStock,
    "target": FactTarget,
}

FUTURE_READY_DIMENSIONS = {
    "dim_customer": "Customer Master is not part of Master Data.xlsx.",
    "dim_sales_force": "Sales Force Master is not part of Master Data.xlsx.",
}

__all__ = [
    "DimDate",
    "DimCustomer",
    "DimSalesForce",
    "MasterSourceStatus",
    "EtlImportBatch",
    "EtlRejectedRecord",
    "StgSales",
    "StgMaterialStock",
    "StgTarget",
    "VoidableMixin",
    "FactSales",
    "FactMaterialStock",
    "FactTarget",
    "STAGING_MODEL_BY_DATA_TYPE",
    "FACT_MODEL_BY_DATA_TYPE",
    "FUTURE_READY_DIMENSIONS",
    "STATUS_AVAILABLE",
    "STATUS_PENDING_SOURCE_DATA",
    "BATCH_STARTED",
    "BATCH_VALIDATING",
    "BATCH_COMPLETED",
    "BATCH_COMPLETED_WITH_ERRORS",
    "BATCH_FAILED",
    "STAGING_PENDING",
    "STAGING_VALID",
    "STAGING_REJECTED",
    "STAGING_DUPLICATE",
]
