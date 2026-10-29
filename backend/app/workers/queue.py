"""Cross-user queue leasing only. This connection cannot read journal/artifact tables."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.models import tables
from app.workers.types import WorkFailure

J = tables["app.jobs"]


class Queue:
    def __init__(self, settings):
        if not settings.queue_database_url:
            raise RuntimeError("QUEUE_DATABASE_URL is required; no runtime-role fallback")
        self.settings = settings
        self.engine = create_async_engine(
            settings.queue_database_url.get_secret_value(),
            pool_size=2,
            pool_pre_ping=True,
            pool_timeout=settings.db_timeout_seconds,
            max_overflow=0,
            hide_parameters=True,
            connect_args={"connect_timeout": 3},
        )

    async def check_role(self):
        async with self.engine.connect() as c:
            allowed = await c.scalar(
                text("""
                SELECT NOT rolsuper AND NOT rolbypassrls
                  AND pg_has_role(current_user, 'dreamthread_queue', 'MEMBER')
                  AND NOT pg_has_role(current_user, 'dreamthread_runtime', 'MEMBER')
                  AND NOT pg_has_role(current_user, 'dreamthread_owner', 'MEMBER')
                FROM pg_roles WHERE rolname=current_user
            """)
            )
            if not allowed:
                raise RuntimeError("Queue connection must have only the restricted queue role")

    async def claim(self):
        s, now = self.settings, func.clock_timestamp()
        async with self.engine.begin() as c:
            candidate = await c.scalar(
                select(J.c.id)
                .where(
                    J.c.kind.in_(["analysis", "embedding"]),
                    J.c.attempts < s.job_max_attempts,
                    or_(
                        and_(J.c.status == "queued", J.c.run_after <= now),
                        and_(
                            J.c.status == "running",
                            or_(
                                J.c.leased_until.is_(None),
                                J.c.deadline_at.is_(None),
                                J.c.leased_until <= now,
                                J.c.deadline_at <= now,
                            ),
                        ),
                    ),
                )
                .order_by(J.c.run_after, J.c.created_at, J.c.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if candidate is None:
                return None
            return (
                (
                    await c.execute(
                        update(J)
                        .where(J.c.id == candidate)
                        .values(
                            status="running",
                            attempts=J.c.attempts + 1,
                            lease_token=uuid4(),
                            heartbeat_at=now,
                            leased_until=now
                            + timedelta(seconds=min(s.job_lease_seconds, s.job_timeout_seconds)),
                            deadline_at=now + timedelta(seconds=s.job_timeout_seconds),
                            stage_deadline_at=now + timedelta(seconds=s.job_stage_timeout_seconds),
                        )
                        .returning(J)
                    )
                )
                .mappings()
                .one()
            )

    async def heartbeat(self, job):
        now = func.clock_timestamp()
        async with self.engine.begin() as c:
            current = (
                (
                    await c.execute(
                        select(J)
                        .where(
                            J.c.id == job["id"],
                            J.c.status == "running",
                            J.c.lease_token == job["lease_token"],
                            J.c.leased_until > now,
                            J.c.deadline_at > now,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current is None:
                return False
            actual_now = await c.scalar(select(func.clock_timestamp()))
            if current["stage_deadline_at"] and current["stage_deadline_at"] <= actual_now:
                raise WorkFailure("stage_timeout", retryable=True)
            result = await c.execute(
                update(J)
                .where(
                    J.c.id == job["id"],
                    J.c.status == "running",
                    J.c.lease_token == job["lease_token"],
                    J.c.leased_until > now,
                    J.c.deadline_at > now,
                )
                .values(
                    heartbeat_at=now,
                    leased_until=func.least(
                        now + timedelta(seconds=self.settings.job_lease_seconds),
                        J.c.deadline_at,
                    ),
                )
            )
            return result.rowcount == 1

    async def maintenance_candidates(self):
        now = func.clock_timestamp()
        async with self.engine.connect() as c:
            return (
                (
                    await c.execute(
                        select(J.c.id, J.c.user_id, J.c.payload)
                        .where(
                            or_(
                                and_(
                                    J.c.status == "running",
                                    or_(
                                        J.c.leased_until.is_(None),
                                        J.c.deadline_at.is_(None),
                                        J.c.leased_until <= now,
                                        J.c.deadline_at <= now,
                                    ),
                                    J.c.attempts >= self.settings.job_max_attempts,
                                ),
                                and_(
                                    J.c.status == "queued",
                                    J.c.attempts >= self.settings.job_max_attempts,
                                ),
                                and_(
                                    J.c.status.in_(["succeeded", "failed", "cancelled"]),
                                    J.c.stage != "retention_cleaned",
                                    J.c.updated_at
                                    < now - timedelta(days=self.settings.checkpoint_retention_days),
                                ),
                            )
                        )
                        .order_by(J.c.updated_at)
                        .limit(100)
                    )
                )
                .mappings()
                .all()
            )

    async def close(self):
        await self.engine.dispose()
