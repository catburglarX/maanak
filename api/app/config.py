"""Application configuration.

Every setting is read from the environment so that the same image can run in
development, test and production. Defaults are safe for local development only;
``Settings.assert_production_ready`` is called at startup when
``MAANAK_ENV=production`` to refuse obviously unsafe values.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]

# Upload limits. Kept here rather than in the route so that the worker,
# the API and the tests all agree on one number.
MIN_UPLOAD_BYTES = 4 * 1024
ABSOLUTE_MAX_UPLOAD_BYTES = 64 * 1024 * 1024


class Settings(BaseSettings):
    """Runtime settings for the API and the worker."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = Field(default="development", alias="MAANAK_ENV")

    # --- Database ---------------------------------------------------------
    database_url: str = Field(
        default="postgresql+psycopg://maanak:maanak@localhost:5432/maanak",
        alias="DATABASE_URL",
    )
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_statement_timeout_ms: int = Field(default=15_000, ge=1_000, le=600_000)
    db_echo: bool = False

    # --- Sessions and signing --------------------------------------------
    # A rotating map of key id -> secret. The newest key signs; older keys stay
    # valid for verification so that key rotation does not log everyone out.
    jwt_secret: str = Field(min_length=32, alias="JWT_SECRET")
    jwt_secret_previous: str | None = Field(default=None, alias="JWT_SECRET_PREVIOUS")
    jwt_issuer: str = Field(default="maanak", alias="JWT_ISSUER")
    access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    refresh_token_ttl_seconds: int = Field(default=60 * 60 * 24 * 14, ge=3600)
    session_idle_timeout_seconds: int = Field(default=60 * 60 * 8, ge=300)
    cookie_domain: str | None = None
    cookie_secure: bool = Field(default=False, alias="COOKIE_SECURE")
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"

    # --- Password policy --------------------------------------------------
    password_min_length: int = Field(default=12, ge=12, le=128)
    argon2_time_cost: int = Field(default=3, ge=1, le=10)
    argon2_memory_cost_kib: int = Field(default=65536, ge=8192)
    argon2_parallelism: int = Field(default=2, ge=1, le=16)

    # --- Brute-force protection ------------------------------------------
    login_max_attempts: int = Field(default=5, ge=3, le=50)
    login_attempt_window_seconds: int = Field(default=900, ge=60)
    login_lockout_seconds: int = Field(default=900, ge=60)

    # --- HTTP -------------------------------------------------------------
    cors_origins: str = Field(default="http://localhost:8080,http://127.0.0.1:8080")
    public_base_url: str = Field(default="http://localhost:8080")
    api_base_url: str = Field(default="http://localhost:8000")
    trusted_hosts: str = Field(default="*")
    request_body_limit_bytes: int = Field(default=ABSOLUTE_MAX_UPLOAD_BYTES)
    docs_enabled: bool = Field(default=True, alias="DOCS_ENABLED")

    # --- Object storage ---------------------------------------------------
    s3_endpoint_url: str = Field(default="http://minio:9000", alias="S3_ENDPOINT_URL")
    s3_public_endpoint_url: str | None = Field(default=None, alias="S3_PUBLIC_ENDPOINT_URL")
    s3_region: str = Field(default="us-east-1", alias="S3_REGION")
    s3_access_key: str = Field(default="maanak", alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(default="maanak-dev-secret", alias="S3_SECRET_KEY")
    s3_bucket_originals: str = Field(default="maanak-originals")
    s3_bucket_derivatives: str = Field(default="maanak-derivatives")
    s3_bucket_reports: str = Field(default="maanak-reports")
    s3_signed_url_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    s3_server_side_encryption: str | None = Field(default="AES256")

    max_upload_mb: int = Field(default=20, ge=1, le=64, alias="MAX_UPLOAD_MB")

    # --- Queue ------------------------------------------------------------
    redis_url: str = Field(default="redis://redis:6379/0", alias="REDIS_URL")
    job_max_tries: int = Field(default=3, ge=1, le=10)
    job_timeout_seconds: int = Field(default=300, ge=30)

    # --- OCR --------------------------------------------------------------
    tesseract_cmd: str | None = None
    ocr_languages: str = Field(default="eng+hin")
    ocr_max_pixels: int = Field(default=40_000_000, ge=1_000_000)
    ocr_min_word_confidence: float = Field(default=0.30, ge=0.0, le=1.0)

    # --- Outbound fetch (e-commerce listings) -----------------------------
    fetch_enabled: bool = Field(default=True)
    fetch_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    fetch_max_bytes: int = Field(default=3 * 1024 * 1024, ge=1024)
    fetch_allowed_ports: str = Field(default="80,443")

    # --- Reports ----------------------------------------------------------
    report_signer: Literal["none", "development"] = Field(default="development")
    report_signing_key: str | None = None

    # --- Notifications ----------------------------------------------------
    notification_backend: Literal["console", "smtp"] = Field(default="console")
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "no-reply@maanak.local"

    # --- Behaviour flags --------------------------------------------------
    rate_limit_enabled: bool = True
    audit_chain_enabled: bool = True
    allow_self_approval: bool = Field(
        default=False,
        description="Maker-checker override. Only ever enabled for isolated tests.",
    )

    @field_validator("jwt_secret")
    @classmethod
    def _reject_placeholder_secret(cls, value: str) -> str:
        lowered = value.lower()
        placeholders = ("replace-with", "change-this", "changeme", "secret-key", "your-secret")
        if any(token in lowered for token in placeholders):
            raise ValueError(
                "JWT_SECRET still contains a placeholder value. Generate a random secret."
            )
        return value

    @property
    def origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def trusted_host_list(self) -> list[str]:
        return [item.strip() for item in self.trusted_hosts.split(",") if item.strip()]

    @property
    def allowed_fetch_ports(self) -> set[int]:
        return {int(item) for item in self.fetch_allowed_ports.split(",") if item.strip()}

    @property
    def max_upload_bytes(self) -> int:
        return min(self.max_upload_mb * 1024 * 1024, ABSOLUTE_MAX_UPLOAD_BYTES)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def sync_database_url(self) -> str:
        """Alembic and a few maintenance scripts use a synchronous driver."""
        return self.database_url.replace("+asyncpg", "+psycopg")

    @property
    def async_database_url(self) -> str:
        if "+asyncpg" in self.database_url:
            return self.database_url
        return self.database_url.replace("+psycopg", "+asyncpg").replace(
            "postgresql://", "postgresql+asyncpg://"
        )

    @property
    def verification_url_template(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/verify.html?report="

    def signing_keys(self) -> dict[str, str]:
        """Key id -> secret. ``current`` signs new tokens."""
        keys = {"current": self.jwt_secret}
        if self.jwt_secret_previous:
            keys["previous"] = self.jwt_secret_previous
        return keys

    def assert_production_ready(self) -> None:
        """Fail fast on configurations that must never reach production."""
        problems: list[str] = []
        if not self.cookie_secure:
            problems.append("COOKIE_SECURE must be true in production")
        if self.allow_self_approval:
            problems.append("ALLOW_SELF_APPROVAL must be false in production")
        if self.report_signer == "development":
            problems.append(
                "REPORT_SIGNER=development produces clearly-labelled development "
                "signatures only; configure a real signer or set 'none'"
            )
        if self.s3_secret_key == "maanak-dev-secret":  # noqa: S105 - comparing to a known default
            problems.append("S3_SECRET_KEY is still the development default")
        if "*" in self.trusted_host_list:
            problems.append("TRUSTED_HOSTS must list explicit hostnames in production")
        if self.docs_enabled:
            problems.append("DOCS_ENABLED should be false in production")
        if problems:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # Values are read from the environment and the .env file by pydantic-settings.
    return Settings()


def generate_secret(length: int = 48) -> str:
    return secrets.token_urlsafe(length)
