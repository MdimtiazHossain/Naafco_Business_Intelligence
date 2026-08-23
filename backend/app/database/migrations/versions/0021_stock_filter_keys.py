"""Storage location key on the material stock view.

Revision ID: 0021_stock_filter_keys
Revises: 0020_remove_receivables_and_warehouse
Create Date: 2026-08-21

``vw_material_stock_detail`` exposed ``storage_location_code`` and no key. That
code is unique only *within a plant* — which is precisely why
:func:`models.storage_location_key` exists and why ``dim_storage_location``
stores the pair joined — so every reader that treated the bare code as an
identity was wrong in the same two ways:

* **Grouping.** "Stock by Storage Location" aggregated ``FG01`` at one plant
  together with ``FG01`` at another. Where the two carried different names the
  report split them into rows sharing one code; where the names matched it
  merged genuinely separate locations into a single figure.
* **Filtering.** A storage-location filter built on the code could not say
  *which* ``FG01`` was meant, so it could never narrow to one location.

This revision adds ``storage_location_key`` to the view so both have a real
identity to work with. The code stays — it is what the source file states and
what an operator reads on a stock sheet — and the name stays, because it is
still the location's own name.

It also adds ``storage_location_label``, which is the name qualified by the
plant that owns it. Once a breakdown groups by the key rather than the code it
returns one row per real location, and 40 of those rows are called "Finished
Goods"; a label that cannot tell them apart would make the corrected report less
readable than the wrong one. The concatenation lives here, in the view, for the
same reason ``total_stock`` does: it is derived from columns this view already
carries, and computing it once means every reader — page, agent and export —
shows a location the same way. ``plant_name`` falls back to ``plant_code``
because the join is an outer one and a plant master row can be missing.

**View only.** No table is created, altered or rewritten, and no row is read or
written, so the SQLite view-capture dance that ``0020`` needed does not apply
here: there is no table rewrite for a dependent view to fail against. The
downgrade restores 0019's body verbatim.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0021_stock_filter_keys"
down_revision: Union[str, None] = "0020_remove_receivables_and_warehouse"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: 0019's body plus ``s.storage_location_key``. Kept as a whole statement rather
#: than assembled from the previous one, so what this revision creates is
#: readable here in full — the rule the migrations in this package follow.
MATERIAL_STOCK_DETAIL = """
SELECT
    f.material_stock_id,
    f.company_code,
    f.plant_code,
    p.plant_name,
    f.storage_location_code,
    s.storage_location_key,
    s.storage_location_name,
    (s.storage_location_name || ' - ' || COALESCE(p.plant_name, f.plant_code))
        AS storage_location_label,
    f.material_code,
    m.material_description,
    f.material_group_code,
    m.material_group_name,
    f.material_brand_code,
    m.material_brand,
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
LEFT JOIN dim_plant p
       ON p.plant_id = f.plant_id
LEFT JOIN dim_storage_location s
       ON s.storage_location_id = f.storage_location_id
LEFT JOIN dim_material m
       ON m.material_id = f.material_id
WHERE f.is_void = 0
"""

#: The 0019 body, for the downgrade. Verbatim rather than derived from the one
#: above, so a downgrade restores exactly what that revision authored.
MATERIAL_STOCK_DETAIL_0019 = """
SELECT
    f.material_stock_id,
    f.company_code,
    f.plant_code,
    p.plant_name,
    f.storage_location_code,
    s.storage_location_name,
    f.material_code,
    m.material_description,
    f.material_group_code,
    m.material_group_name,
    f.material_brand_code,
    m.material_brand,
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
LEFT JOIN dim_plant p
       ON p.plant_id = f.plant_id
LEFT JOIN dim_storage_location s
       ON s.storage_location_id = f.storage_location_id
LEFT JOIN dim_material m
       ON m.material_id = f.material_id
WHERE f.is_void = 0
"""


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS vw_material_stock_detail")
    op.execute(f"CREATE VIEW vw_material_stock_detail AS {MATERIAL_STOCK_DETAIL}")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS vw_material_stock_detail")
    op.execute(f"CREATE VIEW vw_material_stock_detail AS {MATERIAL_STOCK_DETAIL_0019}")
