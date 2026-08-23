"""The Product architecture removed; Material becomes the one item master.

Revision ID: 0022_remove_product_architecture
Revises: 0021_stock_filter_keys
Create Date: 2026-08-22

``dim_product`` and the Material Master described the same goods twice. A sale
and a target named a SKU; a stock position named a material; no source stated a
mapping between the two identities, so the bridge columns revision 0019 added
(``dim_product.material_code``, ``dim_material.sku_code``) stayed empty and
nothing could be reported across them. This revision removes the SKU identity
outright and leaves the Material Code as the single item key on every fact.

**No Product data is migrated, and none is derived from ``dim_product``.** That
is the point of the revision, not an incidental property of it. The Material
Master is populated by its own upload and owes nothing to the master this drops.

**Where the historical facts get their Material Code.** From the transactional
source, not from the Product dimension. ``fact_sales`` and ``fact_target`` carry
only a ``product_id``, which is exactly the obsolete reference this removes — but
each fact row was loaded from a staging row that still holds what the *file*
stated, and the identifier on that staging row is a Material Code. So the
back-fill joins each fact to its own staging row on ``(source_file,
source_row_number)`` and copies the code across. On the database this was written
for that join resolves 9,227 of 9,227 sales rows and 155,040 of 155,040 targets,
and every code it yields already exists in ``dim_material``.

Reading ``dim_product.sku_code`` would have produced the same numbers here and is
still the wrong thing to do: it would make the Product table the authority for
the new mapping, which is the one outcome this change exists to prevent.

**Refusal, not partial conversion.** Four counts are taken before anything is
written — facts with no staging row, facts whose staging rows disagree on the
code, facts whose code names no material, and any pre-existing ``materials``
section grant. A non-zero count on any of them aborts the whole revision with the
facts intact. Anything the join cannot resolve is a dependency for review, in the
sense section 15 of the specification means it, and this migration is not the
place to guess at it.

**``vw_product_sales`` is dropped without a replacement.** Nothing reads it — the
agent, the pages and the reports all go through ``vw_sales_detail`` — and a
material-wise summary view nothing queries would be one more hand-written thing
to keep in step with a schema that derives everything else.

**Target volume loses its unit.** ``vw_target_detail.target_volume_unit`` was
``dim_product.pack_unit``: a target names one item, so the item's pack unit was
the planned volume's unit. The Material Master states no unit of measure, and
asserting one would invent the measurement the source never made — so a target
volume is now the number the file stated, reported unit-free exactly as a sale's
volume already is.

``dim_product``'s 427 rows are exported to ``reports/dim_product_pre0022.csv``
before this runs, and the database is backed up to ``data/dev.db.pre0022.bak``.

**SQLite.** Every column drop and rename below is a table rewrite, and SQLite
re-validates the entire schema during one — a view that names none of the
affected tables fails just as loudly as a direct reader. So every surviving view
is captured verbatim, dropped, and recreated byte-for-byte afterwards, the same
dance revision 0020 needed. The three views this revision genuinely changes are
re-authored here in full.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_remove_product_architecture"
down_revision: Union[str, None] = "0021_stock_filter_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CODE = sa.String(64)
PK = sa.BigInteger().with_variant(sa.Integer, "sqlite")

#: The section key that renames with the page it names. ``products`` served
#: "Product and SKU analytics"; the page beneath it now ranks materials, and a
#: grant naming a section the API no longer publishes is unreachable
#: configuration.
OLD_SECTION = "products"
NEW_SECTION = "materials"

#: Each fact and the staging table that holds the row it was loaded from.
_FACTS: tuple[tuple[str, str], ...] = (
    ("fact_sales", "stg_sales"),
    ("fact_target", "stg_target"),
)

#: Views this revision re-authors. Everything else is preserved verbatim.
_REAUTHORED: tuple[str, ...] = ("vw_sales_detail", "vw_target_detail")

#: Dropped outright — see the module docstring.
_DROPPED_VIEWS: tuple[str, ...] = ("vw_product_sales",)


# ---------------------------------------------------------------------------
# The two views that change
# ---------------------------------------------------------------------------

#: 0020's sales detail body with the ``dim_product`` block replaced by the
#: material one. Written out in full rather than derived from that revision: the
#: join at the bottom changes as well as the columns, so there is no single line
#: to substitute and a half-derived body would be harder to read than this.
#:
#: The material columns are the Material Master's six, in the order the master
#: states them. ``material_code`` comes off the fact rather than the dimension so
#: a line still says what the file said if the master row is later retired, which
#: is the rule the stock detail view already follows.
SALES_DETAIL = """
SELECT
    f.sales_id,
    f.source_system,
    f.import_batch_id,
    f.invoice_no,
    f.invoice_line_no,
    f.batch_code,
    f.customer_code,
    f.sales_force_code,

    d.date_id,
    d.full_date,
    d.month,
    d.month_name,
    d.quarter,
    d.year,
    d.financial_year,
    d.financial_month,
    d.financial_quarter,

    c.company_code,
    c.company_name,
    bu.bu_code,
    bu.bu_name,
    sl.sales_line_code,
    sl.sales_line_name,
    z.zone_code,
    z.zone_name,
    r.region_code,
    r.region_name,
    a.area_code,
    a.area_name,
    u.unit_code,
    u.unit_name,

    t.territory_code,
    t.territory_name,
    st.sub_territory_code,
    st.sub_territory_name,

    f.material_code,
    m.material_description,
    m.material_group_code,
    m.material_group_name,
    m.material_brand_code,
    m.material_brand,

    f.quantity,
    f.gross_sales,
    f.discount,
    f.net_sales,
    f.cost,
    f.gross_profit,
    f.volume
FROM fact_sales f
JOIN dim_date d     ON d.date_id     = f.date_id
JOIN dim_material m ON m.material_id = f.material_id

    LEFT JOIN dim_company       c  ON c.company_id        = f.company_id
    LEFT JOIN dim_business_unit bu ON bu.business_unit_id = f.business_unit_id
    LEFT JOIN dim_sales_line    sl ON sl.sales_line_id    = f.sales_line_id
    LEFT JOIN dim_zone          z  ON z.zone_id           = f.zone_id
    LEFT JOIN dim_region        r  ON r.region_id         = f.region_id
    LEFT JOIN dim_area          a  ON a.area_id           = f.area_id
    LEFT JOIN dim_unit          u  ON u.unit_id           = f.unit_id

    LEFT JOIN dim_territory     t  ON t.territory_id      = f.territory_id
    LEFT JOIN dim_sub_territory st ON st.sub_territory_id = f.sub_territory_id

WHERE f.is_void = FALSE
"""

#: 0014's target detail body, with the same substitution and one column fewer:
#: ``target_volume_unit`` is gone because the master it was read from is gone.
#:
#: The material join stays a LEFT JOIN, as it was: target rows loaded before an
#: item was mandatory carry no material at all, and they are history rather than
#: rows to drop from a report.
TARGET_DETAIL = """
SELECT
    f.target_id,
    f.source_system,
    f.import_batch_id,
    f.target_month,
    f.customer_code,
    f.sales_force_code,

    d.date_id,
    d.full_date,
    d.month,
    d.month_name,
    d.quarter,
    d.year,
    d.financial_year,
    d.financial_month,
    d.financial_quarter,

    c.company_code,
    c.company_name,
    bu.bu_code,
    bu.bu_name,
    sl.sales_line_code,
    sl.sales_line_name,
    z.zone_code,
    z.zone_name,
    r.region_code,
    r.region_name,
    a.area_code,
    a.area_name,
    u.unit_code,
    u.unit_name,

    t.territory_code,
    t.territory_name,
    st.sub_territory_code,
    st.sub_territory_name,

    f.material_code,
    m.material_description,
    m.material_group_code,
    m.material_group_name,
    m.material_brand_code,
    m.material_brand,

    f.target_amount,
    f.target_quantity,
    f.target_volume
FROM fact_target f
JOIN dim_date d ON d.date_id = f.date_id
LEFT JOIN dim_material m ON m.material_id = f.material_id

    LEFT JOIN dim_company       c  ON c.company_id        = f.company_id
    LEFT JOIN dim_business_unit bu ON bu.business_unit_id = f.business_unit_id
    LEFT JOIN dim_sales_line    sl ON sl.sales_line_id    = f.sales_line_id
    LEFT JOIN dim_zone          z  ON z.zone_id           = f.zone_id
    LEFT JOIN dim_region        r  ON r.region_id         = f.region_id
    LEFT JOIN dim_area          a  ON a.area_id           = f.area_id
    LEFT JOIN dim_unit          u  ON u.unit_id           = f.unit_id

    LEFT JOIN dim_territory     t  ON t.territory_id      = f.territory_id
    LEFT JOIN dim_sub_territory st ON st.sub_territory_id = f.sub_territory_id

WHERE f.is_void = FALSE
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _table_exists(bind: sa.engine.Connection, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def _scalar(bind: sa.engine.Connection, sql: str) -> int:
    return bind.execute(sa.text(sql)).scalar() or 0


def _surviving_views(bind: sa.engine.Connection) -> dict[str, str]:
    """SQLite only: every view that stays, as its own ``CREATE VIEW`` statement.

    All of them, for the reason revision 0020 gives: SQLite re-validates the
    whole schema during a table rewrite, so a view two joins away from the table
    being rewritten fails as readily as a direct reader. Returned in creation
    order (``rowid``) so a view built on another is recreated after it —
    ``vw_region_target_achievement`` reads ``vw_target_vs_actual``.

    The views this revision re-authors and the one it drops are excluded: those
    are rebuilt from the bodies above, not restored.
    """
    if bind.dialect.name != "sqlite":
        return {}
    rows = bind.execute(sa.text(
        "SELECT name, sql FROM sqlite_master WHERE type = 'view' "
        "AND sql IS NOT NULL ORDER BY rowid"
    )).all()
    return {
        name: sql for name, sql in rows
        if name not in _REAUTHORED and name not in _DROPPED_VIEWS
    }


def _staging_code(fact: str, staging: str) -> str:
    """The correlated sub-select that reads one fact row's Material Code.

    Matched on the provenance every fact and every staging row carries, which is
    the file it came from and the line it was on. That pair is what makes this a
    read of the *source data* rather than of the Product dimension.

    Correlated, so it runs once per fact row — 155,040 times on ``fact_target``.
    :func:`_provenance_indexes` is what keeps that a lookup rather than a scan of
    206,720 staging rows each time; without it this is a nested loop over tens of
    billions of comparisons and the migration never finishes.
    """
    return (
        f"(SELECT s.sku_code FROM {staging} s "
        f" WHERE s.source_file = {fact}.source_file "
        f"   AND s.source_row_number = {fact}.source_row_number)"
    )


#: Temporary indexes on the staging provenance pair, created for the back-fill
#: and dropped after it.
#:
#: Staging carries no index on ``(source_file, source_row_number)`` because
#: nothing in normal operation looks a staging row up that way — the ETL writes
#: staging and reads it back by batch. This revision is the one thing that ever
#: joins on the pair, so it builds what it needs and takes it away again rather
#: than leaving an index behind that nothing else will ever use.
_PROVENANCE_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_tmp_0022_stg_sales_provenance", "stg_sales"),
    ("ix_tmp_0022_stg_target_provenance", "stg_target"),
)


def _create_provenance_indexes() -> None:
    for name, table in _PROVENANCE_INDEXES:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} "
            f"ON {table} (source_file, source_row_number)"
        )


def _drop_provenance_indexes() -> None:
    for name, _table in _PROVENANCE_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")


# ---------------------------------------------------------------------------


def upgrade() -> None:
    bind = op.get_bind()

    # --- refuse before converting ------------------------------------------
    #
    # Every check runs before a single column is added, so a database this
    # cannot convert cleanly is left exactly as it was.
    #
    # The indexes come first: the guards below join fact to staging on the
    # provenance pair exactly as the back-fill does, and without them the first
    # guard is where this migration would appear to hang.
    _create_provenance_indexes()

    problems: list[str] = []

    for fact, staging in _FACTS:
        if not (_table_exists(bind, fact) and _table_exists(bind, staging)):
            continue
        total = _scalar(bind, f"SELECT count(*) FROM {fact}")
        if not total:
            continue

        code = _staging_code(fact, staging)

        unresolved = _scalar(bind, f"SELECT count(*) FROM {fact} WHERE {code} IS NULL")
        if unresolved:
            problems.append(
                f"{fact}: {unresolved} of {total} row(s) have no staging row to "
                "read a Material Code from. Those rows carry only the obsolete "
                "product reference, which this revision will not convert."
            )

        # More than one staging row for the same file and line would make the
        # sub-select above pick one arbitrarily. It has never happened, and a
        # silently arbitrary item code on a sales row is not something to find
        # out about later.
        #
        # Asked of staging alone — one grouped scan — rather than per fact row.
        # "Does any provenance pair state two different codes?" is the same
        # question and does not need the fact table to answer it.
        ambiguous = _scalar(bind, f"""
            SELECT count(*) FROM (
                SELECT source_file, source_row_number
                  FROM {staging}
                 GROUP BY source_file, source_row_number
                HAVING count(DISTINCT sku_code) > 1
            )
        """)
        if ambiguous:
            problems.append(
                f"{staging}: {ambiguous} provenance pair(s) state more than one "
                "item code, so which one a fact was loaded from cannot be "
                "decided here."
            )

        unknown = _scalar(bind, f"""
            SELECT count(*) FROM {fact}
             WHERE {code} IS NOT NULL
               AND {code} NOT IN (SELECT material_code FROM dim_material)
        """)
        if unknown:
            problems.append(
                f"{fact}: {unknown} row(s) name an item code the Material Master "
                "does not hold. Upload the missing materials first — a fact must "
                "not reference a material that does not exist."
            )

    for table in ("role_section_permissions", "user_section_permissions"):
        if not _table_exists(bind, table):
            continue
        clash = _scalar(
            bind, f"SELECT count(*) FROM {table} WHERE section_key = '{NEW_SECTION}'")
        if clash:
            problems.append(
                f"{table} already holds {clash} grant(s) on '{NEW_SECTION}'. "
                f"Renaming '{OLD_SECTION}' onto it would merge two different "
                "decisions about who may see what."
            )

    if problems:
        _drop_provenance_indexes()
        raise RuntimeError(
            "Refusing to run 0022_remove_product_architecture:\n  - "
            + "\n  - ".join(problems)
            + "\n\nNothing has been changed. Each item above is a dependency to "
              "review, not something to convert automatically."
        )

    # --- the new columns, and the back-fill from the source ----------------
    #
    # Added nullable and without constraints first: ADD COLUMN is not a table
    # rewrite on SQLite, so the views can stay up while the data is written.
    for fact, _staging in _FACTS:
        op.add_column(fact, sa.Column("material_code", CODE))
        op.add_column(fact, sa.Column("material_id", PK))

    for fact, staging in _FACTS:
        op.execute(sa.text(
            f"UPDATE {fact} SET material_code = {_staging_code(fact, staging)}"))
        op.execute(sa.text(
            f"UPDATE {fact} SET material_id = ("
            f"  SELECT m.material_id FROM dim_material m"
            f"   WHERE m.material_code = {fact}.material_code)"
        ))

    # The guard proved every row resolves; this proves the writes landed. A
    # back-fill that silently did nothing would leave a NOT NULL column about to
    # be created over NULLs.
    for fact, _staging in _FACTS:
        total = _scalar(bind, f"SELECT count(*) FROM {fact}")
        filled = _scalar(bind, f"SELECT count(*) FROM {fact} WHERE material_id IS NOT NULL")
        if total != filled:
            raise RuntimeError(
                f"{fact}: back-fill wrote {filled} of {total} material keys. "
                "Aborting with the transaction unfinished rather than leaving "
                "part of the warehouse unattached to the Material Master."
            )

    # The back-fill is done, so the indexes have served their purpose. Dropped
    # before the rewrites rather than after: a batch rewrite reflects and
    # recreates a table's indexes, and there is no reason to carry these through
    # that only to remove them on the far side.
    _drop_provenance_indexes()

    # --- the rewrites ------------------------------------------------------
    preserved = _surviving_views(bind)
    for name in preserved:
        op.execute(f"DROP VIEW IF EXISTS {name}")
    for name in (*_REAUTHORED, *_DROPPED_VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {name}")

    op.drop_index("ix_fact_sales_product_id", table_name="fact_sales")
    with op.batch_alter_table("fact_sales") as batch:
        batch.drop_column("product_id")
        batch.alter_column("material_code", existing_type=CODE, nullable=False)
        batch.alter_column("material_id", existing_type=PK, nullable=False)
        batch.create_foreign_key("fk_fact_sales_material_id", "dim_material",
                                 ["material_id"], ["material_id"],
                                 ondelete="RESTRICT")
    op.create_index("ix_fact_sales_material_id", "fact_sales", ["material_id"])
    op.create_index("ix_fact_sales_material_code", "fact_sales", ["material_code"])

    # ``fact_target`` keeps both columns nullable, for the reason the model
    # gives: targets loaded before an item was mandatory name none, and they are
    # not given an invented one to satisfy a constraint added afterwards.
    op.drop_index("ix_fact_target_product_id", table_name="fact_target")
    with op.batch_alter_table("fact_target") as batch:
        batch.drop_column("product_id")
        batch.create_foreign_key("fk_fact_target_material_id", "dim_material",
                                 ["material_id"], ["material_id"])
    op.create_index("ix_fact_target_material_id", "fact_target", ["material_id"])
    op.create_index("ix_fact_target_material_code", "fact_target", ["material_code"])

    # Staging keeps the shape of the source file, and the source file's column is
    # what changed meaning: it is read against the Material Master now, so it is
    # named for what it holds. The Target file's heading is still "SKU Code" and
    # the dataset spec still accepts it — a column name in this database is not a
    # column name in somebody's spreadsheet.
    for staging in ("stg_sales", "stg_target"):
        with op.batch_alter_table(staging) as batch:
            batch.alter_column("sku_code", new_column_name="material_code",
                               existing_type=CODE)

    # The bridge that was never populated, on the side that survives.
    op.drop_index("ix_dim_material_sku", table_name="dim_material")
    with op.batch_alter_table("dim_material") as batch:
        batch.drop_column("sku_code")

    dropped = _scalar(bind, "SELECT count(*) FROM dim_product")
    op.drop_table("dim_product")

    for statement in preserved.values():
        op.execute(statement)
    op.execute(f"CREATE VIEW vw_sales_detail AS {SALES_DETAIL}")
    op.execute(f"CREATE VIEW vw_target_detail AS {TARGET_DETAIL}")

    # --- configuration that named what is gone -----------------------------
    op.execute(sa.text(
        "DELETE FROM etl_master_source_status WHERE table_name = 'dim_product'"))

    for table in ("role_section_permissions", "user_section_permissions"):
        if _table_exists(bind, table):
            op.execute(sa.text(
                f"UPDATE {table} SET section_key = '{NEW_SECTION}' "
                f"WHERE section_key = '{OLD_SECTION}'"))

    if dropped:
        print(
            f"  NOTE: dropped dim_product with {dropped} row(s). None was "
            "carried into dim_material and none was derived from: the Material "
            "Master is loaded from its own upload. The rows were exported to "
            "reports/dim_product_pre0022.csv before this ran."
        )


def downgrade() -> None:
    """Rebuild ``dim_product`` and repoint the facts at it. Empty, and it stays empty.

    The table comes back with its columns and none of its rows — this revision
    did not move that data anywhere, so there is nowhere to move it back from,
    and inventing 427 SKUs would be exactly the migration the upgrade exists to
    refuse. Which means the facts cannot be repointed either: ``product_id`` is
    restored NULL on every row and ``fact_sales.product_id`` comes back
    *nullable*, unlike the NOT NULL column the upgrade dropped, because there is
    no product to satisfy it with.

    A downgrade is therefore only useful for restoring the schema shape. To get
    the data back, restore ``data/dev.db.pre0022.bak``.
    """
    bind = op.get_bind()

    preserved = _surviving_views(bind)
    for name in preserved:
        op.execute(f"DROP VIEW IF EXISTS {name}")
    for name in _REAUTHORED:
        op.execute(f"DROP VIEW IF EXISTS {name}")

    op.create_table(
        "dim_product",
        sa.Column("product_id", PK, primary_key=True, autoincrement=True),
        sa.Column("sku_code", CODE, nullable=False),
        sa.Column("company_code", CODE),
        sa.Column("sku_id", CODE),
        sa.Column("sku_name_en", sa.Text(), nullable=False),
        sa.Column("sku_name_bn", sa.Text()),
        sa.Column("category", sa.Text()),
        sa.Column("product_type", sa.Text()),
        sa.Column("brand", sa.Text()),
        sa.Column("producer_company", sa.Text()),
        sa.Column("db_price", sa.Numeric(18, 4)),
        sa.Column("trade_price", sa.Numeric(18, 4)),
        sa.Column("retail_price", sa.Numeric(18, 4)),
        sa.Column("pack_size", sa.Text()),
        sa.Column("pack_size_value", sa.Numeric(18, 6)),
        sa.Column("pack_unit", sa.String(16)),
        sa.Column("unit_conversation_ratio", sa.Numeric(18, 4)),
        sa.Column("retailer_unit", sa.Text()),
        sa.Column("consumer_unit", sa.Text()),
        sa.Column("status", sa.Text()),
        sa.Column("sales_type", sa.Text()),
        sa.Column("total_alt_sku", sa.Integer()),
        sa.Column("sequence_no", sa.Integer()),
        sa.Column("start_time", sa.DateTime()),
        sa.Column("material_code", CODE),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.false(),
                  nullable=False),
        sa.Column("deleted_at", sa.DateTime()),
        sa.Column("deleted_by", sa.String(64)),
        sa.UniqueConstraint("sku_code", name="uq_dim_product_sku_code"),
    )
    for name, columns in (
        ("ix_dim_product_sku_code", ["sku_code"]),
        ("ix_dim_product_material_code", ["material_code"]),
        ("ix_dim_product_company_code", ["company_code"]),
        ("ix_dim_product_pack_unit", ["pack_unit"]),
        ("ix_dim_product_sku_id", ["sku_id"]),
        ("ix_dim_product_category", ["category"]),
        ("ix_dim_product_brand", ["brand"]),
        ("ix_dim_product_status", ["status"]),
    ):
        op.create_index(name, "dim_product", columns)

    with op.batch_alter_table("dim_material") as batch:
        batch.add_column(sa.Column("sku_code", CODE))
    op.create_index("ix_dim_material_sku", "dim_material", ["sku_code"])

    for staging in ("stg_sales", "stg_target"):
        with op.batch_alter_table(staging) as batch:
            batch.alter_column("material_code", new_column_name="sku_code",
                               existing_type=CODE)

    op.drop_index("ix_fact_sales_material_id", table_name="fact_sales")
    op.drop_index("ix_fact_sales_material_code", table_name="fact_sales")
    with op.batch_alter_table("fact_sales") as batch:
        batch.drop_constraint("fk_fact_sales_material_id", type_="foreignkey")
        batch.drop_column("material_code")
        batch.drop_column("material_id")
        batch.add_column(sa.Column(
            "product_id", PK,
            sa.ForeignKey("dim_product.product_id",
                          name="fk_fact_sales_product_id")))
    op.create_index("ix_fact_sales_product_id", "fact_sales", ["product_id"])

    op.drop_index("ix_fact_target_material_id", table_name="fact_target")
    op.drop_index("ix_fact_target_material_code", table_name="fact_target")
    with op.batch_alter_table("fact_target") as batch:
        batch.drop_constraint("fk_fact_target_material_id", type_="foreignkey")
        batch.drop_column("material_code")
        batch.drop_column("material_id")
        batch.add_column(sa.Column(
            "product_id", PK,
            sa.ForeignKey("dim_product.product_id",
                          name="fk_fact_target_product_id")))
    op.create_index("ix_fact_target_product_id", "fact_target", ["product_id"])

    for statement in preserved.values():
        op.execute(statement)

    # Restore the three product-shaped views from the revisions that authored
    # them, rather than keeping a fourth copy of each here.
    twelve = _load("0012_batch_and_volume")
    op.execute(f"CREATE VIEW vw_sales_detail AS "
               f"{_without_warehouse(twelve.SALES_DETAIL)}")
    fourteen = _load("0014_target_structure")
    op.execute(f"CREATE VIEW vw_target_detail AS {fourteen._detail_view()}")
    nine = _load("0009_data_management")
    op.execute(f"CREATE VIEW vw_product_sales AS {nine.VIEWS['vw_product_sales']}")

    op.execute(sa.text(
        "INSERT INTO etl_master_source_status (table_name, status, note) "
        "VALUES ('dim_product', 'PENDING_SOURCE_DATA', "
        "'Rebuilt empty by the 0022 downgrade; no rows were restored.')"
    ))
    for table in ("role_section_permissions", "user_section_permissions"):
        if _table_exists(bind, table):
            op.execute(sa.text(
                f"UPDATE {table} SET section_key = '{OLD_SECTION}' "
                f"WHERE section_key = '{NEW_SECTION}'"))


def _without_warehouse(body: str) -> str:
    """0012's sales body minus the column 0020 removed, as 0020 removes it."""
    line = "    f.warehouse_code,\n"
    return body.replace(line, "")


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
