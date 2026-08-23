"""Phase 3: AI agent layer.

Adds:

* ``app_user`` — users, roles and the data scope every query is filtered by
* ``chat_conversations`` / ``chat_messages`` / ``chat_tool_calls`` — conversation
  memory and tool observability
* five **flat detail views** (``vw_*_detail``)

The flat views exist because the Phase 2 aggregate views are each fixed to one
grain — ``vw_product_sales`` carries no organisational columns, so it cannot
answer "product-wise sales for Dhaka region". Rather than re-implementing the
business calculations in Python, these views denormalise the star schema at row
level: every measure is the value ETL already computed and stored on the fact
row, and the agent only ever applies plain SUM/COUNT over them. There is still
exactly one definition of net sales, gross profit, outstanding and closing
stock, and it lives in Phase 2.

The Phase 2 aggregate views remain in use wherever their grain fits
(``vw_outstanding_aging``, ``vw_stock_coverage``, ``vw_target_vs_actual``).

Revision ID: 0004_ai_layer
Revises: 0003_reporting_views
Create Date: 2026-08-10
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0004_ai_layer'
down_revision: Union[str, None] = '0003_reporting_views'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# --------------------------------------------------------------------------
# Flat detail views
# --------------------------------------------------------------------------

_DATE_COLUMNS = """
    d.date_id,
    d.full_date,
    d.year,
    d.month,
    d.month_name,
    d.quarter_name,
    d.financial_year,
    d.financial_month
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
    LEFT JOIN dim_sales_line    sl ON sl.sales_line_id    = f.sales_line_id
    LEFT JOIN dim_zone          z  ON z.zone_id           = f.zone_id
    LEFT JOIN dim_region        r  ON r.region_id         = f.region_id
    LEFT JOIN dim_area          a  ON a.area_id           = f.area_id
    LEFT JOIN dim_unit          u  ON u.unit_id           = f.unit_id
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

DETAIL_VIEWS: dict[str, str] = {
    "vw_sales_detail": f"""
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
""",
    "vw_collection_detail": f"""
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
""",
    "vw_outstanding_detail": f"""
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
""",
    "vw_stock_detail": f"""
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
""",
    "vw_target_detail": f"""
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
""",
}

VIEW_ORDER: tuple[str, ...] = (
    "vw_sales_detail",
    "vw_collection_detail",
    "vw_outstanding_detail",
    "vw_stock_detail",
    "vw_target_detail",
)


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('app_user',
    sa.Column('user_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=True),
    sa.Column('role', sa.String(length=32), nullable=False),
    sa.Column('employee_id', sa.String(length=64), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('data_scope', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('preferred_language', sa.String(length=8), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('user_id'),
    sa.UniqueConstraint('username')
    )
    op.create_index('ix_app_user_role', 'app_user', ['role'], unique=False)
    op.create_index('ix_app_user_username', 'app_user', ['username'], unique=False)
    op.create_table('chat_tool_calls',
    sa.Column('tool_call_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('conversation_id', sa.String(length=36), nullable=False),
    sa.Column('message_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('user_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('tool_name', sa.String(length=64), nullable=False),
    sa.Column('arguments', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('success', sa.Boolean(), nullable=False),
    sa.Column('error_code', sa.String(length=48), nullable=True),
    sa.Column('row_count', sa.Integer(), nullable=True),
    sa.Column('execution_ms', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('tool_call_id')
    )
    op.create_index('ix_chat_tool_calls_conversation_id', 'chat_tool_calls', ['conversation_id'], unique=False)
    op.create_index('ix_chat_tool_calls_created_at', 'chat_tool_calls', ['created_at'], unique=False)
    op.create_index('ix_chat_tool_calls_tool_name', 'chat_tool_calls', ['tool_name'], unique=False)
    op.create_table('chat_conversations',
    sa.Column('conversation_id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('last_message_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('message_count', sa.Integer(), nullable=False),
    sa.Column('context', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['app_user.user_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('conversation_id')
    )
    op.create_index('ix_chat_conversations_user_id', 'chat_conversations', ['user_id'], unique=False)
    op.create_table('chat_messages',
    sa.Column('message_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('conversation_id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('language', sa.String(length=8), nullable=True),
    sa.Column('intent', sa.String(length=48), nullable=True),
    sa.Column('structured_query', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('error_code', sa.String(length=48), nullable=True),
    sa.Column('elapsed_ms', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['chat_conversations.conversation_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('message_id')
    )
    op.create_index('ix_chat_messages_conversation_id', 'chat_messages', ['conversation_id'], unique=False)
    op.create_index('ix_chat_messages_created_at', 'chat_messages', ['created_at'], unique=False)
    op.create_index('ix_chat_messages_intent', 'chat_messages', ['intent'], unique=False)
    for _name in VIEW_ORDER:
        op.execute(f"CREATE VIEW {_name} AS {DETAIL_VIEWS[_name]}")
    # ### end Alembic commands ###


def downgrade() -> None:
    for _name in reversed(VIEW_ORDER):
        op.execute(f"DROP VIEW IF EXISTS {_name}")
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index('ix_chat_messages_intent', table_name='chat_messages')
    op.drop_index('ix_chat_messages_created_at', table_name='chat_messages')
    op.drop_index('ix_chat_messages_conversation_id', table_name='chat_messages')
    op.drop_table('chat_messages')
    op.drop_index('ix_chat_conversations_user_id', table_name='chat_conversations')
    op.drop_table('chat_conversations')
    op.drop_index('ix_chat_tool_calls_tool_name', table_name='chat_tool_calls')
    op.drop_index('ix_chat_tool_calls_created_at', table_name='chat_tool_calls')
    op.drop_index('ix_chat_tool_calls_conversation_id', table_name='chat_tool_calls')
    op.drop_table('chat_tool_calls')
    op.drop_index('ix_app_user_username', table_name='app_user')
    op.drop_index('ix_app_user_role', table_name='app_user')
    op.drop_table('app_user')
    # ### end Alembic commands ###
