"""Company Code on the Material Master.

Revision ID: 0023_material_company
Revises: 0022_remove_product_architecture
Create Date: 2026-08-22

The Material Master gains a Company Code, stated **before** the Material Code,
and a material becomes company-specific: a report filtered to one company must
not offer a material that company does not deal in. The master's stated column
order is now Company Code, Material Group Code, Material Group Name, Material
Brand Code, Material Brand, Material Code, Material Description.

**A column on the material, not half of its key.** ``material_code`` stays
``UNIQUE``: one row per material, with the company as an attribute of it. The
alternative — keying the master on ``(company_code, material_code)`` — was
considered and rejected for what it would cost downstream. Every fact resolves
its item by material code alone, and ``fact_target`` states no company code of
its own: it derives one from its territory, later in the pipeline than material
resolution happens. A composite key would leave a target unable to name an item
at all until the organisational hierarchy had been walked, which is a large
change to the ETL to express a relationship an ordinary column already holds.

So a material belongs to one company, and a company has many materials. A
Material Master naming the same material under two different companies is a
conflict the **upload reports** rather than something this schema can hold:
which of the two is right is a question about the business, and choosing either
would silently halve every report filtered to the loser.

**Nullable, and only because of history.** The 405 materials already loaded
predate the column and the source that would fill it. They keep NULL rather than
being given a guessed company — the upload requires the column on every row it
loads from here on, and a NULL company simply belongs to no company yet. Nothing
back-fills it from ``fact_material_stock``, which does state a company beside
each material: that would make transaction data the authority for a master
attribute, which is the inversion revision 0022 exists to prevent. (It would
also be wrong on the numbers — 21 material codes appear under two or three
different companies in the stock data, because the same goods are stocked at
several group companies' plants.)

**Indexes.** ``company_code`` alone, and the ``(company_code, material_code)``
pair. The filter engine's central question is "which materials does this company
have", asked afresh on every dropdown, so the pair earns an index of its own
rather than being left to two separate lookups.

Purely additive: one column and two indexes. No row is deleted, no column
dropped, no view rebuilt — so every foreign key, the ETL's material resolution
and all eight reporting views keep working untouched.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_material_company"
down_revision: Union[str, None] = "0022_remove_product_architecture"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CODE = sa.String(64)


def upgrade() -> None:
    # ADD COLUMN, not a table rewrite: SQLite appends the column in place, so the
    # views that read this table stay valid throughout and none has to be
    # captured and recreated. Position in the *stated* column order is a
    # presentation concern — ``app.upload.registry`` declares it first, and the
    # table, the template and the export all follow that declaration.
    op.add_column("dim_material", sa.Column("company_code", CODE, nullable=True))
    op.create_index("ix_dim_material_company", "dim_material", ["company_code"])
    op.create_index("ix_dim_material_company_code", "dim_material",
                    ["company_code", "material_code"])

    remaining = op.get_bind().execute(sa.text(
        "SELECT count(*) FROM dim_material WHERE company_code IS NULL"
    )).scalar() or 0
    if remaining:
        print(
            f"  NOTE: {remaining} material(s) have no company code. They predate "
            "this column and none was invented for them — re-upload the Material "
            "Master with its Company Code column to fill them in. Until then they "
            "belong to no company and a company-filtered report will not offer "
            "them."
        )


def downgrade() -> None:
    """Drop the column. Whatever it held is lost, and nothing else is.

    The Material Master is re-uploadable, so the company codes come back with the
    next upload; nothing is derived from this column that would have to be
    reconstructed.

    SQLite drops a column by rewriting the table, and re-validates every stored
    view while the rewrite is half-done — so the views are captured verbatim,
    dropped and recreated byte-for-byte around it, the same dance revisions 0020
    and 0022 needed. Other dialects drop in place and need none of it.
    """
    bind = op.get_bind()
    preserved: dict[str, str] = {}
    if bind.dialect.name == "sqlite":
        preserved = {
            name: sql for name, sql in bind.execute(sa.text(
                "SELECT name, sql FROM sqlite_master WHERE type = 'view' "
                "AND sql IS NOT NULL ORDER BY rowid"
            )).all()
        }
    for name in preserved:
        op.execute(f"DROP VIEW IF EXISTS {name}")

    op.drop_index("ix_dim_material_company_code", table_name="dim_material")
    op.drop_index("ix_dim_material_company", table_name="dim_material")
    with op.batch_alter_table("dim_material") as batch:
        batch.drop_column("company_code")

    for statement in preserved.values():
        op.execute(statement)
