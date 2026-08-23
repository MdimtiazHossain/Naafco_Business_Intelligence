"""Target quantity and target volume.

Revision ID: 0013_target_measures
Revises: 0012_batch_volume
Create Date: 2026-08-15

A target used to carry one measure, ``target_amount``, because that is all the
Phase 2 specification listed. Reports now state quantity, volume and net sales
for actuals, so a target-versus-actual table could compare only one of the
three. This adds the other two to the target line.

**Additive and nullable.** Every existing target row has an amount and no
quantity or volume, and it stays valid: ``target_amount`` remains the required
measure and the two new columns are nullable. A file that sets only an amount
loads exactly as it did, which is what keeps the change safe to deploy against
live data.

**Why the columns keep the ``target_`` prefix.** A target volume is not a
shipped volume: it is a plan, with no invoice line behind it and no pack size to
cross-check it against, so it must not travel under the same name as
``fact_sales.volume`` and be swept into the sales volume machinery. The
unit-safety rule still applies to it — ``queries.volume_by_unit`` takes the
column names to group by, so the target view gets the same "never add a kilogram
to a litre" guarantee without pretending to be a sales line.

**Volume is not added to ``vw_target_vs_actual``.** That view aggregates to one
row per month and region, and a volume summed across units at that grain is the
number this system exists to refuse. Target quantity is added there — quantities
are unitless counts and do sum — and volume stays on the detail view where it
can be reported per unit.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_target_measures"
down_revision: Union[str, None] = "0012_batch_volume"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CODE = sa.String(length=64)
STAGING_TEXT = sa.String(length=64)
MEASURE = sa.Numeric(18, 4)


def _detail_view() -> str:
    """``vw_target_detail`` with the three new measures.

    Built from revision 0009's body, not 0004's: 0009 is where the void clause
    was added, and a target view that silently counts voided rows again would
    undo it. The clause is carried explicitly below for the same reason.
    """
    sibling = _load("0009_data_management")
    return f"""
SELECT
    f.target_id,
    f.source_system,
    f.import_batch_id,
    f.target_period,
    f.target_type,
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
    f.target_volume_unit
FROM fact_target f
JOIN dim_date d ON d.date_id = f.date_id
LEFT JOIN dim_product p ON p.product_id = f.product_id
{sibling._ORG_JOIN}
{sibling._TERRITORY_JOIN}
WHERE {sibling._NOT_VOID}
"""


#: ``vw_target_vs_actual`` with target quantity beside actual quantity.
TARGET_VS_ACTUAL = """
WITH target_month AS (
    SELECT
        d.financial_year,
        d.year,
        d.month,
        f.source_system,
        f.region_id,
        f.target_period,
        f.target_type,
        SUM(f.target_amount)   AS target_amount,
        SUM(f.target_quantity) AS target_quantity
    FROM fact_target f
    JOIN dim_date d ON d.date_id = f.date_id
    WHERE f.is_void = FALSE
    GROUP BY d.financial_year, d.year, d.month, f.source_system, f.region_id,
             f.target_period, f.target_type
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
    t.target_period,
    t.target_type,
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

#: Reads ``vw_target_vs_actual``, so it is dropped first and rebuilt last.
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

#: Dependency order: the achievement view reads the comparison view.
_DEPENDENT_VIEWS: tuple[str, ...] = ("vw_region_target_achievement",
                                     "vw_target_vs_actual")


def upgrade() -> None:
    op.add_column("stg_target", sa.Column("target_quantity", STAGING_TEXT, nullable=True))
    op.add_column("stg_target", sa.Column("target_volume", STAGING_TEXT, nullable=True))
    op.add_column("stg_target",
                  sa.Column("target_volume_unit", CODE, nullable=True))

    # Nullable on the fact too: a target set in taka only is a complete target.
    op.add_column("fact_target", sa.Column("target_quantity", MEASURE, nullable=True))
    op.add_column("fact_target", sa.Column("target_volume", MEASURE, nullable=True))
    op.add_column("fact_target",
                  sa.Column("target_volume_unit", CODE, nullable=True))
    op.create_index("ix_fact_target_volume_unit", "fact_target", ["target_volume_unit"])

    _rebuild_views()


def _rebuild_views() -> None:
    for name in _DEPENDENT_VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {name}")
    op.execute("DROP VIEW IF EXISTS vw_target_detail")

    op.execute(f"CREATE VIEW vw_target_detail AS {_detail_view()}")
    op.execute(f"CREATE VIEW vw_target_vs_actual AS {TARGET_VS_ACTUAL}")
    op.execute(f"CREATE VIEW vw_region_target_achievement AS {REGION_ACHIEVEMENT}")


def downgrade() -> None:
    # Views first: SQLite validates every view against the schema when a column
    # is dropped, so one still naming ``target_quantity`` blocks the drop.
    for name in _DEPENDENT_VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {name}")
    op.execute("DROP VIEW IF EXISTS vw_target_detail")

    op.drop_index("ix_fact_target_volume_unit", table_name="fact_target")
    for column in ("target_volume_unit", "target_volume", "target_quantity"):
        op.drop_column("fact_target", column)
        op.drop_column("stg_target", column)

    # Restore each body from the revision that last authored it, rather than
    # keeping a second copy here. 0009 rewrote the two that read the fact tables
    # directly, to add the void clause; ``vw_region_target_achievement`` reads
    # ``vw_target_vs_actual`` and so inherits that clause, and its body is still
    # the one 0003 wrote.
    voided = _load("0009_data_management")
    op.execute(f"CREATE VIEW vw_target_detail AS {voided.VIEWS['vw_target_detail']}")
    op.execute(
        f"CREATE VIEW vw_target_vs_actual AS {voided.VIEWS['vw_target_vs_actual']}")
    reporting = _load("0003_reporting_views")
    op.execute("CREATE VIEW vw_region_target_achievement AS "
               f"{reporting.VIEWS['vw_region_target_achievement']}")


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
