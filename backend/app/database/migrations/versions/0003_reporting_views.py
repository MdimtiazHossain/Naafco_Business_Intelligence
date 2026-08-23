"""Phase 2: reporting views.

Twelve views over the fact layer, written to be the query surface for the
Phase 3 AI agent: every one joins the facts to readable master names and codes
so a generated query never has to know the surrogate keys.

Divide-by-zero is handled everywhere with ``NULLIF``: a margin, achievement,
growth or coverage figure is NULL when its denominator is zero, never an error
and never a misleading 0.

The SQL is standard enough to run on PostgreSQL and on the SQLite database used
by the tests, so the views are exercised, not just declared.

Revision ID: 0003_reporting_views
Revises: 0002_warehouse_layer
Create Date: 2026-08-10
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0003_reporting_views"
down_revision: Union[str, None] = "0002_warehouse_layer"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Organisational columns every sales-shaped view exposes.
_ORG_JOIN = """
    LEFT JOIN dim_company        c   ON c.company_id        = f.company_id
    LEFT JOIN dim_business_unit  bu  ON bu.business_unit_id  = f.business_unit_id
    LEFT JOIN dim_sales_line     sl  ON sl.sales_line_id     = f.sales_line_id
    LEFT JOIN dim_zone           z   ON z.zone_id            = f.zone_id
    LEFT JOIN dim_region         r   ON r.region_id          = f.region_id
    LEFT JOIN dim_area           a   ON a.area_id            = f.area_id
    LEFT JOIN dim_unit           u   ON u.unit_id            = f.unit_id
"""

_ORG_JOIN_FULL = _ORG_JOIN + """
    LEFT JOIN dim_territory      t   ON t.territory_id       = f.territory_id
    LEFT JOIN dim_sub_territory  st  ON st.sub_territory_id  = f.sub_territory_id
"""

VIEWS: dict[str, str] = {}


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
{_ORG_JOIN_FULL}
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
{_ORG_JOIN}
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
{_ORG_JOIN_FULL}
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
GROUP BY
    d.year, d.month, d.month_name, d.financial_year, d.financial_month,
    f.source_system, c.company_code, z.zone_code, r.region_code, r.region_name
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
GROUP BY
    d.date_id, d.full_date, d.financial_year, f.source_system, f.aging_bucket,
    c.company_code, z.zone_code, r.region_code, r.region_name, a.area_code,
    t.territory_code
"""


# The latest stock position per warehouse and SKU: the newest date_id wins.
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
WHERE f.date_id = (
    SELECT MAX(f2.date_id)
    FROM fact_stock f2
    WHERE f2.product_id = f.product_id
      AND f2.source_system = f.source_system
      AND (f2.warehouse_code = f.warehouse_code
           OR (f2.warehouse_code IS NULL AND f.warehouse_code IS NULL))
)
"""


# Coverage compares the latest stock against average daily sales over the
# period actually present in fact_stock for that SKU/warehouse.
VIEWS["vw_stock_coverage"] = """
WITH sales_window AS (
    SELECT
        product_id,
        warehouse_code,
        source_system,
        SUM(sales_qty)                       AS total_sales_qty,
        COUNT(DISTINCT date_id)              AS day_count
    FROM fact_stock
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
    WHERE f.date_id = (
        SELECT MAX(f2.date_id)
        FROM fact_stock f2
        WHERE f2.product_id = f.product_id
          AND f2.source_system = f.source_system
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


# Target vs actual is matched on financial year + month + region, the grain the
# business reports on. Targets without sales and sales without targets both
# survive, thanks to the FULL-OUTER-equivalent union of keys.
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


VIEWS["vw_region_target_achievement"] = """
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


#: Order matters: ``vw_region_target_achievement`` reads ``vw_target_vs_actual``.
VIEW_ORDER: tuple[str, ...] = (
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
    "vw_region_target_achievement",
)


def upgrade() -> None:
    for name in VIEW_ORDER:
        op.execute(f"CREATE VIEW {name} AS {VIEWS[name]}")


def downgrade() -> None:
    for name in reversed(VIEW_ORDER):
        op.execute(f"DROP VIEW IF EXISTS {name}")
