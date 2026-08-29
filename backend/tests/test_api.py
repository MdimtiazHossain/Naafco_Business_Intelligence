"""API tests: import, ETL monitoring, data quality and reporting endpoints."""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_session
from app.main import app
from conftest_phase2 import sales_row

pytest.importorskip("multipart", reason="python-multipart is required for file uploads")


@pytest.fixture
def client(seeded_engine, monkeypatch):
    """A test client whose sessions and engine point at the seeded warehouse.

    Phase 4 closed the hole these endpoints had: import, ETL and reporting once
    accepted anonymous callers and now require an authenticated user holding the
    matching section. The client therefore signs every request as a super
    administrator via the development ``X-User`` header, which keeps these Phase
    2 tests exercising what they were written to exercise — the pipeline, not the
    permission chain, which has its own tests.
    """
    from sqlalchemy.orm import Session

    import app.api.routes_import as routes_import
    from app.database.models_ai import AppUser, Role

    monkeypatch.setattr(routes_import, "get_engine", lambda *a, **k: seeded_engine)

    with Session(seeded_engine) as session:
        session.add(AppUser(username="etl_admin", display_name="ETL Administrator",
                            role=Role.SUPER_ADMIN, is_active=True))
        session.commit()

    def _session_override():
        session = Session(bind=seeded_engine, expire_on_commit=False, future=True)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _session_override
    try:
        yield TestClient(app, headers={"X-User": "etl_admin"})
    finally:
        app.dependency_overrides.clear()


def _csv_bytes(rows: list[dict]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


# --------------------------------------------------------------------------
# System
# --------------------------------------------------------------------------


def test_health(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["financial_year_start_month"] == 7
    # The health probe must never leak connection details.
    assert not any("password" in key or "host" in key for key in body)


def test_datasets_endpoint_documents_business_keys(client) -> None:
    body = client.get("/api/etl/datasets").json()
    assert set(body["data_types"]) == {
        "sales", "material_stock", "target", "credit_invoice"}
    sales = next(d for d in body["datasets"] if d["data_type"] == "sales")
    assert sales["business_key"] == (
        "company_code + invoice_no + invoice_line_no + source_system when "
        "available, otherwise company_code + invoice_no + material_code + "
        "batch_code + source_system"
    )
    # Net is the required measure; gross is the optional breakdown behind it.
    assert "net_sales" in sales["required_fields"]
    assert "gross_sales" not in sales["required_fields"]


def test_error_code_catalogue(client) -> None:
    codes = {row["error_code"] for row in client.get("/api/etl/error-codes").json()}
    assert {"INVALID_MATERIAL_CODE", "REGION_ZONE_MISMATCH", "AMBIGUOUS_DATE",
            "DUPLICATE_IN_FILE", "NEGATIVE_NOT_ALLOWED"} <= codes


def test_master_source_status_reports_pending_dimensions(client) -> None:
    body = client.get("/api/data-quality/master-sources/status").json()
    assert set(body["pending"]) == {"dim_customer", "dim_sales_force"}


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------


def test_import_csv_upload(client) -> None:
    payload = _csv_bytes([sales_row()])
    response = client.post(
        "/api/import/sales",
        files={"file": ("sales.csv", payload, "text/csv")},
        data={"source_system": "SAP"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["totals"]["valid_rows"] == 1
    assert body["source_file"] == "sales.csv"


def test_import_records_json_is_the_integration_seam(client) -> None:
    response = client.post(
        "/api/import/sales/records",
        json={"records": [sales_row()], "source_system": "SALES_APP"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source_system"] == "SALES_APP"
    assert body["totals"]["inserted_rows"] == 1


def test_import_rejects_unknown_data_type(client) -> None:
    response = client.post("/api/import/widgets/records", json={"records": [{}]})
    assert response.status_code == 404
    assert "Unknown data type" in response.json()["detail"]


def test_import_rejects_unsupported_file_type(client) -> None:
    response = client.post(
        "/api/import/sales",
        files={"file": ("sales.pdf", b"%PDF", "application/pdf")},
    )
    assert response.status_code == 415


def test_import_rejects_empty_payload(client) -> None:
    response = client.post("/api/import/sales/records", json={"records": []})
    assert response.status_code == 400


def test_import_dry_run_writes_nothing(client) -> None:
    response = client.post(
        "/api/import/sales/records",
        json={"records": [sales_row()], "dry_run": True},
    )
    assert response.status_code == 201
    assert client.get("/api/etl/batches").json()["total"] == 0


# --------------------------------------------------------------------------
# ETL monitoring and data quality
# --------------------------------------------------------------------------


def test_batch_listing_and_detail(client) -> None:
    client.post("/api/import/sales/records", json={"records": [sales_row()]})

    listing = client.get("/api/etl/batches").json()
    assert listing["total"] == 1
    batch_id = listing["batches"][0]["batch_id"]

    detail = client.get(f"/api/etl/batches/{batch_id}").json()
    assert detail["data_type"] == "sales"
    assert detail["quality"]["totals"]["valid"] == 1


def test_missing_batch_is_a_404(client) -> None:
    assert client.get("/api/etl/batches/999999").status_code == 404
    assert client.get("/api/data-quality/999999").status_code == 404


def test_data_quality_breaks_rejections_down_by_reason(client) -> None:
    records = [
        sales_row(),
        sales_row(**{"Invoice No": "INV-2", "SKU Code": "NOPE"}),
        sales_row(**{"Invoice No": "INV-3", "Region Code": "R999",
                     "Territory Code": None}),
        sales_row(**{"Invoice No": "INV-4", "Date": "31/02/2026"}),
        sales_row(**{"Invoice No": "INV-5", "Quantity": "abc"}),
        sales_row(),  # duplicate of the first row
    ]
    response = client.post("/api/import/sales/records", json={"records": records})
    batch_id = response.json()["batch_id"]

    quality = client.get(f"/api/data-quality/{batch_id}?include_records=true").json()
    assert quality["totals"]["total_imported"] == 6
    assert quality["totals"]["valid"] == 1
    assert quality["totals"]["rejected"] == 5

    categories = quality["by_category"]
    assert categories["INVALID_MASTER"] == 2      # bad SKU + bad region
    assert categories["INVALID_DATE"] == 1
    assert categories["INVALID_NUMERIC"] == 1
    assert categories["DUPLICATE"] == 1

    codes = {row["error_code"] for row in quality["by_error_code"]}
    assert "INVALID_MATERIAL_CODE" in codes and "IMPOSSIBLE_DATE" in codes
    assert quality["rejected_records"]["total"] == 5
    # Every rejected row keeps its original content for correction.
    assert all(r["raw_data"] for r in quality["rejected_records"]["records"])


def test_quality_overview_aggregates_batches(client) -> None:
    client.post("/api/import/sales/records", json={"records": [sales_row()]})
    client.post("/api/import/sales/records",
                json={"records": [sales_row(**{"SKU Code": "NOPE"})]})

    overview = client.get("/api/data-quality?data_type=sales").json()
    assert overview["batches"] == 2
    assert overview["totals"]["total_imported"] == 2
    assert overview["totals"]["rejected"] == 1
    assert overview["totals"]["rejection_rate_percent"] == 50.0


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", ["sales", "stock", "target"])
def test_report_endpoints_respond(client, endpoint: str) -> None:
    response = client.get(f"/api/reports/{endpoint}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"filters", "metrics", "rows"}


@pytest.mark.parametrize("endpoint", ["collection", "outstanding"])
def test_the_retired_report_endpoints_are_gone(client, endpoint: str) -> None:
    """404, not an empty report: the route was removed with its module."""
    assert client.get(f"/api/reports/{endpoint}").status_code == 404
    assert client.get(f"/api/pages/{endpoint}").status_code == 404


def test_sales_report_endpoint_returns_metrics(client) -> None:
    client.post("/api/import/sales/records", json={"records": [sales_row()]})
    body = client.get("/api/reports/sales?date_to=2026-08-15").json()
    assert body["metrics"]["total"]["net_sales"] == 1000.0
    assert body["metrics"]["financial_year"] == "FY 2026-27"
    assert len(body["rows"]) == 1


def test_report_filters_are_bound_not_interpolated(client) -> None:
    """A SQL-injection style filter value is treated as data and matches nothing."""
    client.post("/api/import/sales/records", json={"records": [sales_row()]})
    body = client.get(
        "/api/reports/sales", params={"region_code": "REG001'; DROP TABLE fact_sales;--"}
    ).json()
    assert body["rows"] == []
    # The table is still there and still holds the row.
    assert client.get("/api/reports/sales").json()["rows"]


def test_report_limit_is_bounded(client) -> None:
    assert client.get("/api/reports/sales?limit=100000").status_code == 422
    assert client.get("/api/reports/sales?limit=10").status_code == 200
