"""Phase 1: master-data dimension tables.

Creates the ten dimensions of the organisational hierarchy plus the independent
product dimension, with surrogate keys, unique business codes, foreign keys,
indexes and audit timestamps.

Revision ID: 0001_master_data_dimensions
Revises:
Create Date: 2026-08-10
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_master_data_dimensions"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CODE = sa.String(length=64)
PHONE = sa.String(length=32)
MONEY = sa.Numeric(precision=18, scale=4)
# BIGSERIAL on PostgreSQL. SQLite only auto-increments a column declared exactly
# INTEGER PRIMARY KEY, so it gets the narrower type through a dialect variant;
# this keeps the migration runnable against the test database.
SURROGATE_PK = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def _timestamps() -> list[sa.Column]:
    return [
        # sa.func.now() renders as now() on PostgreSQL and CURRENT_TIMESTAMP on
        # SQLite, which keeps the migration runnable in tests.
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    ]


def upgrade() -> None:
    # ---------------------------------------------------------------- company
    op.create_table(
        "dim_company",
        sa.Column("company_id", SURROGATE_PK, autoincrement=True, nullable=False),
        sa.Column("company_code", CODE, nullable=False),
        sa.Column("company_name", sa.Text(), nullable=False),
        sa.Column("company_head_id", CODE, nullable=True),
        sa.Column("company_head_name", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("company_id", name="pk_dim_company"),
        sa.UniqueConstraint("company_code", name="uq_dim_company_company_code"),
    )
    op.create_index("ix_dim_company_company_code", "dim_company", ["company_code"])

    # ---------------------------------------------------------- business unit
    op.create_table(
        "dim_business_unit",
        sa.Column("business_unit_id", SURROGATE_PK, autoincrement=True, nullable=False),
        sa.Column("bu_code", CODE, nullable=False),
        sa.Column("bu_name", sa.Text(), nullable=False),
        sa.Column("company_code", CODE, nullable=False),
        sa.Column("bu_head_id", CODE, nullable=True),
        sa.Column("bu_head_name", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("business_unit_id", name="pk_dim_business_unit"),
        sa.UniqueConstraint("bu_code", name="uq_dim_business_unit_bu_code"),
        sa.ForeignKeyConstraint(
            ["company_code"], ["dim_company.company_code"],
            name="fk_dim_business_unit_company_code",
            ondelete="RESTRICT", onupdate="CASCADE",
        ),
    )
    op.create_index("ix_dim_business_unit_bu_code", "dim_business_unit", ["bu_code"])
    op.create_index("ix_dim_business_unit_company_code", "dim_business_unit", ["company_code"])

    # ------------------------------------------------------------- sales line
    op.create_table(
        "dim_sales_line",
        sa.Column("sales_line_id", SURROGATE_PK, autoincrement=True, nullable=False),
        sa.Column("sales_line_code", CODE, nullable=False),
        sa.Column("sales_line_name", sa.Text(), nullable=False),
        sa.Column("bu_code", CODE, nullable=False),
        sa.Column("sales_line_head_id", CODE, nullable=True),
        sa.Column("sales_line_head_name", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("sales_line_id", name="pk_dim_sales_line"),
        sa.UniqueConstraint("sales_line_code", name="uq_dim_sales_line_sales_line_code"),
        sa.ForeignKeyConstraint(
            ["bu_code"], ["dim_business_unit.bu_code"],
            name="fk_dim_sales_line_bu_code",
            ondelete="RESTRICT", onupdate="CASCADE",
        ),
    )
    op.create_index("ix_dim_sales_line_sales_line_code", "dim_sales_line", ["sales_line_code"])
    op.create_index("ix_dim_sales_line_bu_code", "dim_sales_line", ["bu_code"])

    # ------------------------------------------------------------------- zone
    op.create_table(
        "dim_zone",
        sa.Column("zone_id", SURROGATE_PK, autoincrement=True, nullable=False),
        sa.Column("zone_code", CODE, nullable=False),
        sa.Column("zone_name", sa.Text(), nullable=False),
        sa.Column("sales_line_code", CODE, nullable=False),
        sa.Column("zone_head_id", CODE, nullable=True),
        sa.Column("zone_head_name", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("zone_id", name="pk_dim_zone"),
        sa.UniqueConstraint("zone_code", name="uq_dim_zone_zone_code"),
        sa.ForeignKeyConstraint(
            ["sales_line_code"], ["dim_sales_line.sales_line_code"],
            name="fk_dim_zone_sales_line_code",
            ondelete="RESTRICT", onupdate="CASCADE",
        ),
    )
    op.create_index("ix_dim_zone_zone_code", "dim_zone", ["zone_code"])
    op.create_index("ix_dim_zone_sales_line_code", "dim_zone", ["sales_line_code"])

    # ----------------------------------------------------------------- region
    op.create_table(
        "dim_region",
        sa.Column("region_id", SURROGATE_PK, autoincrement=True, nullable=False),
        sa.Column("region_code", CODE, nullable=False),
        sa.Column("region_name", sa.Text(), nullable=False),
        sa.Column("zone_code", CODE, nullable=False),
        sa.Column("region_head_id", CODE, nullable=True),
        sa.Column("region_head_name", sa.Text(), nullable=True),
        sa.Column("region_hq", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("region_id", name="pk_dim_region"),
        sa.UniqueConstraint("region_code", name="uq_dim_region_region_code"),
        sa.ForeignKeyConstraint(
            ["zone_code"], ["dim_zone.zone_code"],
            name="fk_dim_region_zone_code",
            ondelete="RESTRICT", onupdate="CASCADE",
        ),
    )
    op.create_index("ix_dim_region_region_code", "dim_region", ["region_code"])
    op.create_index("ix_dim_region_zone_code", "dim_region", ["zone_code"])

    # ------------------------------------------------------------------- area
    op.create_table(
        "dim_area",
        sa.Column("area_id", SURROGATE_PK, autoincrement=True, nullable=False),
        sa.Column("area_code", CODE, nullable=False),
        sa.Column("area_name", sa.Text(), nullable=False),
        sa.Column("region_code", CODE, nullable=False),
        sa.Column("area_head_id", CODE, nullable=True),
        sa.Column("area_head_name", sa.Text(), nullable=True),
        sa.Column("area_hq", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("area_id", name="pk_dim_area"),
        sa.UniqueConstraint("area_code", name="uq_dim_area_area_code"),
        sa.ForeignKeyConstraint(
            ["region_code"], ["dim_region.region_code"],
            name="fk_dim_area_region_code",
            ondelete="RESTRICT", onupdate="CASCADE",
        ),
    )
    op.create_index("ix_dim_area_area_code", "dim_area", ["area_code"])
    op.create_index("ix_dim_area_region_code", "dim_area", ["region_code"])

    # ------------------------------------------------------------------- unit
    op.create_table(
        "dim_unit",
        sa.Column("unit_id", SURROGATE_PK, autoincrement=True, nullable=False),
        sa.Column("unit_code", CODE, nullable=False),
        sa.Column("unit_name", sa.Text(), nullable=False),
        sa.Column("area_code", CODE, nullable=False),
        sa.Column("unit_head_id", CODE, nullable=True),
        sa.Column("unit_head_name", sa.Text(), nullable=True),
        sa.Column("unit_hq", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("unit_id", name="pk_dim_unit"),
        sa.UniqueConstraint("unit_code", name="uq_dim_unit_unit_code"),
        sa.ForeignKeyConstraint(
            ["area_code"], ["dim_area.area_code"],
            name="fk_dim_unit_area_code",
            ondelete="RESTRICT", onupdate="CASCADE",
        ),
    )
    op.create_index("ix_dim_unit_unit_code", "dim_unit", ["unit_code"])
    op.create_index("ix_dim_unit_area_code", "dim_unit", ["area_code"])

    # -------------------------------------------------------------- territory
    op.create_table(
        "dim_territory",
        sa.Column("territory_id", SURROGATE_PK, autoincrement=True, nullable=False),
        sa.Column("territory_code", CODE, nullable=False),
        sa.Column("territory_name", sa.Text(), nullable=False),
        sa.Column("unit_code", CODE, nullable=False),
        sa.Column("territory_head_id", CODE, nullable=True),
        sa.Column("territory_head_name", sa.Text(), nullable=True),
        sa.Column("territory_head_phone", PHONE, nullable=True),
        sa.Column("territory_hq", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("territory_id", name="pk_dim_territory"),
        sa.UniqueConstraint("territory_code", name="uq_dim_territory_territory_code"),
        sa.ForeignKeyConstraint(
            ["unit_code"], ["dim_unit.unit_code"],
            name="fk_dim_territory_unit_code",
            ondelete="RESTRICT", onupdate="CASCADE",
        ),
    )
    op.create_index("ix_dim_territory_territory_code", "dim_territory", ["territory_code"])
    op.create_index("ix_dim_territory_unit_code", "dim_territory", ["unit_code"])

    # ---------------------------------------------------------- sub-territory
    op.create_table(
        "dim_sub_territory",
        sa.Column("sub_territory_id", SURROGATE_PK, autoincrement=True, nullable=False),
        sa.Column("sub_territory_code", CODE, nullable=False),
        sa.Column("sub_territory_name", sa.Text(), nullable=False),
        sa.Column("territory_code", CODE, nullable=False),
        sa.Column("sub_territory_head_id", CODE, nullable=True),
        sa.Column("sub_territory_head_name", sa.Text(), nullable=True),
        sa.Column("sub_territory_head_phone", PHONE, nullable=True),
        sa.Column("sub_territory_hq", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("sub_territory_id", name="pk_dim_sub_territory"),
        sa.UniqueConstraint("sub_territory_code",
                            name="uq_dim_sub_territory_sub_territory_code"),
        sa.ForeignKeyConstraint(
            ["territory_code"], ["dim_territory.territory_code"],
            name="fk_dim_sub_territory_territory_code",
            ondelete="RESTRICT", onupdate="CASCADE",
        ),
    )
    op.create_index("ix_dim_sub_territory_sub_territory_code", "dim_sub_territory",
                    ["sub_territory_code"])
    op.create_index("ix_dim_sub_territory_territory_code", "dim_sub_territory",
                    ["territory_code"])

    # ---------------------------------------------------------------- product
    # Independent dimension: the workbook exposes no link between a SKU and the
    # organisational hierarchy, so no foreign key is invented here.
    op.create_table(
        "dim_product",
        sa.Column("product_id", SURROGATE_PK, autoincrement=True, nullable=False),
        sa.Column("sku_code", CODE, nullable=False),
        sa.Column("sku_id", CODE, nullable=True),
        sa.Column("sku_name_en", sa.Text(), nullable=False),
        sa.Column("sku_name_bn", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("product_type", sa.Text(), nullable=True),
        sa.Column("brand", sa.Text(), nullable=True),
        sa.Column("producer_company", sa.Text(), nullable=True),
        sa.Column("db_price", MONEY, nullable=True),
        sa.Column("trade_price", MONEY, nullable=True),
        sa.Column("retail_price", MONEY, nullable=True),
        sa.Column("pack_size", sa.Text(), nullable=True),
        sa.Column("unit_conversation_ratio", MONEY, nullable=True),
        sa.Column("retailer_unit", sa.Text(), nullable=True),
        sa.Column("consumer_unit", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("sales_type", sa.Text(), nullable=True),
        sa.Column("total_alt_sku", sa.Integer(), nullable=True),
        sa.Column("sequence_no", sa.Integer(), nullable=True),
        sa.Column("start_time", sa.DateTime(timezone=False), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("product_id", name="pk_dim_product"),
        sa.UniqueConstraint("sku_code", name="uq_dim_product_sku_code"),
    )
    op.create_index("ix_dim_product_sku_code", "dim_product", ["sku_code"])
    op.create_index("ix_dim_product_sku_id", "dim_product", ["sku_id"])
    op.create_index("ix_dim_product_category", "dim_product", ["category"])
    op.create_index("ix_dim_product_brand", "dim_product", ["brand"])
    op.create_index("ix_dim_product_status", "dim_product", ["status"])


def downgrade() -> None:
    # Children first so that foreign keys never block the drop.
    for table in (
        "dim_product",
        "dim_sub_territory",
        "dim_territory",
        "dim_unit",
        "dim_area",
        "dim_region",
        "dim_zone",
        "dim_sales_line",
        "dim_business_unit",
        "dim_company",
    ):
        op.drop_table(table)
