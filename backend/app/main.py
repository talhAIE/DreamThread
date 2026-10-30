"""Application factory: uvicorn app.main:create_app --factory."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware

from app.api.audio import router as audio_router
from app.api.body_limit import BodyLimitMiddleware
from app.api.connections import router as connections_router
from app.api.constellation import router as constellation_router
from app.api.conversations import messages_router
from app.api.conversations import router as conversations_router
from app.api.dreams import router as dreams_router
from app.api.insights import router as insights_router
from app.api.jobs import router as jobs_router
from app.api.me import router as me_router
from app.api.memory import router as memory_router
from app.api.privacy import router as privacy_router
from app.auth.identity import SupabaseIdentity
from app.config import Settings
from app.db import Database
from app.errors import (
    APIError,
    RequestContextMiddleware,
    api_error,
    error_response,
    http_error,
    validation_error,
)
from app.schemas.errors import ErrorEnvelope


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    authenticator=None,
    *,
    db: Database | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        settings: Optional ``Settings`` instance; if ``None`` the settings are
            loaded from the environment.
        database: Optional preconfigured ``Database`` instance.
        authenticator: Optional authenticator instance.
        db: Backward-compatible alias for ``database``.
    Returns:
        Configured ``FastAPI`` application.
    """
    try:
        settings = Settings() if settings is None else settings
    except ValidationError as error:
        fields = [str(err["loc"][0]) for err in error.errors()]
        raise RuntimeError("Invalid configuration: " + ", ".join(fields)) from None

    if database is not None and db is not None:
        raise ValueError("Provide either 'database' or 'db', not both")
    db = database if database is not None else db
    db = db if db is not None else Database(settings)
    authenticator = authenticator if authenticator is not None else SupabaseIdentity(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.database = db
        app.state.settings = settings
        app.state.authenticator = authenticator
        yield
        await authenticator.close()
        await db.close()

    app = FastAPI(
        title="DreamThread API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        responses={
            400: {"model": ErrorEnvelope},
            401: {"model": ErrorEnvelope},
            403: {"model": ErrorEnvelope},
            422: {"model": ErrorEnvelope},
            500: {"model": ErrorEnvelope},
            503: {"model": ErrorEnvelope},
        },
    )
    app.add_middleware(
        BodyLimitMiddleware,
        max_bytes=settings.max_request_bytes,
        audio_prefix=settings.api_prefix + "/audio/" if settings.voice_enabled else None,
        audio_max_bytes=settings.audio_max_bytes,
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Idempotency-Key"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )
    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(APIError, api_error)

    async def database_error(request, exc):
        return error_response(
            "database_unavailable", "Data service is unavailable", request.state.request_id, 503
        )

    app.add_exception_handler(SQLAlchemyError, database_error)
    app.include_router(me_router, prefix=settings.api_prefix)
    app.include_router(dreams_router, prefix=settings.api_prefix)
    app.include_router(jobs_router, prefix=settings.api_prefix)
    app.include_router(memory_router, prefix=settings.api_prefix)
    app.include_router(connections_router, prefix=settings.api_prefix)
    app.include_router(constellation_router, prefix=settings.api_prefix)
    app.include_router(conversations_router, prefix=settings.api_prefix)
    app.include_router(messages_router, prefix=settings.api_prefix)
    app.include_router(insights_router, prefix=settings.api_prefix)
    app.include_router(privacy_router, prefix=settings.api_prefix)
    if settings.voice_enabled:
        app.include_router(audio_router, prefix=settings.api_prefix)

    @app.get("/health/live", tags=["Health"])
    async def live():
        return {"status": "alive"}

    @app.get("/health/ready", tags=["Health"])
    async def ready(request: Request):
        result = await db.probe()
        if result != "ok":
            return error_response(
                "not_ready",
                "Service is not ready",
                request.state.request_id,
                503,
                {"database": result},
            )
        return {
            "status": "ready",
            "database": "ok",
            "schema_revision": settings.expected_schema_revision,
            "providers": settings.provider_status(),
        }

    return app
