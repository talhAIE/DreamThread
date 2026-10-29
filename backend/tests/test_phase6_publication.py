import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text, update
from test_phase2_database import cluster as cluster
from test_phase3_identity import run, session, token
from test_phase4_journal import create
from test_phase6_inference import MODEL, completion, reflection

import app.workers.store as store_module
from app.db.models import tables
from app.providers.nebius import Nebius
from app.workers.handlers import AnalysisHandler
from app.workers.queue import Queue
from app.workers.runner import Worker

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def clear_pending_jobs(cluster):
    yield
    with cluster["admin"].begin() as c:
        c.execute(
            update(tables["app.jobs"])
            .where(
                tables["app.jobs"].c.status.in_(["queued", "running"]),
            )
            .values(status="cancelled", lease_token=None, leased_until=None)
        )


def reply(request):
    payload = json.loads(request.content)
    if "literal observation extractor" in payload["messages"][0]["content"]:
        data = json.loads(payload["messages"][1]["content"])["untrusted_data"]
        start = data["text"].index("house")
        return completion(
            {
                "observations": [
                    {
                        "key": "o1",
                        "type": "place",
                        "label": "house",
                        "citation": {
                            "dream_id": data["dream_id"],
                            "revision_id": data["revision_id"],
                            "source_hash": data["source_hash"],
                            "start": start,
                            "end": start + 5,
                            "quote": "house",
                        },
                    }
                ]
            }
        )
    return completion(reflection())


def test_real_sdk_worker_publishes_analysis_evidence_and_metadata_atomically(cluster):
    async def check():
        user, calls = uuid4(), []
        async with session(
            cluster,
            nebius_api_key="test-key",
            nebius_extraction_model=MODEL,
            queue_database_url=cluster["urls"]["queue"].render_as_string(hide_password=False),
        ) as (client, db, _, settings, _):
            dream = (await create(client, user, reported_emotions=["curious"])).json()

            def transport(request):
                calls.append(request)
                return reply(request)

            queue = Queue(settings)
            try:
                worker = Worker(
                    settings,
                    db,
                    queue,
                    {
                        "analysis": AnalysisHandler(
                            settings, Nebius(settings, transport=httpx.MockTransport(transport))
                        )
                    },
                )
                assert await worker.run_once()
                assert not await worker.run_once()
                result = (
                    await client.get(f"/api/v1/dreams/{dream['id']}/analysis", headers=token(user))
                ).json()
                assert result["status"] == "ready"
                assert result["extraction"]["reported_emotions"] == ["curious"]
                evidence = result["evidence"]
                assert len(evidence) == 1 and evidence[0]["quote"] == "house"
                assert result["reflections"][0]["evidence_ids"] == [evidence[0]["id"]]
                source = (
                    await client.get(f"/api/v1/dreams/{dream['id']}", headers=token(user))
                ).json()
                assert source["text"][evidence[0]["start"] : evidence[0]["end"]] == "house"
                assert source["reported_emotions"] == ["curious"]
                async with db.tenant_transaction(user) as c:
                    assert await c.scalar(text("SELECT count(*) FROM app.analysis_evidence")) == 1
                    runs = (await c.execute(select(tables["app.model_runs"]))).mappings().all()
                    assert len(runs) == 2 and all(r["status"] == "validated" for r in runs)
                    assert all(r["model_id"] == MODEL and r["input_tokens"] == 80 for r in runs)
                    assert "SECRET_REASONING" not in str(runs)
                    assert await c.scalar(select(tables["app.jobs"].c.status)) == "succeeded"
                assert len(calls) == 2
            finally:
                await queue.close()

    run(check)


def test_fabricated_other_source_fails_without_publishing_or_leaking_raw_output(cluster):
    async def check():
        user, calls = uuid4(), []
        async with session(
            cluster,
            nebius_api_key="test-key",
            nebius_extraction_model=MODEL,
            queue_database_url=cluster["urls"]["queue"].render_as_string(hide_password=False),
        ) as (client, db, _, settings, _):
            dream = (await create(client, user)).json()

            def transport(request):
                calls.append(request)
                response = reply(request)
                data = json.loads(response.content)
                generated = json.loads(data["choices"][0]["message"]["content"])
                generated["observations"][0]["citation"]["dream_id"] = str(uuid4())
                return completion(generated)

            queue = Queue(settings)
            try:
                await Worker(
                    settings,
                    db,
                    queue,
                    {
                        "analysis": AnalysisHandler(
                            settings, Nebius(settings, transport=httpx.MockTransport(transport))
                        )
                    },
                ).run_once()
                job = (
                    await client.get(
                        f"/api/v1/jobs/{dream['analysis_job_id']}", headers=token(user)
                    )
                ).json()
                assert job["status"] == "failed" and job["error_code"] == "invalid_output"
                async with db.tenant_transaction(user) as c:
                    for name in ("analyses", "evidence", "analysis_evidence"):
                        assert await c.scalar(text(f"SELECT count(*) FROM app.{name}")) == 0
                assert len(calls) == 2
            finally:
                await queue.close()

    run(check)


def test_failed_link_insert_rolls_back_publication_and_reuses_verified_checkpoints(
    cluster, monkeypatch
):
    async def check():
        user, calls = uuid4(), []
        async with session(
            cluster,
            nebius_api_key="test-key",
            nebius_extraction_model=MODEL,
            queue_database_url=cluster["urls"]["queue"].render_as_string(hide_password=False),
        ) as (client, db, _, settings, _):
            await create(client, user)

            def transport(request):
                calls.append(request)
                return reply(request)

            queue = Queue(settings)
            original = store_module.insert

            def fail_link(table):
                if table.name == "analysis_evidence":
                    raise RuntimeError("simulated evidence-link persistence failure")
                return original(table)

            try:
                worker = Worker(
                    settings,
                    db,
                    queue,
                    {
                        "analysis": AnalysisHandler(
                            settings, Nebius(settings, transport=httpx.MockTransport(transport))
                        )
                    },
                )
                monkeypatch.setattr(store_module, "insert", fail_link)
                await worker.run_once()
                async with db.tenant_transaction(user) as c:
                    for name in ("analyses", "evidence", "analysis_evidence"):
                        assert await c.scalar(text(f"SELECT count(*) FROM app.{name}")) == 0
                    assert await c.scalar(select(tables["app.jobs"].c.status)) == "queued"
                    await c.execute(
                        update(tables["app.jobs"]).values(run_after=text("clock_timestamp()"))
                    )
                monkeypatch.setattr(store_module, "insert", original)
                await worker.run_once()
                async with db.tenant_transaction(user) as c:
                    assert await c.scalar(text("SELECT count(*) FROM app.analyses")) == 1
                    assert await c.scalar(text("SELECT count(*) FROM app.evidence")) == 1
                    assert await c.scalar(select(tables["app.jobs"].c.status)) == "succeeded"
                assert (
                    len(calls) == 2
                )  # retry loads both validated stage artifacts, not the provider
            finally:
                monkeypatch.setattr(store_module, "insert", original)
                await queue.close()

    run(check)
