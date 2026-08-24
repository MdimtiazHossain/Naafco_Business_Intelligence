"""Collection, Outstanding and Warehouse removed.

Revision ID: 0020_remove_receivables_and_warehouse
Revises: 0019_material_architecture
Create Date: 2026-08-20

Three modules leave the platform together because none of them describes
anything the business sends this system.

**Collection and Outstanding.** ``fact_collection`` and ``fact_outstanding``,
their staging tables and their six views have never held a row: no receivables
extract was ever produced, and the ETL that would have loaded one was written
against a specification that no source system fulfils. Reporting on receivables
therefore stops being possible rather than becoming empty — which is the honest
outcome, and the reason the AI tools, intents and metric vocabulary go with the
tables rather than remaining able to ask a question nothing can answer.

**Warehouse.** ``dim_warehouse`` was declared ``PENDING_SOURCE_DATA`` and stayed
that way: it never received a master file and never held a row, and every sales
row carries a NULL ``warehouse_id`` and a blank ``warehouse_code``. It is not
renamed to Storage Location — the two are different things. Stock is located by
Plant and Storage Location, each its own master since revision 0019, and a sale
states no warehouse at all. There is nothing to migrate because nothing was ever
recorded.

**What this revision does not touch.** ``dim_product`` and both facts'
``product_id`` stay exactly as they are. Sales and Target resolve every product
attribute through that dimension, and the Material Master that will replace it
holds no rows yet — moving them now would strip the name, brand and category
from 9,227 sales rows and 155,040 targets. That migration is a separate revision
and waits on data, not on code.

**Refusal, not truncation.** Every drop below is guarded by a row count. On this
database all five tables are empty; anywhere else a non-zero count means the
data must be exported first, and this revision stops rather than discarding it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_remove_receivables_and_warehouse"
down_revision: Union[str, None] = "0019_material_architecture"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Tables this revision drops, in the order it drops them: facts before the
#: dimension they reference, so no foreign key is left dangling mid-migration.
_DROPPED_TABLES: tuple[str, ...] = (
    "fact_collection",
    "stg_collection",
    "fact_outstanding",
    "stg_outstanding",
    "dim_warehouse",
)

#: Views over the two facts. Dropped first — a view is only a stored statement,
#: but dropping the table beneath one leaves a definition nothing can execute.
_DROPPED_VIEWS: tuple[str, ...] = (
    "vw_collection_detail",
    "vw_daily_collection",
    "vw_monthly_collection",
    "vw_outstanding_detail",
    "vw_customer_outstanding",
    "vw_outstanding_aging",
)

#: The section keys leaving with their modules. Live grants naming them are
#: deleted in the same transaction: a grant on a section the API no longer
#: publishes is unreachable configuration, and leaving it would let
#: ``/api/admin/sections`` serve a key the interface cannot resolve.
_REMOVED_SECTIONS: tuple[str, ...] = ("collection", "outstanding")


def _table_exists(bind: sa.engine.Connection, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _count(bind: sa.engine.Connection, table: str) -> int:
    return bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() or 0


def _surviving_views(bind: sa.engine.Connection) -> dict[str, str]:
    """SQLite only: every view that stays, as its own ``CREATE VIEW`` statement.

    All of them, not merely the ones naming a sales table: SQLite re-validates
    the *entire* schema during the table rewrite, so a view two steps removed —
    ``vw_region_target_achievement`` reads ``vw_target_vs_actual``, which reads
    ``fact_sales`` — fails just as loudly as a direct reader.

    Returned in creation order (``rowid``), which is the order they must be
    recreated in so a view built on another comes after it. ``vw_sales_detail``
    is excluded: it is the one view this revision genuinely changes, and is
    rebuilt from the revision that authored it.
    """
    if bind.dialect.name != "sqlite":
        return {}
    rows = bind.execute(sa.text(
        "SELECT name, sql FROM sqlite_master WHERE type = 'view' "
        "AND sql IS NOT NULL ORDER BY rowid"
    )).all()
    return {
        name: sql for name, sql in rows
        if name != "vw_sales_detail" and name not in _DROPPED_VIEWS
    }


def _sales_detail_without_warehouse() -> str:
    """The sales detail view, minus the one column this revision removes.

    Derived from revision ``0012``, which last authored the body, rather than
    copied: a fourth transcription of it is a fourth thing to drift. The single
    line naming ``warehouse_code`` is removed and nothing else changes, which is
    exactly what this revision is doing to the table underneath.
    """
    import importlib.util
    from pathlib import Path

    name = "0012_batch_and_volume"
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"Could not load migration {name}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    body: str = module.SALES_DETAIL
    line = "    f.warehouse_code,\n"
    if line not in body:  # pragma: no cover - defensive
        raise RuntimeError(
            "0012's SALES_DETAIL no longer names warehouse_code; rebuild this "
            "view from whichever revision authored it last."
        )
    return body.replace(line, "")


def upgrade() -> None:
    bind = op.get_bind()

    # --- refuse before destroying -----------------------------------------
    #
    # A count is taken on every table before anything is dropped, so a database
    # holding receivables stops with all five intact rather than part-way
    # through with two already gone.
    holding = {
        table: _count(bind, table)
        for table in _DROPPED_TABLES
        if _table_exists(bind, table) and _count(bind, table)
    }
    if holding:
        detail = ", ".join(f"{table} ({rows} rows)" for table, rows in holding.items())
        raise RuntimeError(
            f"Refusing to run: {detail} still hold data. This revision removes "
            "the Collection, Outstanding and Warehouse modules outright, and "
            "nothing in the remaining schema can hold what those rows say. "
            "Export them first, then re-run."
        )
    for column in ("warehouse_id", "warehouse_code"):
        if not _table_exists(bind, "fact_sales"):
            break
        # ``warehouse_id`` is a surrogate key and ``warehouse_code`` a string, so
        # only the second can be blank rather than NULL. Testing a bigint against
        # '' is what SQLite's dynamic typing quietly accepts and PostgreSQL
        # rejects outright — and it never excluded a row on either, because
        # SQLite orders every integer before every string.
        blank = f" AND {column} <> ''" if column.endswith("_code") else ""
        stamped = bind.execute(sa.text(
            f"SELECT count(*) FROM fact_sales WHERE {column} IS NOT NULL{blank}"
        )).scalar() or 0
        if stamped:
            raise RuntimeError(
                f"Refusing to run: {stamped} sales row(s) carry a "
                f"{column}. Dropping the column would discard the only record "
                "of where those goods were shipped from. Export the column "
                "first, then re-run."
            )

    # --- the two facts and their views ------------------------------------
    for view in _DROPPED_VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {view}")

    # The sales view reads ``fact_sales.warehouse_code``, so it goes before the
    # column and is rebuilt without it below.
    op.execute("DROP VIEW IF EXISTS vw_sales_detail")

    for table in ("fact_collection", "stg_collection",
                  "fact_outstanding", "stg_outstanding"):
        if _table_exists(bind, table):
            op.drop_table(table)

    # --- the warehouse columns, then the dimension ------------------------
    #
    # Order matters: the foreign key lives on the fact, so it is removed before
    # the table it points at.
    #
    # SQLite drops a column by rewriting the whole table, and it re-validates
    # *every* stored view against the schema while the rewrite is half-done — so
    # a view that knows nothing about warehouses still fails the rename. They are
    # therefore captured verbatim, dropped, and recreated byte-for-byte
    # afterwards. Nothing is re-authored here except the sales detail view, which
    # is the one that genuinely changes. Other dialects drop a column in place
    # and need none of this.
    preserved = _surviving_views(bind)
    for name in preserved:
        op.execute(f"DROP VIEW IF EXISTS {name}")

    with op.batch_alter_table("fact_sales") as batch:
        batch.drop_column("warehouse_id")
        batch.drop_column("warehouse_code")
    with op.batch_alter_table("stg_sales") as batch:
        batch.drop_column("warehouse_code")

    if _table_exists(bind, "dim_warehouse"):
        op.drop_table("dim_warehouse")

    for statement in preserved.values():
        op.execute(statement)
    op.execute(f"CREATE VIEW vw_sales_detail AS {_sales_detail_without_warehouse()}")

    # --- configuration that named what is gone ----------------------------
    #
    # The status row first: ``dim_warehouse`` can no longer be PENDING, because
    # there is no dimension left to be pending for.
    op.execute(sa.text(
        "DELETE FROM etl_master_source_status WHERE table_name = 'dim_warehouse'"
    ))

    for table in ("role_section_permissions", "user_section_permissions"):
        if _table_exists(bind, table):
            op.execute(sa.text(
                f"DELETE FROM {table} WHERE section_key IN ('collection', 'outstanding')"
            ))

    # A marker design assigned to the warehouse entity type is *deactivated*,
    # not deleted. Deactivation is the map module's own reversible state, and
    # the design carries version history that says who drew it — history that
    # outlives the entity it was drawn for.
    if _table_exists(bind, "map_marker_designs"):
        op.execute(sa.text(
            "UPDATE map_marker_designs SET status = 'INACTIVE' "
            "WHERE entity_type = 'warehouse' AND status = 'ACTIVE'"
        ))
    if _table_exists(bind, "map_entity_locations"):
        placed = bind.execute(sa.text(
            "SELECT count(*) FROM map_entity_locations WHERE entity_type = 'warehouse'"
        )).scalar() or 0
        if placed:
            print(
                f"  NOTE: {placed} map placement(s) name the warehouse entity "
                "type. They are left in place — a coordinate somebody recorded "
                "is not this migration's to discard — but nothing draws them."
            )


def downgrade() -> None:
    """Rebuild the schema this revision removed. The data is not rebuilt.

    Every table below comes back empty, which is exactly what it was: these
    modules never held a row, so an empty rebuild loses nothing and restores the
    shape revision 0019 left. The section grants deleted above are *not*
    restored — a grant is a decision somebody made about one user, and inventing
    one back would be worse than leaving the section on its declared default.
    """
    bind = op.get_bind()

    code = sa.String(64)
    money = sa.Numeric(18, 4)
    pk = sa.BigInteger().with_variant(sa.Integer, "sqlite")

    def org_columns() -> list[sa.Column]:
        return [
            sa.Column("company_id", pk, sa.ForeignKey("dim_company.company_id")),
            sa.Column("business_unit_id", pk,
                      sa.ForeignKey("dim_business_unit.business_unit_id")),
            sa.Column("sales_line_id", pk,
                      sa.ForeignKey("dim_sales_line.sales_line_id")),
            sa.Column("zone_id", pk, sa.ForeignKey("dim_zone.zone_id")),
            sa.Column("region_id", pk, sa.ForeignKey("dim_region.region_id")),
            sa.Column("area_id", pk, sa.ForeignKey("dim_area.area_id")),
            sa.Column("unit_id", pk, sa.ForeignKey("dim_unit.unit_id")),
            sa.Column("territory_id", pk, sa.ForeignKey("dim_territory.territory_id")),
            sa.Column("sub_territory_id", pk,
                      sa.ForeignKey("dim_sub_territory.sub_territory_id")),
        ]

    def audit_columns() -> list[sa.Column]:
        return [
            sa.Column("source_system", sa.String(32), nullable=False),
            sa.Column("source_transaction_id", sa.String(128)),
            sa.Column("source_file", sa.Text()),
            sa.Column("source_row_number", sa.Integer()),
            sa.Column("business_key", sa.String(512), nullable=False),
            sa.Column("import_batch_id", pk,
                      sa.ForeignKey("etl_import_batches.batch_id", ondelete="RESTRICT"),
                      nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(),
                      nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(),
                      nullable=False),
            sa.Column("is_void", sa.Boolean(), server_default=sa.false(),
                      nullable=False),
            sa.Column("voided_at", sa.DateTime()),
            sa.Column("voided_by", sa.String(64)),
            sa.Column("void_reason", sa.Text()),
        ]

    def staging_columns() -> list[sa.Column]:
        return [
            sa.Column("staging_id", pk, primary_key=True, autoincrement=True),
            sa.Column("import_batch_id", pk,
                      sa.ForeignKey("etl_import_batches.batch_id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("source_file", sa.Text()),
            sa.Column("source_row_number", sa.Integer()),
            sa.Column("source_system", sa.String(32)),
            sa.Column("raw_data", sa.JSON()),
            sa.Column("loaded_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
            sa.Column("validation_status", sa.String(16), nullable=False,
                      server_default="PENDING"),
            sa.Column("validation_error", sa.Text()),
            *[sa.Column(name, code) for name in
              ("company_code", "bu_code", "sales_line_code", "zone_code",
               "region_code", "area_code", "unit_code", "territory_code",
               "sub_territory_code")],
        ]

    op.create_table(
        "dim_warehouse",
        sa.Column("warehouse_id", pk, primary_key=True, autoincrement=True),
        sa.Column("warehouse_code", code, nullable=False),
        sa.Column("warehouse_name", sa.Text()),
        sa.Column("warehouse_type", sa.Text()),
        sa.Column("location", sa.Text()),
        sa.Column("status", sa.Text()),
        sa.Column("is_placeholder", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.false(),
                  nullable=False),
        sa.Column("deleted_at", sa.DateTime()),
        sa.Column("deleted_by", sa.String(64)),
        sa.UniqueConstraint("warehouse_code", name="uq_dim_warehouse_code"),
    )
    op.create_index("ix_dim_warehouse_warehouse_code", "dim_warehouse",
                    ["warehouse_code"])

    op.create_table(
        "stg_collection",
        *staging_columns(),
        sa.Column("transaction_date", sa.String(64)),
        sa.Column("collection_id", sa.String(128)),
        sa.Column("invoice_no", sa.String(128)),
        sa.Column("customer_code", code),
        sa.Column("sales_force_code", code),
        sa.Column("collection_amount", sa.String(64)),
        sa.Column("payment_method", sa.String(64)),
        sa.Column("source_transaction_id", sa.String(128)),
    )
    op.create_index("ix_stg_collection_batch", "stg_collection",
                    ["import_batch_id", "validation_status"])

    op.create_table(
        "stg_outstanding",
        *staging_columns(),
        sa.Column("as_on_date", sa.String(64)),
        sa.Column("invoice_no", sa.String(128)),
        sa.Column("invoice_date", sa.String(64)),
        sa.Column("due_date", sa.String(64)),
        sa.Column("customer_code", code),
        sa.Column("sales_force_code", code),
        sa.Column("invoice_amount", sa.String(64)),
        sa.Column("paid_amount", sa.String(64)),
        sa.Column("outstanding_amount", sa.String(64)),
        sa.Column("source_transaction_id", sa.String(128)),
    )
    op.create_index("ix_stg_outstanding_batch", "stg_outstanding",
                    ["import_batch_id", "validation_status"])

    op.create_table(
        "fact_collection",
        sa.Column("collection_pk", pk, primary_key=True, autoincrement=True),
        sa.Column("date_id", sa.Integer(),
                  sa.ForeignKey("dim_date.date_id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("customer_id", pk, sa.ForeignKey("dim_customer.customer_id")),
        sa.Column("sales_force_id", pk,
                  sa.ForeignKey("dim_sales_force.sales_force_id")),
        sa.Column("customer_code", code),
        sa.Column("sales_force_code", code),
        sa.Column("collection_id", sa.String(128)),
        sa.Column("invoice_no", sa.String(128)),
        sa.Column("collection_amount", money, nullable=False, server_default="0"),
        sa.Column("payment_method", sa.String(64)),
        *org_columns(),
        *audit_columns(),
        sa.UniqueConstraint("business_key", name="uq_fact_collection_business_key"),
    )
    for name, columns in (
        ("ix_fact_collection_date_id", ["date_id"]),
        ("ix_fact_collection_region_id", ["region_id"]),
        ("ix_fact_collection_batch", ["import_batch_id"]),
        ("ix_fact_collection_source_system", ["source_system"]),
        ("ix_fact_collection_invoice_no", ["invoice_no"]),
    ):
        op.create_index(name, "fact_collection", columns)

    op.create_table(
        "fact_outstanding",
        sa.Column("outstanding_id", pk, primary_key=True, autoincrement=True),
        sa.Column("date_id", sa.Integer(),
                  sa.ForeignKey("dim_date.date_id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("customer_id", pk, sa.ForeignKey("dim_customer.customer_id")),
        sa.Column("sales_force_id", pk,
                  sa.ForeignKey("dim_sales_force.sales_force_id")),
        sa.Column("customer_code", code),
        sa.Column("sales_force_code", code),
        sa.Column("invoice_no", sa.String(128)),
        sa.Column("invoice_date", sa.Date()),
        sa.Column("due_date", sa.Date()),
        sa.Column("invoice_amount", money, nullable=False, server_default="0"),
        sa.Column("paid_amount", money, nullable=False, server_default="0"),
        sa.Column("outstanding_amount", money, nullable=False, server_default="0"),
        sa.Column("days_overdue", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("aging_bucket", sa.String(16), nullable=False,
                  server_default="CURRENT"),
        *org_columns(),
        *audit_columns(),
        sa.UniqueConstraint("business_key", name="uq_fact_outstanding_business_key"),
    )
    for name, columns in (
        ("ix_fact_outstanding_date_id", ["date_id"]),
        ("ix_fact_outstanding_region_id", ["region_id"]),
        ("ix_fact_outstanding_aging_bucket", ["aging_bucket"]),
        ("ix_fact_outstanding_batch", ["import_batch_id"]),
        ("ix_fact_outstanding_source_system", ["source_system"]),
    ):
        op.create_index(name, "fact_outstanding", columns)

    # The warehouse columns come back on the sales tables, nullable as they
    # were. Same view dance as the upgrade, and for the same SQLite reason; the
    # foreign key is named because a batch rewrite cannot re-attach an anonymous
    # constraint it has no handle on.
    preserved = _surviving_views(bind)
    for name in preserved:
        op.execute(f"DROP VIEW IF EXISTS {name}")
    op.execute("DROP VIEW IF EXISTS vw_sales_detail")

    with op.batch_alter_table("fact_sales") as batch:
        batch.add_column(sa.Column(
            "warehouse_id", pk,
            sa.ForeignKey("dim_warehouse.warehouse_id",
                          name="fk_fact_sales_warehouse_id")))
        batch.add_column(sa.Column("warehouse_code", code))
    with op.batch_alter_table("stg_sales") as batch:
        batch.add_column(sa.Column("warehouse_code", code))

    for statement in preserved.values():
        op.execute(statement)

    op.execute(sa.text(
        "INSERT INTO etl_master_source_status "
        "(table_name, status, source_description, note) "
        "VALUES ('dim_warehouse', 'PENDING_SOURCE_DATA', NULL, "
        "'No Warehouse Master sheet exists yet.')"
    ))

    # Restore the dropped view bodies by re-running the SQL of the revision that
    # authored them, rather than keeping another copy here. ``0012`` re-authored
    # the sales view after ``0009``, so its body is the one that wins.
    nine = _load("0009_data_management")
    for name in _DROPPED_VIEWS:
        body = nine.VIEWS.get(name)
        if body is None:  # pragma: no cover - defensive
            continue
        op.execute(f"CREATE VIEW {name} AS {body}")
    twelve = _load("0012_batch_and_volume")
    op.execute(f"CREATE VIEW vw_sales_detail AS {twelve.SALES_DETAIL}")

    if not _table_exists(bind, "map_marker_designs"):
        return
    op.execute(sa.text(
        "UPDATE map_marker_designs SET status = 'ACTIVE' "
        "WHERE entity_type = 'warehouse' AND status = 'INACTIVE'"
    ))


def _load(module_name: str):
    """Load another revision file by path.

    Alembic runs version files as standalone modules rather than as a package,
    and a module name beginning with a digit cannot be written as an ``import``
    statement anyway.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).with_name(f"{module_name}.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"Could not load migration {module_name}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
