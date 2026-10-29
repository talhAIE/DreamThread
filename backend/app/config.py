"""Validated startup configuration. Secret values never appear in repr/errors."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    app_env: Literal["development", "test", "production"] = "development"
    api_prefix: str = "/api/v1"
    database_url: SecretStr
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    db_pool_size: int = Field(default=3, ge=1, le=20)
    db_timeout_seconds: float = Field(default=3, gt=0, le=30)
    expected_schema_revision: str = "0003_identity_limits"
    supabase_url: str | None = None
    supabase_publishable_key: SecretStr | None = None
    supabase_secret_key: SecretStr | None = None
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    nebius_api_key: SecretStr | None = None
    nebius_extraction_model: str | None = None
    nebius_reasoning_model: str | None = None
    nebius_embedding_model: str | None = None
    embedding_dimensions: int | None = Field(default=None, gt=0)
    embedding_version: str = "v1"
    prompt_version: str = "v1"
    voice_enabled: bool = False
    demo_enabled: bool = False
    model_timeout_seconds: int = Field(default=60, gt=0, le=600)
    job_max_attempts: int = Field(default=3, ge=1, le=10)
    worker_concurrency: int = Field(default=2, ge=1, le=16)
    auth_timeout_seconds: float = Field(default=5, gt=0, le=30)
    max_request_bytes: int = Field(default=131072, ge=1024, le=1048576)
    ai_daily_limit: int = Field(default=100, ge=1, le=10000)
    anonymous_ai_daily_limit: int = Field(default=5, ge=1, le=100)

    @field_validator("supabase_url")
    @classmethod
    def validate_auth_origin(cls, value: str | None) -> str | None:
        if value and urlsplit(value).path not in {"", "/"}:
            raise ValueError("Supabase URL must be a project origin without an API path")
        return value.rstrip("/") if value else None

    @field_validator("database_url")
    @classmethod
    def validate_database(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
            if url.drivername != "postgresql+psycopg" or not url.host or not url.database:
                raise ValueError
        except Exception:
            raise ValueError("must be a PostgreSQL psycopg URL with host and database") from None
        return value

    @field_validator("api_prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        if not value.startswith("/") or value.endswith("/") or "?" in value or "#" in value:
            raise ValueError("must be an absolute path without trailing slash, query or fragment")
        return value

    @field_validator("cors_origins")
    @classmethod
    def validate_origins(cls, values: list[str]) -> list[str]:
        for value in values:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
                or "*" in value
            ):
                raise ValueError("CORS origins must be explicit HTTP(S) origins without a path")
        return list(dict.fromkeys(values))

    @field_validator("nebius_base_url", "supabase_url")
    @classmethod
    def validate_service_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
        ):
            raise ValueError("must be an HTTPS service URL without credentials or query")
        return value

    @model_validator(mode="after")
    def production_requirements(self) -> "Settings":
        if self.app_env == "production":
            required = (
                "supabase_url", "supabase_publishable_key", "nebius_api_key",
                "nebius_extraction_model", "nebius_reasoning_model",
                "nebius_embedding_model", "embedding_dimensions",
            )
            missing = [name for name in required if not getattr(self, name)]
            if missing:
                raise ValueError("production requires: " + ", ".join(missing))
            if self.expected_schema_revision == "unversioned":
                raise ValueError("production requires a migrated expected_schema_revision")
            if any(not origin.startswith("https://") for origin in self.cors_origins):
                raise ValueError("production CORS origins must use HTTPS")
            if make_url(self.database_url.get_secret_value()).query.get("sslmode") != "verify-full":
                raise ValueError("production database URL requires sslmode=verify-full")
        return self

    def provider_status(self) -> dict[str, str]:
        inference = all((
            self.nebius_api_key, self.nebius_extraction_model, self.nebius_reasoning_model,
        ))
        embeddings = all((
            self.nebius_api_key, self.nebius_embedding_model, self.embedding_dimensions,
        ))
        return {
            "nebius_inference": "configured_unverified" if inference else "not_configured",
            "embeddings": "configured_unverified" if embeddings else "not_configured",
            "supabase_auth": (
                "configured_unverified"
                if self.supabase_url and self.supabase_publishable_key else "not_configured"
            ),
        }
