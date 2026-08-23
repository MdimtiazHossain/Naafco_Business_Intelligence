"""What each transactional data type exposes: columns, search, view, section.

These four maps were defined inside ``api.routes_pages``, which made every
consumer of them import an HTTP module. That is the wrong direction — a service
reaching into the routing layer — and it closed an import cycle the moment
anything but ``app.main`` imported the data-management catalogue first.

They are definitions, not routing, so they live here and both the transaction
report endpoint and the data-management catalogue read them from one place.

The column tuples are also a **whitelist**. Sorting and searching are checked
against them, so no value from a client ever becomes a column name.
"""

from __future__ import annotations

from ..ai import queries as q
from ..security.sections import SectionKey

#: Columns each transaction table exposes, in display order.
TRANSACTION_COLUMNS: dict[str, tuple[str, ...]] = {
    # Quantity, volume and net sales are the reported measures. gross_sales and
    # gross_profit remain on the fact row and are still derived by the ETL; the
    # transaction table simply does not put them on screen.
    #
    # ``volume`` stands alone with no unit column beside it: the source states
    # one Total Volume per line and nothing qualifies it.
    "sales": ("full_date", "invoice_no", "invoice_line_no", "batch_code",
              "customer_code", "customer_name", "material_code", "material_description",
              "material_brand", "material_group_name",
              "quantity", "volume",
              "discount", "net_sales",
              "region_name", "territory_name", "sales_force_code", "source_system"),
    # Material stock is a position, not a movement: the four categories as they
    # stand, the master names they resolve to, and the two dates that belong to
    # the goods. No opening/closing balance, because the source states none.
    "material_stock": ("company_code", "plant_code", "plant_name",
                       "storage_location_code", "storage_location_name",
                       "material_code", "material_description",
                       "material_group_code", "material_group_name",
                       "material_brand_code", "material_brand",
                       "unrestricted_stock", "quality_inspection_stock",
                       "blocked_stock", "stock_in_transit", "total_stock",
                       "production_date", "shelf_life_expiration_date",
                       "source_system"),
    # The columns a target is stated in, plus the names its codes resolve to.
    # No ``target_volume_unit``: it was the SKU's pack unit from the master
    # revision 0022 removed, and the Material Master states no unit of measure,
    # so a planned volume is the unit-free number the planner typed — exactly
    # like the ``volume`` on a sales line above.
    "target": ("target_month", "financial_year", "territory_name",
               "sub_territory_name", "customer_code", "customer_name",
               "material_code", "material_description",
               "material_brand", "material_group_name",
               "sales_force_code",
               "target_quantity", "target_volume", "target_amount",
               "region_name", "source_system"),
}

#: Columns a free-text search looks in. Deliberately the identifiers and names,
#: not every column: a substring match against an amount is meaningless.
SEARCH_COLUMNS: dict[str, tuple[str, ...]] = {
    "sales": ("invoice_no", "invoice_line_no", "batch_code", "customer_code", "customer_name",
              "material_code", "material_description", "material_brand"),
    "material_stock": ("plant_code", "plant_name", "storage_location_code",
                       "storage_location_name", "material_code",
                       "material_description",
                       "material_group_code", "material_group_name",
                       "material_brand_code", "material_brand"),
    "target": ("target_month", "financial_year", "customer_code", "customer_name",
               "material_code", "material_description", "material_brand",
               "sales_force_code"),
}

#: The reporting view each data type reads.
VIEW_BY_TYPE: dict[str, str] = {
    "sales": q.SALES_VIEW,
    "material_stock": q.MATERIAL_STOCK_VIEW,
    "target": q.TARGET_VIEW,
}

#: The section that governs each data type. One route serves three sections, so
#: a user denied Stock must be refused stock rows wherever they ask for them.
SECTION_BY_TYPE: dict[str, str] = {
    "sales": SectionKey.SALES,
    "material_stock": SectionKey.STOCK,
    "target": SectionKey.TARGET,
}

DATA_TYPES: tuple[str, ...] = tuple(TRANSACTION_COLUMNS)


__all__ = [
    "TRANSACTION_COLUMNS",
    "SEARCH_COLUMNS",
    "VIEW_BY_TYPE",
    "SECTION_BY_TYPE",
    "DATA_TYPES",
]
