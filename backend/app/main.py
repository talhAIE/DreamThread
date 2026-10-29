"""Application factory: uvicorn app.main:create_app --factory."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware

from app.api.body_limit import BodyLimitMiddleware
from app.api.dreams import router as dreams_router
from app.api.me import router as me_router
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


def create_app(settings: Settings | None = None, database=None, authenticator=None) -> FastAPI:
    if settings is None:
        try:
            settings = Settings()
        except ValidationError as exc:
            fields = sorted({".".join(str(x) for x in e["loc"]) or "configuration"
                             for e in exc.errors()})
            raise RuntimeError("Invalid configuration: " + ", ".join(fields)) from None
    db = database if database is not None else Database(settings)
    auth = authenticator if authenticator is not None else SupabaseIdentity(settings)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await auth.close()
            await db.close()

    app = FastAPI(
        title="DreamThread API", version="0.1.0", debug=False, lifespan=lifespan,
        responses={code: {"model": ErrorEnvelope}
                   for code in (401, 403, 404, 409, 413, 422, 429, 503)},
    )
    app.state.database = db
    app.state.settings = settings
    app.state.authenticator = auth
    # CORS wraps request handling so error responses also carry approved CORS headers.
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Idempotency-Key"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )
    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(APIError, api_error)

    async def database_error(request, exc):
        return error_response("database_unavailable", "Data service is unavailable",
                              request.state.request_id, 503)

    app.add_exception_handler(SQLAlchemyError, database_error)
    app.include_router(me_router, prefix=settings.api_prefix)
    app.include_router(dreams_router, prefix=settings.api_prefix)

    @app.get("/health/live", tags=["Health"])
    async def live():
        return {"status": "alive"}

    @app.get("/health/ready", tags=["Health"])
    async def ready(request: Request):
        result = await db.probe()
        if result != "ok":
            return error_response(
                "not_ready", "Service is not ready", request.state.request_id, 503,
                {"database": result},
            )
        return {
            "status": "ready", "database": "ok",
            "schema_revision": settings.expected_schema_revision,
            "providers": settings.provider_status(),
        }

    return app
