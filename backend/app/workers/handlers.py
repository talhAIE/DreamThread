"""Production Nemotron extraction/reflection and continuity registry."""

import random
from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update

from app.db.models import tables
from app.services.constellation import ConstellationService
from app.services.detective import DetectiveService
from app.services.extraction import ExtractionPipeline
from app.services.indexing import EmbeddingHandler
from app.workers.types import AnalysisPayload, Publication

J = tables["app.jobs"]
ST = tables["app.storylines"]
M = tables["app.messages"]
C = tables["app.conversations"]


class AnalysisHandler:
    def __init__(self, settings, provider=None):
        self.pipeline = ExtractionPipeline(settings, provider)

    async def run(self, execution):
        return await self.pipeline.run(execution)


class ContinuityHandler:
    def __init__(self, settings, db=None, provider=None):
        self.settings = settings
        self.db = db
        self.provider = provider

    async def run(self, execution):
        from app.services.continuity import ContinuityEngine

        if self.db:
j            engine = ContinuityEngine(self.db, self.settings, self.provider)
            await engine.compare(
                execution.job["user_id"],
                execution.primary.dream_id,
                record_call=execution.record_call,
            )
        return Publication(AnalysisPayload(), require_evidence=False)


class DurableJobHandler:
    def __init__(self, kind, settings, db):
        self.kind = kind
        self.settings = settings
        self.db = db

    async def run(self, job):
        if self.kind == "storyline_refresh":
            await ConstellationService(self.db).run_storyline_refresh(job)
            return
        if self.kind == "time_capsule_refresh":
            from app.services.insights import InsightsService

            await InsightsService(self.db).run_refresh(job)
            return
        if self.kind == "export":
            from app.services.privacy import PrivacyService

            await PrivacyService(self.db, self.settings).run_export(job)
            return
        if self.kind == "journal_delete":
            from app.services.privacy import PrivacyService

            await PrivacyService(self.db, self.settings).run_journal_deletion(job)
            return
        await DetectiveService(self.db, self.settings).run_queued_job(job)

    async def fail(self, job, failure):
        await self._finish(job, failure=failure)

    async def cancel(self, job):
        await self._finish(job, cancelled=True)

    async def maintain(self, candidate):
        if self.kind == "export" and candidate["status"] == "succeeded":
            from app.services.privacy import PrivacyService

            await PrivacyService(self.db, self.settings).expire_export(candidate)
            return
        if candidate["status"] in {"succeeded", "failed", "cancelled"}:
            await self._finish(candidate, retention_cleanup=True)
            return
        await self._finish(candidate, exhausted=True)

    async def _finish(
        self, job, *, failure=None, cancelled=False, exhausted=False,
        retention_cleanup=False,
    ):
        user_id = job["user_id"]
        async with self.db.tenant_transaction(user_id) as c:
            account = (
                await c.execute(
                    select(tables["app.accounts"])
                    .where(tables["app.accounts"].c.id == user_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if account is None or (
                self.kind == "journal_delete" and account["deletion_state"] != "deleting"
            ) or (
                self.kind != "journal_delete" and account["deletion_state"] != "active"
            ):
                return
            query = select(J).where(
                J.c.user_id == user_id,
                J.c.id == job["id"],
                J.c.kind == self.kind,
            )
            if retention_cleanup:
                query = query.where(
                    J.c.status == job["status"],
                    J.c.stage == job["stage"],
                    J.c.attempts == job["attempts"],
                    J.c.status.in_(("succeeded", "failed", "cancelled")),
                    J.c.stage != "retention_cleaned",
                    J.c.updated_at < func.now()
                    - timedelta(days=self.settings.checkpoint_retention_days),
                )
            elif exhausted:
                query = query.where(
                    J.c.status.in_(("queued", "running")),
                    J.c.attempts >= self.settings.job_max_attempts,
                    J.c.attempts == job["attempts"],
                    or_(
                        J.c.status == "queued",
                        and_(
                            J.c.status == "running",
                            or_(
                                J.c.leased_until.is_(None),
                                J.c.deadline_at.is_(None),
                                J.c.leased_until <= func.clock_timestamp(),
                                J.c.deadline_at <= func.clock_timestamp(),
                            ),
                        ),
                    ),
                )
            else:
                query = query.where(
                    J.c.status == "running",
                    J.c.lease_token == job["lease_token"],
                    J.c.leased_until > func.clock_timestamp(),
                    J.c.deadline_at > func.clock_timestamp(),
                )
            current = (await c.execute(query.with_for_update())).mappings().one_or_none()
            if current is None:
                return
            if retention_cleanup:
                await c.execute(
                    update(J)
                    .where(
                        J.c.user_id == user_id,
                        J.c.id == current["id"],
                        J.c.status == job["status"],
                        J.c.stage == job["stage"],
                        J.c.attempts == job["attempts"],
                        J.c.updated_at < func.now()
                        - timedelta(days=self.settings.checkpoint_retention_days),
                    )
                    .values(stage="retention_cleaned")
                )
                return

            retry = bool(
                failure
                and failure.retryable
                and current["attempts"] < self.settings.job_max_attempts
            )
            delay = min(
                self.settings.job_retry_max_seconds,
                self.settings.job_retry_base_seconds
                * 2 ** max(0, current["attempts"] - 1),
            )
            delay = max(
                failure.retry_after if failure else 0,
                delay + random.uniform(0, delay * 0.25),
            )
            terminal_status = "cancelled" if cancelled else "failed"
            status = "queued" if retry else terminal_status
            error_code = (
                "source_changed"
                if cancelled
                else failure.code
                if failure is not None
                else "lease_expired"
            )
            result = await c.execute(
                update(J)
                .where(J.c.user_id == user_id, J.c.id == current["id"])
                .values(
                    status=status,
                    stage="retry_wait" if retry else terminal_status,
                    error_code=error_code,
                    run_after=func.now() + timedelta(seconds=delay),
                    lease_token=None,
                    leased_until=None,
                    deadline_at=None,
                    stage_deadline_at=None,
                )
            )
            if result.rowcount != 1 or retry:
                return
            if self.kind == "export":
                export_id = current["payload"].get("export_id")
                if export_id:
                    await c.execute(
                        update(tables["app.exports"])
                        .where(
                            tables["app.exports"].c.user_id == user_id,
                            tables["app.exports"].c.id == UUID(export_id),
                            tables["app.exports"].c.status.in_(("pending", "ready")),
                        )
                        .values(
                            status="cancelled" if cancelled else "failed",
                            content=None,
                        )
                    )
            elif self.kind == "time_capsule_refresh":
                month = current["payload"].get("month")
                timezone = current["payload"].get("timezone")
                if month and timezone:
                    from datetime import date

                    await c.execute(
                        update(tables["app.monthly_summaries"])
                        .where(
                            tables["app.monthly_summaries"].c.user_id == user_id,
                            tables["app.monthly_summaries"].c.month == date.fromisoformat(
                                month + "-01"
                            ),
                            tables["app.monthly_summaries"].c.timezone == timezone,
                        )
                        .values(state="stale", summary={})
                    )
            if self.kind == "detective_answer":
                await c.execute(
                    update(M)
                    .where(
                        M.c.user_id == user_id,
                        M.c.id == UUID(current["payload"]["user_message_id"]),
                        M.c.status == "pending",
                    )
                    .values(status="failed")
                )
                await c.execute(
                    update(M)
                    .where(
                        M.c.user_id == user_id,
                        M.c.id == UUID(current["payload"]["assistant_message_id"]),
                        M.c.status == "pending",
                    )
                    .values(
                        body=(
                            "This answer is no longer available because its supporting "
                            "dream evidence changed."
                            if cancelled
                            else "I couldn't safely prepare an answer from your dream entries. "
                            "Please try again."
                        ),
                        status="stale" if cancelled else "failed",
                        context_versions={"prompt_version": "detective_v1"},
                    )
                )
                await c.execute(
                    update(C)
                    .where(
                        C.c.user_id == user_id,
                        C.c.id == UUID(current["payload"]["conversation_id"]),
                    )
                    .values(updated_at=func.now())
                )
            else:
                await c.execute(
                    update(ST)
                    .where(
                        ST.c.user_id == user_id,
                        ST.c.id == UUID(current["payload"]["storyline_id"]),
                    )
                    .values(status="stale")
                )


def default_handlers(settings, db=None):
    handlers = {
        "analysis": AnalysisHandler(settings),
        "embedding": EmbeddingHandler(settings),
        "continuity": ContinuityHandler(settings, db=db),
    }
    if db is not None:
        handlers.update(
            {
                "detective_answer": DurableJobHandler("detective_answer", settings, db),
                "storyline_refresh": DurableJobHandler("storyline_refresh", settings, db),
                "time_capsule_refresh": DurableJobHandler("time_capsule_refresh", settings, db),
                "export": DurableJobHandler("export", settings, db),
                "journal_delete": DurableJobHandler("journal_delete", settings, db),
            }
        )
    return handlers
