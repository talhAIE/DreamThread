import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import insert, select, text, update
from test_phase2_database import cluster as cluster
from test_phase3_identity import run, session, token
from test_phase6_publication import reply as base_reply
from test_phase8_memory import CONFIG, queue_url, ready, reflection_inputs, remember, work
from test_phase8_memory import clear_jobs as clear_jobs

from app.db.models import tables
from app.providers.nebius import Nebius
from app.workers.handlers import AnalysisHandler
from app.workers.queue import Queue
from app.workers.runner import Worker

pytestmark = pytest.mark.integration


async def correct(client, user, dream, detail_key, **changes):
    return await client.patch(
        f"/api/v1/dreams/{dream['id']}/details",
        headers=token(user) | {"Idempotency-Key": changes.pop("request_key", uuid4().hex)},
        json={
            "expected_version": changes.pop("expected_version", 1),
            "detail_key": detail_key,
            **changes,
        },
    )


async def followup(client, user, dream):
    return (await client.get(f"/api/v1/dreams/{dream['id']}/followup", headers=token(user))).json()


async def answer(client, user, dream, question, key=None, **changes):
    return await client.put(
        f"/api/v1/dreams/{dream['id']}/followup",
        headers=token(user) | {"Idempotency-Key": key or uuid4().hex},
        json={
            "expected_version": question["dream_version"],
            "question_version": question["question_version"],
            **changes,
        },
    )


def test_corrected_detail_survives_transient_retry_and_reanalysis(cluster):
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
            detail = analysis["extraction"]["observations"][0]
            response = await correct(
                client,
                user,
                dream,
                detail["detail_key"],
                label="Grandmother's cottage",
                request_key="correct-once",
            )
            assert response.status_code == 200, response.text
            assert response.json()["dream_version"] == 2 and response.json()["memory_version"] == 1
            replay = await correct(
                client,
                user,
                dream,
                detail["detail_key"],
                label="Grandmother's cottage",
                request_key="correct-once",
            )
            assert replay.status_code == 200 and replay.json()["dream_version"] == 2
            failed = False

            def failure(request):
                nonlocal failed
                if (
                    "gentle reflection companion"
                    in json.loads(request.content)["messages"][0]["content"]
                    and not failed
                ):
                    failed = True
                    return httpx.Response(503, json={"error": {"message": "simulated outage"}})
                return base_reply(request)

            await work(db, settings, transport=failure)
            async with db.tenant_transaction(user) as c:
                await c.execute(
                    update(tables["app.jobs"])
                    .where(tables["app.jobs"].c.status == "queued")
                    .values(run_after=text("clock_timestamp()"))
                )
            calls = []
            await work(db, settings, calls)
            assert len(calls) == 1  # raw extraction checkpoint reused; correction still applied
            assert (
                reflection_inputs(calls)[0]["verified_observations"]["observations"][0]["label"]
                == "Grandmother's cottage"
            )
            result = (
                await client.get(f"/api/v1/dreams/{dream['id']}/analysis", headers=token(user))
            ).json()
            corrected = result["extraction"]["observations"][0]
            assert corrected["label"] == "Grandmother's cottage" and corrected["origin"] == "user"
            assert (
                corrected["detail_key"] == detail["detail_key"]
                and corrected["confirmation_state"] == "confirmed"
            )
            assert result["evidence"][0]["quote"] == "house"
            async with db.tenant_transaction(user) as c:
                jobs = tables["app.jobs"]
                await c.execute(
                    update(jobs).where(jobs.c.status == "succeeded").values(status="failed")
                )
                await c.execute(update(tables["app.dreams"]).values(analysis_state="failed"))
            response = await client.post(
                f"/api/v1/dreams/{dream['id']}/analyze",
                headers=token(user) | {"Idempotency-Key": "reanalyze-corrected"},
                json={"expected_version": 2},
            )
            assert response.status_code == 202
            await work(db, settings)
            again = (
                await client.get(f"/api/v1/dreams/{dream['id']}/analysis", headers=token(user))
            ).json()
            assert again["extraction"]["observations"][0]["label"] == "Grandmother's cottage"
            source = (await client.get(f"/api/v1/dreams/{dream['id']}", headers=token(user))).json()
            assert source["text"] == dream["text"] and source["revision_id"] == dream["revision_id"]

    run(check)


def test_removed_details_stay_removed_and_foreign_or_stale_corrections_fail(cluster):
    async def check():
        user, other = uuid4(), uuid4()
        async with session(cluster, **CONFIG, queue_database_url=queue_url(cluster)) as (
            client,
            db,
            _,
            settings,
            _,
        ):
            dream, analysis = await ready(client, db, settings, user)
            detail = analysis["extraction"]["observations"][0]
            foreign = (await remember(client, other)).json()
            assert (
                await correct(
                    client, user, dream, detail["detail_key"], motif_id=foreign["motif_id"]
                )
            ).status_code == 404
            assert (
                await correct(client, other, dream, detail["detail_key"], removed=True)
            ).status_code == 404
            assert (
                await correct(client, user, dream, "detail_" + "0" * 40, removed=True)
            ).status_code == 409
            removed = await correct(client, user, dream, detail["detail_key"], removed=True)
            assert removed.status_code == 200
            assert (
                await correct(client, user, dream, detail["detail_key"], label="old")
            ).status_code == 409
            await work(db, settings)
            result = (
                await client.get(f"/api/v1/dreams/{dream['id']}/analysis", headers=token(user))
            ).json()
            assert result["status"] == "ready" and result["extraction"]["observations"] == []
            assert result["evidence"][0]["quote"] == dream["text"]
            async with db.tenant_transaction(user) as c:
                assert await c.scalar(select(tables["app.detail_overrides"].c.removed))
                assert (
                    await c.scalar(select(tables["app.dream_motifs"].c.confirmation_state))
                    == "rejected"
                )

    run(check)


def test_followup_entry_answer_is_private_clarification_without_implicit_memory(cluster):
    async def check():
        user, calls = uuid4(), []
        async with session(cluster, **CONFIG, queue_database_url=queue_url(cluster)) as (
            client,
            db,
            _,
            settings,
            _,
        ):
            dream, _ = await ready(client, db, settings, user)
            question = await followup(client, user, dream)
            assert question["status"] == "available" and question["question"]
            response = await answer(
                client,
                user,
                dream,
                question,
                key="answer-once",
                answer="It reminds me of a summer visit.",
            )
            assert response.status_code == 200, response.text
            assert response.json()["latest_answer"]["save_as_association"] is False
            assert response.json()["latest_answer"]["association_id"] is None
            replay = await answer(
                client,
                user,
                dream,
                question,
                key="answer-once",
                answer="It reminds me of a summer visit.",
            )
            assert replay.json()["dream_version"] == 2
            assert (await client.get("/api/v1/memory", headers=token(user))).json()["items"] == []
            await work(db, settings, calls)
            context = reflection_inputs(calls)[0]
            assert context["personal_associations"] == []
            assert context["entry_clarifications"] == [
                {"answer": "It reminds me of a summer visit.", "scope": "this_entry_only"}
            ]
            assert (await followup(client, user, dream))[
                "question"
            ] is None  # identical answered question suppressed

    run(check)


def test_followup_requires_explicit_consent_and_forgetting_cannot_resurrect_from_answer(cluster):
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
            question = await followup(client, user, dream)
            assert (
                await answer(
                    client,
                    user,
                    dream,
                    question,
                    answer="grandmother's home",
                    save_as_association=True,
                )
            ).status_code == 422
            saved = await answer(
                client,
                user,
                dream,
                question,
                answer="grandmother's home",
                save_as_association=True,
                motif_id=motif,
            )
            assert saved.status_code == 200, saved.text
            association = saved.json()["latest_answer"]["association_id"]
            calls = []
            await work(db, settings, calls)
            assert reflection_inputs(calls)[0]["personal_associations"][0]["id"] == association
            deleted = await client.delete(
                f"/api/v1/memory/{association}?expected_version=1", headers=token(user)
            )
            assert deleted.status_code == 204
            calls.clear()
            await work(db, settings, calls)
            context = reflection_inputs(calls)[0]
            assert context["personal_associations"] == [] and context["entry_clarifications"] == []
            assert "grandmother's home" not in json.dumps(context)
            latest = (await followup(client, user, dream))["latest_answer"]
            assert (
                latest["answer"] == "grandmother's home"
            )  # user's original answer remains their journal data

    run(check)


def test_skip_unknown_question_conflicting_answers_and_edit_existing_answer(cluster):
    async def check():
        user, other = uuid4(), uuid4()
        async with session(cluster, **CONFIG, queue_database_url=queue_url(cluster)) as (
            client,
            db,
            _,
            settings,
            _,
        ):
            dream, _ = await ready(client, db, settings, user)
            question = await followup(client, user, dream)
            assert (
                await client.get(f"/api/v1/dreams/{dream['id']}/followup", headers=token(other))
            ).status_code == 404
            assert (await answer(client, other, dream, question, skipped=True)).status_code == 404
            assert (
                await answer(
                    client, user, dream, question | {"question_version": "0" * 40}, skipped=True
                )
            ).status_code == 409
            assert (
                await answer(client, user, dream, question, skipped=True, answer="conflicting")
            ).status_code == 422
            skipped = await answer(client, user, dream, question, skipped=True)
            assert skipped.status_code == 200 and skipped.json()["status"] == "skipped"
            edit = await answer(
                client,
                user,
                dream,
                question | {"dream_version": 2},
                expected_answer_version=1,
                answer="I recognize the place now.",
            )
            assert edit.status_code == 200, edit.text
            assert edit.json()["latest_answer"]["version"] == 2
            stale = await answer(
                client,
                user,
                dream,
                question | {"dream_version": 3},
                expected_answer_version=1,
                answer="A stale edit.",
            )
            assert stale.status_code == 409
            await work(db, settings)
            result = await followup(client, user, dream)
            assert result["question"] is None and result["status"] == "answered"
            assert (await client.get("/api/v1/memory", headers=token(user))).json()["items"] == []

    run(check)


def test_memory_change_clears_dependent_answers_and_rejects_late_publication(cluster):
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
            memory = (await remember(client, user, dream, motif)).json()
            async with db.tenant_transaction(user) as c:
                conversation = await c.scalar(
                    insert(tables["app.conversations"])
                    .values(user_id=user, title="Private")
                    .returning(tables["app.conversations"].c.id)
                )
                await c.execute(
                    insert(tables["app.messages"]).values(
                        user_id=user,
                        conversation_id=conversation,
                        role="assistant",
                        body="grandmother's home",
                        status="completed",
                        request_key="old-answer",
                    )
                )
            changed = False

            async def late_reply(request):
                nonlocal changed
                if (
                    "gentle reflection companion"
                    in json.loads(request.content)["messages"][0]["content"]
                    and not changed
                ):
                    changed = True
                    removed = await client.delete(
                        f"/api/v1/memory/{memory['id']}?expected_version=1", headers=token(user)
                    )
                    assert removed.status_code == 204
                return base_reply(request)

            queue = Queue(settings)
            try:
                worker = Worker(
                    settings,
                    db,
                    queue,
                    {
                        "analysis": AnalysisHandler(
                            settings, Nebius(settings, transport=httpx.MockTransport(late_reply))
                        )
                    },
                )
                assert await worker.run_once()
            finally:
                await queue.close()
            async with db.tenant_transaction(user) as c:
                message = (await c.execute(select(tables["app.messages"]))).mappings().one()
                assert message["body"] == "" and message["status"] == "stale"
                assert await c.scalar(text("SELECT count(*) FROM app.job_artifacts")) == 0
                assert (
                    await c.scalar(text("SELECT count(*) FROM app.analyses WHERE state='ready'"))
                    == 0
                )
                assert (
                    await c.scalar(text("SELECT count(*) FROM app.jobs WHERE status='queued'")) == 1
                )
            calls = []
            await work(db, settings, calls)
            assert reflection_inputs(calls)[0]["personal_associations"] == []

    run(check)
