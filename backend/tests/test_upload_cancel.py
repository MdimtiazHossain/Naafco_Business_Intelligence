"""Cancelling a running import.

The promise being tested is not "the button works" but **that a cancelled import
leaves the warehouse exactly as it found it**. The run is one transaction, so
stopping it unwinds everything it had written; these tests assert on the fact
table, not only on the status.
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models_admin import UploadBatch, UploadStatus
from app.database.models_ai import AuditAction, AuditLog
from app.database.models_warehouse import FactSales
from app.etl.pipeline import run_import
from app.etl.readers import RecordsSourceReader
from app.upload import jobs
from app.utils import progress as progress_registry
from app.utils.progress import ImportCancelled, Phase, ProgressReporter

from test_data_upload import (  # noqa: F401 - fixtures are used by name
    SALES_HEADERS,
    auth,
    csv_bytes,
    login,
    sales_rows,
    upload,
    upload_client,
    wait_for_job,
)

JOB = "5c2e77a0-9d31-4f18-8a44-2b6f0c9e1d55"


@pytest.fixture(autouse=True)
def _clean_registry():
    progress_registry.clear()
    yield
    progress_registry.clear()


def sales_records(count: int, prefix: str = "CAN") -> list[dict[str, object]]:
    return [
        {"date": "2026-08-20", "invoice_no": f"{prefix}-{n:05d}",
         "sku_code": "SKU001", "territory_code": "TR001", "quantity": 1,
         "net_sales": 100}
        for n in range(count)
    ]


# ==========================================================================
# The mechanism
# ==========================================================================


def test_a_reporter_raises_once_a_stop_is_asked_for():
    reporter = progress_registry.start(JOB, operation="VALIDATE")
    reporter.phase(Phase.VALIDATING, total=100)      # fine so far

    assert progress_registry.request_cancel(JOB) is True
    with pytest.raises(ImportCancelled):
        reporter.rows(10)
    with pytest.raises(ImportCancelled):
        reporter.phase(Phase.MAPPING)


def test_finishing_a_cancelled_job_still_works():
    """``finish`` must never raise: it is what closes a cancelled job out."""
    reporter = progress_registry.start(JOB, operation="VALIDATE")
    reporter.phase(Phase.VALIDATING, total=10)
    reporter.rows(4)
    progress_registry.request_cancel(JOB)

    reporter.finish(message="Cancelled.", cancelled=True)
    final = progress_registry.snapshot(JOB)
    assert final["done"] is True
    assert final["phase"] == Phase.CANCELLED
    # Not driven to 100: the run did not finish, and a full bar would say it had.
    assert final["percent"] < 100


def test_cancelling_an_unknown_or_finished_job_is_reported_not_raised():
    assert progress_registry.request_cancel(JOB) is False   # never started

    reporter = progress_registry.start(JOB, operation="VALIDATE")
    reporter.finish(message="done")
    assert progress_registry.request_cancel(JOB) is False   # already done


def test_a_reporter_with_no_job_never_raises():
    """Scripted imports have no job and must be unaffected by any of this."""
    reporter = ProgressReporter(None)
    reporter.checkpoint()
    reporter.phase(Phase.VALIDATING, total=5)
    reporter.rows(5)


# ==========================================================================
# Data integrity — the point of the whole feature
# ==========================================================================


def test_a_cancelled_import_writes_nothing_to_the_fact_table(agent_engine):
    """Stop it mid-run and the warehouse must be untouched.

    The cancellation is triggered from a watcher thread once the pipeline has
    started, so the run is genuinely interrupted part-way rather than refused
    before it began.
    """
    before = _count_sales(agent_engine)
    reporter = progress_registry.start(JOB, operation="IMPORT")
    records = sales_records(4000)

    def cancel_once_running():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            snap = progress_registry.snapshot(JOB)
            if snap and snap["counts"]["total_records"] > 0:
                progress_registry.request_cancel(JOB)
                return
            time.sleep(0.005)

    watcher = threading.Thread(target=cancel_once_running)
    watcher.start()
    try:
        with pytest.raises(ImportCancelled):
            run_import(agent_engine, "sales", RecordsSourceReader(records),
                       source_system="TEST", dry_run=False, progress=reporter)
    finally:
        watcher.join()

    # Not one row, and not a partial batch: the transaction was rolled back.
    assert _count_sales(agent_engine) == before
    with Session(agent_engine) as session:
        staged = session.execute(
            select(func.count()).select_from(FactSales)
            .where(FactSales.invoice_no.like("CAN-%"))
        ).scalar_one()
    assert staged == 0


def _count_sales(engine) -> int:
    with Session(engine) as session:
        return session.execute(
            select(func.count()).select_from(FactSales)
        ).scalar_one()


# ==========================================================================
# The endpoint
# ==========================================================================


def test_cancelling_a_finished_import_is_refused_with_its_real_outcome(upload_client):
    """§13: the backend is the authority, and a late cancel does not rewrite it."""
    token = login(upload_client)
    finished = upload(upload_client, token, "sales",
                      csv_bytes(SALES_HEADERS, sales_rows("CAN-900"))).json()["upload"]

    response = upload_client.post(
        f"/api/data-upload/jobs/{finished['job_id']}/cancel", headers=auth(token))
    assert response.status_code == 409
    # The message names the real state rather than implying a cancel happened.
    assert "validated" in response.json()["detail"].lower()
    assert "nothing to cancel" in response.json()["detail"].lower()

    # …and the stored status is untouched by the attempt.
    detail = upload_client.get(
        f"/api/data-upload/history/{finished['upload_id']}",
        headers=auth(token)).json()
    assert detail["upload"]["status"] == UploadStatus.VALIDATED
    assert detail["upload"]["cancelled_at"] is None


def test_cancelling_an_unknown_job_is_a_404(upload_client):
    token = login(upload_client)
    response = upload_client.post(
        "/api/data-upload/jobs/00000000-0000-4000-8000-000000000000/cancel",
        headers=auth(token))
    assert response.status_code == 404


def test_a_user_cannot_cancel_another_users_import(upload_client, agent_engine):
    """§14: cancelling is an action on someone's work, gated like reading it."""
    from app.database.models_ai import AppUser
    from app.security.sections import ALLOW, SectionKey

    admin_token = login(upload_client)
    finished = upload(upload_client, admin_token, "sales",
                      csv_bytes(SALES_HEADERS, sales_rows("CAN-910"))).json()["upload"]

    with Session(agent_engine) as session:
        user_id = session.execute(
            select(AppUser.user_id).where(AppUser.username == "dhaka_rm")
        ).scalar_one()
    upload_client.put(f"/api/admin/users/{user_id}/permissions",
                      headers=auth(admin_token),
                      json={"permissions": {SectionKey.DATA_UPLOAD: ALLOW}})

    other = login(upload_client, "dhaka_rm")
    response = upload_client.post(
        f"/api/data-upload/jobs/{finished['job_id']}/cancel", headers=auth(other))
    # 403 rather than 409: they may not act on it at all, which is a different
    # answer from "it is too late", and must not leak that the job even finished.
    assert response.status_code == 403


def test_cancelling_requires_the_data_upload_section(upload_client):
    token = login(upload_client, "dhaka_rm")
    response = upload_client.post(
        f"/api/data-upload/jobs/{JOB}/cancel", headers=auth(token))
    assert response.status_code == 403


def test_a_running_import_can_be_stopped_end_to_end(upload_client, agent_engine):
    """Upload a file big enough to catch mid-flight, then stop it through the API.

    The whole feature in one test: the request answers before the work is done,
    the job is cancellable while it runs, the worker resolves the outcome, and
    the warehouse is left exactly as it was.
    """
    token = login(upload_client)
    before = _count_sales(agent_engine)
    rows = [["2026-08-20", f"CANE-{n:05d}", "SKU001", "TR001", 1, 120, 20, 100]
            for n in range(6000)]
    response = upload_client.post(
        "/api/data-upload/preview", headers=auth(token),
        files={"file": ("big.csv", csv_bytes(SALES_HEADERS, rows), "text/csv")},
        data={"upload_type": "sales", "import_mode": "UPSERT"},
    )
    assert response.status_code == 202
    job_id = response.json()["upload"]["job_id"]
    upload_id = response.json()["upload"]["upload_id"]

    # Wait until the worker has actually picked it up, so the cancel lands on a
    # run in progress rather than one still queued.
    deadline = time.monotonic() + 30
    cancelled = None
    while time.monotonic() < deadline:
        job = upload_client.get(f"/api/data-upload/jobs/{job_id}",
                                headers=auth(token)).json()
        if job["stage"] in (Phase.VALIDATING, Phase.MAPPING, Phase.READING):
            cancelled = upload_client.post(
                f"/api/data-upload/jobs/{job_id}/cancel", headers=auth(token))
            break
        if not job["is_active"]:
            pytest.skip("the import finished before it could be cancelled")
        time.sleep(0.01)

    assert cancelled is not None, "the job never reached a cancellable stage"
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["outcome"] == jobs.CancelOutcome.REQUESTED

    detail = wait_for_job(upload_client, token, upload_id)["upload"]
    assert detail["status"] == UploadStatus.CANCELLED
    assert detail["cancelled_at"] is not None
    assert detail["cancelled_by"] == "root"
    # Stopped part-way, and the history says where rather than claiming 100%.
    assert detail["progress_percent"] < 100

    # Nothing reached the warehouse: the run was one transaction and it unwound.
    assert _count_sales(agent_engine) == before

    # A stop is a decision, not a fault, and the audit trail records it as one:
    # DATA_IMPORT_CANCELLED, never DATA_IMPORT_FAILED.
    with Session(agent_engine) as session:
        actions = set(session.execute(select(AuditLog.action)).scalars())
    assert AuditAction.DATA_IMPORT_CANCELLED in actions
    assert AuditAction.DATA_IMPORT_FAILED not in actions


def test_the_worker_marks_a_cancelled_batch_and_frees_its_file(upload_client,
                                                               agent_engine,
                                                               tmp_path):
    """The worker owns the terminal status, and cleans up after itself."""
    token = login(upload_client)
    finished = upload(upload_client, token, "sales",
                      csv_bytes(SALES_HEADERS, sales_rows("CAN-930"))).json()["upload"]

    with Session(agent_engine) as session:
        batch = session.get(UploadBatch, finished["upload_id"])
        reporter = progress_registry.start(batch.upload_uuid, operation="IMPORT")
        progress_registry.request_cancel(batch.upload_uuid)

    jobs._mark_cancelled(finished["upload_id"], reporter)

    with Session(agent_engine) as session:
        batch = session.get(UploadBatch, finished["upload_id"])
    assert batch.status == UploadStatus.CANCELLED
    assert batch.cancelled_at is not None
    assert batch.stage == Phase.CANCELLED
    # The staged file is released: the batch is terminal and will not be re-run.
    assert batch.stored_path is None
