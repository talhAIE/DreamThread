import asyncio
import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, text, update
from test_phase2_database import cluster as cluster
from test_phase3_identity import run, session, token
from test_phase4_journal import create
from test_phase6_inference import MODEL, completion, reflection
from test_phase6_publication import reply as base_reply

from app.db.models import tables
from app.providers.nebius import Nebius
from app.services.journal import JournalService
from app.services.memory import propose_association
from app.workers.handlers import AnalysisHandler
from app.workers.queue import Queue
from app.workers.runner import Worker

CONFIG = {"nebius_api_key": "test-only", "nebius_extraction_model": MODEL}
pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def clear_jobs(cluster):
    yield
    with cluster["admin"].begin() as c:
        c.execute(
            update(tables["app.jobs"])
            .where(tables["app.jobs"].c.status.in_(["queued", "running"]))
            .values(status="cancelled", lease_token=None, leased_until=None)
        )


def queue_url(cluster):
    return cluster["urls"]["queue"].render_as_string(hide_password=False)


async def work(db, settings, calls=None, transport=None):
    def reply(request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])["untrusted_data"]
        if calls is not None:
            calls.append((payload, data))
        if "literal observation extractor" in payload["messages"][0]["content"]:
            return base_reply(request)
        key = data["allowed_observation_keys"][0]
        output = reflection(key)
        if data["personal_associations"]:
            output["possible_reflections"][0]["text"] = (
                "You could reflect on your stated association with grandmother's home."
            )
        return completion(output)

    queue = Queue(settings)
    try:
        worker = Worker(
            settings,
            db,
            queue,
            {
                "analysis": AnalysisHandler(
                    settings, Nebius(settings, transport=httpx.MockTransport(transport or reply))
                )
            },
        )
        while await worker.run_once():
            pass
    finally:
        await queue.close()


async def ready(client, db, settings, user):
    dream = (await create(client, user, reported_emotions=["curious"])).json()
    await work(db, settings)
    result = (
        await client.get(f"/api/v1/dreams/{dream['id']}/analysis", headers=token(user))
    ).json()
    assert result["status"] == "ready", result
    return dream, result


async def remember(client, user, dream=None, motif_id=None, key=None, **changes):
    payload = {"meaning": "grandmother's home", **changes}
    if motif_id:
        payload["motif_id"] = motif_id
    else:
        payload["motif"] = {"type": "place", "label": "house"}
    if dream:
        payload["source_dream_id"] = dream["id"]
    return await client.post(
        "/api/v1/memory",
        json=payload,
        headers=token(user) | {"Idempotency-Key": key or uuid4().hex},
    )


def reflection_inputs(calls):
    return [data for _, data in calls if "personal_associations" in data]


def test_confirmed_association_affects_next_analysis_and_preserves_original_source(cluster):
    async def check():
        user, calls = uuid4(), []
        async with session(cluster, **CONFIG, queue_database_url=queue_url(cluster)) as (
            client,
            db,
            _,
            settings,
            _,
        ):
            dream, analysis = await ready(client, db, settings, user)
            motif = analysis["extraction"]["observations"][0]["motif_id"]
            response = await remember(client, user, dream, motif, key="remember-once")
            assert response.status_code == 201, response.text
            assert response.json()["origin"] == "user" and response.json()["status"] == "confirmed"
            replay = await remember(client, user, dream, motif, key="remember-once")
            assert replay.json()["id"] == response.json()["id"]
            await work(db, settings, calls)
            data = reflection_inputs(calls)[0]
            assert data["personal_associations"][0]["meaning"] == "grandmother's home"
            assert data["personal_associations"][0]["user_confirmed"] is True
            result = (
                await client.get(f"/api/v1/dreams/{dream['id']}/analysis", headers=token(user))
            ).json()
            assert "grandmother's home" in result["reflections"][0]["text"]
            source = (await client.get(f"/api/v1/dreams/{dream['id']}", headers=token(user))).json()
            assert source["text"] == dream["text"] and source["reported_emotions"] == ["curious"]
            memories = (await client.get("/api/v1/memory", headers=token(user))).json()
            assert memories["memory_version"] == 1 and memories["archive_version"] == 2
            assert len(memories["items"]) == 1

    run(check)


def test_suggestions_disabled_and_deleted_memory_never_enter_future_inputs(cluster):
    async def check():
        user = uuid4()
        async with session(cluster, **CONFIG, queue_database_url=queue_url(cluster)) as (
            client,
            db,
            _,
            settings,
            _,
        ):
            dream, analysis = await ready(client, db, settings, user)
            motif = analysis["extraction"]["observations"][0]["motif_id"]
            memory = (await remember(client, user, dream, motif, status="suggested")).json()
            calls = []
            await work(db, settings, calls)
            assert reflection_inputs(calls)[0]["personal_associations"] == []
            confirmed = await client.patch(
                f"/api/v1/memory/{memory['id']}",
                headers=token(user),
                json={"expected_version": 1, "status": "confirmed"},
            )
            assert confirmed.status_code == 200
            calls.clear()
            await work(db, settings, calls)
            assert len(reflection_inputs(calls)[0]["personal_associations"]) == 1
            disabled = await client.patch(
                f"/api/v1/memory/{memory['id']}",
                headers=token(user),
                json={"expected_version": 2, "enabled": False},
            )
            assert disabled.json()["version"] == 3
            calls.clear()
            await work(db, settings, calls)
            assert reflection_inputs(calls)[0]["personal_associations"] == []
            deleted = await client.delete(
                f"/api/v1/memory/{memory['id']}?expected_version=3", headers=token(user)
            )
            assert deleted.status_code == 204
            calls.clear()
            await work(db, settings, calls)
            assert reflection_inputs(calls)[0]["personal_associations"] == []
            assert (await client.get("/api/v1/memory", headers=token(user))).json()["items"] == []
            async with db.tenant_transaction(user) as c:
                row = (await c.execute(select(tables["app.associations"]))).mappings().one()
                assert row["meaning"] == "" and row["deleted_at"] and row["proposal_hash"]
                assert (
                    await c.scalar(
                        text(
                            "SELECT count(*) FROM app.analyses "
                            "WHERE reflections::text LIKE '%grandmother%'"
                        )
                    )
                    == 0
                )

    run(check)


def test_same_labels_do_not_merge_until_user_explicitly_links_identity(cluster):
    async def check():
        user = uuid4()
        async with session(cluster, **CONFIG, queue_database_url=queue_url(cluster)) as (
            client,
            db,
            _,
            settings,
            _,
        ):
            first, first_analysis = await ready(client, db, settings, user)
            second, second_analysis = await ready(client, db, settings, user)
            one = first_analysis["extraction"]["observations"][0]
            two = second_analysis["extraction"]["observations"][0]
            assert one["label"] == two["label"] and one["motif_id"] != two["motif_id"]
            assert one["confirmation_state"] == two["confirmation_state"] == "suggested"
            await remember(client, user, first, one["motif_id"])
            calls = []
            await work(db, settings, calls)
            contexts = reflection_inputs(calls)
            assert sorted(len(row["personal_associations"]) for row in contexts) == [0, 1]
            linked = await client.patch(
                f"/api/v1/dreams/{second['id']}/details",
                headers=token(user) | {"Idempotency-Key": "link-identity"},
                json={
                    "expected_version": 1,
                    "detail_key": two["detail_key"],
                    "motif_id": one["motif_id"],
                },
            )
            assert linked.status_code == 200, linked.text
            calls.clear()
            await work(db, settings, calls)
            assert all(len(row["personal_associations"]) == 1 for row in reflection_inputs(calls))
            result = (
                await client.get(f"/api/v1/dreams/{second['id']}/analysis", headers=token(user))
            ).json()
            assert result["extraction"]["observations"][0]["motif_id"] == one["motif_id"]
            motifs = (await client.get("/api/v1/motifs", headers=token(user))).json()["items"]
            assert next(row for row in motifs if row["id"] == one["motif_id"])["identity_confirmed"]

    run(check)


def test_foreign_ids_missing_ids_pagination_and_optimistic_memory_updates(cluster):
    async def check():
        user, other = uuid4(), uuid4()
        async with session(cluster) as (client, db, _, _, _):
            own = (await remember(client, user)).json()
            foreign = (await remember(client, other)).json()
            assert (await remember(client, user, motif_id=foreign["motif_id"])).status_code == 404
            assert (
                await client.patch(
                    f"/api/v1/memory/{foreign['id']}",
                    headers=token(user),
                    json={"expected_version": 1, "enabled": False},
                )
            ).status_code == 404
            assert (
                await client.delete(
                    f"/api/v1/memory/{foreign['id']}?expected_version=1", headers=token(user)
                )
            ).status_code == 404
            source = (await create(client, other)).json()
            assert (await remember(client, user, source, own["motif_id"])).status_code == 404
            own2 = (await remember(client, user, meaning="A different meaning")).json()
            assert own["motif_id"] != own2["motif_id"]
            page1 = (await client.get("/api/v1/memory?limit=1", headers=token(user))).json()
            page2 = (
                await client.get(
                    f"/api/v1/memory?limit=1&after={page1['next_after']}", headers=token(user)
                )
            ).json()
            assert {page1["items"][0]["id"], page2["items"][0]["id"]} == {own["id"], own2["id"]}
            replies = await asyncio.gather(
                *[
                    client.patch(
                        f"/api/v1/memory/{own['id']}",
                        headers=token(user),
                        json={"expected_version": 1, "meaning": meaning},
                    )
                    for meaning in ("one", "two")
                ]
            )
            assert sorted(row.status_code for row in replies) == [200, 409]
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(select(tables["app.accounts"].c.memory_version)) == 3

    run(check)


def test_rejected_and_deleted_model_proposals_are_suppressed(cluster):
    async def check():
        user = uuid4()
        async with session(cluster) as (client, db, _, _, _):
            memory = (await remember(client, user)).json()
            async with db.tenant_transaction(user) as c:
                proposal = await propose_association(
                    c, user, UUID(memory["motif_id"]), "A model suggestion"
                )
            listed = (
                await client.get("/api/v1/memory?status=suggested", headers=token(user))
            ).json()["items"]
            assert len(listed) == 1 and listed[0]["origin"] == "model"
            rejected = await client.patch(
                f"/api/v1/memory/{proposal}",
                headers=token(user),
                json={"expected_version": 1, "status": "rejected"},
            )
            assert rejected.status_code == 200
            async with db.tenant_transaction(user) as c:
                assert (
                    await propose_association(
                        c, user, UUID(memory["motif_id"]), " a MODEL suggestion "
                    )
                    is None
                )
            assert (
                await client.delete(
                    f"/api/v1/memory/{proposal}?expected_version=2", headers=token(user)
                )
            ).status_code == 204
            async with db.tenant_transaction(user) as c:
                assert (
                    await propose_association(
                        c, user, UUID(memory["motif_id"]), "A model suggestion"
                    )
                    is None
                )
                assert (
                    await c.scalar(
                        select(tables["app.associations"].c.meaning).where(
                            tables["app.associations"].c.id == proposal
                        )
                    )
                    == ""
                )

    run(check)


def test_quota_exhaustion_saves_memory_and_defers_rebuilds(cluster):
    async def check():
        user = uuid4()
        async with session(cluster, ai_daily_limit=1) as (client, db, _, _, _):
            dream = (await create(client, user)).json()
            memory = await remember(client, user, dream)
            assert memory.status_code == 201
            async with db.tenant_transaction(user) as c:
                jobs = (await c.execute(select(tables["app.jobs"]))).mappings().all()
                assert sorted(row["status"] for row in jobs) == ["cancelled", "queued"]
                assert (
                    next(row for row in jobs if row["status"] == "queued")["stage"]
                    == "waiting_quota"
                )
                assert await c.scalar(text("SELECT used FROM app.ai_usage")) == 1

    run(check)


def test_failed_rebuild_enqueue_rolls_back_memory_versions_and_invalidation(cluster, monkeypatch):
    async def check():
        user = uuid4()
        async with session(cluster) as (client, db, _, _, _):
            await create(client, user)
            original = JournalService._queue

            async def fail(*args, **kwargs):
                raise RuntimeError("simulated queue failure")

            monkeypatch.setattr(JournalService, "_queue", fail)
            result = await remember(client, user, key="rollback-memory")
            assert result.status_code == 500
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT count(*) FROM app.associations")) == 0
                assert await c.scalar(text("SELECT count(*) FROM app.motifs")) == 0
                assert await c.scalar(select(tables["app.accounts"].c.memory_version)) == 0
                assert await c.scalar(select(tables["app.jobs"].c.status)) == "queued"
            monkeypatch.setattr(JournalService, "_queue", original)
            assert (await remember(client, user, key="rollback-memory")).status_code == 201

    run(check)


def test_model_proposal_requires_confirmation_and_confirming_links_only_its_current_source(cluster):
    async def check():
        user = uuid4()
        async with session(cluster, **CONFIG, queue_database_url=queue_url(cluster)) as (
            client,
            db,
            _,
            settings,
            _,
        ):
            dream, analysis = await ready(client, db, settings, user)
            motif = UUID(analysis["extraction"]["observations"][0]["motif_id"])
            async with db.tenant_transaction(user) as c:
                proposal = await propose_association(
                    c, user, motif, "grandmother's home", UUID(dream["id"])
                )
            # The proposal did not alter the source's automatic mention confirmation.
            async with db.tenant_transaction(user) as c:
                assert (
                    await c.scalar(select(tables["app.dream_motifs"].c.confirmation_state))
                    == "suggested"
                )
            confirmed = await client.patch(
                f"/api/v1/memory/{proposal}",
                headers=token(user),
                json={"expected_version": 1, "status": "confirmed"},
            )
            assert confirmed.status_code == 200
            calls = []
            await work(db, settings, calls)
            context = reflection_inputs(calls)[0]
            assert context["personal_associations"][0]["id"] == str(proposal)
            assert context["personal_associations"][0]["origin"] == "model"
            assert context["personal_associations"][0]["user_confirmed"]

    run(check)
