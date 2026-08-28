"""Loading a country target from a file.

Three promises, and each is a place where the convenient behaviour would be the
wrong one.

**The upload is the same act as typing, not a second way in.** Every accepted
row goes through :func:`country.set_lines`, so a file cannot state something the
grid would refuse, and an uploaded figure is validated, audited and versioned by
exactly the same code.

**All-or-nothing, with every problem reported at once.** The ETL rejects
individual rows and imports the rest, which is right for a transaction file. A
country target is one deliberate list: loading 297 of 300 leaves it short by
whatever the other three were worth. So a file with any bad row applies nothing
— and lists every fault, because a file fixed one error at a time takes as many
uploads as it has mistakes.

**It updates what it names and removes nothing.** Materials already on the
version and absent from the file keep their figures, and the count is reported
so that promise can be seen kept rather than taken on trust.
"""

from __future__ import annotations

import io

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import DimMaterial
from app.database.models_target import (
    TargetAudit,
    TargetCountryLine,
    TargetStatus,
)
from app.targetmgmt import bulk, country, plans as plan_service
from app.targetmgmt.errors import VersionFrozen
from app.upload import files as upload_files
from test_target_allocation import (  # noqa: F401  (fixtures reused wholesale)
    MATERIAL,
    SCOPE,
    _worker_engine,
    hierarchy,
    planned,
    with_history,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def priced(with_history):
    """A second material of this company, and one belonging to another.

    The foreign material is what makes the company check testable: a plan for
    one company must not be able to state a target for goods that company does
    not deal in.
    """
    with Session(with_history) as session:
        session.add(DimMaterial(
            material_code="SKU-OTHER", material_description="Other goods",
            material_group_code="MG01", material_group_name="Group",
            material_brand_code="B01", material_brand="Brand",
            company_code="C002"))
        session.add(DimMaterial(
            material_code="SKU-SECOND", material_description="Second item",
            material_group_code="MG01", material_group_name="Group",
            material_brand_code="B01", material_brand="Brand",
            company_code="C001"))
        session.commit()
    return with_history


@pytest.fixture
def staged(tmp_path, monkeypatch):
    """Stage a file the way the endpoint does, into a throwaway directory."""
    monkeypatch.setattr(upload_files, "upload_dir", lambda: tmp_path)

    def stage(text: str, name: str = "country.csv"):
        return upload_files.store(io.BytesIO(text.encode("utf-8")), name,
                                  "text/csv")

    return stage


def _open(engine, planned):
    session = Session(engine)
    version = plan_service.get_version(session, planned["version_id"])
    plan = plan_service.get_plan(session, planned["plan_id"])
    return session, plan, version


def _lines(session, version_id) -> dict[str, float]:
    return {
        line.material_code: float(line.target_volume)
        for line in session.execute(
            select(TargetCountryLine).where(
                TargetCountryLine.version_id == version_id)).scalars()
    }


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_a_clean_file_says_what_it_would_change(priced, planned, staged,
                                                users) -> None:
    session, plan, version = _open(priced, planned)
    stored = staged(f"Material Code,Target Volume\n"
                    f"{MATERIAL},\"150,000\"\n"
                    f"SKU-SECOND,45000\n")
    preview = bulk.read(stored, session, plan=plan, version=version)
    counts = preview.to_dict()["counts"]

    assert counts["read"] == 2
    assert counts["rejected"] == 0
    assert counts["changed"] == 1   # the fixture already sets MATERIAL
    assert counts["new"] == 1
    assert preview.to_dict()["applicable"] is True
    session.close()


def test_a_thousands_separator_is_read_and_a_letter_is_not(priced, planned,
                                                           staged) -> None:
    """``12,5OO`` is not 125. The no-invented-data invariant, applied to a cell."""
    session, plan, version = _open(priced, planned)
    preview = bulk.read(
        staged(f"Material Code,Target Volume\n{MATERIAL},\"12,5OO\"\n"),
        session, plan=plan, version=version)
    assert preview.rejected
    assert "not a number" in preview.rejected[0].error
    assert preview.rejected[0].raw_volume == "12,5OO"
    session.close()


def test_a_negative_volume_takes_its_own_message(priced, planned,
                                                 staged) -> None:
    """"Unreadable" and "negative" send a reader to different places."""
    session, plan, version = _open(priced, planned)
    preview = bulk.read(
        staged(f"Material Code,Target Volume\n{MATERIAL},-40\n"),
        session, plan=plan, version=version)
    assert "zero or more" in preview.rejected[0].error
    session.close()


def test_a_material_named_twice_is_refused(priced, planned, staged) -> None:
    """One figure per material; choosing between two would be a guess."""
    session, plan, version = _open(priced, planned)
    preview = bulk.read(
        staged(f"Material Code,Target Volume\n{MATERIAL},100\n"
               f"{MATERIAL},200\n"),
        session, plan=plan, version=version)
    assert len(preview.rejected) == 1
    assert "appears twice" in preview.rejected[0].error
    session.close()


def test_a_material_the_master_lacks_is_refused(priced, planned,
                                                staged) -> None:
    session, plan, version = _open(priced, planned)
    preview = bulk.read(
        staged("Material Code,Target Volume\nSKU-NOPE,1000\n"),
        session, plan=plan, version=version)
    assert "not in the Material Master" in preview.rejected[0].error
    session.close()


def test_a_material_of_another_company_is_refused(priced, planned,
                                                  staged) -> None:
    """The same rule ``set_lines`` applies, checked here so the file can say why."""
    session, plan, version = _open(priced, planned)
    preview = bulk.read(
        staged("Material Code,Target Volume\nSKU-OTHER,1000\n"),
        session, plan=plan, version=version)
    assert "belongs to C002" in preview.rejected[0].error
    session.close()


def test_every_problem_is_reported_in_one_pass(priced, planned,
                                               staged) -> None:
    """A file fixed one error at a time takes as many uploads as it has faults."""
    session, plan, version = _open(priced, planned)
    preview = bulk.read(
        staged(f"Material Code,Target Volume\n"
               f"{MATERIAL},\"12,5OO\"\n"
               f"SKU-SECOND,-40\n"
               f"SKU-SECOND,50\n"
               f"SKU-NOPE,1000\n"
               f"SKU-OTHER,1000\n"
               f",900\n"),
        session, plan=plan, version=version)
    assert len(preview.rejected) == 6
    assert preview.to_dict()["applicable"] is False
    session.close()


def test_a_blank_row_is_spacing_rather_than_a_mistake(priced, planned,
                                                      staged) -> None:
    session, plan, version = _open(priced, planned)
    preview = bulk.read(
        staged(f"Material Code,Target Volume\n{MATERIAL},1000\n,\n"
               f"SKU-SECOND,2000\n"),
        session, plan=plan, version=version)
    assert preview.to_dict()["counts"]["read"] == 2
    assert preview.rejected == []
    session.close()


def test_the_target_files_own_headings_are_accepted(priced, planned,
                                                    staged) -> None:
    """The Target file's column is headed ``SKU Code`` — what the planners call it.

    Rejecting a correct file over a synonym is the failure the dataset specs
    already avoid, and this reader follows the same rule.
    """
    session, plan, version = _open(priced, planned)
    preview = bulk.read(
        staged(f"SKU Code,Volume\n{MATERIAL},1000\n"),
        session, plan=plan, version=version)
    assert preview.material_header == "SKU Code"
    assert preview.volume_header == "Volume"
    assert preview.rejected == []
    session.close()


def test_a_file_without_the_two_columns_names_what_it_has(priced, planned,
                                                          staged) -> None:
    session, plan, version = _open(priced, planned)
    with pytest.raises(bulk.UploadUnreadable) as caught:
        bulk.read(staged("Region,Amount\nREG001,500\n"), session, plan=plan,
                  version=version)
    assert "Material Code and Target Volume" in caught.value.user_message
    assert "Region" in caught.value.user_message
    session.close()


def test_the_preview_counts_what_it_will_not_touch(priced, planned, staged,
                                                   users) -> None:
    """"The upload removes nothing" is a promise a reader should see kept."""
    session, plan, version = _open(priced, planned)
    country.set_lines(session, users["ceo"], version=version, plan=plan,
                      entries=[("SKU-SECOND", 5000)])
    session.commit()

    preview = bulk.read(
        staged(f"Material Code,Target Volume\n{MATERIAL},1000\n"),
        session, plan=plan, version=version)
    assert preview.untouched == 1
    assert any("removes nothing" in note for note in preview.notes())
    session.close()


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def test_applying_writes_through_the_ordinary_path(priced, planned, staged,
                                                   users) -> None:
    """Same validator, same audit, same version — an upload is not a second way in."""
    session, plan, version = _open(priced, planned)
    result = bulk.apply(session, users["ceo"], plan=plan, version=version,
                        stored=staged(f"Material Code,Target Volume\n"
                                      f"{MATERIAL},\"150,000\"\n"
                                      f"SKU-SECOND,45000\n"))
    session.commit()

    lines = _lines(session, version.version_id)
    assert lines[MATERIAL] == pytest.approx(150000.0)
    assert lines["SKU-SECOND"] == pytest.approx(45000.0)
    assert result["created"] == 1
    assert result["updated"] == 1
    session.close()


def test_each_changed_line_is_audited_individually(priced, planned, staged,
                                                   users) -> None:
    """That is the row the Audit Trail screen shows, and it has old and new."""
    session, plan, version = _open(priced, planned)
    bulk.apply(session, users["ceo"], plan=plan, version=version,
               stored=staged(f"Material Code,Target Volume\n"
                             f"{MATERIAL},150000\n"))
    session.commit()

    entries = session.execute(
        select(TargetAudit).where(
            TargetAudit.action == "COUNTRY_TARGET_EDITED")
    ).scalars().all()
    assert any(MATERIAL in (entry.node_label or "") for entry in entries)
    uploaded = session.execute(
        select(TargetAudit).where(TargetAudit.action == "TARGET_UPLOADED")
    ).scalars().all()
    assert len(uploaded) == 1
    assert "country.csv" in uploaded[0].node_label
    session.close()


def test_a_file_with_one_bad_row_applies_nothing(priced, planned, staged,
                                                 users) -> None:
    """All-or-nothing: a partly loaded country target is short, not small."""
    session, plan, version = _open(priced, planned)
    before = _lines(session, version.version_id)

    with pytest.raises(bulk.UploadRejected) as caught:
        bulk.apply(session, users["ceo"], plan=plan, version=version,
                   stored=staged(f"Material Code,Target Volume\n"
                                 f"SKU-SECOND,45000\n"
                                 f"{MATERIAL},\"12,5OO\"\n"))
    session.rollback()

    assert caught.value.details["rejected_count"] == 1
    assert _lines(session, version.version_id) == before
    session.close()


def test_a_file_stating_no_figures_is_refused(priced, planned, staged,
                                              users) -> None:
    """Nothing to apply, and never applied as a set of zeroes."""
    session, plan, version = _open(priced, planned)
    with pytest.raises(bulk.UploadUnreadable) as caught:
        bulk.apply(session, users["ceo"], plan=plan, version=version,
                   stored=staged(f"Material Code,Target Volume\n{MATERIAL},\n"))
    assert "no Target Volume" in caught.value.user_message
    session.close()


def test_a_material_listed_with_no_figure_is_skipped_not_zeroed(
        priced, planned, staged, users) -> None:
    """The template pre-fills every material, so most rows arrive blank.

    Rejecting them would make the template unusable and writing them as zero
    would set a real target of nothing. They are skipped, counted and named —
    the distinction this platform keeps everywhere between absent and zero.
    """
    session, plan, version = _open(priced, planned)
    preview = bulk.read(
        staged(f"Material Code,Target Volume\n{MATERIAL},150000\n"
               f"SKU-SECOND,\n"),
        session, plan=plan, version=version)

    assert preview.to_dict()["counts"]["skipped"] == 1
    assert preview.to_dict()["applicable"] is True
    assert any("nothing stated here" in note for note in preview.notes())

    bulk.apply(session, users["ceo"], plan=plan, version=version,
               stored=staged(f"Material Code,Target Volume\n"
                             f"{MATERIAL},150000\nSKU-SECOND,\n"))
    session.commit()
    assert "SKU-SECOND" not in _lines(session, version.version_id)
    session.close()


def test_a_frozen_version_refuses_an_upload(priced, planned, staged,
                                            users) -> None:
    """The module's central promise: an approved target is never overwritten."""
    session, plan, version = _open(priced, planned)
    version.status = TargetStatus.APPROVED
    session.commit()

    with pytest.raises(VersionFrozen):
        bulk.apply(session, users["ceo"], plan=plan, version=version,
                   stored=staged(f"Material Code,Target Volume\n"
                                 f"{MATERIAL},150000\n"))
    session.close()


def test_applying_leaves_materials_the_file_does_not_name(priced, planned,
                                                          staged, users) -> None:
    """UPSERT, never REPLACE: a file missing a sheet must not empty a target."""
    session, plan, version = _open(priced, planned)
    country.set_lines(session, users["ceo"], version=version, plan=plan,
                      entries=[("SKU-SECOND", 5000)])
    session.commit()

    result = bulk.apply(session, users["ceo"], plan=plan, version=version,
                        stored=staged(f"Material Code,Target Volume\n"
                                      f"{MATERIAL},150000\n"))
    session.commit()

    lines = _lines(session, version.version_id)
    assert lines["SKU-SECOND"] == pytest.approx(5000.0)
    assert result["untouched"] == 1
    session.close()


def test_apply_revalidates_rather_than_trusting_the_preview(
        priced, planned, staged, users) -> None:
    """The preview is a photograph, not a promise.

    A version approved between the two calls must refuse the write, and a
    material retired in between must be caught — so the staged bytes are read
    and checked again rather than a parsed result being replayed.
    """
    session, plan, version = _open(priced, planned)
    stored = staged(f"Material Code,Target Volume\nSKU-SECOND,45000\n")
    preview = bulk.read(stored, session, plan=plan, version=version)
    assert preview.to_dict()["applicable"] is True

    session.execute(DimMaterial.__table__.update()
                    .where(DimMaterial.material_code == "SKU-SECOND")
                    .values(company_code="C002"))
    session.commit()

    with pytest.raises(bulk.UploadRejected):
        bulk.apply(session, users["ceo"], plan=plan, version=version,
                   stored=stored)
    session.close()


# ---------------------------------------------------------------------------
# The template
# ---------------------------------------------------------------------------


def test_the_template_lists_only_this_companys_materials(priced, planned,
                                                         users) -> None:
    """Which makes the commonest rejection impossible to hit by accident."""
    session, plan, version = _open(priced, planned)
    content, name = bulk.template(session, plan, version)
    text = content.decode("utf-8-sig")

    assert "SKU-SECOND" in text
    assert "SKU-OTHER" not in text
    assert name.startswith(f"country-target-{plan.plan_code}")
    session.close()


def test_the_template_carries_the_current_figures(priced, planned,
                                                  users) -> None:
    """Pre-filled, because the commonest use is adjusting rather than entering."""
    session, plan, version = _open(priced, planned)
    text = bulk.template(session, plan, version)[0].decode("utf-8-sig")
    row = next(line for line in text.splitlines()
               if line.startswith(f"{MATERIAL},"))
    assert row.split(",")[1] not in ("", "0")
    session.close()


def test_a_material_with_no_figure_is_left_blank_not_zero(priced, planned,
                                                          users) -> None:
    """A blank round-trips as "nothing stated"; a zero as a real target of none."""
    session, plan, version = _open(priced, planned)
    text = bulk.template(session, plan, version)[0].decode("utf-8-sig")
    row = next(line for line in text.splitlines()
               if line.startswith("SKU-SECOND,"))
    assert row.split(",")[1] == ""
    session.close()


def test_the_template_round_trips(priced, planned, staged, users) -> None:
    """Whatever the template writes must be readable without anybody editing it."""
    session, plan, version = _open(priced, planned)
    text = bulk.template(session, plan, version)[0].decode("utf-8-sig")
    preview = bulk.read(staged(text), session, plan=plan, version=version)

    assert preview.rejected == []
    assert preview.to_dict()["counts"]["read"] >= 1
    session.close()
