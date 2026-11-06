"""Centralised, validated settings. Every value comes from the environment (or a local .env file)."""

from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Supabase's shared pooler (Supavisor) in transaction mode listens on 6543.
TRANSACTION_POOLER_PORT = 6543


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    # hide_input_in_errors: a rejected DATABASE_URL (with its password) must never appear in an error or log.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    app_env: Environment = Environment.DEVELOPMENT

    # postgresql://, postgres:// and postgresql+psycopg:// are accepted and normalised to psycopg 3.
    database_url: SecretStr | None = None
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    db_pool_timeout_seconds: int = Field(default=10, ge=1, le=60)
    db_connect_timeout_seconds: int = Field(default=10, ge=1, le=60)
    # Applied per transaction with set_config(..., true), so it is safe behind a transaction pooler.
    db_statement_timeout_ms: int = Field(default=10_000, ge=0, le=120_000)
    # None = decide from the URL (disabled for the transaction pooler, which cannot hold prepared statements).
    db_prepared_statements: bool | None = None
    # Let the external pooler own connections instead of SQLAlchemy. Recommended with the transaction pooler.
    db_use_null_pool: bool = False

    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # Demonstration records (is_demo = true) are hidden from public endpoints unless this is on.
    expose_demo_content: bool = False

    # Requests per client IP per minute, per process. 0 disables the limiter.
    rate_limit_per_minute: int = Field(default=120, ge=0, le=100_000)

    log_level: str = "INFO"
    log_json: bool = False
    docs_enabled: bool = True

    # --- Phase 3.1 ingestion. All server-side only; never exposed to the frontend. ---
    # TOML file listing the RSS/Atom feeds and newsletters to collect (relative to backend/).
    ingestion_sources_file: str = "config/ingestion_sources.toml"
    # Bearer token for the admin ingestion endpoints. Unset = the endpoints do not exist (404).
    ingestion_admin_token: SecretStr | None = None
    # Staged items are temporary hand-offs to later stages, deleted automatically after this many days.
    ingestion_item_retention_days: int = Field(default=14, ge=1, le=90)
    # Job bookkeeping rows (no article content) older than this are purged, except each source's last success.
    ingestion_job_retention_days: int = Field(default=90, ge=7, le=730)
    # A job still "running" after this long is treated as crashed and marked failed.
    ingestion_job_stale_minutes: int = Field(default=60, ge=5, le=1440)
    # Interval used by `python -m scripts.ingest schedule`.
    ingestion_schedule_minutes: int = Field(default=360, ge=15, le=10_080)
    ingestion_http_timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    ingestion_max_response_bytes: int = Field(default=5_000_000, ge=10_000, le=50_000_000)
    ingestion_max_retries: int = Field(default=3, ge=0, le=6)
    ingestion_max_items_per_source: int = Field(default=100, ge=1, le=1000)
    ingestion_user_agent: str = Field(default="AI-CAMPUS-Ingestion/0.3", min_length=3, max_length=200)
    # Use HTTP(S)_PROXY from the environment for outbound fetches. Off by default: a proxy hides the real
    # peer address from the SSRF guard's post-connect check.
    ingestion_http_trust_env: bool = False

    # --- Phase 3.2 processing and deduplication thresholds (see README "Similarity thresholds"). ---
    # Near-duplicate (same article, lightly edited): word 3-shingle Jaccard of the two excerpts ...
    processing_near_duplicate_text_threshold: float = Field(default=0.5, ge=0.05, le=1.0)
    # ... and, together with it, at least this much title similarity.
    processing_near_duplicate_title_threshold: float = Field(default=0.4, ge=0.0, le=1.0)
    # Near-duplicate when an excerpt is missing: near-identical title, same site, within the window below.
    processing_title_only_threshold: float = Field(default=0.85, ge=0.5, le=1.0)
    processing_title_only_window_hours: int = Field(default=48, ge=1, le=720)
    processing_near_duplicate_window_hours: int = Field(default=168, ge=1, le=720)
    # Same story (different reports of one event): weighted title/text/entity score and time window.
    processing_story_threshold: float = Field(default=0.3, ge=0.05, le=1.0)
    processing_story_window_hours: int = Field(default=96, ge=1, le=720)
    # Excerpts shorter than this many words are too short for text-based duplicate signals.
    processing_min_body_tokens: int = Field(default=15, ge=3, le=200)
    # Upper bound on staged items loaded per processing run (newest first); keeps a run's cost bounded.
    processing_max_items: int = Field(default=5000, ge=10, le=50_000)

    # Gmail API (OAuth 2.0, gmail.readonly). All three must be set or Gmail sources are skipped.
    gmail_client_id: str | None = None
    gmail_client_secret: SecretStr | None = None
    gmail_refresh_token: SecretStr | None = None

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [o.strip().rstrip("/") for o in value.split(",") if o.strip()]
        return value

    @field_validator("cors_origins")
    @classmethod
    def _no_wildcards(cls, origins: list[str]) -> list[str]:
        for origin in origins:
            if "*" in origin:
                raise ValueError("Wildcard CORS origins are not allowed; list each frontend origin explicitly.")
            if not origin.startswith(("http://", "https://")):
                raise ValueError(f"CORS origin must include the scheme: {origin!r}")
        return origins

    @field_validator("log_level")
    @classmethod
    def _log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        return level

    @field_validator("database_url")
    @classmethod
    def _validate_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value().strip():
            return None
        normalise_database_url(value.get_secret_value())  # raises ValueError on an unusable URL
        return value

    @field_validator("ingestion_admin_token", "gmail_client_secret", "gmail_refresh_token")
    @classmethod
    def _blank_secret_is_unset(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value().strip():
            return None
        return SecretStr(value.get_secret_value().strip())

    @field_validator("gmail_client_id")
    @classmethod
    def _blank_is_unset(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None

    @field_validator("ingestion_admin_token")
    @classmethod
    def _admin_token_strength(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 32:
            raise ValueError("INGESTION_ADMIN_TOKEN must be at least 32 characters (use a random value).")
        return value

    @model_validator(mode="after")
    def _production_rules(self) -> "Settings":
        if self.app_env is Environment.PRODUCTION:
            if self.expose_demo_content:
                raise ValueError("EXPOSE_DEMO_CONTENT must be false in production.")
            if self.database_url is None:
                raise ValueError("DATABASE_URL is required in production.")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env is Environment.PRODUCTION

    def sqlalchemy_url(self) -> URL | None:
        if self.database_url is None:
            return None
        return normalise_database_url(self.database_url.get_secret_value())

    @property
    def gmail_configured(self) -> bool:
        return bool(self.gmail_client_id and self.gmail_client_secret and self.gmail_refresh_token)

    def use_prepared_statements(self) -> bool:
        if self.db_prepared_statements is not None:
            return self.db_prepared_statements
        url = self.sqlalchemy_url()
        return not (url is not None and url.port == TRANSACTION_POOLER_PORT)


def normalise_database_url(raw: str) -> URL:
    """Return a psycopg 3 SQLAlchemy URL. Remote hosts get sslmode=require unless a mode is given."""
    try:
        url = make_url(raw.strip())
    except Exception as exc:  # noqa: BLE001 - never echo the URL, it may contain a password
        raise ValueError("DATABASE_URL is not a valid database URL.") from exc
    if url.drivername in {"postgres", "postgresql", "postgresql+psycopg"}:
        url = url.set(drivername="postgresql+psycopg")
    else:
        raise ValueError("DATABASE_URL must be a PostgreSQL URL (postgresql://...). Other drivers are not supported.")
    if not url.host or not url.database:
        raise ValueError("DATABASE_URL must include a host and a database name.")
    if url.host not in LOCAL_HOSTS and "sslmode" not in url.query:
        url = url.update_query_dict({"sslmode": "require"})
    return url


def describe_database_target(settings: Settings) -> str:
    """Host, port and database name only, for logs and CLI confirmations. Never includes credentials."""
    url = settings.sqlalchemy_url()
    if url is None:
        return "not configured"
    return f"{url.host}:{url.port or 5432}/{url.database}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
