"""Verify bounded database failure without leaking connection details."""

import asyncio

from app.config import Settings
from app.db import Database
from app.loop import create_loop


def test_database_failure_is_sanitized():
    async def check():
        db = Database(Settings(
            _env_file=None,
            database_url="postgresql+psycopg://u:do-not-leak@127.0.0.1:1/test",
            db_timeout_seconds=0.1,
        ))
        try:
            assert await db.probe() == "unavailable"
        finally:
            await db.close()
    with asyncio.Runner(loop_factory=create_loop) as runner:
        runner.run(check())
