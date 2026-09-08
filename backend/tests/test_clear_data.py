"""``scripts/clear_data.py``: what it empties, in what order, and what it refuses.

The script is the one tool in this repository that destroys data, so the thing
worth pinning is not that a delete works — it is that an operator is never told
a clearance happened when it did not. Three properties carry that:

* a table the run could not empty is **named** and the process exits non-zero;
* a run that *would* be blocked is refused **before** anything is written, by
  counting the rows that block it rather than by waiting for a DELETE to fail —
  the two dialects disagree about whether it would fail at all;
* children are emptied before parents, in ``GROUPS`` order rather than in the
  order the operator typed the groups, so the guarantee does not depend on the
  command line.

The order and the group membership are checked against the model metadata, not
against a second copy of the list. A hand-written list of what depends on what
is exactly the kind of list that outlives the schema it describes — ``GROUPS``
named ``dim_product`` for three revisions after 0022 dropped the table.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.orm import Session

from app.database.models import Base, DimMaterial
from app.database.models_warehouse import FactSales
from app.etl.pipeline import LOAD_MODE_INITIAL, run_import
from app.etl.readers import RecordsSourceReader
from conftest_phase2 import sales_row

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"


@pytest.fixture(scope="module")
def script():
    """The script, imported as a module rather than run as one."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    return importlib.import_module("clear_data")


@pytest.fixture
def loaded_engine(seeded_engine: Engine) -> Engine:
    """Master data plus one imported sale, so the facts reference the masters."""
    run_import(seeded_engine, "sales",
               RecordsSourceReader([sales_row(**{"Company Code": "C001"})],
                                   source_name="sales.csv"),
               source_system="TEST", load_mode=LOAD_MODE_INITIAL)
    return seeded_engine


def live_tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def row_count(engine: Engine, table: str) -> int:
    with Session(engine) as session:
        return session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


def run(script, engine, monkeypatch, tmp_path, *argv: str) -> int:
    """``main()`` against a throwaway database, with the file sweep sandboxed.

    ``ROOT`` is redirected at ``tmp_path`` deliberately: the upload and demo
    directories it sweeps are real ones in a developer's checkout, and a test
    that empties them would be destroying data of its own.
    """
    monkeypatch.setattr(script, "get_engine", lambda: engine)
    monkeypatch.setattr(script, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["clear_data.py", *argv])
    return script.main()


# ==========================================================================
# What the groups name
# ==========================================================================


def test_every_table_a_group_names_is_a_table_the_schema_has(script) -> None:
    """A stale name is a silent under-clearance: the operator asked for it and
    the table it meant is gone, so nothing says the request was incomplete."""
    named = {table for tables in script.GROUPS.values() for table in tables}
    assert named <= set(Base.metadata.tables)


def test_a_table_belongs_to_at_most_one_group(script) -> None:
    """``GROUP_OF_TABLE`` answers "which group empties this?" with one group,
    and a table listed twice would make the refusal message name the wrong one."""
    named = [table for tables in script.GROUPS.values() for table in tables]
    assert len(named) == len(set(named))


def test_no_group_empties_a_table_the_script_promises_to_keep(script) -> None:
    """The "Kept" list is a promise printed on every run."""
    named = {table for tables in script.GROUPS.values() for table in tables}
    assert named.isdisjoint(script.PROTECTED)


# ==========================================================================
# Order — the guarantee the header comment makes
# ==========================================================================


def test_the_order_is_the_dicts_and_not_the_operators(script) -> None:
    """``--groups masters,transactions`` must delete what the reverse does."""
    one = script.ordered_tables({"masters", "transactions"})
    other = script.ordered_tables({"transactions", "masters"})
    assert one == other
    assert one == script.GROUPS["transactions"] + script.GROUPS["masters"]


def test_a_child_is_emptied_before_the_parent_it_references(script) -> None:
    """Across groups as well as within one, for every key that would refuse.

    Read from the metadata so that a new foreign key is checked against the
    order rather than against somebody remembering to update this list.
    """
    order = script.ordered_tables(set(script.GROUPS))
    position = {table: index for index, table in enumerate(order)}

    for name in order:
        table = Base.metadata.tables[name]
        for constraint in table.foreign_key_constraints:
            parent = constraint.referred_table.name
            if parent not in position or parent == name:
                continue
            action = (constraint.ondelete or "NO ACTION").upper()
            if action not in script.BLOCKING_ON_DELETE:
                continue
            assert position[name] < position[parent], (
                f"{name} references {parent} and must be emptied first"
            )


# ==========================================================================
# What blocks a run, found before anything is written
# ==========================================================================


def test_facts_block_the_masters_they_reference(script, loaded_engine) -> None:
    """The case an operator actually hits: ``masters`` without ``transactions``."""
    live = live_tables(loaded_engine)
    with Session(loaded_engine) as session:
        blocked = script.blocking_references(
            session, set(script.GROUPS["masters"]), live)

    referenced = {blocker.referenced for blocker in blocked}
    assert "dim_material" in referenced
    assert "dim_territory" in referenced
    assert {blocker.table for blocker in blocked} == {"fact_sales"}
    # The message has to name the fix, not just the problem.
    assert all(blocker.remedy == "add group 'transactions'" for blocker in blocked)


def test_naming_the_group_that_holds_the_children_unblocks_it(
    script, loaded_engine
) -> None:
    live = live_tables(loaded_engine)
    doomed = set(script.ordered_tables({"masters", "transactions"}))
    with Session(loaded_engine) as session:
        assert script.blocking_references(session, doomed, live) == []


def test_a_key_that_is_null_forbids_nothing(script, loaded_engine) -> None:
    """``dim_customer`` is PENDING_SOURCE_DATA, so the sale resolved no customer.

    A NULL key references no row, so it cannot forbid deleting one — MATCH
    SIMPLE. Counting every row of the referring table instead would refuse a
    run for a reference that does not exist.
    """
    with Session(loaded_engine) as session:
        assert session.execute(select(FactSales.customer_id)).scalar_one() is None

        blocked = script.blocking_references(
            session, set(script.GROUPS["masters"]), live_tables(loaded_engine))
    assert "dim_customer" not in {blocker.referenced for blocker in blocked}


def test_a_cascade_or_a_set_null_is_not_a_blocker(script, loaded_engine) -> None:
    """Three tables reference ``etl_import_batches`` with three different
    actions; only the one that would refuse counts as a blocker."""
    with Session(loaded_engine) as session:
        assert row_count(loaded_engine, "stg_sales") > 0
        blocked = script.blocking_references(
            session, {"etl_import_batches"}, live_tables(loaded_engine))

    assert {blocker.table for blocker in blocked} == {"fact_sales"}


# ==========================================================================
# Exit codes — the point of the exercise
# ==========================================================================


def test_clearing_masters_alone_is_refused_and_changes_nothing(
    script, loaded_engine, monkeypatch, tmp_path, capsys
) -> None:
    code = run(script, loaded_engine, monkeypatch, tmp_path,
               "--groups", "masters", "--yes")

    assert code == script.EXIT_REFUSED
    assert row_count(loaded_engine, "dim_material") > 0
    out = capsys.readouterr().out
    assert "REFUSED" in out
    assert "add group 'transactions'" in out


def test_a_dry_run_refuses_where_the_real_run_would(
    script, loaded_engine, monkeypatch, tmp_path
) -> None:
    """A rehearsal that reports a plan it would then refuse is worse than no
    rehearsal, because the operator schedules the real run on the strength of
    it."""
    assert run(script, loaded_engine, monkeypatch, tmp_path,
               "--groups", "masters") == script.EXIT_REFUSED


def test_clearing_the_facts_and_their_masters_together_succeeds(
    script, loaded_engine, monkeypatch, tmp_path
) -> None:
    code = run(script, loaded_engine, monkeypatch, tmp_path,
               "--groups", "masters,transactions", "--yes")

    assert code == script.EXIT_OK
    assert row_count(loaded_engine, "fact_sales") == 0
    assert row_count(loaded_engine, "dim_material") == 0
    # Named in PROTECTED, so it survives a run that empties everything else.
    assert row_count(loaded_engine, "dim_date") > 0


def test_a_table_that_refuses_fails_the_run_and_rolls_it_back(
    script, loaded_engine, monkeypatch, tmp_path, capsys
) -> None:
    """The last line of defence: a constraint the pre-flight did not predict.

    Simulated by blinding the pre-flight, which is what a foreign key added to
    the database but not to the models would do. What must not happen is the
    old behaviour — a per-table ``except`` that prints and carries on, leaving
    the masters in place while the process reports success.
    """
    monkeypatch.setattr(script, "blocking_references", lambda *_: [])
    # Emptied successfully, several tables before the first refusal — so the
    # rollback below has something to undo rather than nothing.
    unreferenced = row_count(loaded_engine, "dim_storage_location")
    assert unreferenced > 0

    code = run(script, loaded_engine, monkeypatch, tmp_path,
               "--groups", "masters", "--yes")

    assert code == script.EXIT_FAILED
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "dim_material" in out
    # Every blocked table, not just the first one to refuse.
    assert "dim_territory" in out
    # One transaction, rolled back whole: a half-emptied selection is a state
    # nobody described. Both halves of that are asserted — the table that
    # refused still has its rows, and so does the one that had already gone.
    assert row_count(loaded_engine, "dim_material") > 0
    assert row_count(loaded_engine, "dim_storage_location") == unreferenced


def test_a_name_the_schema_lacks_is_reported_but_is_not_a_failure(
    script, seeded_engine, monkeypatch, tmp_path, capsys
) -> None:
    """It holds no rows, so nothing was left behind — but it is a fault in
    ``GROUPS`` and the run says so rather than passing over it."""
    monkeypatch.setitem(script.GROUPS, "changelog",
                        ("data_change_log", "dim_product"))

    code = run(script, seeded_engine, monkeypatch, tmp_path,
               "--groups", "changelog", "--yes")

    assert code == script.EXIT_OK
    out = capsys.readouterr().out
    assert "Not in this schema" in out
    assert "dim_product" in out


def test_an_unknown_group_is_a_usage_error(
    script, seeded_engine, monkeypatch, tmp_path
) -> None:
    assert run(script, seeded_engine, monkeypatch, tmp_path,
               "--groups", "everything", "--yes") == script.EXIT_USAGE
    assert run(script, seeded_engine, monkeypatch, tmp_path,
               "--yes") == script.EXIT_USAGE


def test_the_users_group_still_empties_accounts(
    script, seeded_engine, monkeypatch, tmp_path
) -> None:
    """``clear_users`` no longer swallows a failed child delete, so the path it
    takes when every table is present is worth keeping under test."""
    from app.database.models_ai import AppUser

    with Session(seeded_engine) as session:
        for username in ("admin", "ceo"):
            session.add(AppUser(username=username, display_name=username.title(),
                                role="MANAGEMENT"))
        session.commit()

    code = run(script, seeded_engine, monkeypatch, tmp_path,
               "--groups", "users", "--yes")

    assert code == script.EXIT_OK
    with Session(seeded_engine) as session:
        remaining = list(session.execute(select(AppUser.username)).scalars())
    assert remaining == ["admin"]


def test_the_material_master_is_not_reported_as_emptied_when_it_was_not(
    script, loaded_engine, monkeypatch, tmp_path, capsys
) -> None:
    """The regression in one sentence: the run used to print a row total and
    exit 0 while every master it named was still there."""
    before = row_count(loaded_engine, "dim_material")
    assert before > 0

    code = run(script, loaded_engine, monkeypatch, tmp_path,
               "--groups", "masters", "--yes")

    assert code != script.EXIT_OK
    assert "Deleted" not in capsys.readouterr().out
    assert row_count(loaded_engine, "dim_material") == before
    with Session(loaded_engine) as session:
        assert session.execute(select(DimMaterial)).scalars().first() is not None
