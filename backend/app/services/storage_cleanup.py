"""Idempotent object deletion outbox. A missing object is a successful deletion."""

from sqlalchemy import func, select, update

from app.db.models import tables
from app.providers.storage import PrivateStorage, validate_key

C, A, J, ASSETS = (
    tables["app." + name] for name in ("cleanup_tasks", "accounts", "jobs", "audio_assets")
)


class StorageCleanup:
    def __init__(self, db, settings, storage=None):
        self.db, self.settings = db, settings
        self.storage = storage or PrivateStorage(settings)

    async def process(self, user_id, task_id):
        async with self.db.tenant_transaction(user_id) as c:
            row = (
                (
                    await c.execute(
                        select(C).where(
                            C.c.user_id == user_id,
                            C.c.id == task_id,
                            C.c.state != "complete",
                            C.c.not_before <= func.clock_timestamp(),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return
            keys = row["storage_keys"]
            for key in keys:
                validate_key(key, user_id)
            await c.execute(update(C).where(C.c.id == task_id).values(attempts=C.c.attempts + 1))
        # No application transaction or row lock is held during a remote deletion.
        await self.storage.remove(keys)
        async with self.db.tenant_transaction(user_id) as c:
            account = (
                (await c.execute(select(A).where(A.c.id == user_id).with_for_update()))
                .mappings()
                .one_or_none()
            )
            if account is None:
                return
            await c.execute(
                update(C)
                .where(C.c.id == task_id, C.c.user_id == user_id)
                .values(
                    state="complete",
                    storage_keys=[],
                )
            )
            # Remove temporary transcript contents after expiry/deletion, retaining only
            # the minimum asset state needed by an already attached reviewed dream.
            await c.execute(
                update(ASSETS)
                .where(
                    ASSETS.c.user_id == user_id,
                    ASSETS.c.storage_key.in_(keys),
                    ASSETS.c.lifecycle != "attached",
                )
                .values(
                    lifecycle="expired", verified_metadata={"data_epoch": account["data_epoch"]}
                )
            )
            if account["deletion_state"] != "deleting":
                return
            remaining = await c.scalar(
                select(func.count())
                .select_from(C)
                .where(C.c.user_id == user_id, C.c.state != "complete")
            )
            if remaining:
                return
            # Resume the already fenced journal-deletion job once required cleanup finishes.
            await c.execute(
                update(J)
                .where(
                    J.c.user_id == user_id,
                    J.c.kind == "journal_delete",
                    J.c.data_epoch == account["data_epoch"],
                    J.c.status == "failed",
                    J.c.error_code == "storage_cleanup_pending",
                )
                .values(
                    status="queued",
                    stage="queued",
                    error_code=None,
                    attempts=0,
                    run_after=func.now(),
                    lease_token=None,
                    leased_until=None,
                    deadline_at=None,
                    stage_deadline_at=None,
                )
            )

    async def maintain(self, queue):
        # Queue role can see only scheduling columns, never object keys or transcripts.
        for candidate in await queue.cleanup_candidates():
            try:
                await self.process(candidate["user_id"], candidate["id"])
            except Exception:
                # Persisted tasks remain queued and are retried next maintenance pass.
                # Never log a signed URL, key, transcript, provider body or credential.
                continue
