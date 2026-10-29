import asyncio
from datetime import date
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from app.auth.identity import SupabaseIdentity
from app.config import Settings
from app.errors import APIError
from app.main import create_app
from app.schemas.identity import Emotion, MePatch, PreferencesPatch
from app.schemas.pagination import PageParams, decode_cursor, encode_cursor

URL = "postgresql+psycopg://user:secret@localhost:5432/test"


def settings(**kwargs):
    return Settings(_env_file=None, database_url=URL, supabase_url="https://project.supabase.co",
                    supabase_publishable_key="publishable-test", **kwargs)


@pytest.mark.parametrize("upstream,expected", [(401, 401), (403, 401), (429, 503),
                                               (500, 503), (302, 503), (400, 503)])
def test_auth_status_mapping(upstream, expected):
    async def check():
        def transport(request):
            assert str(request.url) == "https://project.supabase.co/auth/v1/user"
            assert request.headers["authorization"] == "Bearer expired-or-wrong-project"
            assert request.headers["apikey"] == "publishable-test"
            return httpx.Response(upstream, json={"secret": "must-not-leak"})
        service = SupabaseIdentity(settings(), httpx.AsyncClient(transport=httpx.MockTransport(
            transport
        )))
        try:
            with pytest.raises(APIError) as error:
                await service.verify("Bearer expired-or-wrong-project")
            assert error.value.status == expected
            assert "must-not-leak" not in error.value.message
        finally:
            await service.close()
    asyncio.run(check())


def test_auth_network_and_invalid_response_fail_closed():
    async def check():
        def timeout(request):
            raise httpx.ReadTimeout("secret transport context", request=request)
        for transport in (
            timeout, lambda request: httpx.Response(200, json={"id": "not-a-uuid"}),
            lambda request: httpx.Response(200, json={"id": 123}),
            lambda request: httpx.Response(200, content=b"not json"),
        ):
            service = SupabaseIdentity(settings(), httpx.AsyncClient(
                transport=httpx.MockTransport(transport)
            ))
            try:
                with pytest.raises(APIError) as error:
                    await service.verify("Bearer token")
                assert error.value.status == 503
            finally:
                await service.close()
    asyncio.run(check())


@pytest.mark.parametrize("header", [None, "", "Basic private", "Bearer", "Bearer two words",
                                   "Bearer " + "x" * 8193])
def test_malformed_credentials_never_call_auth(header):
    async def check():
        def transport(request):
            raise AssertionError("Invalid headers must fail before network access")
        service = SupabaseIdentity(settings(), httpx.AsyncClient(
            transport=httpx.MockTransport(transport)
        ))
        try:
            with pytest.raises(APIError) as error:
                await service.verify(header)
            assert error.value.status == 401
        finally:
            await service.close()
    asyncio.run(check())


def test_verified_identity_ignores_metadata_permissions():
    async def check():
        user = uuid4()
        service = SupabaseIdentity(settings(), httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={
                "id": str(user), "role": "authenticated", "is_anonymous": False,
                "user_metadata": {"id": str(uuid4()), "role": "service_role", "is_anonymous": True},
            })
        )))
        try:
            identity = await service.verify("Bearer opaque-token")
            assert identity.user_id == user
            assert not identity.is_anonymous
        finally:
            await service.close()
    asyncio.run(check())


def test_timezone_emotions_and_pagination_contracts():
    assert MePatch(expected_version=1, timezone="Asia/Karachi").timezone == "Asia/Karachi"
    for data in ({"timezone": "Not/AZone"}, {"display_name": None}, {}, {"user_id": str(uuid4())}):
        with pytest.raises(ValidationError):
            MePatch(expected_version=1, **data)
    with pytest.raises(ValidationError):
        PreferencesPatch(expected_version=1, reduced_motion="true")
    assert TypeAdapter(Emotion).validate_python("calm") == "calm"
    with pytest.raises(ValidationError):
        TypeAdapter(Emotion).validate_python("diagnosed")
    day, user = date(2026, 10, 29), uuid4()
    assert decode_cursor(encode_cursor(day, user)) == (day, user)
    for value in ("invalid!", "a" * 257, "bnVsbA"):
        with pytest.raises(APIError):
            decode_cursor(value)
    assert PageParams().limit == 20
    with pytest.raises(ValidationError):
        PageParams(limit=101)


def test_body_limits_before_auth_and_uniform_errors():
    class DB:
        async def close(self):
            pass

    config = settings(max_request_bytes=1024)
    app = create_app(config, DB())
    with TestClient(app) as client:
        response = client.patch("/api/v1/me", content=b"x" * 1025)
        assert response.status_code == 413
        assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
        assert client.get("/api/v1/me").status_code == 401
        response = client.patch("/api/v1/me", content=b"x", headers={"Content-Encoding": "gzip"})
        assert response.status_code == 413


def test_streaming_body_is_capped_without_content_length():
    class DB:
        async def close(self):
            pass

    with TestClient(create_app(settings(max_request_bytes=1024), DB())) as client:
        response = client.patch("/api/v1/me", content=iter([b"x" * 600, b"x" * 600]))
        assert response.status_code == 413
        assert "request_id" in response.json()["error"]


def test_missing_server_auth_configuration_returns_unavailable():
    async def check():
        service = SupabaseIdentity(Settings(_env_file=None, database_url=URL))
        try:
            with pytest.raises(APIError) as error:
                await service.verify("Bearer token")
            assert error.value.status == 503
        finally:
            await service.close()
    asyncio.run(check())
