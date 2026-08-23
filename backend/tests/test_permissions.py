"""Section permissions, user administration and the audit trail.

The scenario from the Phase 4 specification is exercised end to end: a manager
with Dashboard, Sales and Target allowed and Stock, Data Upload and Admin
denied must get 200 on the first three and 403 on the rest — including when the
API URL is typed directly, because the frontend is not the control.

The specification named Collection as the third allowed section. That module
left the platform in revision 0020, so Target stands in its place; what the
scenario is actually testing — that several grants and several denials coexist,
and that a denial holds on every route serving the section — is unchanged.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.auth.permissions import allowed_sections, has_section, resolve_section
from app.auth.security import hash_password
from app.database.models_admin import UserSectionPermission
from app.database.models_ai import AppUser, AuditAction, AuditLog, Role, UserStatus
from app.main import app
from app.security.sections import ALLOW, DENY, SECTION_BY_KEY, SectionKey
from conftest_phase3 import TODAY

pytest.importorskip("multipart", reason="python-multipart is required by FastAPI forms")

PASSWORD = "Correct-Horse-9"
WINDOW = "?date_from=2026-08-01&date_to=2026-08-31"


@pytest.fixture
def platform(agent_engine, users, monkeypatch):
    """A client with a super administrator and passwords on every seeded user."""
    import app.ai.agent as agent_module

    original_init = agent_module.BusinessIntelligenceAgent.__init__

    def pinned_init(self, session, user, llm=None, today=None):
        original_init(self, session, user, llm=llm, today=today or TODAY)

    monkeypatch.setattr(agent_module.BusinessIntelligenceAgent, "__init__", pinned_init)

    with Session(agent_engine) as session:
        for user in session.query(AppUser).all():
            user.password_hash = hash_password(PASSWORD)
        session.add(AppUser(username="root", display_name="Administrator",
                            role=Role.SUPER_ADMIN, is_active=True,
                            status=UserStatus.ACTIVE,
                            password_hash=hash_password(PASSWORD)))
        session.commit()

    def _session_override():
        session = Session(bind=agent_engine, expire_on_commit=False, future=True)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _session_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def login(client: TestClient, username: str, password: str = PASSWORD) -> str:
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ==========================================================================
# 1. User creation
# ==========================================================================


def test_admin_creates_a_user_with_role_scope_and_permissions(platform, agent_engine):
    token = login(platform, "root")
    response = platform.post("/api/admin/users", headers=auth(token), json={
        "username": "rahim",
        "password": PASSWORD,
        "role": Role.REGIONAL_MANAGER,
        "display_name": "Rahim",
        "employee_id": "EMP-9001",
        "email": "rahim@example.com",
        "department": "Sales",
        "designation": "Regional Manager",
        "status": UserStatus.ACTIVE,
        "data_scope": {"region_code": ["REG001"]},
        "section_permissions": {SectionKey.STOCK: DENY},
    })
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["username"] == "rahim"
    assert body["role"] == Role.REGIONAL_MANAGER
    assert body["status"] == UserStatus.ACTIVE
    assert body["department"] == "Sales"
    assert body["designation"] == "Regional Manager"
    assert body["data_scope"] == {"region_code": ["REG001"]}
    # The password is never echoed back, in any form.
    assert "password" not in body and "password_hash" not in body

    with Session(agent_engine) as session:
        rows = session.execute(
            select(UserSectionPermission).join(
                AppUser, AppUser.user_id == UserSectionPermission.user_id
            ).where(AppUser.username == "rahim")
        ).scalars().all()
        assert {row.section_key: row.access for row in rows} == {SectionKey.STOCK: DENY}


def test_creating_a_user_is_audited(platform, agent_engine):
    token = login(platform, "root")
    platform.post("/api/admin/users", headers=auth(token), json={
        "username": "audited", "password": PASSWORD, "role": Role.VIEWER,
    })
    with Session(agent_engine) as session:
        actions = {
            row.action for row in session.execute(
                select(AuditLog).where(AuditLog.resource == "user:audited")
            ).scalars()
        }
    assert AuditAction.USER_CREATED in actions


def test_a_non_admin_cannot_create_a_user(platform):
    token = login(platform, "dhaka_rm")
    response = platform.post("/api/admin/users", headers=auth(token), json={
        "username": "sneaky", "password": PASSWORD, "role": Role.SUPER_ADMIN,
    })
    assert response.status_code == 403


# ==========================================================================
# 2. Role assignment
# ==========================================================================


def test_changing_a_role_is_recorded_as_its_own_audit_event(platform, agent_engine):
    token = login(platform, "root")
    created = platform.post("/api/admin/users", headers=auth(token), json={
        "username": "promoted", "password": PASSWORD, "role": Role.VIEWER,
    }).json()

    response = platform.patch(f"/api/admin/users/{created['user_id']}",
                              headers=auth(token), json={"role": Role.AREA_MANAGER})
    assert response.status_code == 200
    assert response.json()["role"] == Role.AREA_MANAGER

    with Session(agent_engine) as session:
        entry = session.execute(
            select(AuditLog).where(AuditLog.action == AuditAction.ROLE_CHANGED)
        ).scalars().first()
    assert entry is not None
    assert entry.detail["old"] == Role.VIEWER
    assert entry.detail["new"] == Role.AREA_MANAGER


def test_the_last_administrator_cannot_be_demoted(platform, agent_engine):
    token = login(platform, "root")
    with Session(agent_engine) as session:
        root_id = session.execute(
            select(AppUser.user_id).where(AppUser.username == "root")
        ).scalar_one()

    response = platform.patch(f"/api/admin/users/{root_id}", headers=auth(token),
                              json={"role": Role.VIEWER})
    assert response.status_code == 409


def test_deactivating_a_user_blocks_their_login(platform, agent_engine):
    token = login(platform, "root")
    with Session(agent_engine) as session:
        user_id = session.execute(
            select(AppUser.user_id).where(AppUser.username == "khulna_rm")
        ).scalar_one()

    response = platform.patch(f"/api/admin/users/{user_id}", headers=auth(token),
                              json={"status": UserStatus.LOCKED})
    assert response.status_code == 200
    assert response.json()["status"] == UserStatus.LOCKED
    # ``is_active`` is derived from the status, so the login path sees the lock.
    assert response.json()["is_active"] is False

    refused = platform.post("/api/auth/login",
                            json={"username": "khulna_rm", "password": PASSWORD})
    assert refused.status_code == 401


# ==========================================================================
# 3 & 4. Allow and Deny
# ==========================================================================


def test_role_defaults_allow_reporting_and_withhold_administration():
    for key in (SectionKey.SALES, SectionKey.DASHBOARD, SectionKey.STOCK):
        section = SECTION_BY_KEY[key]
        assert section.role_default(Role.REGIONAL_MANAGER) is True
    for key in (SectionKey.ADMIN, SectionKey.DATA_UPLOAD):
        section = SECTION_BY_KEY[key]
        assert section.role_default(Role.REGIONAL_MANAGER) is False
        assert section.role_default(Role.SUPER_ADMIN) is True


def test_a_user_override_narrows_but_the_role_ceiling_is_absolute():
    stock = SECTION_BY_KEY[SectionKey.STOCK]
    admin = SECTION_BY_KEY[SectionKey.ADMIN]

    # A DENY on an otherwise-allowed section takes effect.
    denied = resolve_section(stock, Role.REGIONAL_MANAGER,
                             user_overrides={SectionKey.STOCK: DENY})
    assert denied.allowed is False
    assert denied.source == "USER_PERMISSION"

    # An ALLOW on a section the role may never hold changes nothing.
    escalation = resolve_section(admin, Role.REGIONAL_MANAGER,
                                 user_overrides={SectionKey.ADMIN: ALLOW})
    assert escalation.allowed is False
    assert escalation.locked is True
    assert escalation.source == "ROLE_LOCK"


def test_a_user_override_can_grant_a_section_the_role_default_withheld():
    upload = SECTION_BY_KEY[SectionKey.DATA_UPLOAD]
    decision = resolve_section(upload, Role.REGIONAL_MANAGER,
                               user_overrides={SectionKey.DATA_UPLOAD: ALLOW})
    assert decision.allowed is True
    assert decision.role_allowed is False


def test_setting_permissions_replaces_the_previous_set(platform, agent_engine):
    token = login(platform, "root")
    created = platform.post("/api/admin/users", headers=auth(token), json={
        "username": "perms", "password": PASSWORD, "role": Role.AREA_MANAGER,
        "data_scope": {"area_code": ["AR001"]},
    }).json()
    user_id = created["user_id"]

    first = platform.put(f"/api/admin/users/{user_id}/permissions",
                         headers=auth(token),
                         json={"permissions": {SectionKey.STOCK: DENY,
                                               SectionKey.TARGET: DENY}})
    assert first.status_code == 200

    second = platform.put(f"/api/admin/users/{user_id}/permissions",
                          headers=auth(token),
                          json={"permissions": {SectionKey.STOCK: DENY}})
    assert second.status_code == 200

    by_key = {row["key"]: row for row in second.json()["sections"]}
    assert by_key[SectionKey.STOCK]["access"] == DENY
    # Target fell back to the role default rather than keeping the old DENY.
    assert by_key[SectionKey.TARGET]["access"] == ALLOW
    assert by_key[SectionKey.TARGET]["user_access"] is None


def test_permission_change_is_audited_with_old_and_new_values(platform, agent_engine):
    token = login(platform, "root")
    created = platform.post("/api/admin/users", headers=auth(token), json={
        "username": "tracked", "password": PASSWORD, "role": Role.VIEWER,
    }).json()

    platform.put(f"/api/admin/users/{created['user_id']}/permissions",
                 headers=auth(token), json={"permissions": {SectionKey.SALES: DENY}})

    with Session(agent_engine) as session:
        entry = session.execute(
            select(AuditLog).where(AuditLog.action == AuditAction.PERMISSION_CHANGED)
        ).scalars().first()
    assert entry is not None
    assert entry.detail["old"] == {}
    assert entry.detail["new"] == {SectionKey.SALES: DENY}


def test_an_unknown_section_is_rejected(platform):
    token = login(platform, "root")
    created = platform.post("/api/admin/users", headers=auth(token), json={
        "username": "typo", "password": PASSWORD, "role": Role.VIEWER,
    }).json()
    response = platform.put(f"/api/admin/users/{created['user_id']}/permissions",
                            headers=auth(token),
                            json={"permissions": {"sails": ALLOW}})
    assert response.status_code == 422
    assert "sails" in response.json()["detail"]


def test_an_invalid_access_value_is_rejected(platform):
    token = login(platform, "root")
    created = platform.post("/api/admin/users", headers=auth(token), json={
        "username": "maybe", "password": PASSWORD, "role": Role.VIEWER,
    }).json()
    response = platform.put(f"/api/admin/users/{created['user_id']}/permissions",
                            headers=auth(token),
                            json={"permissions": {SectionKey.SALES: "MAYBE"}})
    assert response.status_code == 422


# ==========================================================================
# 5. The specification's permission test, end to end
# ==========================================================================


@pytest.fixture
def user_a(platform):
    """User A: an existing manager role, with the specified section permissions."""
    token = login(platform, "root")
    created = platform.post("/api/admin/users", headers=auth(token), json={
        "username": "user_a",
        "password": PASSWORD,
        "role": Role.REGIONAL_MANAGER,
        "display_name": "User A",
        "data_scope": {"region_code": ["REG001"]},
    }).json()
    platform.put(f"/api/admin/users/{created['user_id']}/permissions",
                 headers=auth(token), json={"permissions": {
                     SectionKey.DASHBOARD: ALLOW,
                     SectionKey.SALES: ALLOW,
                     # Target stands where Collection did: a second allowed
                     # section, so the test still proves several grants and
                     # several denials coexist. Collection left with its module
                     # in revision 0020 and there is no such section to grant.
                     SectionKey.TARGET: ALLOW,
                     SectionKey.STOCK: DENY,
                     SectionKey.DATA_UPLOAD: DENY,
                     SectionKey.ADMIN: DENY,
                 }})
    return login(platform, "user_a")


@pytest.mark.parametrize("path,expected", [
    (f"/api/dashboard{WINDOW}", 200),
    (f"/api/pages/sales{WINDOW}", 200),
    (f"/api/pages/target{WINDOW}", 200),
    (f"/api/pages/stock{WINDOW}", 403),
    ("/api/data-upload/types", 403),
    ("/api/admin/users", 403),
])
def test_user_a_gets_exactly_the_sections_they_were_granted(platform, user_a,
                                                            path, expected):
    assert platform.get(path, headers=auth(user_a)).status_code == expected


def test_a_denied_section_is_refused_on_every_route_that_serves_it(platform, user_a):
    """Typing the URL is not a way around a denied section."""
    for path in (
        f"/api/pages/stock{WINDOW}",
        f"/api/transactions/material_stock{WINDOW}",
        f"/api/reports/stock{WINDOW}",
    ):
        assert platform.get(path, headers=auth(user_a)).status_code == 403, path


def test_an_allowed_section_still_serves_its_transaction_table(platform, user_a):
    response = platform.get(f"/api/pages/transactions/sales{WINDOW}",
                            headers=auth(user_a))
    assert response.status_code == 200


def test_the_profile_reports_the_sections_the_backend_will_serve(platform, user_a):
    body = platform.get("/api/auth/me", headers=auth(user_a)).json()
    sections = body["sections"]
    assert sections[SectionKey.SALES] is True
    assert sections[SectionKey.STOCK] is False
    assert sections[SectionKey.ADMIN] is False
    assert sections[SectionKey.DATA_UPLOAD] is False


def test_a_denial_is_written_to_the_audit_log(platform, user_a, agent_engine):
    platform.get(f"/api/pages/stock{WINDOW}", headers=auth(user_a))
    with Session(agent_engine) as session:
        entry = session.execute(
            select(AuditLog)
            .where(AuditLog.action == AuditAction.PERMISSION_DENIED)
            .where(AuditLog.username == "user_a")
        ).scalars().first()
    assert entry is not None
    assert entry.success is False
    assert entry.resource == f"section:{SectionKey.STOCK}"


def test_the_denial_message_does_not_reveal_why(platform, user_a):
    """A refusal must not become a way to map another account's permissions."""
    denied = platform.get(f"/api/pages/stock{WINDOW}", headers=auth(user_a))
    assert denied.json()["detail"] == (
        "You don't have permission to access this information."
    )


# ==========================================================================
# 6. Data scope stays separate from section access
# ==========================================================================


def test_section_allow_still_respects_the_data_scope(platform, user_a):
    """Sales = ALLOW and scope = Dhaka means Dhaka sales, not all sales."""
    allowed = platform.get(f"/api/pages/sales{WINDOW}&region_code=REG001",
                           headers=auth(user_a))
    assert allowed.status_code == 200

    outside = platform.get(f"/api/pages/sales{WINDOW}&region_code=REG002",
                           headers=auth(user_a))
    assert outside.status_code == 403
    # The scope refusal explains what the user *can* see; the section refusal
    # deliberately does not. They are different layers with different messages.
    assert "REG002" in outside.json()["detail"]


def test_an_unscoped_report_endpoint_narrows_to_the_users_own_scope(platform, user_a):
    body = platform.get(f"/api/reports/sales{WINDOW}", headers=auth(user_a)).json()
    assert body["filters"]["region_code"] == "REG001"


def test_a_user_with_no_scope_is_refused_business_data(platform):
    token = login(platform, "no_scope")
    response = platform.get(f"/api/pages/sales{WINDOW}", headers=auth(token))
    assert response.status_code == 403
    assert "scope" in response.json()["detail"].lower()


# ==========================================================================
# 7. Role permissions
# ==========================================================================


def test_a_role_default_can_be_changed_by_a_super_admin(platform, agent_engine):
    token = login(platform, "root")
    response = platform.put(f"/api/admin/roles/{Role.VIEWER}/permissions",
                            headers=auth(token),
                            json={"permissions": {SectionKey.SALES: DENY}})
    assert response.status_code == 200
    by_key = {row["key"]: row for row in response.json()["sections"]}
    assert by_key[SectionKey.SALES]["access"] == DENY
    assert by_key[SectionKey.SALES]["catalogue_default"] == ALLOW

    with Session(agent_engine) as session:
        viewer = AppUser(username="viewer_x", role=Role.VIEWER, is_active=True)
        session.add(viewer)
        session.commit()
        assert has_section(session, _context(viewer), SectionKey.SALES) is False


def _context(user: AppUser):
    from app.ai.permission_filter import UserContext

    return UserContext.from_user(user)


def test_a_plain_admin_cannot_change_role_defaults(platform, agent_engine):
    with Session(agent_engine) as session:
        session.add(AppUser(username="plain_admin", role=Role.ADMIN, is_active=True,
                            status=UserStatus.ACTIVE,
                            password_hash=hash_password(PASSWORD)))
        session.commit()
    token = login(platform, "plain_admin")
    response = platform.put(f"/api/admin/roles/{Role.VIEWER}/permissions",
                            headers=auth(token),
                            json={"permissions": {SectionKey.SALES: DENY}})
    assert response.status_code == 403


def test_the_section_catalogue_is_served_from_the_application_configuration(platform):
    token = login(platform, "root")
    body = platform.get("/api/admin/sections", headers=auth(token)).json()
    keys = {section["key"] for section in body["sections"]}
    assert {SectionKey.DASHBOARD, SectionKey.SALES, SectionKey.DATA_UPLOAD,
            SectionKey.ADMIN} <= keys
    admin = next(s for s in body["sections"] if s["key"] == SectionKey.ADMIN)
    assert admin["locked_to_roles"] == list(Role.ADMIN_ROLES)
    assert body["access_values"] == [ALLOW, DENY]


def test_allowed_sections_covers_every_catalogue_entry(agent_engine, users):
    with Session(agent_engine) as session:
        user = session.execute(
            select(AppUser).where(AppUser.username == "ceo")
        ).scalar_one()
        sections = allowed_sections(session, user.user_id, user.role)
    assert set(sections) == set(SECTION_BY_KEY)


# ==========================================================================
# 8. Admin dashboard
# ==========================================================================


def test_admin_summary_counts_users_by_status(platform):
    token = login(platform, "root")
    body = platform.get("/api/admin/summary", headers=auth(token)).json()
    assert body["total_users"] >= 1
    assert body["active_users"] >= 1
    assert body["roles"] == len(Role.ALL)
    assert "pending_imports" in body and "failed_imports" in body
    assert "uploads_today" in body
