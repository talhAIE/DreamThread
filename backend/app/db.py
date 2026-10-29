"""Short, bounded readiness queries; no AI calls or automatic migrations."""

import asyncio
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings

ProbeResult = Literal["ok", "schema_mismatch", "unavailable"]


class Database:
    def __init__(self, settings: Settings):
        self.timeout = settings.db_timeout_seconds
        self.expected = settings.expected_schema_revision
        self.engine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_size=settings.db_pool_size,
            max_overflow=0,
            pool_timeout=settings.db_timeout_seconds,
            pool_pre_ping=True,
            connect_args={"connect_timeout": int(settings.db_timeout_seconds) + 1},
            hide_parameters=True,
        )

    async def probe(self) -> ProbeResult:
        try:
            async with asyncio.timeout(self.timeout):
                async with self.engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
                    exists = await connection.scalar(
                        text("SELECT to_regclass('app.alembic_version') IS NOT NULL")
                    )
                    if not exists:
                        return "ok" if self.expected == "unversioned" else "schema_mismatch"
                    revisions = set((await connection.execute(
                        text("SELECT version_num FROM app.alembic_version")
                    )).scalars())
                    return "ok" if revisions == {self.expected} else "schema_mismatch"
        except Exception:
            # Do not leak database exception strings, connection URLs, or credentials.
            return "unavailable"

    async def close(self) -> None:
        await self.engine.dispose()
