"""Application factory: uvicorn app.main:create_app --factory."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware

from app.config import Settings
from app.db import Database
from app.errors import RequestContextMiddleware, error_response, http_error, validation_error


def create_app(settings: Settings | None = None, database=None) -> FastAPI:
    if settings is None:
        try:
            settings = Settings()
        except ValidationError as exc:
            fields = sorted({".".join(str(x) for x in e["loc"]) or "configuration"
                             for e in exc.errors()})
            raise RuntimeError("Invalid configuration: " + ", ".join(fields)) from None
    db = database if database is not None else Database(settings)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await db.close()

    app = FastAPI(title="DreamThread API", version="0.1.0", debug=False, lifespan=lifespan)
    app.state.database = db
    app.state.settings = settings
    # CORS wraps request handling so error responses also carry approved CORS headers.
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Idempotency-Key"],
        expose_headers=["X-Request-ID"],
    )
    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)

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
