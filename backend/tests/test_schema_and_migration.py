"""Schema consistency, migration correctness and generated reports."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, event, inspect as sa_inspect

from app.database.models import MODEL_BY_TABLE, Base
from app.master_data.inspector import inspect_workbook
from app.master_data.profiler import profile_workbook
from app.master_data.schema import HIERARCHY, TABLE_SPECS, FieldKind
from app.utils.reporting import build_data_dictionary, build_er_diagram

BACKEND_DIR = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


#: Columns every dimension carries that the workbook knows nothing about: the
#: audit timestamps, and the soft-delete trio the data-management module needs
#: so a record can be retired without orphaning the facts that reference it.
PLATFORM_COLUMNS = {"created_at", "updated_at", "is_deleted", "deleted_at",
                    "deleted_by"}

#: Columns a dimension carries that come from a *different* source than its own
#: workbook sheet. Named per table, one at a time, so adding one stays a
#: deliberate act rather than something a loosened assertion would let through.
#:
#: ``dim_product.material_code`` (0019) is the bridge to the Material Master. It
#: is not in the workbook because no workbook states it: it is loaded from the
#: optional ``SKU Code`` column of the Material Master upload, and is never
#: derived here.
CROSS_SOURCE_COLUMNS = {"dim_product": {"material_code"}}


def test_every_spec_has_a_model_with_matching_columns() -> None:
    """The model is the workbook's columns and nothing but — plus the platform's.

    The point of the assertion is that no *business* column is invented or
    dropped, so the platform's own bookkeeping, and the one column loaded from
    another master, are named explicitly rather than the comparison being
    loosened to a subset.
    """
    for spec in TABLE_SPECS:
        model = MODEL_BY_TABLE[spec.table]
        model_columns = set(model.__table__.columns.keys())
        expected = {spec.surrogate_key, *PLATFORM_COLUMNS}
        expected |= {c.column for c in spec.columns}
        expected |= CROSS_SOURCE_COLUMNS.get(spec.table, set())
        assert model_columns == expected, spec.table


def test_business_keys_are_unique_and_indexed() -> None:
    for spec in TABLE_SPECS:
        model = MODEL_BY_TABLE[spec.table]
        column = model.__table__.columns[spec.business_key]
        assert column.unique, f"{spec.table}.{spec.business_key} must be UNIQUE"
        assert not column.nullable
        assert spec.business_key in spec.indexed_columns


def test_codes_and_phones_are_never_numeric_types() -> None:
    for spec in TABLE_SPECS:
        for col in spec.columns:
            if col.kind in (FieldKind.CODE, FieldKind.PHONE):
                assert "CHAR" in col.sql_type.upper() or "TEXT" in col.sql_type.upper(), \
                    f"{spec.table}.{col.column} must be a text type"


def test_foreign_keys_follow_the_hierarchy() -> None:
    expected_chain = list(zip(HIERARCHY, HIERARCHY[1:]))
    actual = [
        (s.parent_table, s.table) for s in TABLE_SPECS if s.has_foreign_key()
    ]
    assert actual == expected_chain


def test_the_workbook_specs_stop_at_the_organisational_hierarchy() -> None:
    """No Product Master spec, and no item master in this workbook at all.

    ``dim_product`` was the one independent dimension these specs described.
    Revision 0022 removed it, and the Material Master that replaced it is not a
    sheet here — it arrives from its own SAP-side extract and is declared in
    ``app.upload.registry`` beside the Plant and Storage Location masters.
    """
    assert "dim_product" not in {s.table for s in TABLE_SPECS}
    assert "dim_product" not in MODEL_BY_TABLE
    assert "dim_material" not in {s.table for s in TABLE_SPECS}
    assert "dim_material" in MODEL_BY_TABLE


def test_all_columns_are_snake_case() -> None:
    for spec in TABLE_SPECS:
        for name in [spec.surrogate_key, *(c.column for c in spec.columns)]:
            assert name == name.lower()
            assert " " not in name and "-" not in name


def test_source_fields_are_unique_within_a_sheet() -> None:
    for spec in TABLE_SPECS:
        fields = [c.source_field for c in spec.columns]
        assert len(fields) == len(set(fields)), spec.sheet


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "app" / "database" / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_migration_creates_a_schema_matching_the_models(tmp_path, monkeypatch) -> None:
    """`alembic upgrade head` must produce exactly the models' schema."""
    db_path = tmp_path / "migrated.db"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)

    command.upgrade(_alembic_config(url), "head")

    engine = create_engine(url, future=True)
    inspector = sa_inspect(engine)
    assert set(inspector.get_table_names()) == set(Base.metadata.tables) | {"alembic_version"}

    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diffs = compare_metadata(context, Base.metadata)
    # alembic_version is not part of the model metadata; ignore that one entry.
    diffs = [d for d in diffs if "alembic_version" not in str(d)]
    assert diffs == [], diffs
    engine.dispose()


def test_migration_downgrade_removes_every_table(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "down.db"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = _alembic_config(url)

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(url, future=True)
    remaining = set(sa_inspect(engine).get_table_names()) - {"alembic_version"}
    assert remaining == set()
    engine.dispose()


def test_indexes_exist_on_every_business_code(tmp_path, monkeypatch) -> None:
    url = f"sqlite:///{tmp_path / 'idx.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(_alembic_config(url), "head")

    engine = create_engine(url, future=True)
    inspector = sa_inspect(engine)
    for spec in TABLE_SPECS:
        indexed = {
            column
            for index in inspector.get_indexes(spec.table)
            for column in index["column_names"]
        }
        assert set(spec.indexed_columns) <= indexed, spec.table
    engine.dispose()


# --------------------------------------------------------------------------
# Generated reports
# --------------------------------------------------------------------------


def test_er_diagram_covers_the_whole_model() -> None:
    diagram = build_er_diagram()
    assert diagram.startswith("erDiagram")
    assert "DIM_COMPANY ||--o{ DIM_BUSINESS_UNIT : contains" in diagram
    assert "DIM_TERRITORY ||--o{ DIM_SUB_TERRITORY : contains" in diagram
    # No Product anywhere: the diagram is built from TABLE_SPECS, and revision
    # 0022 removed that spec along with the master.
    assert "DIM_PRODUCT" not in diagram
    for spec in TABLE_SPECS:
        assert f"{spec.table.upper()} {{" in diagram


def test_data_dictionary_documents_every_table_and_column(real_workbook_path) -> None:
    profile = profile_workbook(inspect_workbook(real_workbook_path))
    dictionary = build_data_dictionary(profile)

    for spec in TABLE_SPECS:
        assert f"## `{spec.table}`" in dictionary
        for col in spec.columns:
            assert f"`{col.column}`" in dictionary
    assert "erDiagram" in dictionary
    assert "Validation rules" in dictionary


def test_profile_workbook_is_written(tmp_path, real_workbook_path) -> None:
    from openpyxl import load_workbook

    from app.utils.reporting import write_profile_workbook

    profile = profile_workbook(inspect_workbook(real_workbook_path))
    path = write_profile_workbook(tmp_path / "profile.xlsx", profile)
    wb = load_workbook(path)
    assert "Overview" in wb.sheetnames
    assert "All Columns" in wb.sheetnames
    assert "Product Master" in wb.sheetnames


def test_profiler_reports_nulls_and_uniques(full_workbook) -> None:
    path = full_workbook({
        "Company Master": [
            ["Company Code", "Company", "Company Head ID", "Company Head Name"],
            ["C001", "Alpha Ltd.", "EMP001", "Head One"],
            ["C002", "Beta Ltd.", None, "Head Two"],
            ["C003", "Gamma Ltd.", "EMP001", "Head Three"],
        ],
    })
    profile = profile_workbook(inspect_workbook(path))
    company = next(s for s in profile.sheets if s.sheet_name == "Company Master")
    by_name = {c.column_name: c for c in company.columns}

    assert company.record_count == 3
    assert by_name["Company Code"].unique_count == 3
    assert by_name["Company Code"].null_records == 0
    head = by_name["Company Head ID"]
    assert head.null_records == 1
    assert head.null_percent == pytest.approx(33.33, abs=0.01)
    assert head.unique_count == 1
    assert head.duplicate_count == 1
