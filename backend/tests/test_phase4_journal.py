import asyncio
import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, text, update
from test_phase2_database import cluster as cluster
from test_phase3_identity import run, session, token

from app.db.models import tables
from app.services.journal import JournalService, pipeline_version

pytestmark = pytest.mark.integration


def body(**values):
    return {
        "title": "The house in the forest",
        "dream_date": "2026-10-29",
        "text": "I returned to the house.\n  🌙 خواب — calm.\n",
        "reported_emotions": ["calm"],
        **values,
    }


async def create(client, user, key=None, **values):
    return await client.post(
        "/api/v1/dreams",
        json=body(**values),
        headers={
            **token(user),
            "Idempotency-Key": key or uuid4().hex,
        },
    )


def test_exact_roundtrip_and_atomic_idempotent_save(cluster):
    async def check():
        user = uuid4()
        async with session(cluster) as (client, db, _, _, _):
            responses = await asyncio.gather(
                *[create(client, user, "same-create") for _ in range(6)]
            )
            assert [r.status_code for r in responses] == [201] * 6
            dreams = [r.json() for r in responses]
            assert len({d["id"] for d in dreams}) == 1
            assert len({d["analysis_job_id"] for d in dreams}) == 1
            dream = dreams[0]
            assert dream["text"] == body()["text"]
            assert dream["content_hash"] == hashlib.sha256(body()["text"].encode()).hexdigest()
            assert dream["reported_emotions"] == ["calm"]
            assert dream["analysis_state"] == "queued"
            detail = await client.get(f"/api/v1/dreams/{dream['id']}", headers=token(user))
            assert detail.json()["text"] == body()["text"]
            analysis = await client.get(
                f"/api/v1/dreams/{dream['id']}/analysis", headers=token(user)
            )
            assert analysis.json()["status"] == "queued"
            assert analysis.json()["reflections"] is None
            async with db.tenant_transaction(user) as c:
                for name in ("dreams", "dream_revisions", "jobs"):
                    assert await c.scalar(text(f"SELECT count(*) FROM app.{name}")) == 1
                assert await c.scalar(text("SELECT used FROM app.ai_usage")) == 1
            conflict = await create(client, user, "same-create", text="Changed request")
            assert conflict.status_code == 409

    run(check)


def test_draft_promotion_and_immutable_source_revisions(cluster):
    async def check():
        user = uuid4()
        async with session(cluster) as (client, db, _, _, _):
            draft = (await create(client, user, text="", lifecycle="draft")).json()
            assert draft["analysis_job_id"] is None
            assert draft["analysis_state"] == "not_requested"
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT count(*) FROM app.ai_usage")) == 0
                assert await c.scalar(text("SELECT count(*) FROM app.jobs")) == 0
            response = await client.post(
                f"/api/v1/dreams/{draft['id']}/analyze",
                json={"expected_version": 1},
                headers={**token(user), "Idempotency-Key": "draft-analysis"},
            )
            assert response.status_code == 422
            response = await client.patch(
                f"/api/v1/dreams/{draft['id']}",
                headers=token(user),
                json={
                    "expected_version": 1,
                    "text": body()["text"],
                    "lifecycle": "saved",
                    "reported_emotions": ["curious"],
                },
            )
            assert response.status_code == 200
            saved = response.json()
            assert saved["version"] == 2 and saved["revision"] == 2
            assert saved["analysis_job_id"]
            original = await client.get(
                f"/api/v1/dreams/{draft['id']}/revisions/{draft['revision_id']}",
                headers=token(user),
            )
            assert original.json()["text"] == ""
            assert original.json()["is_current"] is False
            response = await client.patch(
                f"/api/v1/dreams/{saved['id']}",
                headers=token(user),
                json={
                    "expected_version": 2,
                    "reported_emotions": ["calm"],
                },
            )
            assert response.json()["revision"] == 3
            assert response.json()["text"] == body()["text"]
            old = await client.get(
                f"/api/v1/dreams/{saved['id']}/revisions/{saved['revision_id']}",
                headers=token(user),
            )
            assert old.json()["reported_emotions"] == ["curious"]

    run(check)


def test_concurrent_edits_and_noop_patch(cluster):
    async def check():
        user = uuid4()
        async with session(cluster) as (client, db, _, _, _):
            dream = (await create(client, user)).json()
            path = f"/api/v1/dreams/{dream['id']}"
            noop = await client.patch(
                path,
                headers=token(user),
                json={
                    "expected_version": 1,
                    "text": body()["text"],
                },
            )
            assert noop.json()["version"] == 1
            responses = await asyncio.gather(
                *[
                    client.patch(
                        path,
                        headers=token(user),
                        json={
                            "expected_version": 1,
                            "text": f"A different dream {i}",
                        },
                    )
                    for i in range(2)
                ]
            )
            assert sorted(r.status_code for r in responses) == [200, 409]
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT count(*) FROM app.dream_revisions")) == 2
                assert (
                    await c.scalar(text("SELECT count(*) FROM app.jobs WHERE status='queued'")) == 1
                )
                assert (
                    await c.scalar(text("SELECT count(*) FROM app.jobs WHERE status='cancelled'"))
                    == 1
                )

    run(check)


def test_listing_filters_literal_keywords_and_pagination(cluster):
    async def check():
        user, outsider = uuid4(), uuid4()
        async with session(cluster) as (client, _, _, _, _):
            await create(client, outsider, title="Outsider secret", dream_date="2026-10-31")
            ids = []
            for i, day in enumerate(["2026-10-29", "2026-10-28", "2026-10-29", "2026-10-27"]):
                response = await create(
                    client,
                    user,
                    title=f"House {i}",
                    dream_date=day,
                    text="100%_sure" if i == 0 else "Ocean and calm",
                    reported_emotions=["calm"] if i % 2 else ["curious"],
                )
                assert response.status_code == 201
                ids.append(response.json()["id"])
            cursor, seen = None, []
            while True:
                response = await client.get(
                    "/api/v1/dreams",
                    headers=token(user),
                    params={"limit": 2, **({"cursor": cursor} if cursor else {})},
                )
                assert response.status_code == 200
                page = response.json()
                seen.extend(item["id"] for item in page["items"])
                cursor = page["next_cursor"]
                if not cursor:
                    break
            assert len(seen) == len(set(seen)) == 4
            assert set(seen) == set(ids)
            expected_first = sorted([ids[0], ids[2]], reverse=True)
            assert seen[:2] == expected_first
            response = await client.get(
                "/api/v1/dreams",
                headers=token(user),
                params={
                    "from_date": "2026-10-28",
                    "to_date": "2026-10-29",
                    "emotion": "calm",
                },
            )
            assert [d["id"] for d in response.json()["items"]] == [ids[1]]
            response = await client.get(
                "/api/v1/dreams", headers=token(user), params={"keyword": "%_"}
            )
            assert [d["id"] for d in response.json()["items"]] == [ids[0]]
            for params in (
                {"cursor": "bad!"},
                {"limit": 101},
                {"emotion": "diagnosis"},
                {"from_date": "2026-10-29", "to_date": "2026-10-27"},
                {"sort": "DROP TABLE"},
            ):
                assert (
                    await client.get("/api/v1/dreams", headers=token(user), params=params)
                ).status_code == 422

    run(check)


def test_every_resource_route_hides_other_users(cluster):
    async def check():
        a, b = uuid4(), uuid4()
        async with session(cluster) as (client, _, _, _, _):
            dream = (await create(client, a)).json()
            path = f"/api/v1/dreams/{dream['id']}"
            for suffix in ("", "/analysis", f"/revisions/{dream['revision_id']}"):
                assert (await client.get(path + suffix, headers=token(b))).status_code == 404
            assert (
                await client.patch(
                    path,
                    headers=token(b),
                    json={
                        "expected_version": 1,
                        "title": "Stolen",
                    },
                )
            ).status_code == 404
            assert (await client.delete(path, headers=token(b))).status_code == 404
            assert (
                await client.post(
                    path + "/analyze",
                    headers={
                        **token(b),
                        "Idempotency-Key": "other-user",
                    },
                    json={"expected_version": 1},
                )
            ).status_code == 404
            assert (await client.get(path, headers=token(a))).status_code == 200

    run(check)


def test_quota_deferred_save_and_explicit_retry(cluster):
    async def check():
        user = uuid4()
        async with session(cluster, ai_daily_limit=1) as (client, db, _, _, _):
            one = (await create(client, user)).json()
            two_response = await create(client, user, text="I still need to save this dream.")
            assert two_response.status_code == 201
            two = two_response.json()
            async with db.tenant_transaction(user) as c:
                job = (
                    (
                        await c.execute(
                            select(tables["app.jobs"]).where(
                                tables["app.jobs"].c.id == UUID(two["analysis_job_id"]),
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                assert job["stage"] == "waiting_quota"
                assert job["input_versions"]["admission"] == "deferred"
                assert job["run_after"] > datetime.now(UTC)
                assert await c.scalar(text("SELECT used FROM app.ai_usage")) == 1
                await c.execute(
                    update(tables["app.jobs"])
                    .where(
                        tables["app.jobs"].c.id == UUID(one["analysis_job_id"]),
                    )
                    .values(status="failed", error_code="provider_unavailable")
                )
            response = await client.post(
                f"/api/v1/dreams/{one['id']}/analyze",
                headers={**token(user), "Idempotency-Key": "retry-failed"},
                json={"expected_version": 1},
            )
            assert response.status_code == 429
            assert (await client.get(f"/api/v1/dreams/{two['id']}", headers=token(user))).json()[
                "text"
            ] == two["text"]

    run(check)


def test_failed_write_rolls_back_dream_revision_key_quota_and_job(cluster, monkeypatch):
    async def check():
        user = uuid4()
        original = JournalService._queue

        async def fail(*args, **kwargs):
            raise RuntimeError("Simulated persistence failure")

        async with session(cluster) as (client, db, _, _, _):
            monkeypatch.setattr(JournalService, "_queue", fail)
            response = await create(client, user, "atomic-failure")
            assert response.status_code == 500
            async with db.tenant_transaction(user) as c:
                for name in ("dreams", "dream_revisions", "jobs", "idempotency_keys", "ai_usage"):
                    assert await c.scalar(text(f"SELECT count(*) FROM app.{name}")) == 0
            monkeypatch.setattr(JournalService, "_queue", original)
            assert (await create(client, user, "atomic-failure")).status_code == 201

    run(check)


def test_analyze_deduplicates_and_retries_failed_current_job(cluster):
    async def check():
        user = uuid4()
        async with session(cluster) as (client, db, _, _, _):
            dream = (await create(client, user)).json()
            path = f"/api/v1/dreams/{dream['id']}/analyze"
            headers = {**token(user), "Idempotency-Key": "analyze-once"}
            one = await client.post(path, headers=headers, json={"expected_version": 1})
            again = await client.post(path, headers=headers, json={"expected_version": 1})
            assert one.status_code == again.status_code == 202
            assert one.json()["job_id"] == again.json()["job_id"] == dream["analysis_job_id"]
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT used FROM app.ai_usage")) == 1
                await c.execute(
                    update(tables["app.jobs"])
                    .where(
                        tables["app.jobs"].c.id == UUID(dream["analysis_job_id"]),
                    )
                    .values(status="failed")
                )
            retry = await client.post(
                path,
                headers={**token(user), "Idempotency-Key": "new-retry"},
                json={"expected_version": 1},
            )
            assert retry.status_code == 202
            assert retry.json()["job_id"] == dream["analysis_job_id"]
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT used FROM app.ai_usage")) == 2
                assert await c.scalar(text("SELECT count(*) FROM app.jobs")) == 1

    run(check)


async def seed_derived(c, user, dream, other, settings):
    analysis_id, conversation_id, message_id = uuid4(), uuid4(), uuid4()
    await c.execute(
        insert(tables["app.analyses"]).values(
            id=analysis_id,
            user_id=user,
            dream_id=UUID(dream["id"]),
            revision_id=UUID(dream["revision_id"]),
            pipeline_version=pipeline_version(settings),
            memory_version=0,
            extraction={"place": "house"},
            reflections=["Old reflection"],
            state="ready",
        )
    )
    await c.execute(
        update(tables["app.dreams"])
        .where(
            tables["app.dreams"].c.id == UUID(dream["id"]),
        )
        .values(analysis_state="ready")
    )
    await c.execute(
        insert(tables["app.conversations"]).values(
            id=conversation_id, user_id=user, title="House questions"
        )
    )
    await c.execute(
        insert(tables["app.messages"]).values(
            id=message_id,
            user_id=user,
            conversation_id=conversation_id,
            role="assistant",
            body="Old historical answer",
            status="completed",
            request_key="derived-message",
        )
    )
    pair = sorted([UUID(dream["id"]), UUID(other["id"])])
    await c.execute(
        insert(tables["app.connection_decisions"]).values(
            user_id=user,
            dream_a_id=pair[0],
            dream_b_id=pair[1],
            relation_key="house",
            decision="confirmed",
        )
    )
    await c.execute(
        insert(tables["app.connections"]).values(
            user_id=user,
            dream_a_id=pair[0],
            dream_b_id=pair[1],
            relation_key="house",
            status="confirmed",
            observation="Old connection",
        )
    )


def test_edits_hide_derived_output_and_preserve_user_decisions(cluster):
    async def check():
        user = uuid4()
        async with session(cluster) as (client, db, _, settings, _):
            one = (await create(client, user)).json()
            two = (await create(client, user)).json()
            async with db.tenant_transaction(user) as c:
                await seed_derived(c, user, one, two, settings)
            path = f"/api/v1/dreams/{one['id']}"
            ready = await client.get(path + "/analysis", headers=token(user))
            assert ready.json()["status"] == "ready"
            changed = await client.patch(
                path,
                headers=token(user),
                json={
                    "expected_version": 1,
                    "text": "The forest has changed.",
                },
            )
            assert changed.status_code == 200
            hidden = (await client.get(path + "/analysis", headers=token(user))).json()
            assert hidden["status"] == "queued"
            assert hidden["reflections"] is None
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT state FROM app.analyses")) == "stale"
                assert await c.scalar(text("SELECT body FROM app.messages")) == ""
                assert await c.scalar(text("SELECT status FROM app.messages")) == "stale"
                assert await c.scalar(text("SELECT status FROM app.connections")) == "stale"
                assert (
                    await c.scalar(text("SELECT decision FROM app.connection_decisions"))
                    == "confirmed"
                )

    run(check)


def test_delete_removes_all_source_access_and_cancels_jobs(cluster):
    async def check():
        user = uuid4()
        async with session(cluster) as (client, db, _, settings, _):
            one = (await create(client, user, "deleted-create")).json()
            two = (await create(client, user)).json()
            async with db.tenant_transaction(user) as c:
                await seed_derived(c, user, two, one, settings)
            path = f"/api/v1/dreams/{one['id']}"
            assert (await client.delete(path, headers=token(user))).status_code == 204
            for suffix in ("", "/analysis", f"/revisions/{one['revision_id']}"):
                assert (await client.get(path + suffix, headers=token(user))).status_code == 404
            assert (await create(client, user, "deleted-create")).status_code == 404
            assert (
                await client.get(f"/api/v1/dreams/{two['id']}", headers=token(user))
            ).status_code == 200
            analysis = (
                await client.get(f"/api/v1/dreams/{two['id']}/analysis", headers=token(user))
            ).json()
            assert analysis["status"] == "stale"
            assert analysis["reflections"] is None
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT count(*) FROM app.dream_revisions")) == 1
                assert await c.scalar(text("SELECT data_epoch FROM app.accounts")) == 1
                assert (
                    await c.scalar(text("SELECT count(*) FROM app.jobs WHERE status='queued'")) == 0
                )

    run(check)


def test_invalid_capture_payloads_do_not_persist(cluster):
    async def check():
        user = uuid4()
        async with session(cluster) as (client, db, _, _, _):
            for invalid in (
                {"text": ""},
                {"text": "x" * 20001},
                {"title": "x" * 161},
                {"reported_emotions": ["diagnosis"]},
                {"user_id": str(uuid4())},
                {"reported_emotions": ["calm", "calm"]},
                {"input_mode": "voice"},
                {"text": "unsupported\x00character"},
            ):
                assert (await create(client, user, **invalid)).status_code == 422
            response = await client.post("/api/v1/dreams", json=body(), headers=token(user))
            assert response.status_code == 422
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT count(*) FROM app.dreams")) == 0

    run(check)


def test_title_and_date_edits_requeue_completed_job_without_new_revision(cluster):
    async def check():
        user = uuid4()
        async with session(cluster) as (client, db, _, settings, _):
            one = (await create(client, user)).json()
            two = (await create(client, user)).json()
            async with db.tenant_transaction(user) as c:
                await seed_derived(c, user, one, two, settings)
                await c.execute(
                    update(tables["app.jobs"])
                    .where(
                        tables["app.jobs"].c.id == UUID(one["analysis_job_id"]),
                    )
                    .values(status="succeeded")
                )
            path = f"/api/v1/dreams/{one['id']}"
            for version, changes in [
                (1, {"title": "The returning house"}),
                (2, {"dream_date": "2026-10-28"}),
            ]:
                changed = await client.patch(
                    path,
                    headers=token(user),
                    json={
                        "expected_version": version,
                        **changes,
                    },
                )
                assert changed.status_code == 200
                assert changed.json()["revision_id"] == one["revision_id"]
                assert changed.json()["version"] == version + 1
                assert changed.json()["analysis_state"] == "queued"
                assert changed.json()["analysis_job_id"] == one["analysis_job_id"]
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT count(*) FROM app.dream_revisions")) == 2
                job = (
                    (
                        await c.execute(
                            select(tables["app.jobs"]).where(
                                tables["app.jobs"].c.id == UUID(one["analysis_job_id"]),
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                assert job["status"] == "queued"
                assert job["input_versions"]["submission"] == 3
                assert job["input_versions"]["admission_key"]

    run(check)


def test_changed_pipeline_hides_cached_analysis_in_all_views(cluster):
    async def check():
        user = uuid4()
        async with session(cluster) as (client, db, _, settings, _):
            one = (await create(client, user)).json()
            two = (await create(client, user)).json()
            async with db.tenant_transaction(user) as c:
                await seed_derived(c, user, one, two, settings)
            settings.prompt_version = "next-prompt"
            path = f"/api/v1/dreams/{one['id']}"
            assert (await client.get(path, headers=token(user))).json()["analysis_state"] == "stale"
            analysis = (await client.get(path + "/analysis", headers=token(user))).json()
            assert analysis["status"] == "stale" and analysis["reflections"] is None
            page = (await client.get("/api/v1/dreams", headers=token(user))).json()
            entry = next(item for item in page["items"] if item["id"] == one["id"])
            assert entry["analysis_state"] == "stale"

    run(check)
