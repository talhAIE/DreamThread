import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import Request
from sqlalchemy import text
from test_phase2_database import cluster as cluster

from app.api.dependencies import CurrentIdentity
from app.auth.identity import Identity, SupabaseIdentity
from app.config import Settings
from app.db import Database
from app.errors import APIError
from app.loop import create_loop
from app.main import create_app
from app.services.accounts import AccountService
from app.services.admission import reserve_ai

pytestmark = pytest.mark.integration


def run(check):
    with asyncio.Runner(loop_factory=create_loop) as runner:
        runner.run(check())


@asynccontextmanager
async def session(cluster, *, db=None, **values):
    settings = Settings(
        _env_file=None, database_url=cluster["urls"]["app"].render_as_string(hide_password=False),
        db_pool_size=4, embedding_dimensions=3,
        supabase_url="https://project.supabase.co", supabase_publishable_key="public-test",
        **values,
    )
    db = db or Database(settings)
    calls = []

    def verify(request):
        calls.append(request.headers["authorization"])
        token = request.headers["authorization"].removeprefix("Bearer ")
        if token in {"invalid", "expired", "other-project"}:
            return httpx.Response(401)
        if token == "outage":
            return httpx.Response(503, json={"private_error": "never expose"})
        anonymous = token.startswith("anonymous-")
        user = token.removeprefix("anonymous-")
        return httpx.Response(200, json={"id": user, "role": "authenticated",
                                        "is_anonymous": anonymous})

    auth = SupabaseIdentity(settings, httpx.AsyncClient(transport=httpx.MockTransport(verify)))
    app = create_app(settings, db, auth)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://local") as client:
            yield client, db, app, settings, calls


def token(user):
    return {"Authorization": "Bearer " + str(user)}


def test_auth_rejection_provisions_nothing(cluster):
    async def check():
        with cluster["admin"].connect() as c:
            before = c.scalar(text("SELECT count(*) FROM app.accounts"))
        async with session(cluster) as (client, _, _, _, _calls):
            for name, status in (("invalid", 401), ("expired", 401), ("other-project", 401),
                                 ("outage", 503)):
                response = await client.get("/api/v1/me", headers=token(name))
                assert response.status_code == status
                assert "never expose" not in response.text
                assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
            assert (await client.get("/api/v1/me")).status_code == 401
        with cluster["admin"].connect() as c:
            assert c.scalar(text("SELECT count(*) FROM app.accounts")) == before
    run(check)


def test_concurrent_first_requests_and_owned_preferences(cluster):
    async def check():
        a, b = uuid4(), uuid4()
        async with session(cluster) as (client, db, _, _, calls):
            responses = await asyncio.gather(*[
                client.get("/api/v1/me", headers=token(a)) for _ in range(8)
            ])
            assert all(r.status_code == 200 for r in responses)
            assert len(calls) == 8  # each token is checked remotely, no unverified session cache
            assert responses[0].json()["timezone"] == "UTC"
            assert responses[0].json()["created_at"].endswith("Z")
            assert (await client.get("/api/v1/me", headers=token(b))).status_code == 200
            response = await client.patch("/api/v1/me", headers=token(a), json={
                "expected_version": 1, "display_name": "Sarah", "timezone": "Asia/Karachi",
            })
            assert response.status_code == 200
            assert response.json()["version"] == 2
            assert (await client.patch("/api/v1/me", headers=token(a), json={
                "expected_version": 1, "display_name": "Stale",
            })).status_code == 409
            pref = (await client.get("/api/v1/me/preferences", headers=token(a))).json()
            assert pref["timezone"] == "Asia/Karachi"
            response = await client.patch("/api/v1/me/preferences", headers=token(a), json={
                "expected_version": pref["version"], "theme": "light", "reduced_motion": True,
            })
            assert response.status_code == 200
            assert response.json()["theme"] == "light"
            assert (await client.get("/api/v1/me", headers=token(b))).json()["display_name"] == ""
            for bad in ({"timezone": "Mars/Crater"}, {"id": str(b)}, {"display_name": None}):
                response = await client.patch("/api/v1/me", headers=token(a), json={
                    "expected_version": 2, **bad,
                })
                assert response.status_code == 422
            async with db.tenant_transaction(a) as c:
                assert await c.scalar(text("SELECT count(*) FROM app.accounts")) == 1
                assert await c.scalar(text("SELECT count(*) FROM app.preferences")) == 1
    run(check)


def test_anonymous_demo_gating(cluster):
    async def check():
        user = uuid4()
        headers = token("anonymous-" + str(user))
        async with session(cluster) as (client, _, _, _, _):
            assert (await client.get("/api/v1/me", headers=headers)).status_code == 403
        async with session(cluster, demo_enabled=True) as (client, _, _, _, _):
            response = await client.get("/api/v1/me", headers=headers)
            assert response.status_code == 200
            assert response.json()["id"] == str(user)
            assert response.json()["is_anonymous"] is True
    run(check)


def test_account_deletion_marker_blocks_reprovisioning(cluster):
    async def check():
        user = uuid4()
        async with session(cluster) as (client, db, _, _, _):
            assert (await client.get("/api/v1/me", headers=token(user))).status_code == 200
            async with db.tenant_transaction(user) as c:
                await c.execute(
                    text("UPDATE app.accounts SET deletion_state='deleting' WHERE id=:u"),
                    {"u": user},
                )
            response = await client.get("/api/v1/me", headers=token(user))
            assert response.status_code == 403
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT deletion_state FROM app.accounts")) == "deleting"
    run(check)


def test_quota_idempotency_and_provider_gate(cluster):
    async def check():
        user = uuid4()
        provider_calls = []
        async with session(cluster, ai_daily_limit=2) as (client, db, app, settings, _):
            @app.post("/test-ai")
            async def expensive(request: Request, identity: CurrentIdentity, body: dict):
                async with db.tenant_transaction(identity.user_id) as c:
                    admission = await reserve_ai(
                        c, identity, settings, route="dream.analyze",
                        key=request.headers.get("Idempotency-Key"), body=body,
                    )
                if not admission.replayed:
                    provider_calls.append(admission.resource_id)
                return {"id": str(admission.resource_id), "replayed": admission.replayed}

            async def request(key, body=None):
                return await client.post(
                    "/test-ai", headers={**token(user), "Idempotency-Key": key},
                    json=body or {"question": "house"},
                )
            first = await request("one")
            again = await request("one")
            assert first.status_code == again.status_code == 200
            assert first.json()["id"] == again.json()["id"]
            assert again.json()["replayed"]
            assert (await request("one", {"question": "different"})).status_code == 409
            assert (await request("two")).status_code == 200
            blocked = await request("three")
            assert blocked.status_code == 429
            assert int(blocked.headers["retry-after"]) > 0
            assert blocked.json()["error"]["code"] == "quota_exceeded"
            assert len(provider_calls) == 2
            assert (await request("one")).status_code == 200  # replay works at exhausted quota
    run(check)


def test_quotas_across_two_database_pools_and_rollback(cluster):
    async def check():
        async with session(cluster, ai_daily_limit=3, anonymous_ai_daily_limit=1) as (
            _, db, _, settings, _,
        ):
            identity = Identity(uuid4())
            await AccountService(db).ensure(identity)
            second = Database(settings)
            try:
                async def attempt(index):
                    pool = db if index % 2 else second
                    try:
                        async with pool.tenant_transaction(identity.user_id) as c:
                            return await reserve_ai(c, identity, settings, route="dream.create",
                                                    key=f"parallel-{index}", body={"index": index})
                    except APIError as error:
                        assert error.status == 429
                        return None
                results = await asyncio.gather(*[attempt(i) for i in range(12)])
                assert sum(result is not None for result in results) == 3
                anonymous = Identity(uuid4(), is_anonymous=True)
                await AccountService(db).ensure(anonymous)
                with pytest.raises(RuntimeError):
                    async with db.tenant_transaction(anonymous.user_id) as c:
                        await reserve_ai(c, anonymous, settings, route="dream.create",
                                         key="rollback", body={})
                        raise RuntimeError("Resource creation failed")
                now = datetime.now(UTC)
                async with db.tenant_transaction(anonymous.user_id) as c:
                    await reserve_ai(c, anonymous, settings, route="dream.create",
                                     key="rollback", body={}, now=now)
                with pytest.raises(APIError) as error:
                    async with db.tenant_transaction(anonymous.user_id) as c:
                        await reserve_ai(c, anonymous, settings, route="dream.create",
                                         key="over-limit", body={}, now=now)
                assert error.value.status == 429
                async with db.tenant_transaction(anonymous.user_id) as c:
                    admission = await reserve_ai(c, anonymous, settings, route="dream.create",
                                                key="new-day", body={}, now=now+timedelta(days=1))
                    assert not admission.replayed
            finally:
                await second.close()
    run(check)
