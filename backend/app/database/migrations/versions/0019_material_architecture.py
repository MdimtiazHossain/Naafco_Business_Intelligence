"""The material architecture, normalised: Plant, Storage Location, Material.

Revision ID: 0019_material_architecture
Revises: 0018_material_code
Create Date: 2026-08-19

``0016_material_stock`` recorded the whole material side in one table,
``dim_material_location``, keyed on Company + Plant + Storage Location +
Material Group; ``0018_material_code`` inserted the Material Code it had been
specified without. That shape was always a flattening of three different things,
and it showed: a plant's name was repeated once per storage location per
material, a storage location could not be named without naming a material, and
there was nowhere to record a Material Brand or a Material Description because
no row belonged to a material alone.

This revision separates them into the three masters the business actually has:

    Company + Plant              -> dim_plant
    Plant + Storage Location     -> dim_storage_location
    Material Group -> Brand -> Material  -> dim_material

and repoints ``fact_material_stock`` at all three.

**Brand and Description are genuinely new.** No file loaded so far carried
either, so nothing here back-fills them — there is nothing to back-fill *from*,
and this project does not invent master data. They arrive with the next Material
Master upload, whose template requires them.

**Why ``dim_material_location`` is dropped rather than decomposed in place.**
Its 155 rows carry no Material Brand and no Material Description, so a row
derived from one would be a Material Master record that is missing the two
columns that now define a material. The operator was asked, and chose a fresh
Material Master upload over inheriting incomplete rows. The rows are exported to
``reports/dim_material_location_pre0019.csv`` before this runs and the database
is backed up to ``data/dev.db.pre0019.bak``; this migration additionally refuses
to run if either fact table holds a row, so nothing that a report could still be
reading is destroyed silently.

**``dim_product.material_code`` is the Sales/Target bridge, and it starts
empty.** ``fact_sales`` and ``fact_target`` identify a product by ``sku_code``;
the material side identifies one by ``material_code``; no source states a
mapping between them. The column is created so the mapping has somewhere to
live, and is populated only from the optional ``SKU Code`` column on the new
Material Master upload — that is, from the source system, never derived here.
Until it is populated, Sales and Target reporting is untouched and continues to
resolve products through ``dim_product.sku_code`` exactly as before.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_material_architecture"
down_revision: Union[str, None] = "0018_material_code"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Portable surrogate primary key, as every revision from 0001 spells it.
def _pk() -> sa.types.TypeEngine:
    return sa.BigInteger().with_variant(sa.Integer, "sqlite")


#: The audit columns ``TimestampMixin`` + ``SoftDeleteMixin`` put on a dimension.
#: Spelled out per table because Alembic takes columns, not mixins, and the
#: models are the thing these must match.
def _master_audit_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_by", sa.String(64), nullable=True),
    ]


#: The stock view, rebuilt over the three masters.
#:
#: Based on the body ``0018_material_code`` authored — the revision that last
#: wrote this view — with the single ``dim_material_location`` join replaced by
#: three, and Material Brand and Material Description added beside the codes
#: that identify them. ``total_stock`` keeps its one definition, unchanged and
#: still defined only here: all four categories including in transit, so the
#: dashboard, the detail table, the export and the agent cannot drift apart on
#: what the word means.
#:
#: Every join is a LEFT JOIN and every filter is on the fact table, so a stock
#: position whose master row was retired still appears in the report with its
#: own codes intact rather than vanishing from a total.
MATERIAL_STOCK_DETAIL = """
SELECT
    f.material_stock_id,
    f.company_code,
    f.plant_code,
    p.plant_name,
    f.storage_location_code,
    s.storage_location_name,
    f.material_code,
    m.material_description,
    f.material_group_code,
    m.material_group_name,
    f.material_brand_code,
    m.material_brand,
    f.unrestricted_stock,
    f.quality_inspection_stock,
    f.blocked_stock,
    f.stock_in_transit,
    (COALESCE(f.unrestricted_stock, 0)
     + COALESCE(f.quality_inspection_stock, 0)
     + COALESCE(f.blocked_stock, 0)
     + COALESCE(f.stock_in_transit, 0)) AS total_stock,
    f.production_date,
    f.shelf_life_expiration_date,
    f.source_system,
    f.import_batch_id,
    f.is_void
FROM fact_material_stock f
LEFT JOIN dim_plant p
       ON p.plant_id = f.plant_id
LEFT JOIN dim_storage_location s
       ON s.storage_location_id = f.storage_location_id
LEFT JOIN dim_material m
       ON m.material_id = f.material_id
WHERE f.is_void = 0
"""

#: The 0018 view body, for the downgrade. Kept verbatim rather than derived from
#: the one above, so a downgrade restores exactly what that revision authored.
MATERIAL_STOCK_DETAIL_0018 = """
SELECT
    f.material_stock_id,
    f.company_code,
    f.plant_code,
    m.plant_name,
    f.storage_location_code,
    m.storage_location_name,
    f.material_code,
    f.material_group_code,
    m.material_group_name,
    f.unrestricted_stock,
    f.quality_inspection_stock,
    f.blocked_stock,
    f.stock_in_transit,
    (COALESCE(f.unrestricted_stock, 0)
     + COALESCE(f.quality_inspection_stock, 0)
     + COALESCE(f.blocked_stock, 0)
     + COALESCE(f.stock_in_transit, 0)) AS total_stock,
    f.production_date,
    f.shelf_life_expiration_date,
    f.source_system,
    f.import_batch_id,
    f.is_void
FROM fact_material_stock f
LEFT JOIN dim_material_location m
       ON m.material_location_id = f.material_location_id
WHERE f.is_void = 0
"""


def _table_exists(bind: sa.engine.Connection, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    bind = op.get_bind()

    # Refuse to destroy transactional data. Both tables are empty on the
    # database this was written for; a non-zero count anywhere else means the
    # positions must be exported and re-imported against the new masters, since
    # a row keyed on ``material_location_id`` cannot be repointed at three
    # dimensions that did not exist when it was written.
    for table in ("fact_material_stock", "stg_material_stock"):
        if not _table_exists(bind, table):
            continue
        rows = bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() or 0
        if rows:
            raise RuntimeError(
                f"{table} holds {rows} row(s). This migration replaces the "
                "single Material Master with three separate masters and cannot "
                "repoint an existing position: the old row references a "
                "material-location surrogate key that no longer exists, and the "
                "Material Brand the new model requires is absent from every "
                "row. Export those positions, run this migration, re-upload the "
                "Material Master, then re-import the stock file."
            )

    # --- Plant Master ----------------------------------------------------
    #
    # Keyed on Company + Plant: a plant code identifies a plant *within* a
    # company, which is why ``plant_key`` joins the two rather than the plant
    # code standing alone.
    op.create_table(
        "dim_plant",
        sa.Column("plant_id", _pk(), primary_key=True, autoincrement=True),
        sa.Column("plant_key", sa.String(160), nullable=False),
        sa.Column("company_code", sa.String(64), nullable=False),
        sa.Column("plant_code", sa.String(64), nullable=False),
        sa.Column("plant_name", sa.Text(), nullable=False),
        *_master_audit_columns(),
        sa.UniqueConstraint("plant_key", name="uq_dim_plant_key"),
    )
    op.create_index("ix_dim_plant_key", "dim_plant", ["plant_key"])
    op.create_index("ix_dim_plant_company", "dim_plant", ["company_code"])
    op.create_index("ix_dim_plant_code", "dim_plant", ["plant_code"])

    # --- Storage Location Master -----------------------------------------
    #
    # Keyed on Plant + Storage Location, and carrying no plant *name*: that is
    # an attribute of the plant, reachable through ``plant_code``, and storing
    # it twice would let a renamed plant keep its old name here.
    op.create_table(
        "dim_storage_location",
        sa.Column("storage_location_id", _pk(), primary_key=True, autoincrement=True),
        sa.Column("storage_location_key", sa.String(160), nullable=False),
        sa.Column("plant_code", sa.String(64), nullable=False),
        sa.Column("storage_location_code", sa.String(64), nullable=False),
        sa.Column("storage_location_name", sa.Text(), nullable=False),
        *_master_audit_columns(),
        sa.UniqueConstraint("storage_location_key", name="uq_dim_storage_location_key"),
    )
    op.create_index("ix_dim_storage_location_key", "dim_storage_location",
                    ["storage_location_key"])
    op.create_index("ix_dim_storage_location_plant", "dim_storage_location", ["plant_code"])
    op.create_index("ix_dim_storage_location_code", "dim_storage_location",
                    ["storage_location_code"])

    # --- Material Master --------------------------------------------------
    #
    # ``material_code`` is the identity, so it is unique and NOT NULL — unlike
    # the column 0018 added to the old table, which had to tolerate rows that
    # pre-dated it. There are no such rows here: this table starts empty.
    #
    # Group and Brand are carried as code + name pairs on the material rather
    # than as two further tables. The source states them per material and
    # states no attribute of a group or a brand beyond its own name, so a
    # separate table would hold nothing the name does not already say.
    #
    # ``sku_code`` is the bridge to ``dim_product`` and is nullable: a material
    # that no SKU corresponds to is normal, and a mapping is only ever loaded,
    # never derived.
    op.create_table(
        "dim_material",
        sa.Column("material_id", _pk(), primary_key=True, autoincrement=True),
        sa.Column("material_code", sa.String(64), nullable=False),
        sa.Column("material_description", sa.Text(), nullable=False),
        sa.Column("material_group_code", sa.String(64), nullable=False),
        sa.Column("material_group_name", sa.Text(), nullable=False),
        sa.Column("material_brand_code", sa.String(64), nullable=False),
        sa.Column("material_brand", sa.Text(), nullable=False),
        sa.Column("sku_code", sa.String(64), nullable=True),
        *_master_audit_columns(),
        sa.UniqueConstraint("material_code", name="uq_dim_material_code"),
    )
    op.create_index("ix_dim_material_code", "dim_material", ["material_code"])
    op.create_index("ix_dim_material_group", "dim_material", ["material_group_code"])
    op.create_index("ix_dim_material_brand", "dim_material", ["material_brand_code"])
    op.create_index("ix_dim_material_sku", "dim_material", ["sku_code"])

    # --- Staging and the fact, rebuilt -----------------------------------
    #
    # Dropped and recreated rather than altered: both are empty (guarded
    # above), the fact's foreign key changes from one dimension to three, and
    # SQLite cannot drop or re-target a constraint in place. The view goes
    # first because it reads the table.
    op.execute("DROP VIEW IF EXISTS vw_material_stock_detail")
    op.drop_table("fact_material_stock")
    op.drop_table("stg_material_stock")

    op.create_table(
        "stg_material_stock",
        sa.Column("staging_id", _pk(), primary_key=True, autoincrement=True),
        sa.Column("import_batch_id", _pk(),
                  sa.ForeignKey("etl_import_batches.batch_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("source_file", sa.Text(), nullable=True),
        sa.Column("source_row_number", sa.Integer(), nullable=True),
        sa.Column("source_system", sa.String(32), nullable=True),
        sa.Column("raw_data", sa.JSON(), nullable=True),
        sa.Column("loaded_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("validation_status", sa.String(16), nullable=False,
                  server_default="PENDING"),
        sa.Column("validation_error", sa.Text(), nullable=True),
        # Every value as the file supplied it — text, unparsed. Staging records
        # what arrived; the pipeline is what decides whether it is a number.
        sa.Column("company_code", sa.String(64), nullable=True),
        sa.Column("plant_code", sa.String(64), nullable=True),
        sa.Column("storage_location_code", sa.String(64), nullable=True),
        sa.Column("material_group_code", sa.String(64), nullable=True),
        sa.Column("material_brand_code", sa.String(64), nullable=True),
        sa.Column("material_code", sa.String(64), nullable=True),
        sa.Column("unrestricted_stock", sa.String(64), nullable=True),
        sa.Column("quality_inspection_stock", sa.String(64), nullable=True),
        sa.Column("blocked_stock", sa.String(64), nullable=True),
        sa.Column("stock_in_transit", sa.String(64), nullable=True),
        sa.Column("production_date", sa.String(64), nullable=True),
        sa.Column("shelf_life_expiration_date", sa.String(64), nullable=True),
        sa.Column("source_transaction_id", sa.String(128), nullable=True),
    )
    op.create_index("ix_stg_material_stock_batch", "stg_material_stock",
                    ["import_batch_id", "validation_status"])

    op.create_table(
        "fact_material_stock",
        sa.Column("material_stock_id", _pk(), primary_key=True, autoincrement=True),
        # The three masters this position resolves to. Nullable in the same
        # sense 0016's single key was: a position whose masters cannot be
        # resolved is *rejected* by the pipeline, so these are null only in the
        # window before resolution, never as a tolerance for bad data.
        sa.Column("plant_id", _pk(),
                  sa.ForeignKey("dim_plant.plant_id", ondelete="RESTRICT"), nullable=True),
        sa.Column("storage_location_id", _pk(),
                  sa.ForeignKey("dim_storage_location.storage_location_id",
                                ondelete="RESTRICT"), nullable=True),
        sa.Column("material_id", _pk(),
                  sa.ForeignKey("dim_material.material_id", ondelete="RESTRICT"),
                  nullable=True),
        # The codes as the file stated them, kept beside the resolved keys so a
        # row still says what it came from after a master record is renamed.
        sa.Column("company_code", sa.String(64), nullable=False),
        sa.Column("plant_code", sa.String(64), nullable=False),
        sa.Column("storage_location_code", sa.String(64), nullable=False),
        sa.Column("material_code", sa.String(64), nullable=False),
        sa.Column("material_group_code", sa.String(64), nullable=False),
        sa.Column("material_brand_code", sa.String(64), nullable=False),
        sa.Column("unrestricted_stock", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("quality_inspection_stock", sa.Numeric(18, 4), nullable=False,
                  server_default="0"),
        sa.Column("blocked_stock", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("stock_in_transit", sa.Numeric(18, 4), nullable=False, server_default="0"),
        # Attributes of the goods, not of the reading. Both nullable: a material
        # with no shelf life legitimately has neither, reported as "no expiry
        # date" rather than guessed.
        sa.Column("production_date", sa.Date(), nullable=True),
        sa.Column("shelf_life_expiration_date", sa.Date(), nullable=True),
        sa.Column("source_system", sa.String(32), nullable=False),
        sa.Column("source_transaction_id", sa.String(128), nullable=True),
        sa.Column("source_file", sa.Text(), nullable=True),
        sa.Column("source_row_number", sa.Integer(), nullable=True),
        sa.Column("business_key", sa.String(512), nullable=False),
        sa.Column("import_batch_id", _pk(),
                  sa.ForeignKey("etl_import_batches.batch_id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("is_void", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column("voided_by", sa.String(64), nullable=True),
        sa.Column("void_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("business_key", name="uq_fact_material_stock_business_key"),
    )
    for name, columns in (
        ("ix_fact_material_stock_plant_id", ["plant_id"]),
        ("ix_fact_material_stock_storage_id", ["storage_location_id"]),
        ("ix_fact_material_stock_material_id", ["material_id"]),
        ("ix_fact_material_stock_company", ["company_code"]),
        ("ix_fact_material_stock_plant", ["plant_code"]),
        ("ix_fact_material_stock_storage", ["storage_location_code"]),
        ("ix_fact_material_stock_material", ["material_code"]),
        ("ix_fact_material_stock_group", ["material_group_code"]),
        ("ix_fact_material_stock_brand", ["material_brand_code"]),
        ("ix_fact_material_stock_expiry", ["shelf_life_expiration_date"]),
        ("ix_fact_material_stock_batch", ["import_batch_id"]),
        ("ix_fact_material_stock_source_system", ["source_system"]),
    ):
        op.create_index(name, "fact_material_stock", columns)

    op.execute(f"CREATE VIEW vw_material_stock_detail AS {MATERIAL_STOCK_DETAIL}")

    # --- The Sales/Target bridge -----------------------------------------
    #
    # Nullable, unindexed by uniqueness, and empty on creation. See the module
    # docstring: this exists so a mapping loaded from the source system has
    # somewhere to live, and nothing derives one.
    op.add_column("dim_product", sa.Column("material_code", sa.String(64), nullable=True))
    op.create_index("ix_dim_product_material_code", "dim_product", ["material_code"])

    # --- The old master goes, last ---------------------------------------
    #
    # Only after everything that replaces it stands, and only after the two
    # guards above have proved nothing transactional still points at it.
    if _table_exists(bind, "dim_material_location"):
        remaining = bind.execute(sa.text(
            "SELECT count(*) FROM dim_material_location")).scalar() or 0
        op.drop_table("dim_material_location")
        if remaining:
            print(
                f"  NOTE: dropped dim_material_location with {remaining} row(s). "
                "They carried no Material Brand and no Material Description, "
                "which the new Material Master requires, so none was carried "
                "forward and none was invented. The rows were exported to "
                "reports/dim_material_location_pre0019.csv before this ran. "
                "Upload the Material Master to populate dim_material."
            )


def downgrade() -> None:
    """Rebuild the single Material Master and drop the three that replaced it.

    Reversible only while the new masters are empty. Once a Material Master has
    been uploaded, Material Brand and Material Description have nowhere to go in
    the old one-table shape and would be destroyed, so this refuses rather than
    discards — the same rule ``0016`` and ``0018`` apply to what they replaced.
    """
    bind = op.get_bind()

    for table in ("fact_material_stock", "stg_material_stock"):
        rows = bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() or 0
        if rows:
            raise RuntimeError(
                f"{table} holds {rows} row(s) recorded against the three-master "
                "model. Downgrading would leave them referencing dimensions "
                "that no longer exist. Export them first."
            )
    materials = bind.execute(sa.text("SELECT count(*) FROM dim_material")).scalar() or 0
    if materials:
        raise RuntimeError(
            f"dim_material holds {materials} row(s) carrying a Material Brand "
            "and a Material Description. The single-table Material Master this "
            "downgrade restores has no column for either, so both would be "
            "destroyed. Export dim_material first if the downgrade is genuinely "
            "wanted."
        )

    op.execute("DROP VIEW IF EXISTS vw_material_stock_detail")

    op.drop_index("ix_dim_product_material_code", table_name="dim_product")
    op.drop_column("dim_product", "material_code")

    op.create_table(
        "dim_material_location",
        sa.Column("material_location_id", _pk(), primary_key=True, autoincrement=True),
        sa.Column("location_key", sa.String(320), nullable=False),
        sa.Column("company_code", sa.String(64), nullable=False),
        sa.Column("plant_code", sa.String(64), nullable=False),
        sa.Column("plant_name", sa.Text(), nullable=False),
        sa.Column("storage_location_code", sa.String(64), nullable=False),
        sa.Column("storage_location_name", sa.Text(), nullable=False),
        sa.Column("material_code", sa.String(64), nullable=True),
        sa.Column("material_group_code", sa.String(64), nullable=False),
        sa.Column("material_group_name", sa.Text(), nullable=False),
        *_master_audit_columns(),
        sa.UniqueConstraint("location_key", name="uq_dim_material_location_key"),
    )
    for name, columns in (
        ("ix_dim_material_location_key", ["location_key"]),
        ("ix_dim_material_location_company", ["company_code"]),
        ("ix_dim_material_location_plant", ["plant_code"]),
        ("ix_dim_material_location_storage", ["storage_location_code"]),
        ("ix_dim_material_location_material", ["material_code"]),
        ("ix_dim_material_location_group", ["material_group_code"]),
    ):
        op.create_index(name, "dim_material_location", columns)

    op.drop_table("fact_material_stock")
    op.drop_table("stg_material_stock")
    op.drop_table("dim_material")
    op.drop_table("dim_storage_location")
    op.drop_table("dim_plant")

    op.create_table(
        "stg_material_stock",
        sa.Column("staging_id", _pk(), primary_key=True, autoincrement=True),
        sa.Column("import_batch_id", _pk(),
                  sa.ForeignKey("etl_import_batches.batch_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("source_file", sa.Text(), nullable=True),
        sa.Column("source_row_number", sa.Integer(), nullable=True),
        sa.Column("source_system", sa.String(32), nullable=True),
        sa.Column("raw_data", sa.JSON(), nullable=True),
        sa.Column("loaded_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("validation_status", sa.String(16), nullable=False,
                  server_default="PENDING"),
        sa.Column("validation_error", sa.Text(), nullable=True),
        sa.Column("company_code", sa.String(64), nullable=True),
        sa.Column("plant_code", sa.String(64), nullable=True),
        sa.Column("storage_location_code", sa.String(64), nullable=True),
        sa.Column("material_group_code", sa.String(64), nullable=True),
        sa.Column("material_code", sa.String(64), nullable=True),
        sa.Column("unrestricted_stock", sa.String(64), nullable=True),
        sa.Column("quality_inspection_stock", sa.String(64), nullable=True),
        sa.Column("blocked_stock", sa.String(64), nullable=True),
        sa.Column("stock_in_transit", sa.String(64), nullable=True),
        sa.Column("production_date", sa.String(64), nullable=True),
        sa.Column("shelf_life_expiration_date", sa.String(64), nullable=True),
        sa.Column("source_transaction_id", sa.String(128), nullable=True),
    )
    op.create_index("ix_stg_material_stock_batch", "stg_material_stock",
                    ["import_batch_id", "validation_status"])

    op.create_table(
        "fact_material_stock",
        sa.Column("material_stock_id", _pk(), primary_key=True, autoincrement=True),
        sa.Column("material_location_id", _pk(),
                  sa.ForeignKey("dim_material_location.material_location_id",
                                ondelete="RESTRICT"), nullable=True),
        sa.Column("company_code", sa.String(64), nullable=False),
        sa.Column("plant_code", sa.String(64), nullable=False),
        sa.Column("storage_location_code", sa.String(64), nullable=False),
        sa.Column("material_code", sa.String(64), nullable=True),
        sa.Column("material_group_code", sa.String(64), nullable=False),
        sa.Column("unrestricted_stock", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("quality_inspection_stock", sa.Numeric(18, 4), nullable=False,
                  server_default="0"),
        sa.Column("blocked_stock", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("stock_in_transit", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("production_date", sa.Date(), nullable=True),
        sa.Column("shelf_life_expiration_date", sa.Date(), nullable=True),
        sa.Column("source_system", sa.String(32), nullable=False),
        sa.Column("source_transaction_id", sa.String(128), nullable=True),
        sa.Column("source_file", sa.Text(), nullable=True),
        sa.Column("source_row_number", sa.Integer(), nullable=True),
        sa.Column("business_key", sa.String(512), nullable=False),
        sa.Column("import_batch_id", _pk(),
                  sa.ForeignKey("etl_import_batches.batch_id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("is_void", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column("voided_by", sa.String(64), nullable=True),
        sa.Column("void_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("business_key", name="uq_fact_material_stock_business_key"),
    )
    for name, columns in (
        ("ix_fact_material_stock_location", ["material_location_id"]),
        ("ix_fact_material_stock_company", ["company_code"]),
        ("ix_fact_material_stock_plant", ["plant_code"]),
        ("ix_fact_material_stock_storage", ["storage_location_code"]),
        ("ix_fact_material_stock_material", ["material_code"]),
        ("ix_fact_material_stock_group", ["material_group_code"]),
        ("ix_fact_material_stock_expiry", ["shelf_life_expiration_date"]),
        ("ix_fact_material_stock_batch", ["import_batch_id"]),
        ("ix_fact_material_stock_source_system", ["source_system"]),
    ):
        op.create_index(name, "fact_material_stock", columns)

    op.execute(f"CREATE VIEW vw_material_stock_detail AS {MATERIAL_STOCK_DETAIL_0018}")
