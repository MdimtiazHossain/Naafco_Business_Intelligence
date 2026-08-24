"""Application configuration, loaded from the environment (12-factor style).

Phase 1 only needs the database URL and the master-data file locations; later
phases extend this module rather than inventing their own settings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

#: Repository root: <repo>/backend/app/config.py -> <repo>
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so scripts work without an extra dependency."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    app_name: str = "AI Business Intelligence & Reporting Agent"
    environment: str = os.getenv("APP_ENV", "development")

    postgres_host: str = os.getenv("POSTGRES_HOST", "localhost")
    postgres_port: int = int(os.getenv("POSTGRES_PORT", "5432"))
    postgres_db: str = os.getenv("POSTGRES_DB", "ai_business_agent")
    postgres_user: str = os.getenv("POSTGRES_USER", "postgres")
    postgres_password: str = os.getenv("POSTGRES_PASSWORD", "postgres")

    # --- Connection pool ----------------------------------------------------
    #: SQLAlchemy pool size and overflow, applied to PostgreSQL only.
    #:
    #: Deliberately small. A managed PostgreSQL bills and caps *connections*,
    #: not queries — Supabase's pooler enforces a per-project client ceiling —
    #: so a worker sitting on twenty idle connections spends the budget of the
    #: workers beside it. Every request here is short; the one long transaction
    #: is an import, and that already runs on its own bounded pool in
    #: ``upload/jobs.py`` rather than on a request's connection.
    db_pool_size: int = int(os.getenv("DB_POOL_SIZE", "5"))
    db_max_overflow: int = int(os.getenv("DB_MAX_OVERFLOW", "5"))
    #: Seconds before a pooled PostgreSQL connection is retired and reopened.
    #:
    #: Set below a managed server's own idle timeout so the pool discards a
    #: connection before the server drops it unannounced. ``pool_pre_ping``
    #: still catches the case this misses; recycling means the common path does
    #: not pay for that extra round trip.
    db_pool_recycle_seconds: int = int(os.getenv("DB_POOL_RECYCLE_SECONDS", "1800"))

    master_data_file: Path = Path(
        os.getenv("MASTER_DATA_FILE", str(PROJECT_ROOT / "data" / "Master Data.xlsx"))
    )
    reports_dir: Path = Path(os.getenv("REPORTS_DIR", str(PROJECT_ROOT / "reports")))
    transactions_dir: Path = Path(
        os.getenv("TRANSACTIONS_DIR", str(PROJECT_ROOT / "data" / "transactions"))
    )

    # --- Financial year -----------------------------------------------------
    # Month the company's financial year starts in (1-12). 7 = July, giving
    # "FY 2026-27" for July 2026 - June 2027. Set to 1 for a calendar financial
    # year, which is then labelled "FY 2026". Never hard-coded anywhere else.
    financial_year_start_month: int = int(os.getenv("FINANCIAL_YEAR_START_MONTH", "7"))
    #: Prefix used when rendering a financial-year label.
    financial_year_label_prefix: str = os.getenv("FINANCIAL_YEAR_LABEL_PREFIX", "FY")

    # --- Stock ---------------------------------------------------------------
    #: How many days ahead counts as "expiring soon" for material stock.
    #:
    #: Configuration rather than a constant because the business had no existing
    #: threshold to inherit: 90 days is a starting point, not a rule, and an
    #: agent question can override it for one answer. Nothing hard-codes it.
    stock_expiring_soon_days: int = int(os.getenv("STOCK_EXPIRING_SOON_DAYS", "90"))

    # --- ETL ----------------------------------------------------------------
    #: Rows per bulk insert / upsert statement.
    etl_batch_size: int = int(os.getenv("ETL_BATCH_SIZE", "5000"))
    #: Default date format for sources that do not declare one. "auto" accepts
    #: only unambiguous values; ambiguous ones are rejected rather than guessed.
    default_date_format: str = os.getenv("DEFAULT_DATE_FORMAT", "auto")

    # --- Import jobs --------------------------------------------------------
    #: How many uploads may be processed at once.
    #:
    #: One by default because SQLite permits a single writer: a second concurrent
    #: import would not run faster, it would sit blocked on the write lock where
    #: nobody can see it. Queueing in the pool instead keeps the wait visible and
    #: reportable. Raise it on PostgreSQL, where concurrent writers are real.
    import_worker_count: int = int(os.getenv("IMPORT_WORKER_COUNT", "1"))
    #: How long a finished job keeps appearing in the upload dock, in seconds.
    #: Long enough to read the result after it lands, short enough that yesterday's
    #: imports are history rather than notifications.
    import_job_retention_seconds: int = int(
        os.getenv("IMPORT_JOB_RETENTION_SECONDS", "300")
    )

    # --- Administrative area layer ------------------------------------------
    #: What an administrative area reports for stock while no transactional
    #: stock is attributable to it.
    #:
    #: A configured placeholder, not a business rule. The resolver asks the
    #: warehouse first and only falls back to this, so the day stock can be
    #: attributed to an upazila the layer switches to real figures with no code
    #: change — and every response says which of the two it used.
    map_area_default_stock: float = float(
        os.getenv("MAP_AREA_DEFAULT_STOCK", "1")
    )
    #: Douglas–Peucker tolerance in degrees applied when boundaries are imported.
    #: ~0.001° is roughly 100 m: far finer than a country-level view resolves,
    #: and typically an order of magnitude fewer vertices than the source file.
    map_area_simplify_tolerance: float = float(
        os.getenv("MAP_AREA_SIMPLIFY_TOLERANCE", "0.001")
    )
    #: Most areas returned in one request. A guard against a viewport-free
    #: query for every upazila in the country arriving as one payload.
    map_area_max_features: int = int(os.getenv("MAP_AREA_MAX_FEATURES", "1000"))

    # --- Basemap ------------------------------------------------------------
    #: MapLibre style URLs for the two themes.
    #:
    #: OpenFreeMap by default, which serves the OpenStreetMap basemap with no
    #: API key, no token and no per-view billing — the reason the map moved off
    #: Google Maps. Both defaults are deliberately muted designs: a business map
    #: is read for what is drawn *on* it, and a vivid basemap competes with the
    #: boundaries and markers it exists to support.
    #:
    #: Overridable so a deployment with no route to the public internet can
    #: point at its own tile server without a frontend rebuild. Whatever is
    #: configured must be a MapLibre style document, not a raster tile URL.
    map_basemap_style_url: str = os.getenv(
        "MAP_BASEMAP_STYLE_URL", "https://tiles.openfreemap.org/styles/positron"
    )
    map_basemap_style_url_dark: str = os.getenv(
        "MAP_BASEMAP_STYLE_URL_DARK", "https://tiles.openfreemap.org/styles/dark"
    )
    #: Where the map opens before anything has been placed. Dhaka by default.
    map_default_latitude: float = float(os.getenv("MAP_DEFAULT_LATITUDE", "23.777"))
    map_default_longitude: float = float(os.getenv("MAP_DEFAULT_LONGITUDE", "90.399"))
    map_default_zoom: int = int(os.getenv("MAP_DEFAULT_ZOOM", "7"))

    # --- Presentation -------------------------------------------------------
    company_name: str = os.getenv("COMPANY_NAME", "Example Industries Ltd.")
    currency_code: str = os.getenv("CURRENCY_CODE", "BDT")
    app_version: str = os.getenv("APP_VERSION", "1.0.0")

    # --- Authentication -----------------------------------------------------
    #: Allow the Phase 3 ``X-User`` header as an identity source.
    #:
    #: It is convenient behind a trusted gateway and for local development, but
    #: it is *not* authentication: anyone who can reach the API can claim any
    #: username. It defaults to off in production.
    allow_header_auth: bool = (
        os.getenv("ALLOW_HEADER_AUTH",
                  "false" if os.getenv("APP_ENV", "development").lower() == "production"
                  else "true").lower() in ("1", "true", "yes")
    )
    #: Origins permitted to call the API from a browser.
    cors_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",")
        if origin.strip()
    )

    @property
    def database_url(self) -> str:
        """SQLAlchemy URL. ``DATABASE_URL`` overrides the individual parts."""
        explicit = os.getenv("DATABASE_URL")
        if explicit:
            return explicit
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
