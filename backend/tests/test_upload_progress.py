"""Live upload progress: the registry, the pipeline's emissions and the endpoint.

The promise being tested is that the progress figure is *measured*. So these
tests assert on the relationship between the reported numbers and the file that
produced them — the phases actually visited, the row counts matching the rows in
the file — rather than merely that some percentage arrived.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models_admin import ImportMode, UploadBatch, UploadStatus
from app.database.models_ai import AppUser
from app.security.sections import ALLOW, SectionKey
from app.etl.pipeline import run_import
from app.etl.readers import RecordsSourceReader
from app.utils import progress as progress_registry
from app.utils.progress import (
    DEFAULT_SCALE,
    DELIMITED_PHASE_WEIGHTS,
    EXCEL_PHASE_WEIGHTS,
    MASTER_PHASE_WEIGHTS,
    PHASE_WEIGHTS_BY_SOURCE,
    Phase,
    ProgressReporter,
    scale_for,
)

from test_data_upload import (  # noqa: F401 - fixtures are used by name
    SALES_HEADERS,
    auth,
    csv_bytes,
    login,
    sales_rows,
    upload_client,
)
from test_data_upload import commit as _commit
from test_data_upload import upload as _upload

JOB = "3f6b1f10-0b1a-4c8f-9f2e-1c3d5e7a9b11"


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts with an empty registry; jobs are process-global."""
    progress_registry.clear()
    yield
    progress_registry.clear()


# ==========================================================================
# The registry itself
# ==========================================================================


def test_a_reporter_without_a_job_is_a_working_no_op():
    """Every scripted import gets this one, so it must never raise."""
    reporter = ProgressReporter(None)
    assert reporter.enabled is False
    reporter.phase(Phase.READING, total=10)
    reporter.rows(5, valid=4, invalid=1)
    reporter.counts(imported_records=4)
    reporter.finish(message="done")
    assert progress_registry.snapshot("anything") is None


def test_a_malformed_token_is_ignored_rather_than_tracked():
    """The token comes from a client, so a bad one must not create an entry."""
    for token in ["", "not-a-uuid", "../etc/passwd", "1" * 200]:
        reporter = progress_registry.start(token, operation="VALIDATE")
        assert reporter.enabled is False
        assert progress_registry.snapshot(token) is None


def test_progress_advances_only_when_a_step_reports():
    reporter = progress_registry.start(JOB, operation="VALIDATE")
    first = progress_registry.snapshot(JOB)
    assert first is not None and first["percent"] == 0

    reporter.phase(Phase.VALIDATING, total=100)
    at_phase_start = progress_registry.snapshot(JOB)["percent"]

    reporter.rows(50)
    halfway = progress_registry.snapshot(JOB)["percent"]
    reporter.rows(100)
    complete = progress_registry.snapshot(JOB)["percent"]

    # Strictly increasing with the rows actually processed, and never on its own.
    assert at_phase_start < halfway < complete
    snapshot = progress_registry.snapshot(JOB)
    assert snapshot["counts"]["processed_records"] == 100
    assert snapshot["counts"]["total_records"] == 100


def test_percent_never_reaches_a_hundred_before_the_run_finishes():
    """100% must mean finished, so no intermediate step may report it."""
    reporter = progress_registry.start(JOB, operation="IMPORT")
    for phase in (Phase.READING, Phase.VALIDATING, Phase.MAPPING, Phase.IMPORTING):
        reporter.phase(phase, total=10)
        reporter.rows(10)
        assert progress_registry.snapshot(JOB)["percent"] < 100
        assert progress_registry.snapshot(JOB)["done"] is False

    reporter.finish(message="Loaded.")
    final = progress_registry.snapshot(JOB)
    assert final["percent"] == 100
    assert final["done"] is True
    assert final["phase"] == Phase.COMPLETED


def test_a_failed_run_finishes_at_a_hundred_but_says_it_failed():
    reporter = progress_registry.start(JOB, operation="VALIDATE")
    reporter.finish(message="Required column missing.", failed=True)
    final = progress_registry.snapshot(JOB)
    assert final["done"] is True
    assert final["percent"] == 100
    assert final["phase"] == Phase.FAILED
    assert final["message"] == "Required column missing."


def test_the_registry_is_bounded():
    """A client mints the tokens, so the registry must not grow without bound."""
    for index in range(progress_registry.MAX_JOBS + 40):
        progress_registry.start(f"{index:08x}-0000-4000-8000-000000000000",
                                operation="VALIDATE")
    # Counted through the public surface: whatever survived, it is capped.
    survivors = sum(
        1 for index in range(progress_registry.MAX_JOBS + 40)
        if progress_registry.snapshot(f"{index:08x}-0000-4000-8000-000000000000")
    )
    assert survivors <= progress_registry.MAX_JOBS


def test_a_snapshot_does_not_change_under_the_caller():
    """The reader serialises outside the lock, so it must hold its own copy."""
    reporter = progress_registry.start(JOB, operation="VALIDATE")
    reporter.phase(Phase.VALIDATING, total=10)
    taken = progress_registry.snapshot(JOB)
    reporter.rows(10)
    assert taken["counts"]["processed_records"] == 0


# ==========================================================================
# What the pipeline emits
# ==========================================================================


def sales_records(count: int, prefix: str = "PRG") -> list[dict[str, object]]:
    """Rows in the canonical field names ``RecordsSourceReader`` expects."""
    return [
        {"date": "2026-08-20", "invoice_no": f"{prefix}-{n:03d}",
         "sku_code": "SKU001", "territory_code": "TR001", "quantity": 10,
         "net_sales": 11000}
        for n in range(count)
    ]


def test_the_pipeline_visits_every_phase_and_counts_the_real_rows(agent_engine):
    """A dry run still reports, which is what the preview call relies on."""
    seen: list[tuple[str, int]] = []
    reporter = progress_registry.start(JOB, operation="VALIDATE")
    records = sales_records(5)

    original_phase = ProgressReporter.phase

    def _record(self, phase, **kwargs):
        seen.append((phase, progress_registry.snapshot(JOB)["percent"]))
        return original_phase(self, phase, **kwargs)

    ProgressReporter.phase = _record
    try:
        result = run_import(agent_engine, "sales", RecordsSourceReader(records),
                            source_system="TEST", dry_run=True, progress=reporter)
    finally:
        ProgressReporter.phase = original_phase

    visited = [phase for phase, _ in seen]
    assert Phase.READING in visited
    assert Phase.VALIDATING in visited
    assert Phase.MAPPING in visited
    assert Phase.IMPORTING in visited
    # The bar only ever moves forward as the phases advance.
    percents = [percent for _, percent in seen]
    assert percents == sorted(percents)

    final = progress_registry.snapshot(JOB)
    assert final["counts"]["total_records"] == len(records) == result.total_rows
    assert final["counts"]["valid_records"] == result.valid_rows


def test_a_progress_failure_cannot_fail_an_import(agent_engine, monkeypatch):
    """Progress is instrumentation. A bug in it must not cost the user a load."""
    def _explode(*_args, **_kwargs):
        raise RuntimeError("progress backend is down")

    reporter = progress_registry.start(JOB, operation="VALIDATE")
    monkeypatch.setattr(progress_registry._REGISTRY, "update", _explode)

    result = run_import(agent_engine, "sales",
                        RecordsSourceReader(sales_records(1, prefix="BOOM")),
                        source_system="TEST", dry_run=True, progress=reporter)
    assert result.total_rows == 1
    assert result.valid_rows == 1


# ==========================================================================
# The endpoint
# ==========================================================================


def test_the_upload_response_names_the_job_to_watch(upload_client):
    """202 and a job id — the work has not happened yet when this returns."""
    token = login(upload_client)
    content = csv_bytes(SALES_HEADERS, sales_rows("PRG-001", "PRG-002"))

    response = upload_client.post(
        "/api/data-upload/preview", headers=auth(token),
        files={"file": ("progress.csv", content, "text/csv")},
        data={"upload_type": "sales", "import_mode": ImportMode.UPSERT},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["queued"] is True
    # The job id is the batch's own uuid rather than a second identifier the
    # client supplied, so the two can never name different things.
    assert body["upload"]["job_id"] == body["upload"]["upload_uuid"]


def test_a_finished_job_reports_the_counts_of_the_upload_it_ran(upload_client):
    token = login(upload_client)
    content = csv_bytes(SALES_HEADERS, sales_rows("PRG-002", "PRG-003"))
    finished = upload(upload_client, token, content)["upload"]

    job = upload_client.get(f"/api/data-upload/jobs/{finished['job_id']}",
                            headers=auth(token)).json()
    assert job["known"] is True
    assert job["status"] == UploadStatus.VALIDATED
    assert job["is_active"] is False
    assert job["progress_percent"] == 100
    assert job["upload_id"] == finished["upload_id"]
    # The counts on the finished job are the counts on the finished upload.
    assert job["counts"]["total_records"] == 2
    assert job["counts"]["valid_records"] == 2
    assert job["counts"]["invalid_records"] == 0


def test_an_import_reports_the_rows_it_actually_wrote(upload_client):
    token = login(upload_client)
    content = csv_bytes(SALES_HEADERS, sales_rows("PRG-010", "PRG-011"))
    preview = upload(upload_client, token, content)
    imported = commit(upload_client, token,
                      preview["upload"]["upload_id"])["upload"]

    job = upload_client.get(f"/api/data-upload/jobs/{imported['job_id']}",
                            headers=auth(token)).json()
    assert job["status"] == UploadStatus.COMPLETED
    assert job["counts"]["imported_records"] == 2
    assert job["counts"]["failed_records"] == 0
    assert job["duration_seconds"] is not None


def test_a_rejected_row_is_counted_as_invalid_not_hidden(upload_client):
    token = login(upload_client)
    rows = sales_rows("PRG-020")
    rows.append(["2026-08-20", "PRG-021", "NOPE", "TR001", 10, 12000, 1000, 11000])
    finished = upload(upload_client, token, csv_bytes(SALES_HEADERS, rows))["upload"]

    job = upload_client.get(f"/api/data-upload/jobs/{finished['job_id']}",
                            headers=auth(token)).json()
    assert job["counts"]["total_records"] == 2
    assert job["counts"]["valid_records"] == 1
    assert job["counts"]["invalid_records"] == 1
    assert job["counts"]["failed_records"] == 1


def test_a_file_missing_a_required_column_fails_the_job_too(upload_client):
    token = login(upload_client)
    content = csv_bytes(["Invoice No", "Quantity"], [["PRG-030", 5]])
    finished = upload(upload_client, token, content)["upload"]

    job = upload_client.get(f"/api/data-upload/jobs/{finished['job_id']}",
                            headers=auth(token)).json()
    assert job["status"] == UploadStatus.FAILED
    assert job["is_active"] is False
    assert job["can_cancel"] is False


def test_a_crashing_job_is_recorded_as_failed_rather_than_left_running(
    upload_client, agent_engine,
):
    """The recovery path must work, or a crashed import hangs forever.

    ``_mark_failed`` opens a *second* session precisely because the worker's own
    one is the thing that just broke. It reached for a bare ``get_engine`` that
    this module deliberately never imports, so the recovery itself raised
    NameError, its own ``except`` swallowed that, and the batch stayed in an
    ACTIVE status with no worker behind it — indistinguishable, to the operator,
    from an upload that silently stopped.

    Driven through ``_guarded`` rather than a real crash because the point is the
    recovery, not any particular way of reaching it.
    """
    from app.upload import jobs

    token = login(upload_client)
    finished = upload(upload_client, token,
                      csv_bytes(SALES_HEADERS, sales_rows("PRG-070")))["upload"]
    upload_id = finished["upload_id"]

    # Put the batch back into a running state, as it would be mid-import.
    with Session(agent_engine) as session:
        batch = session.get(UploadBatch, upload_id)
        batch.status = UploadStatus.IMPORTING
        batch.completed_at = None
        session.commit()

    reporter = progress_registry.start(finished["job_id"], operation="IMPORT")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("the worker died")

    monkeypatch_target = jobs._run
    try:
        jobs._run = _boom
        jobs._guarded("IMPORT", upload_id, finished["job_id"], reporter,
                      None, None, None)
    finally:
        jobs._run = monkeypatch_target

    with Session(agent_engine) as session:
        batch = session.get(UploadBatch, upload_id)
        assert batch.status == UploadStatus.FAILED, (
            "a crashed job must reach a terminal status, not sit in IMPORTING"
        )
        assert batch.status in UploadStatus.TERMINAL
        assert batch.completed_at is not None
        # Nothing was loaded, and the message says so without leaking the
        # exception that caused it.
        assert "nothing was loaded" in (batch.message or "")
        assert "RuntimeError" not in (batch.message or "")


def test_an_unknown_job_is_reported_as_unknown_not_as_an_error(upload_client):
    """A poll for a job that does not exist is a normal answer, not a 404."""
    token = login(upload_client)
    response = upload_client.get(
        "/api/data-upload/jobs/00000000-0000-4000-8000-000000000000",
        headers=auth(token),
    )
    assert response.status_code == 200
    assert response.json()["known"] is False


def test_the_job_list_shows_this_users_recent_imports(upload_client):
    """What the upload dock polls from whichever page the user is on."""
    token = login(upload_client)
    finished = upload(upload_client, token,
                      csv_bytes(SALES_HEADERS, sales_rows("PRG-050")))["upload"]

    listing = upload_client.get("/api/data-upload/jobs", headers=auth(token)).json()
    ids = [job["job_id"] for job in listing["jobs"]]
    assert finished["job_id"] in ids
    entry = next(j for j in listing["jobs"] if j["job_id"] == finished["job_id"])
    assert entry["counts"]["total_records"] == 1
    assert entry["file_name"] == "test.csv"


def test_a_user_cannot_watch_another_users_import(upload_client, agent_engine):
    """§14: a job is visible to whoever started it, and to a super admin."""
    admin_token = login(upload_client)
    finished = upload(upload_client, admin_token,
                      csv_bytes(SALES_HEADERS, sales_rows("PRG-060")))["upload"]

    # Grant the section but not ownership: the section gets them to the endpoint,
    # and ownership is what the endpoint then refuses on.
    with Session(agent_engine) as session:
        user_id = session.execute(
            select(AppUser.user_id).where(AppUser.username == "dhaka_rm")
        ).scalar_one()
    upload_client.put(f"/api/admin/users/{user_id}/permissions",
                      headers=auth(admin_token),
                      json={"permissions": {SectionKey.DATA_UPLOAD: ALLOW}})

    other = login(upload_client, "dhaka_rm")
    response = upload_client.get(f"/api/data-upload/jobs/{finished['job_id']}",
                                 headers=auth(other))
    assert response.status_code == 403
    # …and it does not appear in their own job list either.
    listing = upload_client.get("/api/data-upload/jobs", headers=auth(other)).json()
    assert finished["job_id"] not in [job["job_id"] for job in listing["jobs"]]


def test_the_job_endpoint_requires_the_data_upload_section(upload_client):
    """It reports on business data, so it is gated like every other endpoint.

    ``dhaka_rm`` is the fixture's user without the Data Upload section — the same
    one the rest of the upload suite uses to prove the gate.
    """
    token = login(upload_client, "dhaka_rm")
    response = upload_client.get(f"/api/data-upload/jobs/{JOB}", headers=auth(token))
    assert response.status_code == 403
    assert upload_client.get("/api/data-upload/jobs",
                             headers=auth(token)).status_code == 403


# ---------------------------------------------------------------------------
# Phase weights
# ---------------------------------------------------------------------------


def test_every_weight_table_covers_the_whole_bar():
    """A table that does not sum to 100 leaves the bar short of the end.

    The percentage is a phase's start plus its share, and the starts are derived
    by accumulating the weights — so a table summing to 96 would have the last
    phase finish at 96% and jump, and one summing to 104 would claim work that
    has not happened.
    """
    for name, weights in (("delimited", DELIMITED_PHASE_WEIGHTS),
                          ("excel", EXCEL_PHASE_WEIGHTS)):
        assert sum(weights.values()) == 100, f"{name} sums to {sum(weights.values())}"


def test_every_weight_table_names_only_real_phases():
    """A weight for a phase nobody enters is a share of the bar nothing can fill."""
    for weights in (DELIMITED_PHASE_WEIGHTS, EXCEL_PHASE_WEIGHTS):
        assert set(weights) <= set(Phase.ALL)


def test_every_phase_a_run_passes_through_has_a_weight():
    """The reverse: a phase with no entry contributes nothing and the bar stalls."""
    passed_through = (Phase.QUEUED, Phase.PREPARING, Phase.READING, Phase.STAGING,
                      Phase.VALIDATING, Phase.MAPPING, Phase.IMPORTING, Phase.WRITING)
    for weights in (DELIMITED_PHASE_WEIGHTS, EXCEL_PHASE_WEIGHTS):
        assert set(passed_through) <= set(weights)


def test_the_weight_tables_are_keyed_by_real_source_types():
    """The keys are spelled in ``progress`` but owned by ``etl.readers``.

    ``progress`` cannot import ``readers`` — ``readers`` imports it — so the
    source-type strings are written out by hand there. This is what stops the
    two drifting: renaming a source type fails here rather than silently
    dropping that format back to the default table, which would look like
    nothing more than a slightly wrong bar.
    """
    from app.etl import readers

    known = {readers.SOURCE_TYPE_EXCEL, readers.SOURCE_TYPE_CSV,
             readers.SOURCE_TYPE_API, readers.SOURCE_TYPE_MEMORY}
    assert set(PHASE_WEIGHTS_BY_SOURCE) <= known


def test_an_unknown_source_falls_back_rather_than_failing():
    """A future reader gets a working bar, not an exception."""
    assert scale_for("SAP") is DEFAULT_SCALE
    assert scale_for(None) is DEFAULT_SCALE
    assert scale_for("") is DEFAULT_SCALE


def test_a_scale_never_reports_backwards_within_a_phase():
    """Percent is monotonic in rows processed, for every phase of every table."""
    for scale in (scale_for("EXCEL"), scale_for("CSV")):
        for phase in (Phase.READING, Phase.STAGING, Phase.VALIDATING,
                      Phase.MAPPING, Phase.IMPORTING, Phase.WRITING):
            seen = [scale.percent(phase, done, 100) for done in range(0, 101, 5)]
            assert seen == sorted(seen), f"{scale.name}/{phase}: {seen}"


def test_the_phases_of_a_scale_do_not_overlap_or_leave_gaps():
    """Each phase ends exactly where the next begins.

    Derived from the weights rather than declared, so this is really a check
    that the derivation is right — an overlap would let the bar go backwards
    between two phases and a gap would make it jump.
    """
    for scale in (scale_for("EXCEL"), scale_for("CSV")):
        running = 0
        for phase, weight in scale.weights.items():
            assert scale.starts[phase] == running, f"{scale.name}: {phase}"
            running += weight
        assert running == 100


def test_a_reader_tells_the_reporter_which_format_it_is(tmp_path):
    """The scale is chosen by the source, and before anything is reported."""
    from app.etl.readers import CsvSourceReader, ExcelSourceReader

    csv_path = tmp_path / "rows.csv"
    csv_path.write_text("Code,Name\nA,Alpha\n", encoding="utf-8")
    reporter = ProgressReporter(None)
    CsvSourceReader(csv_path, progress=reporter).headers
    assert reporter._scale is scale_for("CSV")

    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["Code", "Name"])
    book.active.append(["A", "Alpha"])
    xlsx_path = tmp_path / "rows.xlsx"
    book.save(xlsx_path)
    reporter = ProgressReporter(None)
    ExcelSourceReader(xlsx_path, progress=reporter).headers
    assert reporter._scale is scale_for("EXCEL")


def test_reading_is_weighted_differently_for_the_two_formats():
    """The whole reason there are two tables.

    Reading a workbook is most of an Excel import and a rounding error in a
    delimited one, because openpyxl builds the entire sheet before yielding a
    row while a CSV is read a line at a time. One table for both left the bar
    frozen through most of an Excel run.
    """
    assert EXCEL_PHASE_WEIGHTS[Phase.READING] > 50
    assert DELIMITED_PHASE_WEIGHTS[Phase.READING] < 10


def test_the_default_table_under_reports_rather_than_over_reports():
    """An unnamed source must not have its progress overstated.

    The default is the delimited table, whose READING share is the smaller of
    the two: a source that in fact spends longer reading will sit low on the bar
    rather than claiming work it has not done. Erring the other way would show a
    file as half imported while it was still being opened.
    """
    assert DEFAULT_SCALE.weights[Phase.READING] == min(
        DELIMITED_PHASE_WEIGHTS[Phase.READING], EXCEL_PHASE_WEIGHTS[Phase.READING]
    )


def test_a_master_upload_runs_its_phases_in_the_order_its_table_declares():
    """A master upload maps before it validates — the reverse of the ETL.

    ``master_loader.validate`` loads the codes a file will be checked against
    and only then checks the rows. Read against the transactional table, where
    MAPPING sits after VALIDATING, the whole validation pass fell inside a band
    the bar had already passed and sat motionless on the longest step of the
    run. Its own table puts the phases in the order they happen.
    """
    order = [p for p in MASTER_PHASE_WEIGHTS if p != Phase.QUEUED]
    assert order.index(Phase.MAPPING) < order.index(Phase.VALIDATING)
    assert sum(MASTER_PHASE_WEIGHTS.values()) == 100
    # It stages nothing and writes no fact, so those bands would be dead weight.
    assert Phase.STAGING not in MASTER_PHASE_WEIGHTS
    assert Phase.WRITING not in MASTER_PHASE_WEIGHTS


def test_an_explicit_scale_is_not_displaced_by_the_file_format():
    """The shape of the run outranks the format it happens to arrive in.

    ``master_loader`` fixes its table before the file is opened; the reader then
    announces CSV or Excel as it starts. If that announcement won, a master
    upload would be measured against the transactional table again.
    """
    reporter = ProgressReporter(None)
    reporter.use_scale(scale_for("EXCEL"))
    reporter.source("CSV")
    assert reporter._scale is scale_for("EXCEL")

    # Without an explicit choice the format still decides.
    other = ProgressReporter(None)
    other.source("CSV")
    assert other._scale is scale_for("CSV")


def test_a_cancelled_run_records_the_position_it_reached():
    """Not the highest position ever announced.

    ``jobs._mark_cancelled`` copies this figure onto the batch and the audit
    keeps it, so it outlives the run. A clamp that only ever raised it was tried
    here and filed a run stopped a fifth of the way through validation at the
    position meaning validation was complete, making the two indistinguishable
    in the permanent record.
    """
    job = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    progress_registry.clear()
    reporter = progress_registry.start(job, operation="IMPORT")
    reporter.use_scale(scale_for("CSV"))
    reporter.phase(Phase.VALIDATING, total=100)
    reporter.rows(20)
    reached = progress_registry.snapshot(job)["percent"]
    assert reached == scale_for("CSV").percent(Phase.VALIDATING, 20, 100)
    reporter.finish(cancelled=True)
    assert progress_registry.snapshot(job)["percent"] == reached
    progress_registry.clear()


def upload(client, token, content, upload_type: str = "sales"):
    """Upload, wait for the validation job, and return the parsed result."""
    return _upload(client, token, upload_type, content).json()


def commit(client, token, upload_id):
    """Confirm the import, wait for the job, and return the parsed result."""
    return _commit(client, token, upload_id).json()
