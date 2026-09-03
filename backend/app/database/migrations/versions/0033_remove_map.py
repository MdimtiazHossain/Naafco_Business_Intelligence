"""0033_remove_map — remove the business map, keeping the geography it borrowed.

The map is being rebuilt from nothing, so its tables go rather than being
carried forward half-used. Eleven tables are dropped: the marker library
(``0007``), entity coordinates (``0008``), area boundaries and styles
(``0010``), administrative points (``0017``) and the composition tables
(``0032``).

**What deliberately stays.** ``dim_country``, ``dim_division``, ``dim_district``
and ``dim_upazila`` are *master data*, not map furniture. Each has its own
upload template in ``upload.registry``, its own Bangla name column and its own
parent link, and the Upload Centre and Data Management both offer them
independently of anything that draws a map. The map consumed them; it never
owned them, and a rebuild will want them already loaded. Their **geometry**
does go — a boundary polygon and a label point exist only to be drawn — and
both are re-importable from the published GADM/HDX release.

**This revision destroys data, which is the instruction it is carrying out.**
1,397 entity coordinates, 6,284 administrative points and 580 boundary rings on
the deployment database (165 / 6,284 / 580 on the development one) are dropped
without being exported first. That was asked for explicitly. It is the reason
``data/dev.db.pre0033.bak`` exists beside this revision, and the reason the
docstring says so rather than leaving it to be discovered.

Also removed: the ``map`` and ``map_settings`` rows in the two permission
tables. A permission naming a section the application no longer declares grants
nothing and denies nothing; leaving it is the "a list outliving what it names"
failure this codebase has been bitten by before, in the one place where the
list is rows rather than code.

No surviving table references any of these — every foreign key into them came
from another map table — so nothing outside this set changes, and the SQLite
view-capture dance ``0020`` and ``0022`` needed does not apply here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_remove_map"
down_revision = "0032_map_composition"
branch_labels = None
depends_on = None

#: Dropped children first: every foreign key among these is internal to the set,
#: so the order is the only thing that makes the drop work on a database that
#: enforces them.
_DROP_ORDER = (
    "map_point_configurations",
    "map_layers",
    "map_designs",
    "map_marker_assignments",
    "map_marker_design_versions",
    "map_marker_designs",
    "map_marker_assets",
    "map_entity_locations",
    "map_area_boundaries",
    "map_area_styles",
    "map_admin_points",
)

#: Permission rows for sections this revision's application no longer declares.
_DEAD_SECTIONS = ("map", "map_settings")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    present = set(inspector.get_table_names())

    for table in _DROP_ORDER:
        if table in present:
            op.drop_table(table)

    # Inert configuration, not business data: a permission naming a section that
    # no longer exists can neither grant nor deny. Guarded on the table being
    # there so a database that predates either one still migrates.
    for table in ("role_section_permissions", "user_section_permissions"):
        if table in present:
            op.execute(
                sa.text(
                    f"DELETE FROM {table} WHERE section_key IN (:a, :b)"
                ).bindparams(a=_DEAD_SECTIONS[0], b=_DEAD_SECTIONS[1])
            )


def downgrade() -> None:
    """Rebuild the eleven tables, empty, and leave them empty.

    The upgrade moved no rows anywhere, so there is nowhere to move them back
    from — and inventing 1,397 coordinates or 580 boundary rings would be
    precisely the fabrication this platform refuses everywhere else. A
    downgrade restores the *shape* so the revision chain stays walkable; to get
    the data back, restore ``data/dev.db.pre0033.bak`` or re-run
    ``scripts/import_admin_areas.py`` against the published release.

    The dropped permission rows are not restored either: which roles held the
    map sections is not derivable from anything that survives.
    """
    op.create_table(
        "map_marker_assets",
        sa.Column("asset_id", sa.BigInteger(), primary_key=True),
        sa.Column("asset_uuid", sa.String(length=36), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sanitised_report", sa.JSON()),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("uploaded_by", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("asset_uuid"),
    )
    op.create_table(
        "map_marker_designs",
        sa.Column("design_id", sa.BigInteger(), primary_key=True),
        sa.Column("design_uuid", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("design_type", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("asset_id", sa.BigInteger(), sa.ForeignKey("map_marker_assets.asset_id")),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_system_default", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=64)),
        sa.Column("updated_by", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("design_uuid"),
    )
    op.create_table(
        "map_marker_design_versions",
        sa.Column("version_id", sa.BigInteger(), primary_key=True),
        sa.Column("design_id", sa.BigInteger(), sa.ForeignKey("map_marker_designs.design_id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("design_type", sa.String(length=24), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("asset_id", sa.BigInteger()),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("created_by", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("design_id", "version"),
    )
    op.create_table(
        "map_marker_assignments",
        sa.Column("assignment_id", sa.BigInteger(), primary_key=True),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_code", sa.String(length=64)),
        sa.Column("design_id", sa.BigInteger(), sa.ForeignKey("map_marker_designs.design_id"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("condition", sa.JSON()),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("entity_type", "entity_code"),
    )
    op.create_table(
        "map_entity_locations",
        sa.Column("location_id", sa.BigInteger(), primary_key=True),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_code", sa.String(length=64), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("precision", sa.String(length=16), nullable=False),
        sa.Column("label", sa.Text()),
        sa.Column("derived_from", sa.Integer()),
        sa.Column("updated_by", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("entity_type", "entity_code"),
    )
    op.create_table(
        "map_area_boundaries",
        sa.Column("boundary_id", sa.BigInteger(), primary_key=True),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_code", sa.String(length=64), nullable=False),
        sa.Column("geometry", sa.JSON(), nullable=False),
        sa.Column("bbox_north", sa.Float(), nullable=False),
        sa.Column("bbox_south", sa.Float(), nullable=False),
        sa.Column("bbox_east", sa.Float(), nullable=False),
        sa.Column("bbox_west", sa.Float(), nullable=False),
        sa.Column("centroid_latitude", sa.Float(), nullable=False),
        sa.Column("centroid_longitude", sa.Float(), nullable=False),
        sa.Column("point_count", sa.Integer(), nullable=False),
        sa.Column("source_point_count", sa.Integer()),
        sa.Column("simplify_tolerance", sa.Float()),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("source_file", sa.Text()),
        sa.Column("imported_by", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("entity_type", "entity_code"),
    )
    op.create_table(
        "map_area_styles",
        sa.Column("style_id", sa.BigInteger(), primary_key=True),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("fill_color", sa.String(length=16), nullable=False),
        sa.Column("fill_opacity", sa.Float(), nullable=False),
        sa.Column("stroke_color", sa.String(length=16), nullable=False),
        sa.Column("stroke_opacity", sa.Float(), nullable=False),
        sa.Column("stroke_width", sa.Float(), nullable=False),
        sa.Column("hover_fill_color", sa.String(length=16)),
        sa.Column("hover_fill_opacity", sa.Float()),
        sa.Column("selected_fill_color", sa.String(length=16)),
        sa.Column("selected_fill_opacity", sa.Float()),
        sa.Column("z_index", sa.Integer(), nullable=False),
        sa.Column("rules", sa.JSON()),
        sa.Column("is_system_default", sa.Boolean(), nullable=False),
        sa.Column("updated_by", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("entity_type"),
    )
    op.create_table(
        "map_admin_points",
        sa.Column("point_id", sa.BigInteger(), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("admin_level", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_bn", sa.Text()),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("country_code", sa.String(length=64)),
        sa.Column("division_code", sa.String(length=64)),
        sa.Column("district_code", sa.String(length=64)),
        sa.Column("upazila_code", sa.String(length=64)),
        sa.Column("source_file", sa.Text()),
        sa.Column("imported_by", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("kind", "admin_level", "latitude", "longitude"),
    )
    op.create_table(
        "map_designs",
        sa.Column("design_id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=512)),
        sa.Column("basemap", sa.String(length=32), nullable=False),
        sa.Column("default_metric", sa.String(length=32), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_system_default", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "map_layers",
        sa.Column("layer_id", sa.BigInteger(), primary_key=True),
        sa.Column("design_id", sa.BigInteger(), sa.ForeignKey("map_designs.design_id"), nullable=False),
        sa.Column("layer_name", sa.String(length=128), nullable=False),
        sa.Column("point_level", sa.String(length=32), nullable=False),
        sa.Column("metric", sa.String(length=32)),
        sa.Column("color_metric", sa.String(length=32)),
        sa.Column("size_metric", sa.String(length=32)),
        sa.Column("marker_design_id", sa.BigInteger(), sa.ForeignKey("map_marker_designs.design_id")),
        sa.Column("is_visible", sa.Boolean(), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("min_zoom", sa.Integer(), nullable=False),
        sa.Column("cluster_at", sa.Integer()),
        sa.Column("configuration_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("design_id", "point_level"),
    )
    op.create_table(
        "map_point_configurations",
        sa.Column("config_id", sa.BigInteger(), primary_key=True),
        sa.Column("layer_id", sa.BigInteger(), sa.ForeignKey("map_layers.layer_id"), nullable=False),
        sa.Column("label_field", sa.String(length=64)),
        sa.Column("show_label", sa.Boolean(), nullable=False),
        sa.Column("label_min_zoom", sa.Integer(), nullable=False),
        sa.Column("tooltip_fields", sa.JSON()),
        sa.Column("style_config", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("layer_id"),
    )

    # The indexes matter as much as the tables. An earlier revision's own
    # downgrade drops the index it created, so a chain walked all the way back
    # fails on the first DROP INDEX if this recreation left them out — which is
    # exactly what test_migration_downgrade_removes_every_table caught.
    op.create_index("ix_map_marker_assets_checksum", "map_marker_assets", ["checksum"])
    op.create_index("ix_map_marker_designs_entity_status", "map_marker_designs", ["entity_type", "status"])
    op.create_index("ix_map_marker_designs_entity_type", "map_marker_designs", ["entity_type"])
    op.create_index("ix_map_marker_designs_status", "map_marker_designs", ["status"])
    op.create_index("ix_map_marker_design_versions_design_id", "map_marker_design_versions", ["design_id"])
    op.create_index("ix_map_marker_assignments_design_id", "map_marker_assignments", ["design_id"])
    op.create_index("ix_map_marker_assignments_entity_type", "map_marker_assignments", ["entity_type"])
    op.create_index("ix_map_entity_locations_entity_type", "map_entity_locations", ["entity_type"])
    op.create_index("ix_map_area_boundaries_bbox", "map_area_boundaries", ["entity_type", "bbox_south", "bbox_north"])
    op.create_index("ix_map_area_boundaries_entity_type", "map_area_boundaries", ["entity_type"])
    op.create_index("ix_map_area_styles_entity_type", "map_area_styles", ["entity_type"])
    op.create_index("ix_map_admin_points_bbox", "map_admin_points", ["kind", "latitude", "longitude"])
    op.create_index("ix_map_admin_points_kind_level", "map_admin_points", ["kind", "admin_level"])
    op.create_index("ix_map_designs_active", "map_designs", ["is_active"])
    op.create_index("ix_map_designs_default", "map_designs", ["is_default"])
    op.create_index("ix_map_layers_design", "map_layers", ["design_id"])
    op.create_index("ix_map_layers_order", "map_layers", ["design_id", "display_order"])
