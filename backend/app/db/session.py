"""Short transactions with explicit, transaction-local tenant identity."""

import asyncio
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.db.connections import connect_args
from app.db.roles import unsafe_login


class Database:
    def __init__(self, settings: Settings):
        self.timeout = settings.db_timeout_seconds
        self.expected = settings.expected_schema_revision
        self.dimensions = settings.embedding_dimensions
        self.engine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_size=settings.db_pool_size,
            max_overflow=0,
            pool_timeout=self.timeout,
            pool_pre_ping=True,
            pool_recycle=settings.db_pool_recycle_seconds,
            connect_args=connect_args(
                settings.database_url.get_secret_value(), settings.db_connect_timeout_seconds
            ),
            hide_parameters=True,
        )

    @asynccontextmanager
    async def tenant_transaction(
        self, verified_user_id: UUID, *, isolation_level: str | None = None
    ):
        """Only pass identity obtained from verified auth or an owned persisted job.

        PostgreSQL custom settings are an application trust boundary, not authentication.
        Never expose arbitrary SQL or this identity argument to clients/models.
        """
        user_id = str(UUID(str(verified_user_id)))
        async with self.engine.connect() as connection:
            if isolation_level is not None:
                await connection.execution_options(isolation_level=isolation_level)
            async with connection.begin():
                await connection.execute(
                    text("SELECT set_config('app.user_id', :user_id, true)"),
                    {"user_id": user_id},
                )
                await connection.execute(text("SET LOCAL search_path TO app, extensions, public"))
                await connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
                yield connection

    async def probe(self) -> str:
        try:
            async with asyncio.timeout(self.timeout):
                async with self.engine.connect() as connection:
                    unsafe = await connection.scalar(
                        text("""
                        SELECT r.rolsuper OR r.rolbypassrls OR EXISTS (
                            SELECT 1 FROM pg_namespace n
                            WHERE n.nspname = 'app'
                              AND pg_has_role(current_user, n.nspowner, 'MEMBER')
                        ) OR EXISTS (
                            SELECT 1 FROM pg_roles q WHERE q.rolname = 'dreamthread_queue'
                              AND pg_has_role(current_user, q.oid, 'MEMBER')
                        ) OR EXISTS (
                            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                            WHERE n.nspname = 'app' AND c.relkind = 'r'
                              AND pg_has_role(current_user, c.relowner, 'MEMBER')
                        ) FROM pg_roles r WHERE r.rolname = current_user
                    """)
                    )
                    unsafe = unsafe or await unsafe_login(connection)
                    member = await connection.scalar(
                        text("""
                        SELECT EXISTS(SELECT 1 FROM pg_roles p WHERE p.rolname='dreamthread_runtime'
                                      AND pg_has_role(current_user, p.oid, 'MEMBER'))
                    """)
                    )
                    unsafe = unsafe or not member
                    if unsafe and self.expected != "unversioned":
                        return "unsafe_role"
                    exists = await connection.scalar(
                        text("SELECT to_regclass('app.alembic_version') IS NOT NULL")
                    )
                    if not exists:
                        return "ok" if self.expected == "unversioned" else "schema_mismatch"
                    revisions = set(
                        (
                            await connection.execute(
                                text("SELECT version_num FROM app.alembic_version")
                            )
                        ).scalars()
                    )
                    if revisions != {self.expected}:
                        return "schema_mismatch"
                    if self.expected in {
                        "0005_embedding_spaces",
                        "0006_personal_memory",
                        "0007_insights_privacy",
                        "0008_request_rate_limits",
                        "0009_continuity_search_index",
                        "0010_audio_cleanup",
                    }:
                        # Versioned spaces enforce their own dimensions. The legacy fixed-size
                        # vector column is retained for migration compatibility, never searched.
                        if self.dimensions is not None and not 1 <= self.dimensions <= 16000:
                            return "schema_mismatch"
                    else:
                        dimension = await connection.scalar(
                            text(
                                "SELECT embedding_dimensions FROM app.schema_configuration "
                                "WHERE id = 1"
                            )
                        )
                        if self.dimensions is None or dimension != self.dimensions:
                            return "schema_mismatch"
                    return "ok"
        except Exception:
            return "unavailable"

    async def close(self):
        await self.engine.dispose()
