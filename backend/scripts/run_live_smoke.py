"""Opt-in synthetic backend journey against real Supabase Auth, Neon and Nebius.

Creates two temporary confirmed test users without sending emails, and processes ONLY their
jobs. Finally removes only those generated users/data/objects. Never resets a database.
The optional --audio fixture must contain synthetic speech. Prints metadata only.
"""

import argparse
import asyncio
import json
import secrets
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete, func, select, update

from app.config import Settings
from app.db import Database
from app.db.models import tables
from app.loop import create_loop
from app.main import create_app
from app.providers.storage import PrivateStorage
from app.services.storage_cleanup import StorageCleanup
from app.workers.queue import Queue
from app.workers.runner import Worker

A, J, ASSETS, CLEANUP = (
    tables["app." + name] for name in ("accounts", "jobs", "audio_assets", "cleanup_tasks")
)


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


async def drain(db, worker, user_id):
    for _ in range(30):
        async with db.tenant_transaction(user_id) as c:
            row = (
                (
                    await c.execute(
                        select(J)
                        .where(J.c.user_id == user_id, J.c.status == "queued")
                        .order_by(J.c.created_at, J.c.id)
                        .limit(1)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                failures = (
                    await c.execute(
                        select(J.c.kind, J.c.error_code).where(
                            J.c.user_id == user_id, J.c.status == "failed"
                        )
                    )
                ).all()
                require(not failures, "synthetic_job_failed")
                return
            leased = (
                (
                    await c.execute(
                        update(J)
                        .where(J.c.id == row["id"])
                        .values(
                            status="running",
                            attempts=J.c.attempts + 1,
                            lease_token=uuid4(),
                            heartbeat_at=func.clock_timestamp(),
                            leased_until=func.clock_timestamp()
                            + timedelta(seconds=worker.settings.job_lease_seconds),
                            deadline_at=func.clock_timestamp()
                            + timedelta(seconds=worker.settings.job_timeout_seconds),
                            stage_deadline_at=func.clock_timestamp()
                            + timedelta(seconds=worker.settings.job_stage_timeout_seconds),
                        )
                        .returning(J)
                    )
                )
                .mappings()
                .one()
            )
        print(
            json.dumps({"synthetic_job": leased["kind"], "attempt": leased["attempts"]}), flush=True
        )
        await worker.process(dict(leased))
    raise RuntimeError("synthetic_job_budget_exhausted")


async def probe(audio=None):
    settings = Settings()
    if audio:
        settings = settings.model_copy(update={"voice_enabled": True, "voice_verified": True})
    require(bool(settings.supabase_secret_key), "server_secret_required")
    users = []
    db, queue = Database(settings), Queue(settings)
    worker = Worker(settings, db, queue)
    app = create_app(settings, db)
    secret_headers = {"apikey": settings.supabase_secret_key.get_secret_value()}
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=30) as remote:
        try:
            require(await db.probe() == "ok", "database_not_ready")
            await queue.check_role()
            for _ in range(2):
                email, password = (
                    f"dreamthread-smoke-{uuid4().hex}@example.com",
                    secrets.token_urlsafe(32),
                )
                created = await remote.post(
                    settings.supabase_url + "/auth/v1/admin/users",
                    headers=secret_headers,
                    json={
                        "email": email,
                        "password": password,
                        "email_confirm": True,
                        "app_metadata": {"dreamthread_synthetic_smoke": True},
                    },
                )
                require(created.status_code in {200, 201}, "synthetic_user_creation_failed")
                user_id = UUID(created.json()["id"])
                users.append((user_id, None))
                signed_in = await remote.post(
                    settings.supabase_url + "/auth/v1/token?grant_type=password",
                    headers={"apikey": settings.supabase_publishable_key.get_secret_value()},
                    json={"email": email, "password": password},
                )
                require(signed_in.status_code == 200, "synthetic_sign_in_failed")
                users[-1] = user_id, signed_in.json()["access_token"]
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://synthetic.local",
                    timeout=180,
                ) as client:
                    uid, token = users[0]
                    auth = {"Authorization": "Bearer " + token}
                    other = {"Authorization": "Bearer " + users[1][1]}

                    async def api(method, path, *, headers=None, status=200, **kwargs):
                        response = await client.request(
                            method, settings.api_prefix + path, headers=headers or auth, **kwargs
                        )
                        require(
                            response.status_code == status,
                            "api_" + path.split("/")[1] + "_status_" + str(response.status_code),
                        )
                        return response.json() if status != 204 else None

                    await api("GET", "/me")
                    await api("GET", "/me", headers=other)
                    require(
                        (await client.get(settings.api_prefix + "/dreams")).status_code == 401,
                        "missing_auth_not_rejected",
                    )
                    print(json.dumps({"supabase_real_sign_in": True}), flush=True)
                    dreams = []
                    for day, text in [
                        (
                            "2026-10-04",
                            "I stood outside a cedar house in a forest. "
                            "The door was closed. I felt curious.",
                        ),
                        (
                            "2026-10-16",
                            "I walked into the cedar house in the forest. "
                            "The door was open. I felt calm.",
                        ),
                    ]:
                        entry = await api(
                            "POST",
                            "/dreams",
                            status=201,
                            headers={**auth, "Idempotency-Key": uuid4().hex},
                            json={
                                "title": "Synthetic cedar house",
                                "dream_date": day,
                                "text": text,
                                "reported_emotions": ["curious"],
                            },
                        )
                        dreams.append(entry["id"])
                        await drain(db, worker, uid)
                        analysis = await api("GET", f"/dreams/{entry['id']}/analysis")
                        require(bool(analysis), "analysis_missing")
                    foreign = await client.get(
                        settings.api_prefix + f"/dreams/{dreams[0]}", headers=other
                    )
                    require(foreign.status_code == 404, "cross_user_dream_access")
                    graph = await api("GET", "/constellation")
                    connections = await api("GET", f"/dreams/{dreams[-1]}/connections")
                    print(
                        json.dumps(
                            {
                                "journal_analysis_embedding_continuity": True,
                                "constellation": True,
                                "connection_response_present": bool(connections),
                                "graph_response_present": bool(graph),
                            }
                        ),
                        flush=True,
                    )
                    conversation = await api(
                        "POST",
                        "/conversations",
                        status=201,
                        json={"title": "Synthetic investigation"},
                    )
                    answer = await api(
                        "POST",
                        f"/conversations/{conversation['id']}/messages",
                        status=202,
                        headers={**auth, "Idempotency-Key": uuid4().hex},
                        json={"question": "When did I first mention the house?"},
                    )
                    await drain(db, worker, uid)
                    await api("GET", f"/conversations/{conversation['id']}/messages")
                    await api("GET", "/insights?month=2026-10&timezone=UTC")
                    await api("POST", "/time-capsules/2026-10/refresh", status=202)
                    await drain(db, worker, uid)
                    await api("GET", "/time-capsules/2026-10?timezone=UTC")
                    print(
                        json.dumps(
                            {
                                "detective": True,
                                "insights": True,
                                "time_capsule": True,
                                "detective_submission_present": bool(answer),
                            }
                        ),
                        flush=True,
                    )
                    if audio:
                        data = audio.read_bytes()
                        intent = await api(
                            "POST",
                            "/audio/upload-intents",
                            status=201,
                            headers={**auth, "Idempotency-Key": uuid4().hex},
                            json={"content_type": "audio/wav", "size_bytes": len(data)},
                        )
                        response = await client.put(
                            intent["upload_url"],
                            content=data,
                            headers={**auth, "Content-Type": "audio/wav"},
                        )
                        require(response.status_code == 204, "synthetic_audio_upload_failed")
                        await api("POST", f"/audio/{intent['asset_id']}/complete", status=202)
                        await drain(db, worker, uid)
                        draft = await api("GET", f"/audio/{intent['asset_id']}")
                        require(bool(draft["transcript_draft"]), "transcript_draft_missing")
                        await api(
                            "POST",
                            "/dreams",
                            status=201,
                            headers={**auth, "Idempotency-Key": uuid4().hex},
                            json={
                                "title": "Reviewed synthetic voice",
                                "dream_date": "2026-10-28",
                                "text": draft["transcript_draft"],
                                "input_mode": "voice",
                                "audio_asset_id": intent["asset_id"],
                            },
                        )
                        await drain(db, worker, uid)
                        async with db.tenant_transaction(uid) as c:
                            tasks = (
                                (
                                    await c.execute(
                                        select(CLEANUP.c.id).where(
                                            CLEANUP.c.user_id == uid, CLEANUP.c.state != "complete"
                                        )
                                    )
                                )
                                .scalars()
                                .all()
                            )
                        for task in tasks:
                            await StorageCleanup(db, settings).process(uid, task)
                        print(json.dumps({"voice_review_save_and_raw_cleanup": True}), flush=True)
                    export = await api(
                        "POST",
                        "/exports",
                        status=202,
                        headers={**auth, "Idempotency-Key": uuid4().hex},
                        json={"format": "json"},
                    )
                    await drain(db, worker, uid)
                    downloaded = await client.get(
                        settings.api_prefix + f"/exports/{export['id']}/download", headers=auth
                    )
                    require(downloaded.status_code == 200, "export_download_failed")
                    require(
                        len(downloaded.json()["dreams"]) == (3 if audio else 2),
                        "export_content_mismatch",
                    )
                    await api("DELETE", "/me/journal", status=202, json={"confirm": True})
                    await drain(db, worker, uid)
                    listing = await api("GET", "/dreams")
                    require(not listing["items"], "deleted_journal_not_empty")
                    print(
                        json.dumps(
                            {
                                "backend_live_smoke": True,
                                "export_and_journal_deletion": True,
                                "cross_user_isolation": True,
                                "voice": bool(audio),
                            }
                        ),
                        flush=True,
                    )
        finally:
            # Identity IDs originate exclusively from successful admin-created smoke users.
            # Never inspect/reset another account or consume the shared queue globally.
            cleanup_db = Database(settings)
            try:
                for uid, _ in users:
                    async with cleanup_db.tenant_transaction(uid) as c:
                        keys = (
                            (
                                await c.execute(
                                    select(ASSETS.c.storage_key).where(ASSETS.c.user_id == uid)
                                )
                            )
                            .scalars()
                            .all()
                        )
                    if keys:
                        await PrivateStorage(settings).remove(list(keys))
                    async with cleanup_db.tenant_transaction(uid) as c:
                        await c.execute(delete(A).where(A.c.id == uid))
                    result = await remote.delete(
                        settings.supabase_url + f"/auth/v1/admin/users/{uid}",
                        headers=secret_headers,
                    )
                    require(result.status_code in {200, 204}, "synthetic_auth_cleanup_failed")
            finally:
                await cleanup_db.close()
                await queue.close()
                await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--audio", type=Path)
    args = parser.parse_args()
    if not args.live:
        parser.error("--live creates/removes synthetic users/data and incurs model charges")
    try:
        with asyncio.Runner(loop_factory=create_loop) as runner:
            runner.run(probe(args.audio))
    except Exception as error:
        # RuntimeError strings are fixed test categories; other exceptions can contain secrets.
        print(
            json.dumps(
                {
                    "backend_live_smoke": False,
                    "error_type": type(error).__name__,
                    "error_code": str(error)
                    if type(error) is RuntimeError
                    else getattr(error, "code", "dependency_or_validation_failure"),
                }
            )
        )
        raise SystemExit(1) from None
