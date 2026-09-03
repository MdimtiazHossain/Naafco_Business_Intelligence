"""Database package: engine, session handling and ORM models.

Importing this package registers **all** tables on ``Base.metadata`` — the
Phase 1 master dimensions and the Phase 2 warehouse layer — so that Alembic and
``create_all`` always see the complete schema.
"""

from .connection import check_connection, get_engine, session_scope
from .models import Base, MODEL_BY_TABLE
from .models_warehouse import (  # noqa: F401  (import registers the tables)
    FACT_MODEL_BY_DATA_TYPE,
    STAGING_MODEL_BY_DATA_TYPE,
    DimDate,
    EtlImportBatch,
    EtlRejectedRecord,
)
from . import models_ai  # noqa: F401  (registers the Phase 3 tables)
from . import models_admin  # noqa: F401  (registers the Phase 4 tables)
from . import models_geo  # noqa: F401  (registers the administrative geography)
from . import models_learning  # noqa: F401  (registers the agent-learning tables)
from . import models_target  # noqa: F401  (registers the target-management tables)

__all__ = [
    "Base",
    "MODEL_BY_TABLE",
    "STAGING_MODEL_BY_DATA_TYPE",
    "FACT_MODEL_BY_DATA_TYPE",
    "DimDate",
    "EtlImportBatch",
    "EtlRejectedRecord",
    "get_engine",
    "session_scope",
    "check_connection",
]
