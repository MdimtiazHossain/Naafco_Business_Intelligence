"""Material stock: a new Material Master and stock position, replacing the old.

Revision ID: 0016_material_stock
Revises: 0015_import_jobs
Create Date: 2026-08-18

The old stock module modelled a *daily movement* per warehouse and SKU — opening,
purchases, sales, transfers, closing — and derived coverage in days by dividing
it against sales. The business does not hold stock that way. It holds a *current
position* per Plant, Storage Location and Material Group, split by what the stock
is available for: unrestricted, in quality inspection, blocked, or in transit.
Those are different questions with different keys, so this is a replacement
rather than a rename.

**Dropping ``fact_stock`` and ``stg_stock`` is safe here, and only here.** This
project does not delete data; it retires and voids. Both tables were empty — zero
rows, and no stock file had ever been uploaded through the Upload Centre — so
there is nothing to retire. The check is worth repeating before this runs on any
other database: if either table holds rows there, stop and migrate them first,
because nothing below preserves them.

**What is deliberately *not* dropped.** ``dim_product`` and ``dim_warehouse``
were the old fact's dimensions, and both remain: products are central to Sales
and Target, and warehouse is a sales dimension in its own right. Only the three
stock-exclusive views go with the tables that fed them.

**The new grain, and why it stops where it does.** Company → Plant → Storage
Location → Material Group. The Material Master carries no material code and no
material description, so a material-level position cannot be reported without
inventing an identifier. It also carries no posting date: Production Date and
Shelf Life Expiration Date describe the goods rather than the reading, so
``fact_material_stock`` is a snapshot that a re-upload updates in place, and
there is no ``date_id`` on it because there is no date to put there.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_material_stock"
down_revision: Union[str, None] = "0015_import_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The one reporting view for material stock.
#:
#: The fact keeps the four codes it was loaded with; the names live once in the
#: master and are joined for. Renaming a plant therefore corrects every stock
#: report at once, which is the same rule ``vw_target_detail`` follows.
#:
#: ``total_stock`` is computed here rather than in each caller so the dashboard,
#: the table, the export and the agent cannot drift apart on what it means. It is
#: all four categories, which is the definition the business confirmed —
#: including in-transit, which is owned but has not arrived.
MATERIAL_STOCK_DETAIL = """
SELECT
    f.material_stock_id,
    f.company_code,
    f.plant_code,
    m.plant_name,
    f.storage_location_code,
    m.storage_location_name,
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
WHERE f.is_void = FALSE
"""

OLD_STOCK_VIEWS = ("vw_stock_detail", "vw_current_stock", "vw_stock_coverage")


def _table_exists(bind, table: str) -> bool:
    return bool(bind.execute(sa.text(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=:t"
        if bind.dialect.name == "sqlite" else
        "SELECT count(*) FROM information_schema.tables WHERE table_name=:t"
    ), {"t": table}).scalar())


def upgrade() -> None:
    bind = op.get_bind()

    # Refuse to destroy data. On the database this was written for both tables
    # are empty; anywhere else, a non-zero count means this migration is not the
    # right tool and the rows need moving first.
    for table in ("fact_stock", "stg_stock"):
        if not _table_exists(bind, table):
            continue
        rows = bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar()
        if rows:
            raise RuntimeError(
                f"{table} holds {rows} row(s). This migration replaces the old "
                "stock module and does not migrate its data — the two models "
                "have different grains and no mapping between them exists. "
                "Export or archive those rows first, then re-run."
            )

    op.create_table(
        "dim_material_location",
        sa.Column("material_location_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("location_key", sa.String(320), nullable=False),
        sa.Column("company_code", sa.String(64), nullable=False),
        sa.Column("plant_code", sa.String(64), nullable=False),
        sa.Column("plant_name", sa.Text(), nullable=False),
        sa.Column("storage_location_code", sa.String(64), nullable=False),
        sa.Column("storage_location_name", sa.Text(), nullable=False),
        sa.Column("material_group_code", sa.String(64), nullable=False),
        sa.Column("material_group_name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_by", sa.String(64), nullable=True),
        sa.UniqueConstraint("location_key", name="uq_dim_material_location_key"),
    )
    op.create_index("ix_dim_material_location_key", "dim_material_location", ["location_key"])
    op.create_index("ix_dim_material_location_company", "dim_material_location", ["company_code"])
    op.create_index("ix_dim_material_location_plant", "dim_material_location", ["plant_code"])
    op.create_index("ix_dim_material_location_storage", "dim_material_location",
                    ["storage_location_code"])
    op.create_index("ix_dim_material_location_group", "dim_material_location",
                    ["material_group_code"])

    # The audit columns come from ``StagingMixin`` on the model, so they are
    # spelled out here to match it exactly — staging keeps the source shape, and
    # ``raw_data`` keeps the whole original row including columns the canonical
    # mapping does not use.
    op.create_table(
        "stg_material_stock",
        sa.Column("staging_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("import_batch_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
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
        sa.Column("unrestricted_stock", sa.String(64), nullable=True),
        sa.Column("quality_inspection_stock", sa.String(64), nullable=True),
        sa.Column("blocked_stock", sa.String(64), nullable=True),
        sa.Column("stock_in_transit", sa.String(64), nullable=True),
        sa.Column("production_date", sa.String(64), nullable=True),
        sa.Column("shelf_life_expiration_date", sa.String(64), nullable=True),
        sa.Column("source_transaction_id", sa.String(128), nullable=True),
        # No created_at: a staging row's arrival time is loaded_at, which the
        # StagingMixin already carries. A second timestamp would be the same
        # instant under a different name.
    )
    op.create_index("ix_stg_material_stock_batch", "stg_material_stock",
                    ["import_batch_id", "validation_status"])

    op.create_table(
        "fact_material_stock",
        sa.Column("material_stock_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("material_location_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                  sa.ForeignKey("dim_material_location.material_location_id",
                                ondelete="RESTRICT"), nullable=True),
        sa.Column("company_code", sa.String(64), nullable=False),
        sa.Column("plant_code", sa.String(64), nullable=False),
        sa.Column("storage_location_code", sa.String(64), nullable=False),
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
        sa.Column("import_batch_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"),
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
        ("ix_fact_material_stock_group", ["material_group_code"]),
        ("ix_fact_material_stock_expiry", ["shelf_life_expiration_date"]),
        ("ix_fact_material_stock_batch", ["import_batch_id"]),
        ("ix_fact_material_stock_source_system", ["source_system"]),
    ):
        op.create_index(name, "fact_material_stock", columns)

    op.execute("DROP VIEW IF EXISTS vw_material_stock_detail")
    op.execute(f"CREATE VIEW vw_material_stock_detail AS {MATERIAL_STOCK_DETAIL}")

    # The old module goes only once the new one stands.
    #
    # Guarded rather than dropped outright: a database created fresh from this
    # revision never had these tables, and one that has already been through the
    # downgrade/upgrade cycle no longer does — the downgrade deliberately does
    # not rebuild them. An unconditional DROP would make this migration runnable
    # exactly once.
    for view in OLD_STOCK_VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {view}")
    for table in ("fact_stock", "stg_stock"):
        if _table_exists(bind, table):
            op.drop_table(table)


def downgrade() -> None:
    """Remove the new module. The old one is **not** rebuilt.

    Recreating ``fact_stock`` would give back an empty table and three views over
    it — the shape, with none of the data, because there was none. Anyone needing
    the old module back wants revision 0015's schema, which is what downgrading
    past this point to that revision produces.
    """
    op.execute("DROP VIEW IF EXISTS vw_material_stock_detail")
    op.drop_table("fact_material_stock")
    op.drop_table("stg_material_stock")
    op.drop_table("dim_material_location")
