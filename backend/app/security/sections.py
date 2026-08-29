"""The application-section catalogue.

A *section* is one navigable area of the product — Sales, Stock, Data Upload,
Administration. It is the unit an administrator grants or denies per user, and
the unit every backend endpoint checks before returning data.

The catalogue is declared once, here, and is consumed by:

* :mod:`app.auth.permissions` — to resolve a user's effective access;
* ``require_section`` on every protected route;
* ``GET /api/admin/sections`` — so the admin UI renders whatever actually
  exists rather than a hard-coded list;
* ``GET /api/auth/me`` — so the frontend can hide what the backend would refuse.

Two properties of a section drive authorisation:

``role_default``
    Whether a role gets the section when nobody has said otherwise. Business
    reporting is on by default for every role; administration and the upload
    centre are off unless a role or an administrator turns them on.

``locked_to_roles``
    A hard ceiling. When set, no per-user override can grant the section to a
    role outside the tuple. This is what keeps "user permission" strictly below
    "role" in the precedence chain: a lower layer may narrow access, never widen
    a security restriction imposed above it.

Granular actions (VIEW / CREATE / EDIT / DELETE / EXPORT / UPLOAD) sit *inside*
a section: holding the section is what lets a user in, and the action decides
what they may do once there. Every section declares which actions are meaningful
for it and which roles hold each one by default; an administrator can override
either, per role or per user, through the ``actions`` map on the permission row.
Resolution lives in :mod:`app.auth.permissions`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..database.models_ai import Role


class SectionKey:
    """Stable identifiers. Never renamed: they are stored per user."""

    DASHBOARD = "dashboard"
    AI_ASSISTANT = "ai_assistant"
    SALES = "sales"
    STOCK = "stock"
    TARGET = "target"
    TARGET_MANAGEMENT = "target_management"
    PERFORMANCE = "performance"
    MATERIALS = "materials"
    CUSTOMERS = "customers"
    CREDIT_CONTROL = "credit_control"
    ALERTS = "alerts"
    DATA_QUALITY = "data_quality"
    DATA_UPLOAD = "data_upload"
    MASTER_DATA = "master_data"
    TRANSACTION_DATA = "transaction_data"
    MAP = "map"
    MAP_SETTINGS = "map_settings"
    AGENT_LEARNING = "agent_learning"
    ADMIN = "admin"
    SETTINGS = "settings"


class Action:
    """Granular actions the permission model is architected for.

    ``APPROVE`` and ``REVISE`` were added for Target Management, and are
    deliberately two actions rather than one. Approving is signing off on a
    figure somebody else proposed; revising is asking for it to change. A sales
    officer may do the second for their own sub-territory and must never do the
    first, and an approver two levels up is the reverse — so a single "workflow"
    permission could not express either.

    Neither is a synonym for ``EDIT``. Editing is typing the country target
    directly; both of these act on a figure the allocation engine generated,
    inside a workflow that records who did it and why.
    """

    VIEW = "VIEW"
    CREATE = "CREATE"
    EDIT = "EDIT"
    DELETE = "DELETE"
    EXPORT = "EXPORT"
    UPLOAD = "UPLOAD"
    APPROVE = "APPROVE"
    REVISE = "REVISE"

    ALL = (VIEW, CREATE, EDIT, DELETE, EXPORT, UPLOAD, APPROVE, REVISE)


ALLOW = "ALLOW"
DENY = "DENY"
ACCESS_VALUES = (ALLOW, DENY)

#: Roles trusted to change data by default: everyone who runs part of the
#: business, which is every role except the two that only consume reports.
#: Naming the exclusions rather than the inclusions means a role added later is
#: an editor unless it is deliberately made read-only.
EDITOR_ROLES: tuple[str, ...] = tuple(
    role for role in Role.ALL if role not in (Role.VIEWER, Role.SALES_OFFICER)
)

#: Roles that may sign off on a target by default.
#:
#: The line managers, and deliberately **not** the two administrator roles. An
#: administrator configures the approval matrix and owns the run; signing off on
#: a business number is a business judgement, and an administrator who is also
#: an approver holds the business role that says so. Territory Manager and
#: Sales Officer are absent for the opposite reason: a target is questioned at
#: their level, not approved there.
#:
#: This is the coarse gate only. *Which* node a holder may approve, in what
#: order and within what adjustment limit, is ``target_approval_matrix``.
APPROVER_ROLES: tuple[str, ...] = (
    Role.MANAGEMENT, Role.BUSINESS_UNIT_HEAD, Role.ZONE_MANAGER,
    Role.REGIONAL_MANAGER, Role.AREA_MANAGER, Role.UNIT_MANAGER,
)

#: Roles that may ask for an allocated figure to be changed.
#:
#: Everyone who runs part of the business, Sales Officer included — asking is
#: not changing, a revision states a reason and goes to an approver, and the
#: person closest to the customer is the one who knows the target is wrong.
#: Only ``VIEWER`` is excluded, because a read-only role has nothing to request
#: a change to.
REVISER_ROLES: tuple[str, ...] = tuple(
    role for role in Role.ALL if role != Role.VIEWER
)

#: Which roles hold each action when nobody has said otherwise. ``None`` means
#: "any role that holds the section" — reading and exporting what you can
#: already see is not a separate privilege.
#:
#: Deletion defaults to administrators alone. It is the one action that removes
#: something from view, so the safe default is the narrowest one; an
#: administrator can still grant it explicitly to a role or a user.
DEFAULT_ACTION_ROLES: dict[str, tuple[str, ...] | None] = {
    Action.VIEW: None,
    Action.EXPORT: None,
    Action.CREATE: EDITOR_ROLES,
    Action.EDIT: EDITOR_ROLES,
    Action.UPLOAD: EDITOR_ROLES,
    Action.DELETE: Role.ADMIN_ROLES,
    # Both are explicit rather than absent. ``resolve_action`` reads ``None`` as
    # "any role that holds the section", so an action missing from this map
    # would default to *everyone* — which is the right answer for VIEW and
    # EXPORT and precisely the wrong one for signing off on a target.
    Action.APPROVE: APPROVER_ROLES,
    Action.REVISE: REVISER_ROLES,
}

#: Section groups, used only for presentation ordering in the admin UI.
GROUP_REPORTING = "Reporting"
GROUP_DATA = "Data"
GROUP_SYSTEM = "System"


@dataclass(frozen=True)
class Section:
    """One grantable area of the application."""

    key: str
    label: str
    #: Frontend route, so the UI can hide navigation for a denied section.
    route: str
    group: str
    description: str
    #: API path prefixes this section guards. Documentation and test material —
    #: enforcement itself is explicit, via ``require_section`` on each route.
    api_prefixes: tuple[str, ...] = ()
    #: Actions meaningful for this section. An action outside this tuple cannot
    #: be granted here, so a report section can never acquire a DELETE.
    actions: tuple[str, ...] = (Action.VIEW, Action.EXPORT)
    #: Roles that may hold this section at all. ``None`` means every role may.
    locked_to_roles: tuple[str, ...] | None = None
    #: Roles this section is granted to when no explicit permission exists.
    default_roles: tuple[str, ...] | None = None
    #: True when the section is on by default for every role.
    default_allow: bool = True
    #: Per-action overrides of :data:`DEFAULT_ACTION_ROLES`, for a section whose
    #: risk profile differs from the norm.
    action_roles: dict[str, tuple[str, ...] | None] | None = None

    def role_default(self, role: str) -> bool:
        """Whether ``role`` gets this section with no explicit permission set."""
        if self.locked_to_roles is not None and role not in self.locked_to_roles:
            return False
        if self.default_roles is not None:
            return role in self.default_roles
        return self.default_allow

    def role_may_hold(self, role: str) -> bool:
        """The hard ceiling: can this role ever hold this section?"""
        return self.locked_to_roles is None or role in self.locked_to_roles

    def supports(self, action: str) -> bool:
        return action in self.actions

    def action_default(self, action: str, role: str) -> bool:
        """Whether ``role`` may perform ``action`` here with nothing configured.

        Assumes the section itself is already granted — this answers only "and
        what may they do inside it", never "may they come in".
        """
        if not self.supports(action):
            return False
        source = (self.action_roles or {})
        roles = source[action] if action in source else DEFAULT_ACTION_ROLES.get(action)
        return True if roles is None else role in roles

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "route": self.route,
            "group": self.group,
            "description": self.description,
            "actions": list(self.actions),
            "locked_to_roles": list(self.locked_to_roles or ()),
            "api_prefixes": list(self.api_prefixes),
        }


_REPORT_ACTIONS = (Action.VIEW, Action.EXPORT)


#: The catalogue, in navigation order. Adding a section here is all that is
#: needed for it to appear in the admin permission table and in ``/auth/me``.
SECTIONS: tuple[Section, ...] = (
    Section(
        key=SectionKey.DASHBOARD,
        label="Dashboard",
        route="/",
        group=GROUP_REPORTING,
        description="Executive dashboard: KPIs, trends and alerts summary.",
        api_prefixes=("/api/dashboard", "/api/period-options"),
        actions=_REPORT_ACTIONS,
    ),
    Section(
        key=SectionKey.AI_ASSISTANT,
        label="AI Assistant",
        route="/ai",
        group=GROUP_REPORTING,
        description="Natural-language business questions answered from the warehouse.",
        api_prefixes=("/api/chat",),
        actions=(Action.VIEW, Action.CREATE, Action.EXPORT),
    ),
    Section(
        key=SectionKey.SALES,
        label="Sales",
        route="/sales",
        group=GROUP_REPORTING,
        description="Sales reporting, trends and performance breakdowns.",
        api_prefixes=("/api/pages/sales", "/api/reports/sales",
                      "/api/pages/transactions/sales"),
        actions=_REPORT_ACTIONS,
    ),
    Section(
        key=SectionKey.STOCK,
        label="Stock",
        route="/stock",
        group=GROUP_REPORTING,
        description="Stock position, coverage and movement.",
        api_prefixes=("/api/pages/stock", "/api/reports/stock",
                      "/api/pages/transactions/stock"),
        actions=_REPORT_ACTIONS,
    ),
    Section(
        key=SectionKey.TARGET,
        label="Target",
        route="/target",
        group=GROUP_REPORTING,
        description="Targets and achievement against them.",
        api_prefixes=("/api/pages/target", "/api/reports/target",
                      "/api/pages/transactions/target"),
        actions=_REPORT_ACTIONS,
    ),
    Section(
        key=SectionKey.TARGET_MANAGEMENT,
        label="Target Management",
        route="/target-management",
        group=GROUP_REPORTING,
        description=(
            "Build a target rather than read one: country volume by material, "
            "allocation down the hierarchy, review, approval, versions and the "
            "audit trail behind every figure."
        ),
        api_prefixes=("/api/target-management",),
        actions=(Action.VIEW, Action.CREATE, Action.EDIT, Action.EXPORT,
                 Action.UPLOAD, Action.APPROVE, Action.REVISE),
        # Off by default for everyone, unlike the Target section beside it.
        # Reading achievement against a target is reporting; setting the target
        # the whole sales force is measured on is not, and a role that should
        # see one does not automatically get the other. Granted per role or per
        # user by an administrator — the same shape as Data Upload, and for the
        # same reason.
        #
        # There is no DELETE. A plan is superseded by a new version and a
        # version is never removed, so the action has nothing to do here and
        # cannot be granted by mistake.
        default_allow=False,
        default_roles=Role.ADMIN_ROLES,
    ),
    Section(
        key=SectionKey.PERFORMANCE,
        label="Performance",
        route="/performance",
        group=GROUP_REPORTING,
        description="Organisational performance league tables and drill-down.",
        api_prefixes=("/api/pages/performance",),
        actions=_REPORT_ACTIONS,
    ),
    Section(
        key=SectionKey.MATERIALS,
        label="Materials",
        route="/materials",
        group=GROUP_REPORTING,
        description="Material, brand and group analytics.",
        api_prefixes=("/api/pages/materials",),
        actions=_REPORT_ACTIONS,
    ),
    Section(
        key=SectionKey.CUSTOMERS,
        label="Customers",
        route="/customers",
        group=GROUP_REPORTING,
        description="Customer analytics and sales performance.",
        api_prefixes=("/api/pages/customers",),
        actions=_REPORT_ACTIONS,
    ),
    Section(
        key=SectionKey.CREDIT_CONTROL,
        label="Credit Control",
        route="/credit-control",
        group=GROUP_REPORTING,
        description=(
            "Receivables: what each customer owes, how overdue it is, and "
            "against which invoices."
        ),
        api_prefixes=("/api/reports/credit-control",),
        actions=_REPORT_ACTIONS,
        # Off by default for everyone except the roles that already see the
        # whole company. What a customer owes is more sensitive than what they
        # bought — it is the basis for stopping their supply — so it does not
        # ride along with the Customers grant the way the other reporting
        # sections ride along with each other.
        #
        # Not ``locked_to_roles``: an administrator may grant it to a regional
        # manager, whose data scope then limits them to their own region's
        # receivables. A hard ceiling would make that impossible, and collections
        # are chased by the line managers rather than by head office.
        default_roles=Role.UNRESTRICTED,
    ),
    Section(
        key=SectionKey.ALERTS,
        label="Alerts",
        route="/alerts",
        group=GROUP_REPORTING,
        description="Business alert centre and notifications.",
        api_prefixes=("/api/alerts", "/api/notifications"),
        actions=(Action.VIEW,),
    ),
    Section(
        key=SectionKey.MAP,
        label="Business Map",
        route="/map",
        group=GROUP_REPORTING,
        description=(
            "The multi-level geographic view: performance by zone, region, area, "
            "unit and territory, with drill-down."
        ),
        api_prefixes=("/api/map/data", "/api/map/config", "/api/map/levels",
                      "/api/map/marker-config", "/api/map/legend"),
        actions=_REPORT_ACTIONS,
    ),
    Section(
        key=SectionKey.DATA_QUALITY,
        label="Data Quality",
        route="/data-quality",
        group=GROUP_DATA,
        description="Import batches, rejected records and warehouse data quality.",
        api_prefixes=("/api/data-quality", "/api/etl"),
        actions=(Action.VIEW, Action.EXPORT),
    ),
    Section(
        key=SectionKey.DATA_UPLOAD,
        label="Data Upload",
        route="/data-upload",
        group=GROUP_DATA,
        description=(
            "Upload master and transactional data: templates, validation, import "
            "and upload history."
        ),
        api_prefixes=("/api/data-upload", "/api/import"),
        actions=(Action.VIEW, Action.UPLOAD, Action.CREATE, Action.EXPORT),
        # Off by default for everyone; an administrator grants it per user.
        default_allow=False,
        default_roles=Role.ADMIN_ROLES,
    ),
    Section(
        key=SectionKey.MASTER_DATA,
        label="Master Data",
        route="/data-management/master",
        group=GROUP_DATA,
        description=(
            "Browse, edit and deactivate the master data: the organisational "
            "hierarchy, materials, plants, storage locations, customers and "
            "sales force."
        ),
        api_prefixes=("/api/master",),
        actions=(Action.VIEW, Action.CREATE, Action.EDIT, Action.DELETE,
                 Action.EXPORT),
        # Off by default: seeing a report is not permission to change the master
        # data behind it. An administrator grants this per role or per user.
        default_allow=False,
        default_roles=Role.ADMIN_ROLES,
    ),
    Section(
        key=SectionKey.TRANSACTION_DATA,
        label="Transaction Data",
        route="/data-management/transactions",
        group=GROUP_DATA,
        description=(
            "Browse transactional records — sales, material stock and target — "
            "and void them under permission."
        ),
        api_prefixes=("/api/transactions",),
        actions=(Action.VIEW, Action.EDIT, Action.DELETE, Action.EXPORT),
        default_allow=False,
        default_roles=Role.ADMIN_ROLES,
        # A transaction is an accounting record. Editing one is narrower than
        # editing master data, and voiding one is narrower still: both are
        # restricted to administrators unless explicitly granted. CREATE is
        # absent entirely — transactions arrive through the validated ETL, and
        # hand-typing one would bypass every check that pipeline applies.
        action_roles={
            Action.EDIT: Role.ADMIN_ROLES,
            Action.DELETE: (Role.SUPER_ADMIN,),
        },
    ),
    Section(
        key=SectionKey.MAP_SETTINGS,
        label="Map Settings",
        route="/admin/map-settings/markers",
        group=GROUP_SYSTEM,
        description=(
            "Marker and shape designs for the business map: create, preview, "
            "version and assign the markers each entity type is drawn with."
        ),
        api_prefixes=("/api/map/marker-designs", "/api/map/marker-assets",
                      "/api/map/assignments"),
        actions=(Action.VIEW, Action.CREATE, Action.EDIT, Action.DELETE,
                 Action.UPLOAD, Action.EXPORT),
        # A permission in its own right, not a synonym for "administrator": it is
        # off by default for everyone and on by default for administrators, and
        # an administrator can grant it to, say, a marketing lead who owns how
        # the map looks without handing over user administration.
        default_allow=False,
        default_roles=Role.ADMIN_ROLES,
    ),
    Section(
        key=SectionKey.AGENT_LEARNING,
        label="Agent Learning",
        route="/admin/agent-learning",
        group=GROUP_SYSTEM,
        description=(
            "Questions the assistant handled badly, and the vocabulary and "
            "worked examples approved from them."
        ),
        api_prefixes=("/api/learning",),
        actions=(Action.VIEW, Action.CREATE, Action.EDIT, Action.EXPORT),
        # Reviewing vocabulary is a *business* judgement, not an administrative
        # one: knowing that "chini" means a particular brand is knowledge a
        # sales analyst has and a system administrator generally does not. So
        # this is grantable on its own, off by default for everyone, and on by
        # default for administrators — the same shape as Map Settings, and for
        # the same reason.
        #
        # There is no DELETE. An approved mapping is retired, never removed, so
        # the record of what the assistant was once taught survives being wrong.
        default_allow=False,
        default_roles=Role.ADMIN_ROLES,
    ),
    Section(
        key=SectionKey.ADMIN,
        label="Administration",
        route="/admin",
        group=GROUP_SYSTEM,
        description="Users, roles, section permissions, data scopes and audit logs.",
        api_prefixes=("/api/admin",),
        actions=(Action.VIEW, Action.CREATE, Action.EDIT, Action.DELETE),
        # A hard ceiling: no per-user override can hand administration to a
        # non-administrator role.
        locked_to_roles=Role.ADMIN_ROLES,
        default_roles=Role.ADMIN_ROLES,
        default_allow=False,
    ),
    Section(
        key=SectionKey.SETTINGS,
        label="Settings",
        route="/profile",
        group=GROUP_SYSTEM,
        description="Personal profile, language, theme and password.",
        api_prefixes=("/api/auth/preferences", "/api/auth/password"),
        actions=(Action.VIEW, Action.EDIT),
    ),
)

SECTION_BY_KEY: dict[str, Section] = {section.key: section for section in SECTIONS}


def section_keys() -> tuple[str, ...]:
    return tuple(section.key for section in SECTIONS)


def get_section(key: str) -> Section:
    """Look up a section, raising a clear error for an unknown key."""
    section = SECTION_BY_KEY.get(key)
    if section is None:
        raise KeyError(
            f"Unknown section {key!r}. Known sections: {', '.join(section_keys())}."
        )
    return section


__all__ = [
    "Section",
    "SectionKey",
    "Action",
    "SECTIONS",
    "SECTION_BY_KEY",
    "ACCESS_VALUES",
    "DEFAULT_ACTION_ROLES",
    "EDITOR_ROLES",
    "APPROVER_ROLES",
    "REVISER_ROLES",
    "ALLOW",
    "DENY",
    "GROUP_REPORTING",
    "GROUP_DATA",
    "GROUP_SYSTEM",
    "section_keys",
    "get_section",
]
