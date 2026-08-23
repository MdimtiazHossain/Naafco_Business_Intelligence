"""Target restructured: month + financial year, territory grain, customer code.

Revision ID: 0014_target_structure
Revises: 0013_target_measures
Create Date: 2026-08-15

The Target table becomes a clean transactional fact of ten columns — Target
Month, Financial Year, Territory Code, Sub Territory Code, Customer Code, SKU
Code, Sales Force Code, Target Quantity, Target Volume, Target Amount — and
nothing else. Every master attribute is reached through the code that references
it, so a renamed customer or a re-pointed SKU changes every report at once
instead of leaving old targets naming the old value.

**What goes.** ``target_period`` and ``target_type`` are replaced by
``target_month`` and ``financial_year``: a target is set for a month of a
financial year, and stating that directly is what lets the upload validate the
pair against the configured calendar instead of accepting any label an operator
types. ``target_volume_unit`` goes because a target now always names a SKU, and
the SKU's ``pack_unit`` in the Product Master *is* the unit — reading it through
the relationship is the same rule the rest of this change follows, and
``vw_target_detail`` exposes it under the old name so the unit-safety machinery
in ``ai.queries`` is untouched.

**What arrives.** ``customer_id`` / ``customer_code``, so a target can be set for
a customer and be checked against the Customer Master's own record of where that
customer sits. The code is kept beside the key for the same reason
``fact_sales`` keeps it: ``dim_customer`` is PENDING_SOURCE_DATA, so an unknown
code is a deferred mapping rather than a rejection.

**Existing rows are kept, not rewritten.** ``target_month`` and
``financial_year`` are back-filled from each row's own ``dim_date`` entry —
derived from the date the row already carries, never guessed — and the columns
that go are dropped only after that. No target row is deleted, and no row is
given an invented customer or SKU to satisfy the new structure, which is why
``product_id`` stays nullable in the schema while the ETL requires it.

**Business keys are re-derived where that is unambiguous.** The key's field list
changed, so a re-upload of the same target would otherwise insert a second row
instead of updating the first. Each existing row's key is recomputed from the
columns it already has, and rewritten **only where the new key is unique** —
two old rows that differed solely by a column this revision drops (a MONTHLY and
a QUARTERLY target for the same month and territory, say) cannot be told apart
under the new grain, so both keep their original keys and stay visible as the
historical rows they are.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_target_structure"
down_revision: Union[str, None] = "0013_target_measures"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CODE = sa.String(length=64)
FK_TYPE = sa.BigInteger().with_variant(sa.Integer, "sqlite")
MONTH = sa.String(length=16)
YEAR_LABEL = sa.String(length=32)
STAGING_TEXT = sa.String(length=64)

#: Dropped from the fact table, with the index each one carries.
_REMOVED_FACT_COLUMNS: tuple[tuple[str, str | None], ...] = (
    ("target_period", "ix_fact_target_period"),
    ("target_type", None),
    ("target_volume_unit", "ix_fact_target_volume_unit"),
)

#: Dropped from staging. ``target_date`` goes because the file never carried a
#: date: the month and the financial year are what arrive, and the date they
#: resolve to is derived during validation.
_REMOVED_STAGING_COLUMNS: tuple[str, ...] = (
    "target_date", "target_period", "target_type", "target_volume_unit",
)

#: Dependency order: the achievement view reads the comparison view, so it is
#: dropped first and rebuilt last.
_DEPENDENT_VIEWS: tuple[str, ...] = ("vw_region_target_achievement",
                                     "vw_target_vs_actual")


def _detail_view() -> str:
    """``vw_target_detail`` for the new structure.

    Built from 0009's shared fragments, which is where the void clause was
    authored — a target view that silently counted voided rows again would undo
    it, so the clause is carried explicitly below for the same reason.

    ``financial_year`` is taken from ``dim_date`` rather than from the fact row.
    The two agree by construction (the upload rejects a row whose month and year
    disagree), and exposing one of them keeps the view's column set the same
    shape as every other detail view.
    """
    sibling = _load("0009_data_management")
    return f"""
SELECT
    f.target_id,
    f.source_system,
    f.import_batch_id,
    f.target_month,
    f.customer_code,
    f.sales_force_code,
{sibling._DATE_COLUMNS},
{sibling._ORG_COLUMNS},
{sibling._TERRITORY_COLUMNS},
    p.sku_code,
    p.sku_name_en,
    p.category,
    p.brand,
    f.target_amount,
    f.target_quantity,
    f.target_volume,
    p.pack_unit AS target_volume_unit
FROM fact_target f
JOIN dim_date d ON d.date_id = f.date_id
LEFT JOIN dim_product p ON p.product_id = f.product_id
{sibling._ORG_JOIN}
{sibling._TERRITORY_JOIN}
WHERE {sibling._NOT_VOID}
"""


#: ``vw_target_vs_actual`` with the month label in place of period and type.
#:
#: The grain is unchanged — financial year, calendar month, source system and
#: region — because that is the grain the business compares achievement at.
#: ``target_month`` is one-to-one with year and month, so grouping by it adds no
#: rows; it is carried so a report can name the period without reconstructing it.
TARGET_VS_ACTUAL = """
WITH target_month AS (
    SELECT
        d.financial_year,
        d.year,
        d.month,
        f.source_system,
        f.region_id,
        f.target_month,
        SUM(f.target_amount)   AS target_amount,
        SUM(f.target_quantity) AS target_quantity
    FROM fact_target f
    JOIN dim_date d ON d.date_id = f.date_id
    WHERE f.is_void = FALSE
    GROUP BY d.financial_year, d.year, d.month, f.source_system, f.region_id,
             f.target_month
),
actual_month AS (
    SELECT
        d.financial_year,
        d.year,
        d.month,
        f.source_system,
        f.region_id,
        SUM(f.net_sales) AS actual_sales,
        SUM(f.quantity)  AS actual_quantity
    FROM fact_sales f
    JOIN dim_date d ON d.date_id = f.date_id
    WHERE f.is_void = FALSE
    GROUP BY d.financial_year, d.year, d.month, f.source_system, f.region_id
),
keys AS (
    SELECT financial_year, year, month, source_system, region_id FROM target_month
    UNION
    SELECT financial_year, year, month, source_system, region_id FROM actual_month
)
SELECT
    k.financial_year,
    k.year,
    k.month,
    k.source_system,
    r.region_code,
    r.region_name,
    t.target_month,
    COALESCE(t.target_amount, 0)  AS target_amount,
    t.target_quantity             AS target_quantity,
    COALESCE(a.actual_sales, 0)   AS actual_sales,
    COALESCE(a.actual_quantity, 0) AS actual_quantity,
    COALESCE(a.actual_sales, 0) * 100.0 / NULLIF(t.target_amount, 0) AS achievement_percent,
    COALESCE(t.target_amount, 0) - COALESCE(a.actual_sales, 0)       AS gap,
    COALESCE(a.actual_quantity, 0) * 100.0
        / NULLIF(t.target_quantity, 0) AS quantity_achievement_percent
FROM keys k
LEFT JOIN target_month t
       ON t.financial_year = k.financial_year AND t.year = k.year AND t.month = k.month
      AND t.source_system = k.source_system
      AND (t.region_id = k.region_id OR (t.region_id IS NULL AND k.region_id IS NULL))
LEFT JOIN actual_month a
       ON a.financial_year = k.financial_year AND a.year = k.year AND a.month = k.month
      AND a.source_system = k.source_system
      AND (a.region_id = k.region_id OR (a.region_id IS NULL AND k.region_id IS NULL))
LEFT JOIN dim_region r ON r.region_id = k.region_id
"""

#: Unchanged from 0013 in substance; repeated because it must be dropped and
#: rebuilt around the view it reads.
REGION_ACHIEVEMENT = """
SELECT
    financial_year,
    source_system,
    region_code,
    region_name,
    SUM(target_amount)   AS target_amount,
    SUM(target_quantity) AS target_quantity,
    SUM(actual_sales)    AS actual_sales,
    SUM(actual_quantity) AS actual_quantity,
    SUM(actual_sales) * 100.0 / NULLIF(SUM(target_amount), 0) AS achievement_percent,
    SUM(target_amount) - SUM(actual_sales) AS gap
FROM vw_target_vs_actual
GROUP BY financial_year, source_system, region_code, region_name
"""

#: ``date_id`` is ``YYYYMMDD`` as an integer, so the month label is the first
#: six digits of its text form. Written this way because it is the one spelling
#: of the derivation that behaves identically on PostgreSQL and on the SQLite
#: the tests run against.
_MONTH_FROM_DATE_ID = (
    "substr(CAST(d.date_id AS VARCHAR), 1, 4) || '-' || "
    "substr(CAST(d.date_id AS VARCHAR), 5, 2)"
)


def upgrade() -> None:
    # Views first, in dependency order: SQLite validates every view against the
    # schema when a column is dropped, so one still naming target_period would
    # block the drop.
    _drop_views()

    op.add_column("stg_target", sa.Column("target_month", sa.String(length=32), nullable=True))
    op.add_column("stg_target",
                  sa.Column("financial_year", sa.String(length=32), nullable=True))
    op.add_column("stg_target", sa.Column("customer_code", CODE, nullable=True))

    op.add_column("fact_target", sa.Column("target_month", MONTH, nullable=True))
    op.add_column("fact_target", sa.Column("financial_year", YEAR_LABEL, nullable=True))
    op.add_column("fact_target", sa.Column("customer_code", CODE, nullable=True))

    # ``customer_id`` arrives inside a batch block because it carries a foreign
    # key and SQLite cannot ALTER a table to add a constraint. Batch mode
    # recreates the table there and emits an ordinary ALTER on PostgreSQL, so
    # both dialects end up with the same schema — which is what the
    # schema-versus-models test checks, and what stops the constraint from
    # existing only in production.
    with op.batch_alter_table("fact_target") as batch_op:
        batch_op.add_column(sa.Column("customer_id", FK_TYPE, nullable=True))
        batch_op.create_foreign_key(
            "fk_fact_target_customer_id", "dim_customer",
            ["customer_id"], ["customer_id"], ondelete="RESTRICT",
        )

    # Back-fill from each row's own date. Derived, not guessed: the row already
    # states which day it is anchored to, and dim_date already states which
    # month and financial year that day belongs to.
    op.execute(f"""
        UPDATE fact_target
        SET target_month = (
                SELECT {_MONTH_FROM_DATE_ID}
                FROM dim_date d WHERE d.date_id = fact_target.date_id
            ),
            financial_year = (
                SELECT d.financial_year
                FROM dim_date d WHERE d.date_id = fact_target.date_id
            )
    """)

    _rekey_existing_targets()

    op.create_index("ix_fact_target_month", "fact_target", ["target_month"])
    op.create_index("ix_fact_target_financial_year", "fact_target", ["financial_year"])
    op.create_index("ix_fact_target_customer_id", "fact_target", ["customer_id"])
    op.create_index("ix_fact_target_territory_id", "fact_target", ["territory_id"])

    for column, index in _REMOVED_FACT_COLUMNS:
        if index:
            op.drop_index(index, table_name="fact_target")
        op.drop_column("fact_target", column)
    for column in _REMOVED_STAGING_COLUMNS:
        op.drop_column("stg_target", column)

    _create_views()


def _rekey_existing_targets() -> None:
    """Re-derive the business key of rows loaded under the old field list.

    The key names the fields it was built from, so an old key and a new one can
    never collide — which is exactly why a re-upload of the same target would
    insert a second row rather than update the first. Recomputing closes that,
    but only where the answer is unambiguous.

    The key is spelled out here rather than imported from ``etl.datasets``
    deliberately. A migration records what the key was at the moment it ran; if
    the spec changes again, that change belongs in its own revision and must not
    silently rewrite what this one wrote.
    """
    connection = op.get_bind()
    rows = connection.execute(sa.text("""
        SELECT f.target_id,
               f.financial_year,
               f.target_month,
               t.territory_code,
               st.sub_territory_code,
               f.customer_code,
               p.sku_code,
               f.sales_force_code,
               f.source_system
        FROM fact_target f
        LEFT JOIN dim_territory     t  ON t.territory_id      = f.territory_id
        LEFT JOIN dim_sub_territory st ON st.sub_territory_id = f.sub_territory_id
        LEFT JOIN dim_product       p  ON p.product_id        = f.product_id
    """)).all()
    if not rows:
        return

    prefix = ("financial_year:target_month:territory_code:sub_territory_code:"
              "customer_code:sku_code:sales_force_code")
    keys: dict[int, str] = {}
    counts: dict[str, int] = {}
    for row in rows:
        values = tuple(row)
        parts = [prefix] + [
            "" if value is None else str(value) for value in values[1:8]
        ] + [values[8] or ""]
        key = "|".join(parts)
        keys[values[0]] = key
        counts[key] = counts.get(key, 0) + 1

    updatable = [
        {"pk": target_id, "key": key}
        for target_id, key in keys.items() if counts[key] == 1
    ]
    if updatable:
        connection.execute(
            sa.text("UPDATE fact_target SET business_key = :key WHERE target_id = :pk"),
            updatable,
        )


def _drop_views() -> None:
    for name in _DEPENDENT_VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {name}")
    op.execute("DROP VIEW IF EXISTS vw_target_detail")


def _create_views() -> None:
    op.execute(f"CREATE VIEW vw_target_detail AS {_detail_view()}")
    op.execute(f"CREATE VIEW vw_target_vs_actual AS {TARGET_VS_ACTUAL}")
    op.execute(f"CREATE VIEW vw_region_target_achievement AS {REGION_ACHIEVEMENT}")


def downgrade() -> None:
    _drop_views()

    for index in ("ix_fact_target_month", "ix_fact_target_financial_year",
                  "ix_fact_target_customer_id", "ix_fact_target_territory_id"):
        op.drop_index(index, table_name="fact_target")

    # The columns come back empty. Their values were never derivable from what
    # remains — a target's type was a label the file supplied and nothing else
    # records it — so restoring the shape is all a downgrade can honestly do.
    op.add_column("fact_target", sa.Column("target_period", sa.String(length=32), nullable=True))
    op.add_column("fact_target", sa.Column("target_type", sa.String(length=32), nullable=True))
    op.add_column("fact_target", sa.Column("target_volume_unit", CODE, nullable=True))
    op.create_index("ix_fact_target_period", "fact_target", ["target_period"])
    op.create_index("ix_fact_target_volume_unit", "fact_target", ["target_volume_unit"])

    op.add_column("stg_target", sa.Column("target_date", STAGING_TEXT, nullable=True))
    op.add_column("stg_target", sa.Column("target_period", sa.String(length=32), nullable=True))
    op.add_column("stg_target", sa.Column("target_type", sa.String(length=32), nullable=True))
    op.add_column("stg_target", sa.Column("target_volume_unit", CODE, nullable=True))

    with op.batch_alter_table("fact_target") as batch_op:
        batch_op.drop_constraint("fk_fact_target_customer_id", type_="foreignkey")
        batch_op.drop_column("customer_id")
    for column in ("customer_code", "financial_year", "target_month"):
        op.drop_column("fact_target", column)
    for column in ("customer_code", "financial_year", "target_month"):
        op.drop_column("stg_target", column)

    # Each body from the revision that last authored it, rather than a second
    # copy kept here: 0013 wrote the three views this revision replaced.
    previous = _load("0013_target_quantity_and_volume")
    op.execute(f"CREATE VIEW vw_target_detail AS {previous._detail_view()}")
    op.execute(f"CREATE VIEW vw_target_vs_actual AS {previous.TARGET_VS_ACTUAL}")
    op.execute("CREATE VIEW vw_region_target_achievement AS "
               f"{previous.REGION_ACHIEVEMENT}")


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
