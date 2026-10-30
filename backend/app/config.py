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
    queue_database_url: SecretStr | None = None
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    db_pool_size: int = Field(default=3, ge=1, le=20)
    db_timeout_seconds: float = Field(default=3, gt=0, le=30)
    db_connect_timeout_seconds: int = Field(default=15, ge=3, le=60)
    db_pool_recycle_seconds: int = Field(default=300, ge=30, le=3600)
    queue_pool_size: int = Field(default=1, ge=1, le=5)
    expected_schema_revision: str = "0010_audio_cleanup"
    supabase_url: str | None = None
    supabase_publishable_key: SecretStr | None = None
    supabase_secret_key: SecretStr | None = None
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    nebius_api_key: SecretStr | None = None
    nebius_extraction_model: str | None = Field(default=None, min_length=1, max_length=200)
    nebius_reasoning_model: str | None = Field(default=None, min_length=1, max_length=200)
    nebius_embedding_model: str | None = Field(default=None, min_length=1, max_length=200)
    embedding_dimensions: int | None = Field(default=None, gt=0, le=16000)
    embedding_version: str = Field(default="v1", min_length=1, max_length=50)
    embedding_document_prefix: str | None = Field(default=None, max_length=1000)
    embedding_query_prefix: str | None = Field(default=None, max_length=1000)
    embedding_prefixes_verified: bool = False
    embedding_input_token_budget: int = Field(default=2048, ge=512, le=32768)
    embedding_batch_byte_budget: int = Field(default=32768, ge=4096, le=262144)
    prompt_version: str = Field(default="v2", min_length=1, max_length=50)
    voice_enabled: bool = False
    voice_verified: bool = False
    supabase_audio_bucket: str = Field(default="dreamthread-audio", pattern=r"^[a-z0-9-]{3,63}$")
    storage_timeout_seconds: int = Field(default=30, ge=5, le=60)
    audio_max_bytes: int = Field(default=10485760, ge=1024, le=10485760)
    audio_max_duration_seconds: int = Field(default=180, ge=1, le=180)
    audio_upload_intent_ttl_seconds: int = Field(default=300, ge=60, le=900)
    audio_draft_ttl_seconds: int = Field(default=86400, ge=600, le=86400)
    asr_model: str = Field(default="small", pattern=r"^(tiny|base|small)(\.en)?$")
    asr_timeout_seconds: int = Field(default=150, ge=10, le=240)
    demo_enabled: bool = False
    model_timeout_seconds: int = Field(default=60, gt=0, le=600)
    nebius_output_mode: Literal["json_object", "json_schema"] = "json_object"
    model_max_output_tokens: int = Field(default=2048, ge=256, le=8192)
    model_context_token_budget: int = Field(default=32768, ge=4096, le=262144)
    job_max_attempts: int = Field(default=3, ge=1, le=10)
    worker_concurrency: int = Field(default=2, ge=1, le=16)
    auth_timeout_seconds: float = Field(default=5, gt=0, le=30)
    max_request_bytes: int = Field(default=131072, ge=1024, le=1048576)
    user_request_rate_limit: int = Field(default=120, ge=10, le=10000)
    ai_daily_limit: int = Field(default=100, ge=1, le=10000)
    anonymous_ai_daily_limit: int = Field(default=5, ge=1, le=100)
    job_lease_seconds: float = Field(default=120, gt=0, le=600)
    job_heartbeat_seconds: float = Field(default=20, gt=0, le=120)
    job_stage_timeout_seconds: float = Field(default=90, gt=0, le=1800)
    job_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    job_retry_base_seconds: float = Field(default=2, gt=0, le=60)
    job_retry_max_seconds: float = Field(default=60, gt=0, le=3600)
    worker_poll_seconds: float = Field(default=1, gt=0, le=30)
    worker_maintenance_seconds: float = Field(default=30, gt=0, le=300)
    worker_shutdown_seconds: float = Field(default=10, gt=0, le=60)
    checkpoint_retention_days: int = Field(default=7, ge=1, le=30)
    agent_max_rounds: int = Field(default=4, ge=1, le=10)
    agent_max_tool_calls: int = Field(default=8, ge=1, le=20)
    agent_max_candidate_dreams: int = Field(default=12, ge=1, le=50)
    agent_timeout_seconds: float = Field(default=90.0, gt=0, le=300)
    agent_max_output_tokens: int = Field(default=2048, ge=256, le=8192)

    @field_validator(
        "nebius_api_key",
        "nebius_extraction_model",
        "nebius_reasoning_model",
        "nebius_embedding_model",
        "supabase_publishable_key",
        "supabase_secret_key",
        mode="before",
    )
    @classmethod
    def empty_optional_setting(cls, value):
        return None if value == "" else value

    @field_validator("supabase_url")
    @classmethod
    def validate_auth_origin(cls, value: str | None) -> str | None:
        if value and urlsplit(value).path not in {"", "/"}:
            raise ValueError("Supabase URL must be a project origin without an API path")
        return value.rstrip("/") if value else None

    @field_validator("database_url", "queue_database_url")
    @classmethod
    def validate_database(cls, value: SecretStr | None) -> SecretStr | None:
        from app.db.connections import postgres_url

        if value is None:
            return value
        return SecretStr(
            postgres_url(value.get_secret_value()).render_as_string(hide_password=False)
        )

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
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("must be an HTTPS service URL without credentials or query")
        return value

    @model_validator(mode="after")
    def production_requirements(self) -> "Settings":
        if self.voice_enabled and not (
            self.voice_verified and self.supabase_url and self.supabase_secret_key
        ):
            raise ValueError("Voice requires verified private storage and speech processing")
        if self.voice_enabled and self.asr_timeout_seconds + 10 >= self.job_timeout_seconds:
            raise ValueError("ASR timeout must leave publication time within the job timeout")
        if self.job_heartbeat_seconds >= self.job_lease_seconds / 2:
            raise ValueError("Job heartbeat must be shorter than half the lease")
        if self.job_stage_timeout_seconds > self.job_timeout_seconds:
            raise ValueError("Stage timeout must not exceed the job timeout")
        if self.app_env == "production":
            required = (
                "supabase_url",
                "supabase_publishable_key",
                "nebius_api_key",
                "nebius_extraction_model",
                "nebius_reasoning_model",
                "nebius_embedding_model",
                "embedding_dimensions",
            )
            missing = [name for name in required if not getattr(self, name)]
            if missing:
                raise ValueError("production requires: " + ", ".join(missing))
            if (
                not self.embedding_prefixes_verified
                or self.embedding_document_prefix is None
                or self.embedding_query_prefix is None
            ):
                raise ValueError("production requires verified explicit embedding prefixes")
            if self.expected_schema_revision == "unversioned":
                raise ValueError("production requires a migrated expected_schema_revision")
            if any(not origin.startswith("https://") for origin in self.cors_origins):
                raise ValueError("production CORS origins must use HTTPS")
            if make_url(self.database_url.get_secret_value()).query.get("sslmode") != "verify-full":
                raise ValueError("production database URL requires sslmode=verify-full")
            if (
                self.queue_database_url
                and make_url(self.queue_database_url.get_secret_value()).query.get("sslmode")
                != "verify-full"
            ):
                raise ValueError("production queue database URL requires sslmode=verify-full")
        return self

    def provider_status(self) -> dict[str, str]:
        inference = all(
            (
                self.nebius_api_key,
                self.nebius_extraction_model,
                self.nebius_reasoning_model,
            )
        )
        embeddings = all(
            (
                self.nebius_api_key,
                self.nebius_embedding_model,
                self.embedding_dimensions,
                self.embedding_prefixes_verified,
                self.embedding_document_prefix is not None,
                self.embedding_query_prefix is not None,
            )
        )
        return {
            "nebius_inference": "configured_unverified" if inference else "not_configured",
            "embeddings": "configured_unverified" if embeddings else "not_configured",
            "supabase_auth": (
                "configured_unverified"
                if self.supabase_url and self.supabase_publishable_key
                else "not_configured"
            ),
            "voice": "disabled_unverified",
        }
