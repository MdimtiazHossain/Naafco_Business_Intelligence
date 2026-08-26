"""Phase 3 models: users with a data scope, and conversation memory.

The security chain is ``user -> role -> permission -> data scope``. A user's
scope is a mapping of organisational level to the codes they may see, e.g.
``{"region_code": ["REG001"]}`` for a regional manager. The agent cannot widen
it: the scope is read from the database and injected into every tool call.

Conversation storage keeps what is needed to follow up on a question and to
audit what the agent did — and nothing more. No secrets, no SQL, no raw tool
internals.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import CODE, SURROGATE_PK, Base, TimestampMixin
from .models_warehouse import FK_TYPE, JSON_TYPE


class Role:
    """Roles, broadest access first.

    ``SUPER_ADMIN`` and ``ADMIN`` were added in Phase 4 for the admin panel;
    the operational roles are unchanged from Phase 3, so existing users keep
    exactly the access they had.
    """

    SUPER_ADMIN = "SUPER_ADMIN"
    ADMIN = "ADMIN"
    MANAGEMENT = "MANAGEMENT"
    BUSINESS_UNIT_HEAD = "BUSINESS_UNIT_HEAD"
    ZONE_MANAGER = "ZONE_MANAGER"
    REGIONAL_MANAGER = "REGIONAL_MANAGER"
    AREA_MANAGER = "AREA_MANAGER"
    UNIT_MANAGER = "UNIT_MANAGER"
    TERRITORY_MANAGER = "TERRITORY_MANAGER"
    SALES_OFFICER = "SALES_OFFICER"
    VIEWER = "VIEWER"

    ALL = (
        SUPER_ADMIN, ADMIN, MANAGEMENT, BUSINESS_UNIT_HEAD, ZONE_MANAGER,
        REGIONAL_MANAGER, AREA_MANAGER, UNIT_MANAGER, TERRITORY_MANAGER,
        SALES_OFFICER, VIEWER,
    )
    #: Roles that see the whole company when no explicit scope is assigned.
    UNRESTRICTED = (SUPER_ADMIN, ADMIN, MANAGEMENT)
    #: Roles allowed into the admin panel. Enforced server-side.
    ADMIN_ROLES = (SUPER_ADMIN, ADMIN)


class UserStatus:
    """Account lifecycle, as the admin panel presents it.

    ``is_active`` remains the single flag the authentication path reads, and is
    kept in step with the status by :func:`app.auth.users.apply_status`: only
    ``ACTIVE`` is active. ``LOCKED`` is distinguished from ``INACTIVE`` so an
    administrator can tell a security lock-out from a deliberate deactivation,
    without either of them being able to sign in.
    """

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    LOCKED = "LOCKED"

    ALL = (ACTIVE, INACTIVE, LOCKED)


class AppUser(Base, TimestampMixin):
    """A person who may ask the agent questions.

    Authentication itself (password / SSO / JWT) is deliberately **not** built
    here — Phase 3 consumes an already-identified user. What *is* built is
    authorisation: the role and the data scope that every query is filtered by.
    """

    __tablename__ = "app_user"

    user_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    employee_id: Mapped[str | None] = mapped_column(CODE)
    department: Mapped[str | None] = mapped_column(Text)
    designation: Mapped[str | None] = mapped_column(Text)
    #: ACTIVE / INACTIVE / LOCKED. ``is_active`` is derived from it and is what
    #: the login path checks, so an existing deployment keeps working unchanged.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=UserStatus.ACTIVE)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: PBKDF2-HMAC-SHA256, salted per user. NULL means the account cannot log in
    #: with a password yet (created before a password was set).
    password_hash: Mapped[str | None] = mapped_column(String(255))
    #: E.164 number used to identify the user on WhatsApp. Unique so one number
    #: can never resolve to two accounts.
    phone_number: Mapped[str | None] = mapped_column(String(32), unique=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    theme: Mapped[str] = mapped_column(String(16), nullable=False, default="system")
    #: ``{"region_code": ["REG001"], "territory_code": [...]}``. Empty means
    #: "no explicit scope"; only an unrestricted role may then see everything.
    data_scope: Mapped[dict | None] = mapped_column(JSON_TYPE)
    preferred_language: Mapped[str] = mapped_column(String(8), nullable=False, default="en")

    __table_args__ = (
        Index("ix_app_user_username", "username"),
        Index("ix_app_user_role", "role"),
        Index("ix_app_user_email", "email"),
        Index("ix_app_user_phone_number", "phone_number"),
    )


class ChatConversation(Base):
    """One conversation thread. Context is only ever reused for its own user."""

    __tablename__ = "chat_conversations"

    conversation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        FK_TYPE, ForeignKey("app_user.user_id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Carried-forward filters and period, so "Dhaka only" needs no repetition.
    context: Mapped[dict | None] = mapped_column(JSON_TYPE)

    __table_args__ = (Index("ix_chat_conversations_user_id", "user_id"),)


class ChatMessage(Base):
    """A single turn. ``role`` is ``user`` or ``assistant``."""

    __tablename__ = "chat_messages"

    message_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chat_conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(FK_TYPE, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(8))
    intent: Mapped[str | None] = mapped_column(String(48))
    #: The validated structured query and resolved filters — not raw tool output.
    structured_query: Mapped[dict | None] = mapped_column(JSON_TYPE)
    error_code: Mapped[str | None] = mapped_column(String(48))
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_chat_messages_conversation_id", "conversation_id"),
        Index("ix_chat_messages_created_at", "created_at"),
        Index("ix_chat_messages_intent", "intent"),
    )


class ChatToolCall(Base):
    """Observability: which tool ran, how long it took, whether it succeeded.

    Arguments are stored because they are already sanitised (codes and dates
    only). No SQL, no credentials and no prompt text is ever written here.
    """

    __tablename__ = "chat_tool_calls"

    tool_call_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                              autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    message_id: Mapped[int | None] = mapped_column(FK_TYPE)
    user_id: Mapped[int] = mapped_column(FK_TYPE, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict | None] = mapped_column(JSON_TYPE)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error_code: Mapped[str | None] = mapped_column(String(48))
    row_count: Mapped[int | None] = mapped_column(Integer)
    execution_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_chat_tool_calls_conversation_id", "conversation_id"),
        Index("ix_chat_tool_calls_tool_name", "tool_name"),
        Index("ix_chat_tool_calls_created_at", "created_at"),
    )


class AuditLog(Base):
    """Who did what, when, and from where.

    Written for logins, report views, AI questions, exports and admin changes.
    Deliberately stores no secret: never a password, token, API key or
    connection string — only the action, the target and sanitised parameters.
    """

    __tablename__ = "audit_logs"

    audit_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                          autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(FK_TYPE)
    username: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    resource: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSON_TYPE)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_audit_logs_user_id", "user_id"),
        Index("ix_audit_logs_action", "action"),
        Index("ix_audit_logs_created_at", "created_at"),
    )


class AuditAction:
    LOGIN = "LOGIN"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
    VIEW_REPORT = "VIEW_REPORT"
    AI_QUERY = "AI_QUERY"
    EXPORT = "EXPORT"
    ADMIN_CHANGE = "ADMIN_CHANGE"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    WHATSAPP_MESSAGE = "WHATSAPP_MESSAGE"

    # --- Phase 4: user administration ------------------------------------
    USER_CREATED = "USER_CREATED"
    USER_UPDATED = "USER_UPDATED"
    ROLE_CHANGED = "ROLE_CHANGED"
    PERMISSION_CHANGED = "PERMISSION_CHANGED"
    SCOPE_CHANGED = "SCOPE_CHANGED"
    USER_ACTIVATED = "USER_ACTIVATED"
    USER_DEACTIVATED = "USER_DEACTIVATED"
    PASSWORD_RESET = "PASSWORD_RESET"

    # --- Phase 4: data upload centre -------------------------------------
    DATA_UPLOADED = "DATA_UPLOADED"
    DATA_VALIDATED = "DATA_VALIDATED"
    DATA_IMPORTED = "DATA_IMPORTED"
    DATA_IMPORT_FAILED = "DATA_IMPORT_FAILED"
    #: A user stopped a running import. Its own action rather than a failure:
    #: nothing went wrong, somebody made a decision, and the audit trail should
    #: be able to answer "who stopped it" without that reading as a fault.
    DATA_IMPORT_CANCELLED = "DATA_IMPORT_CANCELLED"
    DATA_ROLLBACK = "DATA_ROLLBACK"
    TEMPLATE_DOWNLOADED = "TEMPLATE_DOWNLOADED"

    # --- Phase 4: map marker designer ------------------------------------
    MARKER_DESIGN_CREATED = "MARKER_DESIGN_CREATED"
    MARKER_DESIGN_UPDATED = "MARKER_DESIGN_UPDATED"
    MARKER_DESIGN_DUPLICATED = "MARKER_DESIGN_DUPLICATED"
    MARKER_DESIGN_ASSIGNED = "MARKER_DESIGN_ASSIGNED"
    MARKER_DESIGN_ACTIVATED = "MARKER_DESIGN_ACTIVATED"
    MARKER_DESIGN_DEACTIVATED = "MARKER_DESIGN_DEACTIVATED"
    MARKER_DESIGN_DELETED = "MARKER_DESIGN_DELETED"
    MARKER_ASSET_UPLOADED = "MARKER_ASSET_UPLOADED"
    MARKER_RESET_TO_DEFAULT = "MARKER_RESET_TO_DEFAULT"
    MARKER_CONFIG_IMPORTED = "MARKER_CONFIG_IMPORTED"
    MAP_LOCATION_UPDATED = "MAP_LOCATION_UPDATED"

    # --- Phase 4: master and transaction data management -------------------
    RECORD_CREATED = "RECORD_CREATED"
    RECORD_UPDATED = "RECORD_UPDATED"
    RECORD_DELETED = "RECORD_DELETED"
    RECORD_RESTORED = "RECORD_RESTORED"
    RECORD_VOIDED = "RECORD_VOIDED"
    RECORD_UNVOIDED = "RECORD_UNVOIDED"
    RECORD_BULK_CHANGE = "RECORD_BULK_CHANGE"

    # --- Agent learning: the review queue and what is approved from it -----
    #
    # Their own actions rather than ``ADMIN_CHANGE``, for the reason
    # ``DATA_IMPORT_CANCELLED`` gives above: an approved alias changes how the
    # assistant reads a question for every user, and "who decided that, and
    # when" should be answerable from the audit log without reading the row.
    # One set for both aliases and examples — ``resource`` already says which.
    AGENT_SIGNAL_TRIAGED = "AGENT_SIGNAL_TRIAGED"
    AGENT_LEARNING_PROPOSED = "AGENT_LEARNING_PROPOSED"
    AGENT_LEARNING_APPROVED = "AGENT_LEARNING_APPROVED"
    AGENT_LEARNING_REJECTED = "AGENT_LEARNING_REJECTED"
    AGENT_LEARNING_RETIRED = "AGENT_LEARNING_RETIRED"


class Notification(Base):
    """A message for one user, shown in the header dropdown.

    Business alerts are computed live from the warehouse; this table holds the
    *delivered* ones plus system and import notices, so read state can persist.
    """

    __tablename__ = "notifications"

    notification_id: Mapped[int] = mapped_column(SURROGATE_PK, primary_key=True,
                                                 autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        FK_TYPE, ForeignKey("app_user.user_id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="MEDIUM")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    #: In-app route the notification links to, e.g. "/stock".
    link: Mapped[str | None] = mapped_column(String(255))
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_notifications_user_id", "user_id"),
        Index("ix_notifications_is_read", "is_read"),
        Index("ix_notifications_created_at", "created_at"),
    )


class NotificationCategory:
    """What a notification is about.

    ``COLLECTION`` and ``OUTSTANDING`` are gone with their modules (revision
    0020). No new notification can carry either, and none ever did — nothing
    raised one. A stored notification is free text plus a category string, so a
    historical row naming a retired category still renders.
    """

    SYSTEM = "SYSTEM"
    IMPORT = "IMPORT"
    SALES = "SALES"
    TARGET = "TARGET"
    STOCK = "STOCK"

    ALL = (SYSTEM, IMPORT, SALES, TARGET, STOCK)


__all__ = [
    "Role",
    "UserStatus",
    "AppUser",
    "ChatConversation",
    "ChatMessage",
    "ChatToolCall",
    "AuditLog",
    "AuditAction",
    "Notification",
    "NotificationCategory",
]
