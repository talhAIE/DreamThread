import asyncio
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from test_phase2_database import cluster as cluster
from test_phase3_identity import run, session, token
from test_phase4_journal import create

from app.db.models import tables
from app.workers.queue import Queue
from app.workers.runner import Worker
from app.workers.types import AnalysisPayload, LeaseLost, Publication, SourceChanged, WorkFailure

pytestmark = pytest.mark.integration
J, ART = tables["app.jobs"], tables["app.job_artifacts"]


@pytest.fixture(autouse=True)
def clear_pending_fixture_jobs(cluster):
    yield
    with cluster["admin"].begin() as c:
        c.execute(
            update(J)
            .where(J.c.status.in_(["queued", "running"]))
            .values(
                status="cancelled",
                lease_token=None,
                leased_until=None,
            )
        )


class FixtureHandler:
    def __init__(self):
        self.calls = 0

    async def run(self, execution):
        self.calls += 1
        await execution.set_stage("extracting")
        await execution.checkpoint("fixture", {"synthetic_test": True})
        return Publication(AnalysisPayload(extraction={"synthetic_test": True}))


@asynccontextmanager
async def worker_session(cluster, handler=None, **settings):
    url = cluster["urls"]["queue"].render_as_string(hide_password=False)
    async with session(cluster, queue_database_url=url, **settings) as values:
        client, db, _, config, _ = values
        queue = Queue(config)
        try:
            await queue.check_role()
            worker = Worker(config, db, queue, {"analysis": handler} if handler else None)
            yield client, db, config, queue, worker
        finally:
            await queue.close()


async def expire(db, user, job_id):
    async with db.tenant_transaction(user) as c:
        await c.execute(
            update(J)
            .where(J.c.id == UUID(str(job_id)))
            .values(
                leased_until=text("clock_timestamp() - interval '1 second'"),
            )
        )


def test_default_handler_reports_unconfigured_inference_without_fake_output(cluster):
    async def check():
        user = uuid4()
        async with worker_session(cluster) as (client, _, _, _, worker):
            dream = (await create(client, user)).json()
            assert await worker.run_once()
            job = (
                await client.get(f"/api/v1/jobs/{dream['analysis_job_id']}", headers=token(user))
            ).json()
            assert job["status"] == "failed"
            assert job["error_code"] == "provider_not_configured"
            assert job["checkpoint_stages"] == ["prepared"]
            assert job["retry_allowed"]
            assert "payload" not in job and "lease_token" not in job
            analysis = (
                await client.get(f"/api/v1/dreams/{dream['id']}/analysis", headers=token(user))
            ).json()
            assert analysis["status"] == "failed" and analysis["reflections"] is None

    run(check)


def test_two_workers_publish_once_and_queue_cannot_read_checkpoints(cluster):
    async def check():
        user = uuid4()
        handler = FixtureHandler()
        async with worker_session(cluster, handler) as (client, db, config, queue, first):
            dream = (await create(client, user)).json()
            second_queue = Queue(config)
            try:
                second = Worker(config, db, second_queue, {"analysis": handler})
                outcomes = await asyncio.gather(first.run_once(), second.run_once())
                assert sorted(outcomes) == [False, True]
                assert handler.calls == 1
                async with db.tenant_transaction(user) as c:
                    assert await c.scalar(text("SELECT count(*) FROM app.analyses")) == 1
                job = (
                    await client.get(
                        f"/api/v1/jobs/{dream['analysis_job_id']}", headers=token(user)
                    )
                ).json()
                assert job["status"] == "succeeded" and job["attempts"] == 1
                with pytest.raises(DBAPIError):
                    async with queue.engine.begin() as c:
                        await c.execute(select(ART))
            finally:
                await second_queue.close()

    run(check)


def test_checkpoint_survives_retry_and_skips_completed_stage(cluster):
    class ResumeHandler:
        extraction_calls = 0

        async def run(self, execution):
            cached = await execution.load_checkpoint("extraction")
            if cached is None:
                self.extraction_calls += 1
                await execution.checkpoint("extraction", {"synthetic_test": "checkpoint"})
                raise WorkFailure("provider_unavailable", retryable=True, retry_after=5)
            return Publication(AnalysisPayload(extraction=cached))

    async def check():
        user, handler = uuid4(), ResumeHandler()
        async with worker_session(cluster, handler) as (client, db, _, _, worker):
            dream = (await create(client, user)).json()
            await worker.run_once()
            job = (
                await client.get(f"/api/v1/jobs/{dream['analysis_job_id']}", headers=token(user))
            ).json()
            assert job["status"] == "queued" and job["error_code"] == "provider_unavailable"
            assert job["checkpoint_stages"] == ["extraction"]
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(
                    select(J.c.run_after - text("clock_timestamp()")).where(
                        J.c.id == UUID(dream["analysis_job_id"]),
                    )
                ) > timedelta(seconds=4)
                await c.execute(update(J).values(run_after=text("clock_timestamp()")))
            await worker.run_once()
            assert handler.extraction_calls == 1
            job = (
                await client.get(f"/api/v1/jobs/{dream['analysis_job_id']}", headers=token(user))
            ).json()
            assert job["status"] == "succeeded" and job["attempts"] == 2

    run(check)


def test_heartbeat_retains_lease_and_handler_holds_no_database_lock(cluster):
    class SlowHandler(FixtureHandler):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()

        async def run(self, execution):
            self.started.set()
            await asyncio.sleep(0.7)
            return await super().run(execution)

    async def check():
        user, handler = uuid4(), SlowHandler()
        async with worker_session(
            cluster,
            handler,
            job_lease_seconds=0.3,
            job_heartbeat_seconds=0.04,
            job_stage_timeout_seconds=3,
            job_timeout_seconds=5,
        ) as (
            client,
            db,
            config,
            _,
            worker,
        ):
            dream = (await create(client, user)).json()
            processing = asyncio.create_task(worker.run_once())
            await asyncio.wait_for(handler.started.wait(), 2)
            response = await asyncio.wait_for(
                client.get(f"/api/v1/dreams/{dream['id']}", headers=token(user)), 1
            )
            assert response.status_code == 200
            await asyncio.sleep(0.4)
            second = Queue(config)
            try:
                assert await second.claim() is None
            finally:
                await second.close()
            await processing
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(select(J.c.attempts)) == 1

    run(check)


def test_expired_worker_cannot_heartbeat_or_publish_after_reclaim(cluster):
    async def check():
        user = uuid4()
        async with worker_session(cluster, FixtureHandler()) as (client, db, _, queue, worker):
            dream = (await create(client, user)).json()
            old = await queue.claim()
            execution = await worker.store.prepare(old)
            await expire(db, user, old["id"])
            replacement = await queue.claim()
            assert replacement["lease_token"] != old["lease_token"]
            assert not await queue.heartbeat(old)
            with pytest.raises(LeaseLost):
                await worker.store.complete(execution, Publication(AnalysisPayload()))
            await worker.process(replacement)
            job = (
                await client.get(f"/api/v1/jobs/{dream['analysis_job_id']}", headers=token(user))
            ).json()
            assert job["status"] == "succeeded" and job["attempts"] == 2
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT count(*) FROM app.analyses")) == 1

    run(check)


def test_delete_during_processing_prevents_publication(cluster):
    class BlockedHandler(FixtureHandler):
        def __init__(self):
            super().__init__()
            self.started, self.release = asyncio.Event(), asyncio.Event()

        async def run(self, execution):
            self.started.set()
            await self.release.wait()
            return Publication(AnalysisPayload(extraction={"must_not_return": True}))

    async def check():
        user, handler = uuid4(), BlockedHandler()
        async with worker_session(cluster, handler) as (client, db, _, _, worker):
            dream = (await create(client, user)).json()
            processing = asyncio.create_task(worker.run_once())
            await asyncio.wait_for(handler.started.wait(), 2)
            assert (
                await client.delete(f"/api/v1/dreams/{dream['id']}", headers=token(user))
            ).status_code == 204
            handler.release.set()
            await processing
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT count(*) FROM app.dreams")) == 0
                assert await c.scalar(text("SELECT count(*) FROM app.analyses")) == 0
                assert await c.scalar(select(J.c.status)) == "cancelled"

    run(check)


def test_additional_source_version_and_archive_fences(cluster):
    async def check():
        user = uuid4()
        async with worker_session(cluster, FixtureHandler()) as (client, db, _, queue, worker):
            one, two = (await create(client, user)).json(), (await create(client, user)).json()
            claim = await queue.claim()
            execution = await worker.store.prepare(claim)
            additional = UUID(
                two["id"] if str(execution.primary.dream_id) == one["id"] else one["id"]
            )
            await execution.get_source(additional)
            # Deliberately bypass the API invalidator to independently verify source fences.
            async with db.tenant_transaction(user) as c:
                await c.execute(
                    update(tables["app.dreams"])
                    .where(
                        tables["app.dreams"].c.id == additional,
                    )
                    .values(version=tables["app.dreams"].c.version + 1)
                )
            with pytest.raises(SourceChanged):
                await worker.store.complete(execution, Publication(AnalysisPayload()))
            await worker.store.cancel(claim)
            # The second job also has a superseded version; process it to cancel safely.
            remaining = await queue.claim()
            await worker.process(remaining)
            await create(client, user)
            remaining = await queue.claim()
            context = await worker.store.prepare(remaining)
            if context is not None:
                await create(client, user)
                with pytest.raises(SourceChanged):
                    await worker.store.complete(
                        context, Publication(AnalysisPayload(), archive_bound=True)
                    )
                await worker.store.cancel(remaining)

    run(check)


def test_attempt_exhaustion_reaper_and_owned_idempotent_retry(cluster):
    async def check():
        user, other = uuid4(), uuid4()
        async with worker_session(cluster, FixtureHandler()) as (client, db, config, queue, worker):
            dream = (await create(client, user)).json()
            for _ in range(config.job_max_attempts):
                claim = await queue.claim()
                assert claim is not None
                await expire(db, user, claim["id"])
            assert await queue.claim() is None
            await worker.maintain_once()
            path = f"/api/v1/jobs/{dream['analysis_job_id']}"
            job = (await client.get(path, headers=token(user))).json()
            assert job["status"] == "failed" and job["error_code"] == "lease_expired"
            assert job["retry_allowed"]
            assert (await client.get(path, headers=token(other))).status_code == 404
            payload = {
                "expected_attempts": job["attempts"],
                "expected_submission": job["submission"],
            }
            headers = {**token(user), "Idempotency-Key": "manual-retry"}
            retry = await client.post(path + "/retry", headers=headers, json=payload)
            assert retry.status_code == 202 and retry.json()["attempts"] == 0
            assert retry.json()["submission"] == 2
            assert (
                await client.post(path + "/retry", headers=headers, json=payload)
            ).status_code == 202
            assert (
                await client.post(
                    path + "/retry",
                    headers={**token(user), "Idempotency-Key": "wrong-new-key"},
                    json=payload,
                )
            ).status_code == 409
            assert (
                await client.post(
                    path + "/retry",
                    headers={**token(other), "Idempotency-Key": "other"},
                    json=payload,
                )
            ).status_code == 404
            await worker.run_once()
            assert (await client.get(path, headers=token(user))).json()["status"] == "succeeded"

    run(check)


def test_stage_timeout_is_bounded_and_retried(cluster):
    class TimedOutHandler:
        async def run(self, execution):
            await execution.set_stage("extracting")
            await asyncio.sleep(10)

    async def check():
        user = uuid4()
        async with worker_session(
            cluster,
            TimedOutHandler(),
            job_stage_timeout_seconds=0.3,
            job_heartbeat_seconds=0.03,
            job_lease_seconds=2,
            job_timeout_seconds=3,
        ) as (client, _, _, _, worker):
            dream = (await create(client, user)).json()
            await asyncio.wait_for(worker.run_once(), 3)
            job = (
                await client.get(f"/api/v1/jobs/{dream['analysis_job_id']}", headers=token(user))
            ).json()
            assert job["status"] == "queued" and job["error_code"] == "stage_timeout"

    run(check)


def test_process_killed_mid_call_is_recovered_by_replacement(cluster):
    async def check():
        user = uuid4()
        async with worker_session(cluster, FixtureHandler()) as (client, db, config, queue, worker):
            dream = (await create(client, user)).json()
            script = """
import asyncio
from app.config import Settings
from app.db import Database
from app.loop import create_loop
from app.workers.queue import Queue
from app.workers.store import Store
async def blocked():
    settings = Settings()
    db, queue = Database(settings), Queue(settings)
    job = await queue.claim()
    execution = await Store(db, settings).prepare(job)
    await execution.checkpoint("prepared", {"synthetic_crash_checkpoint": True})
    print("mid-call", flush=True)
    await asyncio.sleep(60)
with asyncio.Runner(loop_factory=create_loop) as runner:
    runner.run(blocked())
"""
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env={
                    **os.environ,
                    "APP_ENV": "test",
                    "EMBEDDING_DIMENSIONS": "3",
                    "DATABASE_URL": config.database_url.get_secret_value(),
                    "QUEUE_DATABASE_URL": config.queue_database_url.get_secret_value(),
                    "EXPECTED_SCHEMA_REVISION": config.expected_schema_revision,
                },
            )
            try:
                line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 10)
                assert line.strip() == "mid-call"
                process.kill()
                await asyncio.to_thread(process.wait, 5)
                await expire(db, user, dream["analysis_job_id"])
                assert await worker.run_once()
                job = (
                    await client.get(
                        f"/api/v1/jobs/{dream['analysis_job_id']}", headers=token(user)
                    )
                ).json()
                assert job["status"] == "succeeded" and job["attempts"] == 2
                assert "prepared" in job["checkpoint_stages"]
            finally:
                if process.poll() is None:
                    process.kill()
                    await asyncio.to_thread(process.wait, 5)
                process.stdout.close()

    run(check)


def test_deferred_quota_releases_lease_and_is_reserved_once_before_work(cluster):
    async def check():
        user, handler = uuid4(), FixtureHandler()
        async with worker_session(cluster, handler, ai_daily_limit=1) as (
            client,
            db,
            _,
            _,
            worker,
        ):
            await create(client, user)
            await worker.run_once()
            deferred = (await create(client, user)).json()
            async with db.tenant_transaction(user) as c:
                await c.execute(
                    update(J)
                    .where(J.c.id == UUID(deferred["analysis_job_id"]))
                    .values(run_after=text("clock_timestamp()"))
                )
            await worker.run_once()
            assert handler.calls == 1
            async with db.tenant_transaction(user) as c:
                job = (
                    (
                        await c.execute(
                            select(J).where(
                                J.c.id == UUID(deferred["analysis_job_id"]),
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                assert job["stage"] == "waiting_quota" and job["attempts"] == 0
                assert job["lease_token"] is None
                # Simulate a fresh allowance without waiting a calendar day.
                await c.execute(update(tables["app.ai_usage"]).values(used=0))
                await c.execute(
                    update(J).where(J.c.id == job["id"]).values(run_after=text("clock_timestamp()"))
                )
            await worker.run_once()
            assert handler.calls == 2
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT used FROM app.ai_usage")) == 1
                row = (
                    (
                        await c.execute(
                            select(J).where(
                                J.c.id == UUID(deferred["analysis_job_id"]),
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["status"] == "succeeded"
                assert row["input_versions"]["admission"] == "reserved"

    run(check)


@pytest.mark.parametrize("field", ["data_epoch", "memory_version", "deletion_state", "pipeline"])
def test_account_and_pipeline_changes_fence_publication(cluster, field):
    async def check():
        user = uuid4()
        async with worker_session(cluster, FixtureHandler()) as (
            client,
            db,
            settings,
            queue,
            worker,
        ):
            await create(client, user)
            claim = await queue.claim()
            execution = await worker.store.prepare(claim)
            if field == "pipeline":
                settings.prompt_version = "changed"
            else:
                async with db.tenant_transaction(user) as c:
                    accounts = tables["app.accounts"]
                    value = "deleting" if field == "deletion_state" else accounts.c[field] + 1
                    await c.execute(update(accounts).values({field: value}))
            with pytest.raises(SourceChanged):
                await worker.store.complete(execution, Publication(AnalysisPayload()))
            await worker.store.cancel(claim)
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT count(*) FROM app.analyses")) == 0

    run(check)


def test_retention_reaper_purges_private_artifacts_and_expired_keys(cluster):
    async def check():
        user = uuid4()
        async with worker_session(cluster) as (client, db, _, _, worker):
            dream = (await create(client, user)).json()
            await (
                worker.run_once()
            )  # Default handler produces a prepared checkpoint and safe failure.
            # Administrator-only clock fixture in this disposable test DB; triggers stay unchanged.
            with cluster["admin"].begin() as c:
                c.execute(text("SET LOCAL session_replication_role = replica"))
                c.execute(
                    update(J)
                    .where(J.c.id == UUID(dream["analysis_job_id"]))
                    .values(
                        updated_at=text("clock_timestamp() - interval '8 days'"),
                    )
                )
                keys = tables["app.idempotency_keys"]
                c.execute(
                    update(keys)
                    .where(keys.c.user_id == user)
                    .values(
                        expires_at=text("clock_timestamp() - interval '1 day'"),
                    )
                )
            await worker.maintain_once()
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(text("SELECT count(*) FROM app.job_artifacts")) == 0
                assert await c.scalar(text("SELECT count(*) FROM app.idempotency_keys")) == 0
                assert await c.scalar(select(J.c.stage)) == "retention_cleaned"

    run(check)


def test_shutdown_leaves_recoverable_lease(cluster):
    class Blocking:
        def __init__(self):
            self.started = asyncio.Event()

        async def run(self, execution):
            self.started.set()
            await asyncio.sleep(10)

    async def check():
        user, handler = uuid4(), Blocking()
        async with worker_session(
            cluster, handler, worker_shutdown_seconds=0.1, worker_poll_seconds=0.05
        ) as (client, db, config, queue, worker):
            dream = (await create(client, user)).json()
            stop = asyncio.Event()
            serving = asyncio.create_task(worker.serve(stop))
            await asyncio.wait_for(handler.started.wait(), 2)
            stop.set()
            await asyncio.wait_for(serving, 2)
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(select(J.c.status)) == "running"
            await expire(db, user, dream["analysis_job_id"])
            replacement = Worker(config, db, queue, {"analysis": FixtureHandler()})
            assert await replacement.run_once()
            assert (
                await client.get(f"/api/v1/jobs/{dream['analysis_job_id']}", headers=token(user))
            ).json()["status"] == "succeeded"

    run(check)
