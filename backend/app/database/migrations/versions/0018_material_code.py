"""Material Code: the identifier the Material Master was specified without.

Revision ID: 0018_material_code
Revises: 0017_admin_layers
Create Date: 2026-08-19

Revision ``0016_material_stock`` built the Material Master and the stock
position on Company + Plant + Storage Location + Material Group, because that is
what the supplied structure listed. It was incomplete: the source carries a
**Material Code**, and a material group is a classification of many materials,
not an identifier of one. Everything ``0016`` said about the *grain* stopping at
the group follows from that omission and is corrected here.

**This is additive.** No column is dropped, no row is deleted, no measure
changes, and the four stock categories keep their meaning. ``fact_material_stock``
and ``stg_material_stock`` gain a column; ``dim_material_location`` gains a
column and a wider key.

**The key.** ``location_key`` becomes five segments —
``company|plant|storage_location|material_code|material_group`` — in the order
of the business hierarchy. Material Code is *inserted*, and Material Group is
kept, deliberately: rows already loaded are distinguished from one another only
by their material group, so dropping it from the key would collapse every group
in a storage location onto one key and violate the unique constraint on live
data.

**Rows loaded before this correction.** ``material_code`` is nullable for their
sake and for no other reason. No code can be derived for them — the Material
Master file that produced them did not contain one — and this project neither
invents data nor deletes it. Their key is rebuilt with an empty material-code
segment, which keeps them unique, visible in Data Management and searchable,
and makes them unmatchable by any stock position, since a position must state a
material code. Re-uploading the Material Master with codes inserts the correct
rows beside them; the count of incomplete rows is reported below so they can be
found rather than discovered later. The upload requires the column, so nothing
loaded from here on can be missing one.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_material_code"
down_revision: Union[str, None] = "0017_admin_layers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The reporting view, rebuilt to carry the material code.
#:
#: Based on the body ``0016_material_stock`` authored — the revision that last
#: wrote this view — with ``material_code`` added beside the other identifying
#: codes and nothing else changed. ``total_stock`` keeps its one definition: all
#: four categories including in transit, computed here so the dashboard, the
#: table, the export and the agent cannot drift apart on what it means.
MATERIAL_STOCK_DETAIL = """
SELECT
    f.material_stock_id,
    f.company_code,
    f.plant_code,
    m.plant_name,
    f.storage_location_code,
    m.storage_location_name,
    f.material_code,
    f.material_group_code,
    m.material_group_name,
    f.unrestricted_stock,
    f.quality_inspection_stock,
    f.blocked_stock,
    f.stock_in_transit,
    (COALESCE(f.unrestricted_stock, 0)
     + COALESCE(f.quality_inspection_stock, 0)
     + COALESCE(f.blocked_stock, 0)
     + COALESCE(f.stock_in_transit, 0)) AS total_stock,
    f.production_date,
    f.shelf_life_expiration_date,
    f.source_system,
    f.import_batch_id,
    f.is_void
FROM fact_material_stock f
LEFT JOIN dim_material_location m
       ON m.material_location_id = f.material_location_id
WHERE f.is_void = FALSE
"""

#: The 0016 view body, for the downgrade. Kept verbatim rather than derived from
#: the one above, so a downgrade restores exactly what the earlier revision
#: authored instead of an approximation of it.
MATERIAL_STOCK_DETAIL_0016 = """
SELECT
    f.material_stock_id,
    f.company_code,
    f.plant_code,
    m.plant_name,
    f.storage_location_code,
    m.storage_location_name,
    f.material_group_code,
    m.material_group_name,
    f.unrestricted_stock,
    f.quality_inspection_stock,
    f.blocked_stock,
    f.stock_in_transit,
    (COALESCE(f.unrestricted_stock, 0)
     + COALESCE(f.quality_inspection_stock, 0)
     + COALESCE(f.blocked_stock, 0)
     + COALESCE(f.stock_in_transit, 0)) AS total_stock,
    f.production_date,
    f.shelf_life_expiration_date,
    f.source_system,
    f.import_batch_id,
    f.is_void
FROM fact_material_stock f
LEFT JOIN dim_material_location m
       ON m.material_location_id = f.material_location_id
WHERE f.is_void = FALSE
"""


def upgrade() -> None:
    bind = op.get_bind()

    # --- The master ------------------------------------------------------
    op.add_column("dim_material_location",
                  sa.Column("material_code", sa.String(64), nullable=True))
    op.create_index("ix_dim_material_location_material", "dim_material_location",
                    ["material_code"])

    # Every existing key gains its empty material-code segment, in position.
    # Written as one statement rather than row by row: the separator and the
    # order are the same for every row, and the segment is empty for all of
    # them because none has a code to put there.
    #
    # ``company|plant|storage|group`` becomes ``company|plant|storage||group``:
    # the group moves one place right and the material's place is left blank,
    # which is what makes these rows identifiable and unmatchable rather than
    # quietly wrong.
    op.execute(
        "UPDATE dim_material_location "
        "SET location_key = company_code || '|' || plant_code || '|' "
        "                   || storage_location_code || '||' || material_group_code "
        "WHERE material_code IS NULL"
    )

    incomplete = bind.execute(sa.text(
        "SELECT count(*) FROM dim_material_location WHERE material_code IS NULL"
    )).scalar() or 0
    if incomplete:
        # Reported, never fixed up. Printing is the only channel a migration
        # has to the operator running it, and these rows are the one thing here
        # that needs a human decision — re-upload the Material Master with
        # codes, and these become inert history beside the corrected rows.
        print(
            f"  NOTE: {incomplete} Material Master row(s) carry no Material Code. "
            "They pre-date this correction and no code can be derived for them, "
            "so none was invented. Find them in Data Management -> Material "
            "(blank Material Code), or with:\n"
            "    SELECT * FROM dim_material_location WHERE material_code IS NULL;\n"
            "  Re-upload the Material Master with Material Code to load the "
            "corrected records. Stock positions cannot resolve to these rows."
        )

    # --- Staging and the fact -------------------------------------------
    #
    # Both are nullable with no default: staging holds the file's text as it
    # arrived, and the fact table is empty on every database this runs against
    # (revision 0016 created it and no stock file has been imported). A server
    # default of '' would be an invented material code the moment one were.
    op.add_column("stg_material_stock",
                  sa.Column("material_code", sa.String(64), nullable=True))
    op.add_column("fact_material_stock",
                  sa.Column("material_code", sa.String(64), nullable=True))
    op.create_index("ix_fact_material_stock_material", "fact_material_stock",
                    ["material_code"])

    op.execute("DROP VIEW IF EXISTS vw_material_stock_detail")
    op.execute(f"CREATE VIEW vw_material_stock_detail AS {MATERIAL_STOCK_DETAIL}")


def downgrade() -> None:
    """Undo the column, and put the four-part key back.

    Reversible only while no material code has actually been recorded. Once one
    has, dropping the column would destroy data that cannot be reconstructed
    from what remains — and two materials in the same storage location and group
    would collapse onto one key besides — so this refuses rather than discards,
    the same rule ``0016_material_stock`` applies to the tables it replaced.
    """
    bind = op.get_bind()
    for table in ("dim_material_location", "fact_material_stock", "stg_material_stock"):
        rows = bind.execute(sa.text(
            f"SELECT count(*) FROM {table} WHERE material_code IS NOT NULL"
        )).scalar() or 0
        if rows:
            raise RuntimeError(
                f"{table} holds {rows} row(s) with a Material Code. Downgrading "
                "would drop the column and destroy them, and would collapse two "
                "materials in one storage location onto a single key. Export "
                "those rows first if the downgrade is genuinely wanted."
            )

    op.execute("DROP VIEW IF EXISTS vw_material_stock_detail")
    op.execute(f"CREATE VIEW vw_material_stock_detail AS {MATERIAL_STOCK_DETAIL_0016}")

    op.drop_index("ix_fact_material_stock_material", table_name="fact_material_stock")
    op.drop_column("fact_material_stock", "material_code")
    op.drop_column("stg_material_stock", "material_code")

    op.drop_index("ix_dim_material_location_material",
                  table_name="dim_material_location")
    op.execute(
        "UPDATE dim_material_location "
        "SET location_key = company_code || '|' || plant_code || '|' "
        "                   || storage_location_code || '|' || material_group_code"
    )
    op.drop_column("dim_material_location", "material_code")
