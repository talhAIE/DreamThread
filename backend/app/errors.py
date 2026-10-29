"""Stable errors and request correlation, including unexpected server failures."""

import logging
import re
from uuid import uuid4

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("dreamthread")
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def error_response(code, message, request_id, status_code, details=None, headers=None):
    return JSONResponse(
        {"error": {"code": code, "message": message, "request_id": request_id,
                   "details": details or {}}},
        status_code=status_code, headers=headers,
    )


async def http_error(request: Request, exc: HTTPException):
    messages = {404: "Resource not found", 405: "Method not allowed"}
    return error_response(
        f"http_{exc.status_code}", messages.get(exc.status_code, "Request failed"),
        request.state.request_id, exc.status_code, headers=exc.headers,
    )


async def validation_error(request: Request, exc: RequestValidationError):
    # Only locations/types; Pydantic's input and context may contain submitted secrets.
    issues = [{"location": list(item["loc"]), "type": item["type"]} for item in exc.errors()]
    return error_response(
        "validation_error", "Invalid request", request.state.request_id, 422,
        {"issues": issues},
    )


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = Headers(scope=scope).get("x-request-id", "")
        request_id = supplied if REQUEST_ID.fullmatch(supplied) else uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = False

        async def send_with_id(message: Message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:
            logger.error("request_failed request_id=%s type=%s", request_id, type(exc).__name__)
            if started:
                raise
            response = error_response(
                "internal_error", "An unexpected error occurred", request_id, 500
            )
            await response(scope, receive, send_with_id)
