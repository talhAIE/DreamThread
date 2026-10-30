"""Bounded work execution with independent heartbeat and metadata-only logging."""

import asyncio
import logging

from app.errors import APIError
from app.services.indexing import EmbeddingPublication
from app.workers.handlers import default_handlers
from app.workers.store import Store
from app.workers.types import LeaseLost, Publication, SourceChanged, WorkFailure, classify

logger = logging.getLogger("dreamthread.worker")
DURABLE_JOB_KINDS = {
    "detective_answer",
    "storyline_refresh",
    "time_capsule_refresh",
    "export",
    "journal_delete",
}


class Worker:
    def __init__(self, settings, db, queue, handlers=None):
        self.settings, self.queue = settings, queue
        self.store = Store(db, settings)
        self.handlers = default_handlers(settings, db=db) if handlers is None else handlers

    async def _execute(self, job, state):
        handler = self.handlers.get(job["kind"])
        if job["kind"] in DURABLE_JOB_KINDS:
            if handler is None:
                raise WorkFailure("unsupported_job")
            async with asyncio.timeout(self.settings.job_timeout_seconds):
                await handler.run(job)
            return
        execution = await self.store.prepare(job)
        if execution is None:
            return  # deferred quota; lease was released atomically
        state["execution"] = execution
        if handler is None:
            raise WorkFailure("unsupported_job")
        async with asyncio.timeout(self.settings.job_timeout_seconds):
            result = await handler.run(execution)
        if not isinstance(result, (Publication, EmbeddingPublication)):
            raise WorkFailure("invalid_output")
        await self.store.complete(execution, result)

    async def _heartbeat(self, job):
        interval = min(
            self.settings.job_heartbeat_seconds, self.settings.job_stage_timeout_seconds / 4
        )
        while True:
            await asyncio.sleep(interval)
            if not await self.queue.heartbeat(job):
                raise LeaseLost

    async def process(self, job):
        state = {}
        operation = asyncio.create_task(self._execute(job, state))
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            await asyncio.wait((operation, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            if operation.done():
                await operation
            else:
                # A lost lease or expired stage cancels active external work promptly.
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                await heartbeat
        except LeaseLost:
            logger.info("lease_lost job_id=%s", job["id"])
        except SourceChanged:
            handler = self.handlers.get(job["kind"])
            if job["kind"] in DURABLE_JOB_KINDS:
                await handler.cancel(job)
            else:
                await self.store.cancel(job)
        except Exception as error:
            failure = classify(error)
            execution = state.get("execution")
            try:
                if (
                    job["kind"] in DURABLE_JOB_KINDS
                    and isinstance(error, APIError)
                    and error.status < 500
                ):
                    failure = WorkFailure("invalid_output")
                handler = self.handlers.get(job["kind"])
                if job["kind"] in DURABLE_JOB_KINDS:
                    await handler.fail(job, failure)
                else:
                    await self.store.fail(
                        job, failure, execution.sources if execution else ()
                    )
            except SourceChanged:
                await self.store.cancel(job)
            except LeaseLost:
                pass
            logger.info("job_failure job_id=%s code=%s", job["id"], failure.code)
        finally:
            # Shutdown/cancellation deliberately leaves the lease to expire and be reclaimed.
            operation.cancel()
            heartbeat.cancel()
            await asyncio.gather(operation, heartbeat, return_exceptions=True)

    async def run_once(self):
        job = await self.queue.claim()
        if job is None:
            return False
        await self.process(job)
        return True

    async def maintain_once(self):
        for candidate in await self.queue.maintenance_candidates():
            try:
                handler = self.handlers.get(candidate["kind"])
                if candidate["kind"] in DURABLE_JOB_KINDS:
                    await handler.maintain(candidate)
                else:
                    await self.store.maintain(candidate)
            except LeaseLost:
                continue

    async def serve(self, stop):
        async def consume():
            while not stop.is_set():
                try:
                    worked = await self.run_once()
                except Exception as error:
                    # Infrastructure errors leave leases recoverable, never emit raw DB details.
                    logger.error("worker_dependency_failed type=%s", type(error).__name__)
                    worked = False
                if not worked:
                    try:
                        await asyncio.wait_for(stop.wait(), self.settings.worker_poll_seconds)
                    except TimeoutError:
                        pass

        async def maintenance():
            while not stop.is_set():
                try:
                    await self.maintain_once()
                except Exception as error:
                    logger.error("maintenance_failed type=%s", type(error).__name__)
                try:
                    await asyncio.wait_for(stop.wait(), self.settings.worker_maintenance_seconds)
                except TimeoutError:
                    pass

        tasks = [asyncio.create_task(consume()) for _ in range(self.settings.worker_concurrency)]
        tasks.append(asyncio.create_task(maintenance()))
        try:
            await stop.wait()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks), self.settings.worker_shutdown_seconds
                )
            except TimeoutError:
                pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
