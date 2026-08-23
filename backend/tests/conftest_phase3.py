"""Phase 3 fixtures: users with data scopes, loaded facts and an agent.

Master data and users here are **test doubles** in a throwaway SQLite database.
The real warehouse, the real workbook and the real user table are untouched.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.ai.agent import BusinessIntelligenceAgent
from app.ai.llm import NullLLMClient
from app.ai.orchestrator import Orchestrator
from app.ai.permission_filter import UserContext
from app.database.models_ai import AppUser, Role
from app.etl.pipeline import run_import
from app.etl.readers import RecordsSourceReader
from conftest_phase2 import (
    material_stock_row,
    sales_row,
    target_row,
)

#: A fixed "today" so period resolution is reproducible.
TODAY = dt.date(2026, 8, 15)


def _load(engine: Engine, data_type: str, records: list[dict[str, Any]]) -> None:
    reader = RecordsSourceReader(records, source_name="t.csv", source_type="CSV")
    result = run_import(engine, data_type, reader, source_system="TEST")
    assert result.rejected_rows == 0, result.error_counts


@pytest.fixture
def agent_engine(seeded_engine: Engine) -> Engine:
    """Warehouse seeded with master data plus a realistic spread of facts.

    Two regions (REG001 Dhaka, REG002 Khulna) so permission scoping is testable,
    and both the current and previous month so growth and root cause work.
    """
    _load(seeded_engine, "sales", [
        # Current month, Dhaka (via territory TR001).
        sales_row(**{"Invoice No": "INV-D1", "Date": "2026-08-10", "Quantity": 100,
                     "Gross Sales": 1_200_000, "Discount": 200_000, "Cost": 700_000}),
        sales_row(**{"Invoice No": "INV-D2", "Date": "2026-08-15", "Quantity": 50,
                     "Gross Sales": 600_000, "Discount": 100_000, "Cost": 350_000,
                     "SKU Code": "SKU002"}),
        # Current month, Khulna (region REG002 / area AR002).
        sales_row(**{"Invoice No": "INV-K1", "Date": "2026-08-12", "Quantity": 20,
                     "Gross Sales": 300_000, "Discount": 0, "Cost": 200_000,
                     "Territory Code": None, "Area Code": "AR002"}),
        # Previous month, Dhaka — larger, so the current month shows a decline.
        sales_row(**{"Invoice No": "INV-P1", "Date": "2026-07-10", "Quantity": 200,
                     "Gross Sales": 2_400_000, "Discount": 400_000, "Cost": 1_400_000}),
        sales_row(**{"Invoice No": "INV-P2", "Date": "2026-07-20", "Quantity": 30,
                     "Gross Sales": 400_000, "Discount": 0, "Cost": 250_000,
                     "Territory Code": None, "Area Code": "AR002"}),
    ])
    # Material stock: one position per expiry bucket against TODAY (2026-08-15)
    # and the default 90-day horizon, so every bucket an answer can report has
    # data behind it. Two storage locations, material groups and material brands
    # so the breakdowns group over more than one value, and three materials —
    # two of them inside the same group and brand — so a material breakdown is
    # not just the group breakdown under another name.
    #
    # Each row states the group and brand the Material Master records for its
    # material; a row that disagreed would be rejected, which is the check
    # ``test_etl_pipeline`` covers directly.
    _load(seeded_engine, "material_stock", [
        # Already expired.
        material_stock_row(**{"Unrestricted": 40, "Quality Inspection": 0,
                              "Blocked": 10, "In Transit": 0,
                              "Production Date": "2025-01-01",
                              "Shelf Life Expiration Date": "2026-06-30"}),
        # Inside the 90-day horizon, and the second material of the same group.
        material_stock_row(**{"Material": "MAT-003",
                              "Unrestricted": 200, "Quality Inspection": 50,
                              "Blocked": 0, "In Transit": 25,
                              "Production Date": "2026-02-01",
                              "Shelf Life Expiration Date": "2026-09-30"}),
        # Comfortably valid, and in the other storage location / group.
        material_stock_row(**{"Storage Location": "SL02", "Material": "MAT-002",
                              "Material Group": "MG02", "Material Brand": "MB02",
                              "Unrestricted": 500, "Quality Inspection": 0,
                              "Blocked": 0, "In Transit": 0,
                              "Production Date": "2026-05-01",
                              "Shelf Life Expiration Date": "2027-12-31"}),
        # No shelf life stated — its own bucket, never treated as expired.
        material_stock_row(**{"Storage Location": "SL02", "Material": "MAT-002",
                              "Material Group": "MG02", "Material Brand": "MB02",
                              "Unrestricted": 75, "Quality Inspection": 0,
                              "Blocked": 0, "In Transit": 0,
                              "Production Date": "2026-05-02",
                              "Shelf Life Expiration Date": None}),
    ])
    # A target is set for a *territory*, and the Khulna sales above are booked at
    # area level because the seed had nothing below AR002. Completing that branch
    # is what lets the out-of-scope target exist at all, and it puts that target
    # in the same region as the sales it is measured against.
    _complete_khulna_branch(seeded_engine)
    _load(seeded_engine, "target", [
        target_row(**{"Target Amount": 2_000_000}),
        target_row(**{"Target Amount": 1_000_000, "Territory Code": "TR002"}),
    ])
    return seeded_engine


def _complete_khulna_branch(engine: Engine) -> None:
    """A unit and a territory under AR002, so Khulna can carry a target.

    ``UN002`` and ``TR002`` are the codes the map and data-management suites
    already use for this branch, so it stays one Khulna rather than becoming two
    with different names.
    """
    from app.database.models import DimTerritory, DimUnit

    with Session(engine) as session:
        session.add(DimUnit(unit_code="UN002", unit_name="Khulna Unit 1",
                            area_code="AR002"))
        session.add(DimTerritory(territory_code="TR002", territory_name="Daulatpur",
                                 unit_code="UN002"))
        session.commit()


USERS: dict[str, dict[str, Any]] = {
    "ceo": {"role": Role.MANAGEMENT, "scope": None,
            "display_name": "Managing Director"},
    "dhaka_rm": {"role": Role.REGIONAL_MANAGER,
                 "scope": {"region_code": ["REG001"]},
                 "display_name": "Dhaka Regional Manager"},
    "khulna_rm": {"role": Role.REGIONAL_MANAGER,
                  "scope": {"region_code": ["REG002"]},
                  "display_name": "Khulna Regional Manager"},
    "mirpur_am": {"role": Role.AREA_MANAGER, "scope": {"area_code": ["AR001"]},
                  "display_name": "Mirpur Area Manager"},
    "kazipara_tm": {"role": Role.TERRITORY_MANAGER,
                    "scope": {"territory_code": ["TR001"]},
                    "display_name": "Kazipara Territory Manager"},
    "no_scope": {"role": Role.SALES_OFFICER, "scope": None,
                 "display_name": "Officer without a scope"},
}


@pytest.fixture
def users(agent_engine: Engine) -> dict[str, UserContext]:
    """Create the test users and return their contexts by username."""
    contexts: dict[str, UserContext] = {}
    with Session(agent_engine) as session:
        for username, spec in USERS.items():
            user = AppUser(
                username=username,
                display_name=spec["display_name"],
                role=spec["role"],
                data_scope=spec["scope"],
                is_active=True,
            )
            session.add(user)
            session.flush()
            contexts[username] = UserContext.from_user(user)
        session.commit()
    return contexts


@pytest.fixture
def session(agent_engine: Engine) -> Iterator[Session]:
    with Session(agent_engine, expire_on_commit=False) as active:
        yield active


@pytest.fixture
def make_agent(session: Session, users: dict[str, UserContext]):
    """Factory: ``make_agent("dhaka_rm") -> BusinessIntelligenceAgent``."""

    def _make(username: str = "ceo", llm=None) -> BusinessIntelligenceAgent:
        return BusinessIntelligenceAgent(
            session, users[username], llm=llm or NullLLMClient(), today=TODAY
        )

    return _make


@pytest.fixture
def make_orchestrator(session: Session, users: dict[str, UserContext]):
    """Factory: ``make_orchestrator("ceo") -> Orchestrator``."""

    def _make(username: str = "ceo", llm=None) -> Orchestrator:
        return Orchestrator(session, users[username], llm=llm or NullLLMClient(),
                            today=TODAY)

    return _make
