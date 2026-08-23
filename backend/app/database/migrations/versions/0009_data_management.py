"""Master and transaction data management: soft delete, void, change history.

Revision ID: 0009_data_management
Revises: 0008_map_locations
Create Date: 2026-08-12

Three additive changes and one rewrite.

**Additive.** Master dimensions gain ``is_deleted`` / ``deleted_at`` /
``deleted_by``; fact tables gain ``is_void`` / ``voided_at`` / ``voided_by`` /
``void_reason``; ``data_change_log`` records field-level history. Nothing is
dropped, nothing is back-filled with anything but the neutral default.

**The rewrite.** The nine reporting views that read a fact table are recreated
with ``WHERE f.is_void = FALSE``. This is what makes voiding mean something: a
voided sale leaves the dashboard, the reports, the exports, the map and the AI
agent simultaneously, because every one of them reads through these views. Doing
it here rather than in application code means no future query can forget.

The view bodies are copied from ``0003_reporting_views`` and ``0004_ai_layer``
with that one clause added. A migration is a snapshot of intent at a point in
time, so it carries its own SQL rather than importing another revision's.

``FALSE`` rather than ``0``: PostgreSQL rejects comparing a boolean to an
integer, and SQLite has understood the ``FALSE`` keyword since 3.23.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_data_management"
down_revision: Union[str, None] = "0008_map_locations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSON_TYPE = sa.JSON()

#: Every master dimension a user can manage through the new tables.
MASTER_TABLES: tuple[str, ...] = (
    "dim_company",
    "dim_business_unit",
    "dim_sales_line",
    "dim_zone",
    "dim_region",
    "dim_area",
    "dim_unit",
    "dim_territory",
    "dim_sub_territory",
    "dim_product",
    "dim_customer",
    "dim_sales_force",
    "dim_warehouse",
)

FACT_TABLES: tuple[str, ...] = (
    "fact_sales",
    "fact_collection",
    "fact_outstanding",
    "fact_stock",
    "fact_target",
)


# ---------------------------------------------------------------------------
# View bodies, with the void filter applied
# ---------------------------------------------------------------------------

_NOT_VOID = "f.is_void = FALSE"

_DATE_COLUMNS = """
    d.date_id,
    d.full_date,
    d.month,
    d.month_name,
    d.quarter,
    d.year,
    d.financial_year,
    d.financial_month,
    d.financial_quarter
"""

_ORG_COLUMNS = """
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
    u.unit_name
"""

_ORG_JOIN = """
    LEFT JOIN dim_company       c  ON c.company_id       = f.company_id
    LEFT JOIN dim_business_unit bu ON bu.business_unit_id = f.business_unit_id
    LEFT JOIN dim_sales_line    sl ON sl.sales_line_id   = f.sales_line_id
    LEFT JOIN dim_zone          z  ON z.zone_id          = f.zone_id
    LEFT JOIN dim_region        r  ON r.region_id        = f.region_id
    LEFT JOIN dim_area          a  ON a.area_id          = f.area_id
    LEFT JOIN dim_unit          u  ON u.unit_id          = f.unit_id
"""

_TERRITORY_COLUMNS = """
    t.territory_code,
    t.territory_name,
    st.sub_territory_code,
    st.sub_territory_name
"""

_TERRITORY_JOIN = """
    LEFT JOIN dim_territory     t  ON t.territory_id      = f.territory_id
    LEFT JOIN dim_sub_territory st ON st.sub_territory_id = f.sub_territory_id
"""

_PRODUCT_COLUMNS = """
    p.sku_code,
    p.sku_name_en,
    p.sku_name_bn,
    p.category,
    p.brand,
    p.product_type
"""

VIEWS: dict[str, str] = {}

VIEWS["vw_sales_detail"] = f"""
SELECT
    f.sales_id,
    f.source_system,
    f.import_batch_id,
    f.invoice_no,
    f.customer_code,
    f.sales_force_code,
    f.warehouse_code,
{_DATE_COLUMNS},
{_ORG_COLUMNS},
{_TERRITORY_COLUMNS},
{_PRODUCT_COLUMNS},
    f.quantity,
    f.gross_sales,
    f.discount,
    f.net_sales,
    f.cost,
    f.gross_profit
FROM fact_sales f
JOIN dim_date d    ON d.date_id    = f.date_id
JOIN dim_product p ON p.product_id = f.product_id
{_ORG_JOIN}
{_TERRITORY_JOIN}
WHERE {_NOT_VOID}
"""

VIEWS["vw_collection_detail"] = f"""
SELECT
    f.collection_pk,
    f.source_system,
    f.import_batch_id,
    f.collection_id,
    f.invoice_no,
    f.customer_code,
    f.sales_force_code,
    f.payment_method,
{_DATE_COLUMNS},
{_ORG_COLUMNS},
{_TERRITORY_COLUMNS},
    f.collection_amount
FROM fact_collection f
JOIN dim_date d ON d.date_id = f.date_id
{_ORG_JOIN}
{_TERRITORY_JOIN}
WHERE {_NOT_VOID}
"""

VIEWS["vw_outstanding_detail"] = f"""
SELECT
    f.outstanding_id,
    f.source_system,
    f.import_batch_id,
    f.invoice_no,
    f.invoice_date,
    f.due_date,
    f.customer_code,
    f.sales_force_code,
    f.aging_bucket,
    f.days_overdue,
{_DATE_COLUMNS},
{_ORG_COLUMNS},
{_TERRITORY_COLUMNS},
    f.invoice_amount,
    f.paid_amount,
    f.outstanding_amount
FROM fact_outstanding f
JOIN dim_date d ON d.date_id = f.date_id
{_ORG_JOIN}
{_TERRITORY_JOIN}
WHERE {_NOT_VOID}
"""

VIEWS["vw_stock_detail"] = f"""
SELECT
    f.stock_id,
    f.source_system,
    f.import_batch_id,
    f.warehouse_code,
    w.warehouse_name,
{_DATE_COLUMNS},
{_ORG_COLUMNS},
{_PRODUCT_COLUMNS},
    f.opening_stock,
    f.purchase_qty,
    f.sales_qty,
    f.transfer_in,
    f.transfer_out,
    f.closing_stock,
    f.closing_stock_from_source
FROM fact_stock f
JOIN dim_date d    ON d.date_id    = f.date_id
JOIN dim_product p ON p.product_id = f.product_id
LEFT JOIN dim_warehouse w ON w.warehouse_id = f.warehouse_id
{_ORG_JOIN}
WHERE {_NOT_VOID}
"""

VIEWS["vw_target_detail"] = f"""
SELECT
    f.target_id,
    f.source_system,
    f.import_batch_id,
    f.target_period,
    f.target_type,
    f.sales_force_code,
{_DATE_COLUMNS},
{_ORG_COLUMNS},
{_TERRITORY_COLUMNS},
    p.sku_code,
    p.sku_name_en,
    p.category,
    p.brand,
    f.target_amount
FROM fact_target f
JOIN dim_date d ON d.date_id = f.date_id
LEFT JOIN dim_product p ON p.product_id = f.product_id
{_ORG_JOIN}
{_TERRITORY_JOIN}
WHERE {_NOT_VOID}
"""

VIEWS["vw_customer_outstanding"] = """
SELECT
    f.customer_code,
    f.source_system,
    d.full_date AS as_on_date,
    d.date_id,
    r.region_code,
    r.region_name,
    a.area_code,
    t.territory_code,
    COUNT(*)                      AS invoice_count,
    SUM(f.invoice_amount)         AS invoice_amount,
    SUM(f.paid_amount)            AS paid_amount,
    SUM(f.outstanding_amount)     AS outstanding_amount,
    SUM(CASE WHEN f.days_overdue > 0 THEN f.outstanding_amount ELSE 0 END) AS overdue_amount,
    SUM(CASE WHEN f.days_overdue > 0 THEN f.outstanding_amount ELSE 0 END) * 100.0
        / NULLIF(SUM(f.outstanding_amount), 0) AS overdue_percent,
    MAX(f.days_overdue)           AS max_days_overdue
FROM fact_outstanding f
JOIN dim_date d ON d.date_id = f.date_id
LEFT JOIN dim_region    r ON r.region_id    = f.region_id
LEFT JOIN dim_area      a ON a.area_id      = f.area_id
LEFT JOIN dim_territory t ON t.territory_id = f.territory_id
WHERE f.is_void = FALSE
GROUP BY
    f.customer_code, f.source_system, d.full_date, d.date_id,
    r.region_code, r.region_name, a.area_code, t.territory_code
"""

VIEWS["vw_outstanding_aging"] = """
SELECT
    d.date_id,
    d.full_date AS as_on_date,
    d.financial_year,
    f.source_system,
    f.aging_bucket,
    c.company_code,
    z.zone_code,
    r.region_code,
    r.region_name,
    a.area_code,
    t.territory_code,
    COUNT(*)                  AS invoice_count,
    SUM(f.invoice_amount)     AS invoice_amount,
    SUM(f.paid_amount)        AS paid_amount,
    SUM(f.outstanding_amount) AS outstanding_amount
FROM fact_outstanding f
JOIN dim_date d ON d.date_id = f.date_id
LEFT JOIN dim_company   c ON c.company_id   = f.company_id
LEFT JOIN dim_zone      z ON z.zone_id      = f.zone_id
LEFT JOIN dim_region    r ON r.region_id    = f.region_id
LEFT JOIN dim_area      a ON a.area_id      = f.area_id
LEFT JOIN dim_territory t ON t.territory_id = f.territory_id
WHERE f.is_void = FALSE
GROUP BY
    d.date_id, d.full_date, d.financial_year, f.source_system, f.aging_bucket,
    c.company_code, z.zone_code, r.region_code, r.region_name, a.area_code,
    t.territory_code
"""

# "Latest stock position" must mean the latest *live* one, so the correlated
# subquery excludes voided rows too — otherwise voiding the newest snapshot
# would leave the view showing no position at all rather than the one before it.
VIEWS["vw_current_stock"] = """
SELECT
    f.source_system,
    f.warehouse_code,
    w.warehouse_name,
    p.sku_code,
    p.sku_name_en,
    p.category,
    p.brand,
    d.full_date AS stock_date,
    f.date_id,
    c.company_code,
    r.region_code,
    a.area_code,
    u.unit_code,
    f.opening_stock,
    f.purchase_qty,
    f.sales_qty,
    f.transfer_in,
    f.transfer_out,
    f.closing_stock AS current_stock,
    f.closing_stock_from_source,
    CASE WHEN f.closing_stock <= 0 THEN 'OUT_OF_STOCK' ELSE 'IN_STOCK' END AS stock_state
FROM fact_stock f
JOIN dim_date d    ON d.date_id    = f.date_id
JOIN dim_product p ON p.product_id = f.product_id
LEFT JOIN dim_warehouse w ON w.warehouse_id = f.warehouse_id
LEFT JOIN dim_company   c ON c.company_id   = f.company_id
LEFT JOIN dim_region    r ON r.region_id    = f.region_id
LEFT JOIN dim_area      a ON a.area_id      = f.area_id
LEFT JOIN dim_unit      u ON u.unit_id      = f.unit_id
WHERE f.is_void = FALSE
  AND f.date_id = (
    SELECT MAX(f2.date_id)
    FROM fact_stock f2
    WHERE f2.product_id = f.product_id
      AND f2.source_system = f.source_system
      AND f2.is_void = FALSE
      AND (f2.warehouse_code = f.warehouse_code
           OR (f2.warehouse_code IS NULL AND f.warehouse_code IS NULL))
)
"""

VIEWS["vw_stock_coverage"] = """
WITH sales_window AS (
    SELECT
        product_id,
        warehouse_code,
        source_system,
        SUM(sales_qty)                       AS total_sales_qty,
        COUNT(DISTINCT date_id)              AS day_count
    FROM fact_stock
    WHERE is_void = FALSE
    GROUP BY product_id, warehouse_code, source_system
),
latest AS (
    SELECT
        f.product_id,
        f.warehouse_code,
        f.source_system,
        f.date_id,
        f.closing_stock
    FROM fact_stock f
    WHERE f.is_void = FALSE
      AND f.date_id = (
        SELECT MAX(f2.date_id)
        FROM fact_stock f2
        WHERE f2.product_id = f.product_id
          AND f2.source_system = f.source_system
          AND f2.is_void = FALSE
          AND (f2.warehouse_code = f.warehouse_code
               OR (f2.warehouse_code IS NULL AND f.warehouse_code IS NULL))
    )
)
SELECT
    l.source_system,
    l.warehouse_code,
    p.sku_code,
    p.sku_name_en,
    p.category,
    p.brand,
    d.full_date AS stock_date,
    l.closing_stock AS current_stock,
    s.total_sales_qty,
    s.day_count,
    s.total_sales_qty / NULLIF(s.day_count, 0) AS average_daily_sales,
    l.closing_stock / NULLIF(s.total_sales_qty / NULLIF(s.day_count, 0), 0)
        AS stock_coverage_days,
    CASE
        WHEN l.closing_stock <= 0 THEN 'OUT_OF_STOCK'
        WHEN s.total_sales_qty IS NULL OR s.total_sales_qty = 0 THEN 'NORMAL'
        WHEN l.closing_stock / NULLIF(s.total_sales_qty / NULLIF(s.day_count, 0), 0) <= 7
            THEN 'CRITICAL'
        WHEN l.closing_stock / NULLIF(s.total_sales_qty / NULLIF(s.day_count, 0), 0) <= 15
            THEN 'WARNING'
        ELSE 'NORMAL'
    END AS coverage_status
FROM latest l
JOIN dim_product p ON p.product_id = l.product_id
JOIN dim_date d    ON d.date_id    = l.date_id
LEFT JOIN sales_window s
       ON s.product_id = l.product_id
      AND s.source_system = l.source_system
      AND (s.warehouse_code = l.warehouse_code
           OR (s.warehouse_code IS NULL AND l.warehouse_code IS NULL))
"""

VIEWS["vw_target_vs_actual"] = """
WITH target_month AS (
    SELECT
        d.financial_year,
        d.year,
        d.month,
        f.source_system,
        f.region_id,
        f.target_period,
        f.target_type,
        SUM(f.target_amount) AS target_amount
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
    COALESCE(a.actual_sales, 0)   AS actual_sales,
    COALESCE(a.actual_quantity, 0) AS actual_quantity,
    COALESCE(a.actual_sales, 0) * 100.0 / NULLIF(t.target_amount, 0) AS achievement_percent,
    COALESCE(t.target_amount, 0) - COALESCE(a.actual_sales, 0)       AS gap
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

# The Phase 2 aggregate views. Bodies as authored in 0003, with the void clause
# added: they are recreated rather than left alone because they read the fact
# tables directly, and a view that silently still counts voided rows is worse
# than one that never existed.

_AGG_ORG_JOIN = """
    LEFT JOIN dim_company        c   ON c.company_id        = f.company_id
    LEFT JOIN dim_business_unit  bu  ON bu.business_unit_id  = f.business_unit_id
    LEFT JOIN dim_sales_line     sl  ON sl.sales_line_id     = f.sales_line_id
    LEFT JOIN dim_zone           z   ON z.zone_id            = f.zone_id
    LEFT JOIN dim_region         r   ON r.region_id          = f.region_id
    LEFT JOIN dim_area           a   ON a.area_id            = f.area_id
    LEFT JOIN dim_unit           u   ON u.unit_id            = f.unit_id
"""

_AGG_ORG_JOIN_FULL = _AGG_ORG_JOIN + """
    LEFT JOIN dim_territory      t   ON t.territory_id       = f.territory_id
    LEFT JOIN dim_sub_territory  st  ON st.sub_territory_id  = f.sub_territory_id
"""

VIEWS["vw_daily_sales"] = f"""
SELECT
    d.date_id,
    d.full_date,
    d.year,
    d.month,
    d.month_name,
    d.financial_year,
    d.financial_month,
    f.source_system,
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
    COUNT(*)                       AS transaction_count,
    COUNT(DISTINCT f.invoice_no)   AS invoice_count,
    SUM(f.quantity)                AS quantity,
    SUM(f.gross_sales)             AS gross_sales,
    SUM(f.discount)                AS discount,
    SUM(f.net_sales)               AS net_sales,
    SUM(f.cost)                    AS cost,
    SUM(f.gross_profit)            AS gross_profit,
    SUM(f.net_sales) / NULLIF(SUM(f.quantity), 0)          AS average_selling_price,
    SUM(f.gross_profit) * 100.0 / NULLIF(SUM(f.net_sales), 0) AS gross_margin_percent
FROM fact_sales f
JOIN dim_date d ON d.date_id = f.date_id
{_AGG_ORG_JOIN_FULL}
WHERE {_NOT_VOID}
GROUP BY
    d.date_id, d.full_date, d.year, d.month, d.month_name,
    d.financial_year, d.financial_month, f.source_system,
    c.company_code, c.company_name, bu.bu_code, bu.bu_name,
    sl.sales_line_code, sl.sales_line_name, z.zone_code, z.zone_name,
    r.region_code, r.region_name, a.area_code, a.area_name,
    u.unit_code, u.unit_name, t.territory_code, t.territory_name
"""

VIEWS["vw_monthly_sales"] = f"""
SELECT
    d.year,
    d.month,
    d.month_name,
    d.financial_year,
    d.financial_month,
    d.quarter_name,
    f.source_system,
    c.company_code,
    bu.bu_code,
    sl.sales_line_code,
    z.zone_code,
    r.region_code,
    r.region_name,
    a.area_code,
    u.unit_code,
    COUNT(*)                     AS transaction_count,
    COUNT(DISTINCT f.invoice_no) AS invoice_count,
    SUM(f.quantity)              AS quantity,
    SUM(f.gross_sales)           AS gross_sales,
    SUM(f.discount)              AS discount,
    SUM(f.net_sales)             AS net_sales,
    SUM(f.cost)                  AS cost,
    SUM(f.gross_profit)          AS gross_profit,
    SUM(f.net_sales) / NULLIF(SUM(f.quantity), 0)             AS average_selling_price,
    SUM(f.gross_profit) * 100.0 / NULLIF(SUM(f.net_sales), 0) AS gross_margin_percent
FROM fact_sales f
JOIN dim_date d ON d.date_id = f.date_id
{_AGG_ORG_JOIN}
WHERE {_NOT_VOID}
GROUP BY
    d.year, d.month, d.month_name, d.financial_year, d.financial_month,
    d.quarter_name, f.source_system, c.company_code, bu.bu_code,
    sl.sales_line_code, z.zone_code, r.region_code, r.region_name,
    a.area_code, u.unit_code
"""

VIEWS["vw_region_sales"] = """
SELECT
    d.financial_year,
    d.year,
    d.month,
    f.source_system,
    z.zone_code,
    z.zone_name,
    r.region_code,
    r.region_name,
    r.region_hq,
    COUNT(*)             AS transaction_count,
    SUM(f.quantity)      AS quantity,
    SUM(f.gross_sales)   AS gross_sales,
    SUM(f.discount)      AS discount,
    SUM(f.net_sales)     AS net_sales,
    SUM(f.gross_profit)  AS gross_profit,
    SUM(f.gross_profit) * 100.0 / NULLIF(SUM(f.net_sales), 0) AS gross_margin_percent
FROM fact_sales f
JOIN dim_date d   ON d.date_id   = f.date_id
JOIN dim_region r ON r.region_id = f.region_id
LEFT JOIN dim_zone z ON z.zone_id = f.zone_id
WHERE f.is_void = FALSE
GROUP BY
    d.financial_year, d.year, d.month, f.source_system,
    z.zone_code, z.zone_name, r.region_code, r.region_name, r.region_hq
"""

VIEWS["vw_product_sales"] = """
SELECT
    d.financial_year,
    d.year,
    d.month,
    f.source_system,
    p.sku_code,
    p.sku_name_en,
    p.sku_name_bn,
    p.category,
    p.brand,
    p.product_type,
    p.status,
    COUNT(*)            AS transaction_count,
    SUM(f.quantity)     AS quantity,
    SUM(f.gross_sales)  AS gross_sales,
    SUM(f.discount)     AS discount,
    SUM(f.net_sales)    AS net_sales,
    SUM(f.cost)         AS cost,
    SUM(f.gross_profit) AS gross_profit,
    SUM(f.net_sales) / NULLIF(SUM(f.quantity), 0)             AS average_selling_price,
    SUM(f.gross_profit) * 100.0 / NULLIF(SUM(f.net_sales), 0) AS gross_margin_percent
FROM fact_sales f
JOIN dim_date d    ON d.date_id    = f.date_id
JOIN dim_product p ON p.product_id = f.product_id
WHERE f.is_void = FALSE
GROUP BY
    d.financial_year, d.year, d.month, f.source_system,
    p.sku_code, p.sku_name_en, p.sku_name_bn, p.category, p.brand,
    p.product_type, p.status
"""

VIEWS["vw_daily_collection"] = f"""
SELECT
    d.date_id,
    d.full_date,
    d.year,
    d.month,
    d.financial_year,
    f.source_system,
    c.company_code,
    bu.bu_code,
    sl.sales_line_code,
    z.zone_code,
    r.region_code,
    r.region_name,
    a.area_code,
    u.unit_code,
    t.territory_code,
    f.customer_code,
    COUNT(*)                    AS collection_count,
    SUM(f.collection_amount)    AS collection_amount
FROM fact_collection f
JOIN dim_date d ON d.date_id = f.date_id
{_AGG_ORG_JOIN_FULL}
WHERE {_NOT_VOID}
GROUP BY
    d.date_id, d.full_date, d.year, d.month, d.financial_year, f.source_system,
    c.company_code, bu.bu_code, sl.sales_line_code, z.zone_code,
    r.region_code, r.region_name, a.area_code, u.unit_code, t.territory_code,
    f.customer_code
"""

VIEWS["vw_monthly_collection"] = """
SELECT
    d.year,
    d.month,
    d.month_name,
    d.financial_year,
    d.financial_month,
    f.source_system,
    c.company_code,
    z.zone_code,
    r.region_code,
    r.region_name,
    COUNT(*)                 AS collection_count,
    SUM(f.collection_amount) AS collection_amount
FROM fact_collection f
JOIN dim_date d ON d.date_id = f.date_id
LEFT JOIN dim_company c ON c.company_id = f.company_id
LEFT JOIN dim_zone    z ON z.zone_id    = f.zone_id
LEFT JOIN dim_region  r ON r.region_id  = f.region_id
WHERE f.is_void = FALSE
GROUP BY
    d.year, d.month, d.month_name, d.financial_year, d.financial_month,
    f.source_system, c.company_code, z.zone_code, r.region_code, r.region_name
"""

#: Recreated in dependency order — ``vw_region_target_achievement`` reads
#: ``vw_target_vs_actual``, so that one has to exist again first.
REBUILD_ORDER: tuple[str, ...] = (
    "vw_daily_sales",
    "vw_monthly_sales",
    "vw_region_sales",
    "vw_product_sales",
    "vw_daily_collection",
    "vw_monthly_collection",
    "vw_customer_outstanding",
    "vw_outstanding_aging",
    "vw_current_stock",
    "vw_stock_coverage",
    "vw_target_vs_actual",
    "vw_sales_detail",
    "vw_collection_detail",
    "vw_outstanding_detail",
    "vw_stock_detail",
    "vw_target_detail",
)

#: Depends on ``vw_target_vs_actual`` and so must be dropped before it and
#: recreated after it. Its own body is unchanged.
DEPENDENT_VIEW = "vw_region_target_achievement"

DEPENDENT_VIEW_SQL = """
SELECT
    financial_year,
    source_system,
    region_code,
    region_name,
    SUM(target_amount) AS target_amount,
    SUM(actual_sales)  AS actual_sales,
    SUM(actual_sales) * 100.0 / NULLIF(SUM(target_amount), 0) AS achievement_percent,
    SUM(target_amount) - SUM(actual_sales) AS gap
FROM vw_target_vs_actual
GROUP BY financial_year, source_system, region_code, region_name
"""


def upgrade() -> None:
    # --- master data: retire, never destroy --------------------------------
    #
    # No index on the flag. Almost every row is ``false``, so an index on it is
    # too unselective for a planner to use for the common "not deleted" filter,
    # and the rare opposite query — "show me the retired ones" — is a small
    # result the existing code index already narrows.
    for table in MASTER_TABLES:
        op.add_column(table, sa.Column("is_deleted", sa.Boolean(), nullable=False,
                                       server_default=sa.false()))
        op.add_column(table, sa.Column("deleted_at", sa.DateTime(timezone=True),
                                       nullable=True))
        op.add_column(table, sa.Column("deleted_by", sa.String(length=64),
                                       nullable=True))

    # --- transactions: void, never delete ----------------------------------
    for table in FACT_TABLES:
        op.add_column(table, sa.Column("is_void", sa.Boolean(), nullable=False,
                                       server_default=sa.false()))
        op.add_column(table, sa.Column("voided_at", sa.DateTime(timezone=True),
                                       nullable=True))
        op.add_column(table, sa.Column("voided_by", sa.String(length=64),
                                       nullable=True))
        op.add_column(table, sa.Column("void_reason", sa.Text(), nullable=True))

    # --- field-level history ------------------------------------------------
    op.create_table(
        "data_change_log",
        sa.Column("change_id", PK, autoincrement=True, nullable=False),
        sa.Column("entity_key", sa.String(length=48), nullable=False),
        sa.Column("record_key", sa.String(length=256), nullable=False),
        sa.Column("record_label", sa.Text(), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("user_id", PK, nullable=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=True),
        sa.Column("changed_fields", JSON_TYPE, nullable=True),
        sa.Column("old_values", JSON_TYPE, nullable=True),
        sa.Column("new_values", JSON_TYPE, nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("change_id"),
    )
    op.create_index("ix_data_change_log_record", "data_change_log",
                    ["entity_key", "record_key"])
    op.create_index("ix_data_change_log_created_at", "data_change_log",
                    ["created_at"])
    op.create_index("ix_data_change_log_username", "data_change_log", ["username"])

    # --- make voiding mean something ---------------------------------------
    _rebuild_views()


def _rebuild_views() -> None:
    op.execute(f"DROP VIEW IF EXISTS {DEPENDENT_VIEW}")
    for name in REBUILD_ORDER:
        op.execute(f"DROP VIEW IF EXISTS {name}")
    for name in REBUILD_ORDER:
        op.execute(f"CREATE VIEW {name} AS {VIEWS[name]}")
    op.execute(f"CREATE VIEW {DEPENDENT_VIEW} AS {DEPENDENT_VIEW_SQL}")


def downgrade() -> None:
    # Views first: SQLite validates every view against the schema when a column
    # is dropped, so a view still naming ``is_void`` makes dropping that column
    # fail. The columns cannot go until nothing refers to them.
    op.execute(f"DROP VIEW IF EXISTS {DEPENDENT_VIEW}")
    for name in REBUILD_ORDER:
        op.execute(f"DROP VIEW IF EXISTS {name}")

    op.drop_index("ix_data_change_log_username", table_name="data_change_log")
    op.drop_index("ix_data_change_log_created_at", table_name="data_change_log")
    op.drop_index("ix_data_change_log_record", table_name="data_change_log")
    op.drop_table("data_change_log")

    # ``fact_stock`` is conditional: revision 0016 replaced the whole stock
    # module and dropped that table, so a downgrade reaching this revision on
    # any current database will not find it. Its absence is expected, not an
    # error — but every other fact must still be unwound.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table in FACT_TABLES:
        if table not in existing:
            continue
        for column in ("void_reason", "voided_by", "voided_at", "is_void"):
            op.drop_column(table, column)

    for table in MASTER_TABLES:
        for column in ("deleted_by", "deleted_at", "is_deleted"):
            op.drop_column(table, column)

    # Now restore the original bodies. Rather than restate them here — a third
    # copy, free to drift from both — the two revisions that authored them are
    # loaded and their own SQL re-run. This revision never wrote ``vw_*``; it
    # only added a clause to them.
    #
    # A view whose body reads a table 0016 removed cannot be rebuilt, and is
    # skipped for the same reason its table was.
    def _restore(name: str, body: str) -> None:
        if "fact_stock" in body and "fact_stock" not in existing:
            return
        op.execute(f"CREATE VIEW {name} AS {body}")

    reporting = _sibling("0003_reporting_views")
    ai_layer = _sibling("0004_ai_layer")
    for name in reporting.VIEW_ORDER:
        _restore(name, reporting.VIEWS[name])
    for name in ai_layer.VIEW_ORDER:
        _restore(name, ai_layer.DETAIL_VIEWS[name])


def _sibling(module_name: str):
    """Load another revision file by path.

    Alembic runs version files as standalone modules rather than as a package,
    so ``import_module`` has no package to resolve against; and a module name
    beginning with a digit cannot be written as an ``import`` statement at all.
    Loading from the file next to this one sidesteps both.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).with_name(f"{module_name}.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"Could not load migration {module_name} from {path}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
