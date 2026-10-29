import json
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import insert, select, text, update
from test_phase2_database import cluster as cluster
from test_phase3_identity import run, session, token
from test_phase4_journal import create

import app.services.indexing as indexing_module
from app.db.models import tables
from app.providers.embeddings import Embeddings
from app.services.indexing import EmbeddingHandler
from app.workers.queue import Queue
from app.workers.runner import Worker

pytestmark = pytest.mark.integration
CONFIG = dict(
    nebius_api_key="test-only",
    nebius_embedding_model="test/embedding",
    embedding_document_prefix="passage: ",
    embedding_query_prefix="query: ",
    embedding_prefixes_verified=True,
)


@pytest.fixture(autouse=True)
def clear_jobs(cluster):
    yield
    with cluster["admin"].begin() as c:
        c.execute(
            update(tables["app.jobs"])
            .where(tables["app.jobs"].c.status.in_(["queued", "running"]))
            .values(status="cancelled", lease_token=None, leased_until=None)
        )


async def entry(client, db, user, text_value):
    result = await create(client, user, text=text_value)
    assert result.status_code == 201, result.text
    # Indexing can run independently of single-entry interpretation.
    async with db.tenant_transaction(user) as c:
        await c.execute(
            update(tables["app.jobs"])
            .where(tables["app.jobs"].c.kind == "analysis")
            .values(status="cancelled")
        )
    return result.json()


async def index(client, user, dream, key=None):
    return await client.post(
        f"/api/v1/dreams/{dream['id']}/index",
        headers=token(user) | {"Idempotency-Key": key or uuid4().hex},
        json={"expected_version": dream["version"]},
    )


def reply(request):
    payload = json.loads(request.content)
    values = []
    for index, value in enumerate(payload["input"]):
        lowered = value.casefold()
        topic = (
            0
            if any(w in lowered for w in ("house", "cabin", "home"))
            else 1
            if any(w in lowered for w in ("ocean", "sea", "wave"))
            else 2
        )
        vector = [0.02, 0.02, 0.02]
        vector[topic] = 1.0
        values.append({"object": "embedding", "index": index, "embedding": vector})
    return httpx.Response(
        200,
        json={
            "object": "list",
            "model": payload["model"],
            "data": values,
            "usage": {"prompt_tokens": 20, "total_tokens": 20},
        },
    )


async def process(db, settings, transport=reply):
    queue = Queue(settings)
    try:
        worker = Worker(
            settings,
            db,
            queue,
            {
                "embedding": EmbeddingHandler(
                    settings, Embeddings(settings, transport=httpx.MockTransport(transport))
                )
            },
        )
        while await worker.run_once():
            pass
    finally:
        await queue.close()


def test_exact_cosine_current_source_tenant_scope_and_no_identity_merge(cluster):
    async def check():
        user, other = uuid4(), uuid4()
        async with session(
            cluster,
            **CONFIG,
            queue_database_url=cluster["urls"]["queue"].render_as_string(hide_password=False),
        ) as (client, db, _, settings, _):
            current = await entry(client, db, user, "I entered the cedar house with a brass key.")
            recurring = await entry(client, db, user, "The cedar house had the familiar brass key.")
            unrelated = await entry(client, db, user, "A glass house stood on Mars.")
            distractors = [
                await entry(client, db, user, f"Ocean waves carried a boat number {i}.")
                for i in range(12)
            ]
            private = await entry(client, db, other, "The cedar house had a brass key. PRIVATE")
            for dream in [current, recurring, unrelated, *distractors]:
                assert (await index(client, user, dream)).status_code == 202
            assert (await index(client, other, private)).status_code == 202
            await process(db, settings)
            response = await client.get(
                f"/api/v1/dreams/{current['id']}/candidates", headers=token(user)
            )
            assert response.status_code == 200, response.text
            result = response.json()
            assert not result["degraded"] and result["indexed_dreams"] == 15
            ids = [candidate["dream_id"] for candidate in result["candidates"]]
            assert recurring["id"] in ids[:5] and len(ids) <= 12 and len(ids) == len(set(ids))
            assert private["id"] not in ids and current["id"] not in ids
            semantic = [item for item in result["candidates"] if item["semantic_rank"]]
            assert len(semantic) <= 8 and semantic[0]["cosine_similarity"] > 0.99
            assert all(item["revision_id"] and item["source_hash"] for item in result["candidates"])
            assert (
                await client.get(f"/api/v1/dreams/{private['id']}/candidates", headers=token(user))
            ).status_code == 404
            assert (await index(client, user, private)).status_code == 404
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT count(*) FROM app.connections")) == 0
                assert await c.scalar(text("SELECT count(*) FROM app.motifs")) == 0
                spaces = (await c.execute(select(tables["app.embedding_spaces"]))).mappings().all()
                assert len(spaces) == 1 and spaces[0]["state"] == "active"
                assert await c.scalar(text("SELECT count(*) FROM app.embedding_chunks")) == 15

    run(check)


def test_outage_returns_text_fallback_retains_job_and_recovers(cluster):
    async def check():
        user = uuid4()
        async with session(
            cluster,
            **CONFIG,
            queue_database_url=cluster["urls"]["queue"].render_as_string(hide_password=False),
        ) as (client, db, _, settings, _):
            primary = await entry(client, db, user, "The cedar house held a brass key.")
            match = await entry(client, db, user, "A brass key opened the cedar house.")
            for dream in (primary, match):
                await index(client, user, dream)
            await process(
                db, settings, lambda _: httpx.Response(503, json={"error": {"message": "PRIVATE"}})
            )
            result = (
                await client.get(f"/api/v1/dreams/{primary['id']}/candidates", headers=token(user))
            ).json()
            assert result["degraded"] and result["embedding_job_status"] == "queued"
            assert result["candidates"][0]["dream_id"] == match["id"]
            assert "PRIVATE" not in str(result)
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT count(*) FROM app.embedding_chunks")) == 0
                await c.execute(
                    update(tables["app.jobs"])
                    .where(tables["app.jobs"].c.kind == "embedding")
                    .values(run_after=text("clock_timestamp()"))
                )
            await process(db, settings)
            result = (
                await client.get(f"/api/v1/dreams/{primary['id']}/candidates", headers=token(user))
            ).json()
            assert not result["degraded"] and result["embedding_job_status"] == "succeeded"

    run(check)


def test_atomic_space_switch_requires_all_current_sources_and_new_version(cluster):
    async def check():
        user = uuid4()
        async with session(
            cluster,
            **CONFIG,
            queue_database_url=cluster["urls"]["queue"].render_as_string(hide_password=False),
        ) as (client, db, _, settings, _):
            one = await entry(client, db, user, "I walked into a house.")
            two = await entry(client, db, user, "I entered the cabin.")
            for dream in (one, two):
                await index(client, user, dream)
            await process(db, settings)
            settings.nebius_embedding_model = "test/new-model"
            conflict = await index(client, user, one)
            assert (
                conflict.status_code == 503
                and conflict.json()["error"]["code"] == "embedding_version_conflict"
            )
            settings.embedding_version = "v2"
            await index(client, user, one)
            await process(db, settings)
            partial = (
                await client.get(f"/api/v1/dreams/{one['id']}/candidates", headers=token(user))
            ).json()
            assert partial["active_embedding_version"] == "v1" and partial["degraded"]
            assert partial["candidates"][0]["semantic_rank"] == 1
            await index(client, user, two)
            await process(db, settings)
            complete = (
                await client.get(f"/api/v1/dreams/{one['id']}/candidates", headers=token(user))
            ).json()
            assert complete["active_embedding_version"] == "v2" and not complete["degraded"]
            async with db.tenant_transaction(user) as c:
                states = dict(
                    (
                        await c.execute(
                            select(
                                tables["app.embedding_spaces"].c.version,
                                tables["app.embedding_spaces"].c.state,
                            )
                        )
                    ).all()
                )
                assert states == {"v1": "retired", "v2": "active"}

    run(check)


def test_index_idempotency_wrong_version_and_dimension_failure(cluster):
    async def check():
        user = uuid4()
        async with session(
            cluster,
            **CONFIG,
            queue_database_url=cluster["urls"]["queue"].render_as_string(hide_password=False),
        ) as (client, db, _, settings, _):
            dream = await entry(client, db, user, "I found a house.")
            first = await index(client, user, dream, "same-index")
            replay = await index(client, user, dream, "same-index")
            assert first.json()["job_id"] == replay.json()["job_id"]
            assert (await index(client, user, dream | {"version": 999})).status_code == 409

            def bad_dimension(request):
                result = json.loads(reply(request).content)
                result["data"][0]["embedding"] = [1.0, 0.0]
                return httpx.Response(200, json=result)

            await process(db, settings, bad_dimension)
            job = (
                await client.get(f"/api/v1/jobs/{first.json()['job_id']}", headers=token(user))
            ).json()
            assert job["status"] == "failed" and job["error_code"] == "embedding_dimension_mismatch"
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT count(*) FROM app.embedding_chunks")) == 0
                assert (
                    await c.scalar(text("SELECT used FROM app.ai_usage")) == 2
                )  # analysis + explicit indexing
            retry = await index(client, user, dream)
            assert retry.json()["job_id"] == first.json()["job_id"]
            await process(db, settings)
            result = (
                await client.get(f"/api/v1/dreams/{dream['id']}/candidates", headers=token(user))
            ).json()
            assert not result["degraded"]

    run(check)


def test_stale_deleted_and_draft_sources_are_excluded(cluster):
    async def check():
        user = uuid4()
        async with session(
            cluster,
            **CONFIG,
            queue_database_url=cluster["urls"]["queue"].render_as_string(hide_password=False),
        ) as (client, db, _, settings, _):
            one = await entry(client, db, user, "A cedar house and brass key.")
            two = await entry(client, db, user, "The cedar house and brass key.")
            await index(client, user, one)
            await index(client, user, two)
            await process(db, settings)
            patched = await client.patch(
                f"/api/v1/dreams/{two['id']}",
                headers=token(user),
                json={"expected_version": 1, "text": "An ocean without any buildings."},
            )
            assert patched.status_code == 200
            result = (
                await client.get(f"/api/v1/dreams/{one['id']}/candidates", headers=token(user))
            ).json()
            assert result["degraded"] and all(
                item["dream_id"] != two["id"] for item in result["candidates"]
            )
            assert (
                await client.delete(f"/api/v1/dreams/{two['id']}", headers=token(user))
            ).status_code == 204
            draft = await create(
                client, user, lifecycle="draft", text="A cedar house and brass key."
            )
            result = (
                await client.get(f"/api/v1/dreams/{one['id']}/candidates", headers=token(user))
            ).json()
            assert not result["candidates"] and result["saved_dreams"] == 1
            assert (await index(client, user, draft.json())).status_code == 409

    run(check)


def test_confirmed_associations_and_specific_motifs_outrank_generic_text(cluster):
    async def check():
        user = uuid4()
        async with session(cluster) as (client, db, _, _, _):
            primary = await entry(
                client, db, user, "My grandmother left a brass key by the cedar door."
            )
            specific = await entry(client, db, user, "I returned to the quiet cottage.")
            generic = await entry(client, db, user, "A door, a key, and another door with a key.")
            async with db.tenant_transaction(user) as c:
                motif_id = uuid4()
                await c.execute(
                    insert(tables["app.motifs"]).values(
                        id=motif_id,
                        user_id=user,
                        type="place",
                        canonical_label="grandmother's cottage",
                        normalized_key="cottage-one",
                    )
                )
                for dream in (primary, specific):
                    await c.execute(
                        insert(tables["app.dream_motifs"]).values(
                            user_id=user,
                            dream_id=UUID(dream["id"]),
                            revision_id=UUID(dream["revision_id"]),
                            motif_id=motif_id,
                            origin="user",
                            confirmation_state="confirmed",
                        )
                    )
                association_id = await c.scalar(
                    insert(tables["app.associations"])
                    .values(
                        user_id=user,
                        motif_id=motif_id,
                        meaning="My grandmother's home",
                        status="confirmed",
                        enabled=True,
                    )
                    .returning(tables["app.associations"].c.id)
                )
            result = (
                await client.get(f"/api/v1/dreams/{primary['id']}/candidates", headers=token(user))
            ).json()
            assert result["degraded"]
            assert result["candidates"][0]["dream_id"] == specific["id"]
            assert str(association_id) in result["candidates"][0]["association_ids"]
            assert "confirmed_association" in result["candidates"][0]["reasons"]
            assert generic["id"] in [item["dream_id"] for item in result["candidates"]]

    run(check)


def test_chunk_insert_failure_rolls_back_vectors_activation_and_success(cluster, monkeypatch):
    async def check():
        user = uuid4()
        async with session(
            cluster,
            **CONFIG,
            queue_database_url=cluster["urls"]["queue"].render_as_string(hide_password=False),
        ) as (client, db, _, settings, _):
            dream = await entry(client, db, user, "A house.")
            await index(client, user, dream)
            original = indexing_module.activate_if_complete

            async def fail(*_):
                raise RuntimeError("simulated activation failure")

            monkeypatch.setattr(indexing_module, "activate_if_complete", fail)
            await process(db, settings)
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT count(*) FROM app.embedding_chunks")) == 0
                assert (
                    await c.scalar(
                        text("SELECT count(*) FROM app.embedding_spaces WHERE state='active'")
                    )
                    == 0
                )
                assert (
                    await c.scalar(
                        select(tables["app.jobs"].c.status).where(
                            tables["app.jobs"].c.kind == "embedding"
                        )
                    )
                    == "queued"
                )
            monkeypatch.setattr(indexing_module, "activate_if_complete", original)

    run(check)


def test_labeled_fixture_recall_at_five_benchmark(cluster):
    async def check():
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/retrieval_benchmark.json").read_text()
        )
        user, entries = uuid4(), {}
        async with session(
            cluster,
            **CONFIG,
            queue_database_url=cluster["urls"]["queue"].render_as_string(hide_password=False),
        ) as (client, db, _, settings, _):
            for name, content in fixture["documents"].items():
                entries[name] = await entry(client, db, user, content)
                assert (await index(client, user, entries[name])).status_code == 202
            await process(db, settings)
            recalls = []
            for query in fixture["queries"]:
                result = (
                    await client.get(
                        f"/api/v1/dreams/{entries[query['id']]['id']}/candidates",
                        headers=token(user),
                    )
                ).json()
                top = {item["dream_id"] for item in result["candidates"][:5]}
                expected = {entries[name]["id"] for name in query["relevant"]}
                recalls.append(len(top & expected) / len(expected))
                assert not result["degraded"]
            recall = sum(recalls) / len(recalls)
            print(
                json.dumps(
                    {
                        "fixture": "retrieval_benchmark.json",
                        "documents": len(entries),
                        "queries": len(recalls),
                        "recall_at_5": recall,
                        "per_query_recall": recalls,
                        "provider": "synthetic SDK transport",
                    }
                )
            )
            assert recall == 1.0

    run(check)


def test_new_dimensions_rebuild_without_mixed_space_comparisons(cluster):
    async def check():
        user = uuid4()
        async with session(
            cluster,
            **CONFIG,
            queue_database_url=cluster["urls"]["queue"].render_as_string(hide_password=False),
        ) as (client, db, _, settings, _):
            one = await entry(client, db, user, "The cedar house.")
            two = await entry(client, db, user, "The cedar house again.")
            for dream in (one, two):
                await index(client, user, dream)
            await process(db, settings)
            settings.embedding_version, settings.embedding_dimensions = "v2", 4
            db.dimensions = 4
            assert await db.probe() == "ok"

            def four_dimensions(request):
                result = json.loads(reply(request).content)
                for item in result["data"]:
                    item["embedding"].append(0.1)
                return httpx.Response(200, json=result)

            for dream in (one, two):
                await index(client, user, dream)
            await process(db, settings, four_dimensions)
            result = (
                await client.get(f"/api/v1/dreams/{one['id']}/candidates", headers=token(user))
            ).json()
            assert not result["degraded"] and result["active_embedding_version"] == "v2"
            assert result["candidates"][0]["cosine_similarity"] > 0.99
            async with db.tenant_transaction(user) as c:
                assert (
                    await c.execute(
                        text(
                            "SELECT DISTINCT dimensions FROM app.embedding_chunks "
                            "ORDER BY dimensions"
                        )
                    )
                ).scalars().all() == [3, 4]

    run(check)


def test_long_unicode_index_covers_every_codepoint_and_deduplicates_candidates(cluster):
    async def check():
        user = uuid4()
        async with session(
            cluster,
            **CONFIG,
            embedding_input_token_budget=512,
            queue_database_url=cluster["urls"]["queue"].render_as_string(hide_password=False),
        ) as (client, db, _, settings, _):
            one = await entry(client, db, user, "A cedar house 🌙 خواب\n" * 100)
            two = await entry(client, db, user, "The cedar house 🌙 خواب\n" * 100)
            for dream in (one, two):
                await index(client, user, dream)
            await process(db, settings)
            result = (
                await client.get(f"/api/v1/dreams/{one['id']}/candidates", headers=token(user))
            ).json()
            assert not result["degraded"] and len(result["candidates"]) == 1
            async with db.tenant_transaction(user) as c:
                chunks = (
                    (
                        await c.execute(
                            select(tables["app.embedding_chunks"])
                            .where(tables["app.embedding_chunks"].c.dream_id == UUID(one["id"]))
                            .order_by(tables["app.embedding_chunks"].c.chunk_index)
                        )
                    )
                    .mappings()
                    .all()
                )
                assert len(chunks) > 1
                assert chunks[0]["start"] == 0 and chunks[-1]["end"] == len(one["text"])
                assert all(a["end"] == b["start"] for a, b in zip(chunks, chunks[1:], strict=False))

    run(check)


def test_analysis_automatically_queues_index_without_second_quota_charge(cluster):
    from test_phase6_inference import MODEL
    from test_phase6_publication import reply as analysis_reply

    from app.providers.nebius import Nebius
    from app.workers.handlers import AnalysisHandler

    async def check():
        user = uuid4()
        async with session(
            cluster,
            **CONFIG,
            nebius_extraction_model=MODEL,
            queue_database_url=cluster["urls"]["queue"].render_as_string(hide_password=False),
        ) as (client, db, _, settings, _):
            dream = (await create(client, user)).json()
            queue = Queue(settings)
            try:
                worker = Worker(
                    settings,
                    db,
                    queue,
                    {
                        "analysis": AnalysisHandler(
                            settings,
                            Nebius(settings, transport=httpx.MockTransport(analysis_reply)),
                        ),
                        "embedding": EmbeddingHandler(
                            settings, Embeddings(settings, transport=httpx.MockTransport(reply))
                        ),
                    },
                )
                assert await worker.run_once()
                assert await worker.run_once()
                assert not await worker.run_once()
            finally:
                await queue.close()
            analysis = (
                await client.get(f"/api/v1/dreams/{dream['id']}/analysis", headers=token(user))
            ).json()
            assert analysis["status"] == "ready"
            candidates = (
                await client.get(f"/api/v1/dreams/{dream['id']}/candidates", headers=token(user))
            ).json()
            assert not candidates["degraded"]
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT used FROM app.ai_usage")) == 1

    run(check)


def test_source_edit_during_embedding_cancels_late_publication(cluster):
    async def check():
        user = uuid4()
        async with session(
            cluster,
            **CONFIG,
            queue_database_url=cluster["urls"]["queue"].render_as_string(hide_password=False),
        ) as (client, db, _, settings, _):
            dream = await entry(client, db, user, "A cedar house.")
            await index(client, user, dream)
            changed = False

            async def delayed_reply(request):
                nonlocal changed
                if not changed:
                    changed = True
                    patched = await client.patch(
                        f"/api/v1/dreams/{dream['id']}",
                        headers=token(user),
                        json={"expected_version": 1, "text": "A new ocean."},
                    )
                    assert patched.status_code == 200
                return reply(request)

            await process(db, settings, delayed_reply)
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT count(*) FROM app.embedding_chunks")) == 0
                assert (
                    await c.scalar(
                        select(tables["app.jobs"].c.status).where(
                            tables["app.jobs"].c.kind == "embedding"
                        )
                    )
                    == "cancelled"
                )

    run(check)
