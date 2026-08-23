"""Batch code, invoice line number and volume on sales transactions.

Revision ID: 0012_batch_volume
Revises: 0011_customer_company
Create Date: 2026-08-14

Additive columns, plus a rebuild of the two detail views so the new measures
reach reporting.

**Why the unique constraint is not changed.** ``fact_sales`` is unique on
``business_key``, a string the ETL composes — it has never been
``UNIQUE(invoice_no, sku_code)`` at the database level. The bug the
specification describes lived in *what the pipeline put into that string*, not
in the constraint, so the fix is in ``etl.datasets`` and no constraint is
touched. That also means existing rows keep their keys and nothing has to be
re-keyed: the new composition only applies to rows imported from now on, and a
re-import of an old file updates the row it always would have.

**Why volume columns are nullable.** A product with no usable pack size yields
no volume, and NULL is the honest answer. Zero is a real volume — a line for
nothing — and must not be how "unknown" is spelled.

``volume_factor`` is the snapshot that keeps history stable: it records the pack
size actually applied, so correcting a product from 5 KG to 10 KG next year
leaves last July's invoices saying what they said.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_batch_volume"
down_revision: Union[str, None] = "0011_customer_company"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CODE = sa.String(length=64)
MEASURE = sa.Numeric(18, 4)
FACTOR = sa.Numeric(18, 6)

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

#: The sales detail view, with the line identifiers and the volume measures.
#: ``pack_size_value`` and ``pack_unit`` come from the product so a report can
#: show what the conversion *currently* is next to what was applied — the two
#: differing is exactly the signal that a pack size changed.
SALES_DETAIL = f"""
SELECT
    f.sales_id,
    f.source_system,
    f.import_batch_id,
    f.invoice_no,
    f.invoice_line_no,
    f.batch_code,
    f.customer_code,
    f.sales_force_code,
    f.warehouse_code,
{_DATE_COLUMNS},
{_ORG_COLUMNS},
{_TERRITORY_COLUMNS},
{_PRODUCT_COLUMNS},
    p.pack_size,
    p.pack_size_value,
    p.pack_unit,
    f.quantity,
    f.gross_sales,
    f.discount,
    f.net_sales,
    f.cost,
    f.gross_profit,
    f.volume,
    f.volume_unit,
    f.volume_factor
FROM fact_sales f
JOIN dim_date d    ON d.date_id    = f.date_id
JOIN dim_product p ON p.product_id = f.product_id
{_ORG_JOIN}
{_TERRITORY_JOIN}
WHERE {_NOT_VOID}
"""

STOCK_DETAIL = f"""
SELECT
    f.stock_id,
    f.source_system,
    f.import_batch_id,
    f.warehouse_code,
    w.warehouse_name,
{_DATE_COLUMNS},
{_ORG_COLUMNS},
{_PRODUCT_COLUMNS},
    p.pack_size,
    p.pack_size_value,
    p.pack_unit,
    f.opening_stock,
    f.purchase_qty,
    f.sales_qty,
    f.transfer_in,
    f.transfer_out,
    f.closing_stock,
    f.closing_stock_from_source,
    f.volume,
    f.volume_unit,
    f.volume_factor
FROM fact_stock f
JOIN dim_date d    ON d.date_id    = f.date_id
JOIN dim_product p ON p.product_id = f.product_id
LEFT JOIN dim_warehouse w ON w.warehouse_id = f.warehouse_id
{_ORG_JOIN}
WHERE {_NOT_VOID}
"""

REBUILT = (("vw_sales_detail", SALES_DETAIL), ("vw_stock_detail", STOCK_DETAIL))


def upgrade() -> None:
    # --- product conversion --------------------------------------------------
    op.add_column("dim_product",
                  sa.Column("pack_size_value", FACTOR, nullable=True))
    op.add_column("dim_product",
                  sa.Column("pack_unit", sa.String(length=16), nullable=True))
    op.create_index("ix_dim_product_pack_unit", "dim_product", ["pack_unit"])

    # --- the line identifiers ------------------------------------------------
    op.add_column("stg_sales",
                  sa.Column("invoice_line_no", sa.String(length=64), nullable=True))
    op.add_column("stg_sales", sa.Column("batch_code", CODE, nullable=True))
    op.add_column("stg_sales",
                  sa.Column("volume", sa.String(length=64), nullable=True))
    op.add_column("stg_sales",
                  sa.Column("volume_unit", sa.String(length=16), nullable=True))

    op.add_column("fact_sales",
                  sa.Column("invoice_line_no", sa.String(length=64), nullable=True))
    op.add_column("fact_sales", sa.Column("batch_code", CODE, nullable=True))
    op.create_index("ix_fact_sales_invoice_line", "fact_sales",
                    ["invoice_no", "invoice_line_no"])
    op.create_index("ix_fact_sales_batch_code", "fact_sales", ["batch_code"])

    # --- volume --------------------------------------------------------------
    for table in ("fact_sales", "fact_stock"):
        op.add_column(table, sa.Column("volume", MEASURE, nullable=True))
        op.add_column(table,
                      sa.Column("volume_unit", sa.String(length=16), nullable=True))
        op.add_column(table, sa.Column("volume_factor", FACTOR, nullable=True))
        # Reports filter by unit constantly — "show me the KG" — and the column
        # has a handful of distinct values over millions of rows.
        op.create_index(f"ix_{table}_volume_unit", table, ["volume_unit"])

    _rebuild_views()


def _rebuild_views() -> None:
    for name, body in REBUILT:
        op.execute(f"DROP VIEW IF EXISTS {name}")
        op.execute(f"CREATE VIEW {name} AS {body}")


def downgrade() -> None:
    # Views first: SQLite validates every view against the schema when a column
    # is dropped, so one still naming ``volume`` blocks the drop.
    for name, _body in REBUILT:
        op.execute(f"DROP VIEW IF EXISTS {name}")

    # ``fact_stock`` is conditional: revision 0016 replaced the whole stock
    # module and dropped that table, so by the time a downgrade reaches this
    # revision it may legitimately be gone. Dropping its columns unconditionally
    # would make the downgrade path fail on any database that has been through
    # 0016 — which is every current one.
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table in ("fact_sales", "fact_stock"):
        if table not in existing:
            continue
        op.drop_index(f"ix_{table}_volume_unit", table_name=table)
        for column in ("volume_factor", "volume_unit", "volume"):
            op.drop_column(table, column)

    op.drop_index("ix_fact_sales_batch_code", table_name="fact_sales")
    op.drop_index("ix_fact_sales_invoice_line", table_name="fact_sales")
    op.drop_column("fact_sales", "batch_code")
    op.drop_column("fact_sales", "invoice_line_no")

    for column in ("volume_unit", "volume", "batch_code", "invoice_line_no"):
        op.drop_column("stg_sales", column)

    op.drop_index("ix_dim_product_pack_unit", table_name="dim_product")
    op.drop_column("dim_product", "pack_unit")
    op.drop_column("dim_product", "pack_size_value")

    # Restore the bodies revision 0009 left in place, by re-running its own SQL
    # rather than keeping a third copy of it here.
    _sibling = _load("0009_data_management")
    for name in ("vw_sales_detail", "vw_stock_detail"):
        # vw_stock_detail reads fact_stock, so it can only be rebuilt where that
        # table still stands — see the note above.
        if name == "vw_stock_detail" and "fact_stock" not in existing:
            continue
        op.execute(f"CREATE VIEW {name} AS {_sibling.VIEWS[name]}")


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
