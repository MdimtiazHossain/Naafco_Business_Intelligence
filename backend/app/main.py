"""FastAPI entry point.

Phase 1 exposed master-data metadata; Phase 2 adds transaction import, ETL
monitoring, data quality and the reporting layer. AI-agent, WhatsApp and
dashboard routes belong to Phase 3+ and are deliberately absent.
"""

from __future__ import annotations

from fastapi import FastAPI

from fastapi.middleware.cors import CORSMiddleware

from .api import ALL_ROUTERS
from .config import get_settings
from .database.connection import check_connection
from .etl.calendar import FinancialYearConfig
from .master_data.schema import HIERARCHY, TABLE_SPECS, VALIDATION_RULES

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "Phase 1: master-data foundation. "
        "Phase 2: transaction warehouse, ETL and reporting layer. "
        "Phase 3: AI business intelligence agent. "
        "Phase 4: web platform, authentication and integrations."
    ),
)

# The browser app is served from a different origin in development, so the API
# declares exactly which origins may call it — never "*" with credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-User"],
)

for _router in ALL_ROUTERS:
    app.include_router(_router)


@app.on_event("startup")
def close_interrupted_imports() -> None:
    """Mark imports that a restart killed, before the first request is served.

    A worker thread dies with its process and its transaction dies with it, so
    nothing an interrupted import was writing ever reached the warehouse. What it
    leaves behind is a batch row still claiming to be running, which would sit in
    the upload dock forever waiting for a worker that no longer exists. Sweeping
    at startup is what turns "stuck" into "failed, upload it again".

    Non-fatal for the same reason as the seed above: a database that is
    unreachable at boot is the health probe's problem.
    """
    import logging

    from sqlalchemy.orm import Session

    from .database.connection import get_engine
    from .targetmgmt.jobs import sweep_interrupted as sweep_allocations
    from .upload.jobs import sweep_interrupted

    logger = logging.getLogger("app.startup")
    try:
        with Session(get_engine()) as session:
            swept = sweep_interrupted(session)
            # Allocation runs die with their process too, and leave a job row
            # stuck at 40% waiting for a worker that no longer exists.
            allocations = sweep_allocations(session)
            session.commit()
        if swept:
            logger.warning("closed %s import(s) interrupted by a restart", swept)
        if allocations:
            logger.warning("closed %s allocation(s) interrupted by a restart",
                           allocations)
    except Exception:  # noqa: BLE001 - never block startup
        logger.warning("could not sweep interrupted imports", exc_info=True)


@app.on_event("shutdown")
def stop_import_workers() -> None:
    """Stop accepting import jobs on the way down.

    Not waited on: a running import holds an uncommitted transaction, so letting
    the process exit rolls it back — which is the right outcome for a run that
    was interrupted, and the same one blocking would reach more slowly. The
    startup sweep closes the batch row on the way back up.
    """
    from .targetmgmt.jobs import shutdown as shutdown_allocations
    from .upload.jobs import shutdown

    shutdown(wait=False)
    shutdown_allocations(wait=False)


@app.get("/health", tags=["system"])
def health() -> dict[str, object]:
    """Liveness plus a database connectivity probe.

    Deliberately reports only whether the database answered — never the host,
    user or connection string.
    """
    config = FinancialYearConfig.from_settings()
    connected = check_connection()
    return {
        "status": "ok",
        "version": settings.app_version,
        "database": "connected" if connected else "unavailable",
        "environment": settings.environment,
        "database_connected": connected,
        "financial_year_start_month": config.start_month,
    }


@app.get("/master-data/schema", tags=["master-data"])
def master_data_schema() -> dict[str, object]:
    """The dimension model as discovered from the master workbook."""
    return {
        "hierarchy": list(HIERARCHY),
        "tables": [
            {
                "table": spec.table,
                "sheet": spec.sheet,
                "surrogate_key": spec.surrogate_key,
                "business_key": spec.business_key,
                "parent_table": spec.parent_table,
                "parent_column": spec.parent_column,
                "parent_key": spec.parent_key,
                "columns": [
                    {
                        "column": c.column,
                        "source_field": c.source_field,
                        "sql_type": c.sql_type,
                        "nullable": c.nullable,
                        "description": c.description,
                    }
                    for c in spec.columns
                ],
            }
            for spec in TABLE_SPECS
        ],
    }


@app.get("/master-data/validation-rules", tags=["master-data"])
def validation_rules() -> list[dict[str, str]]:
    """The data-quality rule catalogue applied to the master data."""
    return [
        {
            "rule_id": r.rule_id,
            "name": r.name,
            "severity": r.severity,
            "description": r.description,
            "applies_to": r.applies_to,
        }
        for r in VALIDATION_RULES
    ]
