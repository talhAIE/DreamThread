"""Short transactions with explicit, transaction-local tenant identity."""

import asyncio
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings


class Database:
    def __init__(self, settings: Settings):
        self.timeout = settings.db_timeout_seconds
        self.expected = settings.expected_schema_revision
        self.dimensions = settings.embedding_dimensions
        self.engine = create_async_engine(
            settings.database_url.get_secret_value(), pool_size=settings.db_pool_size,
            max_overflow=0, pool_timeout=self.timeout, pool_pre_ping=True,
            connect_args={"connect_timeout": int(self.timeout) + 1}, hide_parameters=True,
        )

    @asynccontextmanager
    async def tenant_transaction(self, verified_user_id: UUID):
        """Only pass identity obtained from verified auth or an owned persisted job.

        PostgreSQL custom settings are an application trust boundary, not authentication.
        Never expose arbitrary SQL or this identity argument to clients/models.
        """
        user_id = str(UUID(str(verified_user_id)))
        async with self.engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.user_id', :user_id, true)"), {"user_id": user_id}
            )
            await connection.execute(text("SET LOCAL search_path TO app, extensions, public"))
            yield connection

    async def probe(self) -> str:
        try:
            async with asyncio.timeout(self.timeout):
                async with self.engine.connect() as connection:
                    unsafe = await connection.scalar(text("""
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
                    """))
                    if unsafe and self.expected != "unversioned":
                        return "unsafe_role"
                    exists = await connection.scalar(
                        text("SELECT to_regclass('app.alembic_version') IS NOT NULL")
                    )
                    if not exists:
                        return "ok" if self.expected == "unversioned" else "schema_mismatch"
                    revisions = set((await connection.execute(
                        text("SELECT version_num FROM app.alembic_version")
                    )).scalars())
                    if revisions != {self.expected}:
                        return "schema_mismatch"
                    dimension = await connection.scalar(text(
                        "SELECT embedding_dimensions FROM app.schema_configuration WHERE id = 1"
                    ))
                    if self.dimensions is None or dimension != self.dimensions:
                        return "schema_mismatch"
                    return "ok"
        except Exception:
            return "unavailable"

    async def close(self):
        await self.engine.dispose()
