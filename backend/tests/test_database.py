"""Run with TEST_DATABASE_URL pointing only to a disposable PostgreSQL database."""

import asyncio
import os

import pytest

from app.config import Settings
from app.db import Database


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
    asyncio.run(check())


@pytest.mark.integration
def test_real_database_connectivity_and_revision():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set; real PostgreSQL verification required")

    async def check():
        good = Database(Settings(_env_file=None, database_url=url))
        mismatch = Database(Settings(
            _env_file=None, database_url=url, expected_schema_revision="missing-revision"
        ))
        try:
            assert await good.probe() == "ok"
            assert await mismatch.probe() == "schema_mismatch"
        finally:
            await good.close()
            await mismatch.close()
    asyncio.run(check())
