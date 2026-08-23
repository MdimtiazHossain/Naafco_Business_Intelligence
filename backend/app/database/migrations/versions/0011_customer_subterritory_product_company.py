"""Customer sub-territory and product company code.

Revision ID: 0011_customer_company
Revises: 0010_admin_areas
Create Date: 2026-08-12

Two nullable columns and two indexes. Nothing is dropped, renamed, recreated or
back-filled here — the schema change is separated from the data mapping on
purpose.

**Why the columns are nullable, and stay so.** A product whose company cannot be
determined from the existing data, and a customer whose sub-territory cannot,
must end up NULL rather than guessed. NULL is the honest answer and it is what
the mapping report counts as "unmapped".

**Why there is no foreign key.** ``dim_customer`` is populated by upload *and* by
the ETL, which creates a placeholder the moment a transaction names an unknown
customer code; ``dim_product`` is documented as an independent dimension and a
test asserts it has no foreign keys. A constraint on either would turn a single
unrecognised code into a failed import, where the pipeline already has a
row-level rejection path that says which row and why. Both columns are validated
against their master on every write and every upload instead — see
``upload.registry.LOOKUPS_BY_TABLE``.

**Why the backfill is not here.** Mapping existing rows is a judgement about data,
not about schema: it has to inspect what relationships actually exist, refuse to
guess where they are ambiguous, and produce a report of what it could not do.
That belongs in ``scripts/map_master_data.py``, which can be run, inspected and
re-run. A migration that silently wrote business values would give no chance to
check them first.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_customer_company"
down_revision: Union[str, None] = "0010_admin_areas"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CODE = sa.String(length=64)


def upgrade() -> None:
    # The map asks "which customers are in this sub-territory" on every draw,
    # so the column that answers it is indexed from the start.
    op.add_column("dim_customer",
                  sa.Column("sub_territory_code", CODE, nullable=True))
    op.create_index("ix_dim_customer_sub_territory_code", "dim_customer",
                    ["sub_territory_code"])

    op.add_column("dim_product",
                  sa.Column("company_code", CODE, nullable=True))
    op.create_index("ix_dim_product_company_code", "dim_product",
                    ["company_code"])


def downgrade() -> None:
    op.drop_index("ix_dim_product_company_code", table_name="dim_product")
    op.drop_column("dim_product", "company_code")

    op.drop_index("ix_dim_customer_sub_territory_code", table_name="dim_customer")
    op.drop_column("dim_customer", "sub_territory_code")
