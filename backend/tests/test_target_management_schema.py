"""Schema invariants of the Target Management tables (revision 0027).

Checked against a database built by ``alembic upgrade head`` rather than by
``create_all``, so what is asserted is what a real deployment gets — including
the seeded approval matrix, which ``create_all`` would not produce at all.

The constraints here are the ones later steps rely on being enforced by the
*database* rather than by whichever code happens to write to it. Two matter
more than the rest:

* only one version of a plan can be current, which is what stops a report and
  an approval disagreeing about which V3 they mean;
* one plan per scope, which is what makes "the FY 2026-27 Q1 target for SL001"
  name one thing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect as sa_inspect, text
from sqlalchemy.exc import IntegrityError

from app.database.models_target import DEFAULT_APPROVAL_MATRIX, TargetLevel

BACKEND_DIR = Path(__file__).resolve().parents[1]

TARGET_TABLES = (
    "target_plan",
    "target_version",
    "target_country_line",
    "target_allocation",
    "target_revision",
    "target_approval",
    "target_approval_matrix",
    "target_audit",
)


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option(
        "script_location", str(BACKEND_DIR / "app" / "database" / "migrations")
    )
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture()
def migrated_engine(tmp_path, monkeypatch):
    """A throwaway SQLite database at head, with foreign keys enforced.

    ``PRAGMA foreign_keys=ON`` matters: without it SQLite accepts a foreign key
    pointing at nothing, and every ON DELETE assertion below would pass
    vacuously.
    """
    url = f"sqlite:///{tmp_path / 'targets.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(_alembic_config(url), "head")

    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    try:
        yield engine
    finally:
        engine.dispose()


def _plan(connection, code: str = "TP-2026-001", period: str = "Q1",
          sales_line: str = "SL001") -> int:
    connection.execute(
        text(
            "INSERT INTO target_plan (plan_code, financial_year, target_period, "
            "company_code, bu_code, sales_line_code, status) VALUES "
            "(:code, 'FY 2026-27', :period, 'C001', 'BU001', :line, 'DRAFT')"
        ),
        {"code": code, "period": period, "line": sales_line},
    )
    return connection.execute(
        text("SELECT plan_id FROM target_plan WHERE plan_code = :code"),
        {"code": code},
    ).scalar_one()


def _version(connection, plan_id: int, version_no: int, *,
             current: bool) -> int:
    connection.execute(
        text(
            "INSERT INTO target_version (plan_id, version_no, status, "
            "current_plan_id) VALUES (:plan, :no, 'DRAFT', :current)"
        ),
        {"plan": plan_id, "no": version_no,
         "current": plan_id if current else None},
    )
    return connection.execute(
        text("SELECT version_id FROM target_version WHERE plan_id = :plan "
             "AND version_no = :no"),
        {"plan": plan_id, "no": version_no},
    ).scalar_one()


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_every_target_table_is_created(migrated_engine) -> None:
    present = set(sa_inspect(migrated_engine).get_table_names())
    assert set(TARGET_TABLES) <= present


def test_the_migration_adds_the_two_derivation_inputs(migrated_engine) -> None:
    """``dim_material`` gains a conversion factor and a transfer price."""
    columns = {
        column["name"]: column
        for column in sa_inspect(migrated_engine).get_columns("dim_material")
    }
    assert "conversion_factor" in columns
    assert "transfer_price" in columns


def test_neither_derivation_input_is_defaulted(migrated_engine) -> None:
    """Both stay NULL until a Material Master file states them.

    The whole reason these columns are nullable. A default of 1.0 would not read
    as "unknown" — it would read as "one volume unit per saleable unit", which
    is a claim about the goods, and the derived quantity and value would be
    confidently wrong rather than absent.
    """
    columns = {
        column["name"]: column
        for column in sa_inspect(migrated_engine).get_columns("dim_material")
    }
    for name in ("conversion_factor", "transfer_price"):
        assert columns[name]["nullable"] is True, name
        assert columns[name]["default"] is None, name


# ---------------------------------------------------------------------------
# The seeded approval matrix
# ---------------------------------------------------------------------------


def test_the_approval_matrix_is_seeded(migrated_engine) -> None:
    """Configuration with no rows is a workflow nobody can approve anything in."""
    with migrated_engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT role, hierarchy_level, approval_sequence FROM "
            "target_approval_matrix ORDER BY role")).all()
    assert len(rows) == len(DEFAULT_APPROVAL_MATRIX)
    assert {row[0] for row in rows} == {entry[0] for entry in DEFAULT_APPROVAL_MATRIX}


def test_every_seeded_level_is_one_the_allocator_knows(migrated_engine) -> None:
    """A level name must not outlive what it names.

    The failure CLAUDE.md warns about, applied here before it can happen: a
    matrix row naming a level ``TargetLevel`` does not have would route a target
    to a node that cannot exist.
    """
    with migrated_engine.connect() as connection:
        levels = {
            level for (level,) in connection.execute(
                text("SELECT DISTINCT hierarchy_level FROM target_approval_matrix"))
        }
    assert levels <= set(TargetLevel.ORDERED)


def test_administrators_hold_no_approval_sequence(migrated_engine) -> None:
    """They configure the run and never sign off on a number.

    NULL, not zero: sitting outside the chain is a different thing from being
    first in it.
    """
    with migrated_engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT role, approval_sequence FROM target_approval_matrix "
            "WHERE role IN ('ADMIN', 'SUPER_ADMIN')")).all()
    assert len(rows) == 2
    assert all(sequence is None for _, sequence in rows)


def test_only_management_may_adjust_without_a_limit(migrated_engine) -> None:
    """NULL means unlimited; 0.00 means "may not change a figure at all"."""
    with migrated_engine.connect() as connection:
        unlimited = {
            role for (role,) in connection.execute(text(
                "SELECT role FROM target_approval_matrix "
                "WHERE adjustment_limit_percent IS NULL AND approval_sequence "
                "IS NOT NULL"))
        }
        zero = {
            role for (role,) in connection.execute(text(
                "SELECT role FROM target_approval_matrix "
                "WHERE adjustment_limit_percent = 0"))
        }
    assert unlimited == {"MANAGEMENT"}
    assert zero == {"SALES_OFFICER"}


# ---------------------------------------------------------------------------
# Uniqueness
# ---------------------------------------------------------------------------


def test_one_plan_per_scope(migrated_engine) -> None:
    with migrated_engine.begin() as connection:
        _plan(connection)
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            # Same financial year, period, company, business unit and sales
            # line: a different plan code does not make it a different plan.
            _plan(connection, code="TP-2026-002")


def test_a_different_period_is_a_different_plan(migrated_engine) -> None:
    with migrated_engine.begin() as connection:
        _plan(connection, code="TP-2026-001", period="Q1")
        _plan(connection, code="TP-2026-002", period="Q2")
    with migrated_engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM target_plan")).scalar_one() == 2


def test_only_one_version_of_a_plan_may_be_current(migrated_engine) -> None:
    with migrated_engine.begin() as connection:
        plan_id = _plan(connection)
        _version(connection, plan_id, 1, current=True)
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            _version(connection, plan_id, 2, current=True)


def test_any_number_of_versions_may_be_not_current(migrated_engine) -> None:
    """NULLs are distinct in a unique constraint on both dialects.

    This is what the ``current_plan_id`` device rests on: superseded versions
    pile up freely while at most one can claim the plan.
    """
    with migrated_engine.begin() as connection:
        plan_id = _plan(connection)
        for version_no in (1, 2, 3):
            _version(connection, plan_id, version_no, current=False)
        _version(connection, plan_id, 4, current=True)
    with migrated_engine.connect() as connection:
        assert connection.execute(text(
            "SELECT COUNT(*) FROM target_version")).scalar_one() == 4
        assert connection.execute(text(
            "SELECT COUNT(*) FROM target_version WHERE current_plan_id IS NOT NULL"
        )).scalar_one() == 1


def test_two_plans_may_each_have_a_current_version(migrated_engine) -> None:
    """The constraint is per plan, not global. It holds ``plan_id`` for a reason."""
    with migrated_engine.begin() as connection:
        first = _plan(connection, code="TP-2026-001", period="Q1")
        second = _plan(connection, code="TP-2026-002", period="Q2")
        _version(connection, first, 1, current=True)
        _version(connection, second, 1, current=True)
    with migrated_engine.connect() as connection:
        assert connection.execute(text(
            "SELECT COUNT(*) FROM target_version WHERE current_plan_id IS NOT NULL"
        )).scalar_one() == 2


def test_a_version_number_is_unique_within_its_plan(migrated_engine) -> None:
    with migrated_engine.begin() as connection:
        plan_id = _plan(connection)
        _version(connection, plan_id, 1, current=False)
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            _version(connection, plan_id, 1, current=False)


def test_one_country_line_per_material_per_version(migrated_engine) -> None:
    with migrated_engine.begin() as connection:
        plan_id = _plan(connection)
        version_id = _version(connection, plan_id, 1, current=True)
        connection.execute(
            text("INSERT INTO target_country_line (version_id, material_code, "
                 "target_volume) VALUES (:v, 'MAT-1', 100)"), {"v": version_id})
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            connection.execute(
                text("INSERT INTO target_country_line (version_id, "
                     "material_code, target_volume) VALUES (:v, 'MAT-1', 200)"),
                {"v": version_id})


# ---------------------------------------------------------------------------
# Referential behaviour
# ---------------------------------------------------------------------------


def test_a_plan_with_versions_cannot_be_deleted(migrated_engine) -> None:
    """RESTRICT: a plan carrying approvals is not something a delete takes with it."""
    with migrated_engine.begin() as connection:
        plan_id = _plan(connection)
        _version(connection, plan_id, 1, current=True)
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            connection.execute(text("DELETE FROM target_plan WHERE plan_id = :p"),
                               {"p": plan_id})


def test_deleting_a_version_takes_its_country_lines(migrated_engine) -> None:
    """CASCADE: a country line has no meaning apart from its version."""
    with migrated_engine.begin() as connection:
        plan_id = _plan(connection)
        version_id = _version(connection, plan_id, 1, current=True)
        connection.execute(
            text("INSERT INTO target_country_line (version_id, material_code, "
                 "target_volume) VALUES (:v, 'MAT-1', 100)"), {"v": version_id})
    with migrated_engine.begin() as connection:
        connection.execute(text("DELETE FROM target_version WHERE version_id = :v"),
                           {"v": version_id})
    with migrated_engine.connect() as connection:
        assert connection.execute(text(
            "SELECT COUNT(*) FROM target_country_line")).scalar_one() == 0


def test_the_audit_trail_outlives_the_version_it_describes(migrated_engine) -> None:
    """SET NULL, not CASCADE. Losing the link is acceptable; losing the record is not."""
    with migrated_engine.begin() as connection:
        plan_id = _plan(connection)
        version_id = _version(connection, plan_id, 1, current=True)
        connection.execute(
            text("INSERT INTO target_audit (plan_id, version_id, action, actor) "
                 "VALUES (:p, :v, 'PLAN_CREATED', 'root')"),
            {"p": plan_id, "v": version_id})
    with migrated_engine.begin() as connection:
        connection.execute(text("DELETE FROM target_version WHERE version_id = :v"),
                           {"v": version_id})
    with migrated_engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT action, version_id FROM target_audit")).all()
    assert rows == [("PLAN_CREATED", None)]


def test_a_revision_must_state_a_reason(migrated_engine) -> None:
    """NOT NULL in the schema, because an unexplained revision is the failure."""
    with migrated_engine.begin() as connection:
        plan_id = _plan(connection)
        version_id = _version(connection, plan_id, 1, current=True)
        connection.execute(
            text("INSERT INTO target_allocation (version_id, level, node_code, "
                 "material_code, target_month, system_volume, current_volume, "
                 "status) VALUES (:v, 'company', 'C001', 'MAT-1', '2026-07', "
                 "100, 100, 'ALLOCATED')"), {"v": version_id})
        allocation_id = connection.execute(text(
            "SELECT allocation_id FROM target_allocation")).scalar_one()
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            connection.execute(
                text("INSERT INTO target_revision (allocation_id, system_volume, "
                     "requested_volume, status) VALUES (:a, 100, 90, 'PENDING')"),
                {"a": allocation_id})


def test_an_allocation_row_is_unique_per_node_material_and_month(
        migrated_engine) -> None:
    """Every row is monthly, so a node total is a sum and cannot double-count."""
    with migrated_engine.begin() as connection:
        plan_id = _plan(connection)
        version_id = _version(connection, plan_id, 1, current=True)
        for month in ("2026-07", "2026-08"):
            connection.execute(
                text("INSERT INTO target_allocation (version_id, level, "
                     "node_code, material_code, target_month, system_volume, "
                     "current_volume, status) VALUES (:v, 'zone', 'Z001', "
                     "'MAT-1', :m, 100, 100, 'ALLOCATED')"),
                {"v": version_id, "m": month})
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            connection.execute(
                text("INSERT INTO target_allocation (version_id, level, "
                     "node_code, material_code, target_month, system_volume, "
                     "current_volume, status) VALUES (:v, 'zone', 'Z001', "
                     "'MAT-1', '2026-07', 50, 50, 'ALLOCATED')"),
                {"v": version_id})


def test_the_approval_sequence_ascends_from_the_bottom(migrated_engine) -> None:
    """Sequence 1 is the sub-territory, and Management signs last.

    Revision 0027 seeded this upside down — Management at 1 — which read as the
    CEO approving a target before the regional manager had looked at it.
    Revision 0030 corrects it, and this pins the direction so it cannot quietly
    invert again.
    """
    with migrated_engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT role FROM target_approval_matrix "
            "WHERE approval_sequence IS NOT NULL "
            "ORDER BY approval_sequence")).all()
    assert [role for (role,) in rows] == [
        "SALES_OFFICER", "TERRITORY_MANAGER", "UNIT_MANAGER", "AREA_MANAGER",
        "REGIONAL_MANAGER", "ZONE_MANAGER", "BUSINESS_UNIT_HEAD", "MANAGEMENT",
    ]


def test_the_seeded_chain_matches_the_declared_default(migrated_engine) -> None:
    """One copy of the default, so a restore and a fresh install agree."""
    with migrated_engine.connect() as connection:
        stored = {
            role: sequence for role, sequence in connection.execute(text(
                "SELECT role, approval_sequence FROM target_approval_matrix"))
        }
    declared = {entry[0]: entry[2] for entry in DEFAULT_APPROVAL_MATRIX}
    assert stored == declared


def test_a_revision_names_its_node(migrated_engine) -> None:
    """Revision 0030's four columns.

    "Which revisions are open on this territory?" is the commonest question
    asked of this table, and the answer must not depend on which allocation row
    happened to be picked as the anchor.
    """
    inspector = sa_inspect(migrated_engine)
    columns = {column["name"] for column in
               inspector.get_columns("target_revision")}
    assert {"version_id", "level", "node_code", "material_code"} <= columns
    # The anchor stays, and stays required: it is what makes a pending request
    # vanish when the allocation it questioned is regenerated.
    anchor = next(column for column in inspector.get_columns("target_revision")
                  if column["name"] == "allocation_id")
    assert anchor["nullable"] is False
