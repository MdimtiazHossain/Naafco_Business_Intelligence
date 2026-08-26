"""HTTP API.

Phase 2 added import, ETL monitoring, data quality and reporting; Phase 3 added
chat and export; Phase 4 adds authentication, the web platform's page endpoints,
master-data filters, admin and the WhatsApp integration.
"""

from .routes_admin import router as admin_router
from .routes_auth import router as auth_router
from .routes_chat import router as chat_router
from .routes_dashboard import router as dashboard_router
from .routes_data_management import router as data_management_router
from .routes_data_upload import router as data_upload_router
from .routes_etl import router as etl_router
from .routes_export import router as export_router
from .routes_import import router as import_router
from .routes_learning import router as learning_router
from .routes_map import router as map_router
from .routes_masterdata import router as master_data_router
from .routes_pages import router as pages_router
from .routes_reports import router as reports_router
from .routes_whatsapp import router as whatsapp_router

#: Registration order. Auth first so its paths are unambiguous, then the page
#: and data routers, then integrations.
ALL_ROUTERS = (
    auth_router,
    data_upload_router,
    import_router,
    etl_router,
    reports_router,
    export_router,
    chat_router,
    dashboard_router,
    pages_router,
    master_data_router,
    # After the page routers: its ``/api/master/{entity}`` paths must not be
    # confused with ``/api/master-data/*``, and registering it later keeps the
    # more specific existing prefixes matched first.
    data_management_router,
    map_router,
    admin_router,
    learning_router,
    whatsapp_router,
)

__all__ = [
    "ALL_ROUTERS",
    "data_management_router",
    "auth_router",
    "data_upload_router",
    "import_router",
    "etl_router",
    "reports_router",
    "export_router",
    "chat_router",
    "dashboard_router",
    "pages_router",
    "master_data_router",
    "map_router",
    "admin_router",
    "whatsapp_router",
]
