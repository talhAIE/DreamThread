"""Verify runtime-role/schema/dimension readiness without starting the API."""

import asyncio

from app.config import Settings
from app.db import Database
from app.loop import create_loop


async def check():
    db = Database(Settings())
    try:
        status = await db.probe()
        print("Database readiness:", status)
        return 0 if status == "ok" else 1
    finally:
        await db.close()


if __name__ == "__main__":
    with asyncio.Runner(loop_factory=create_loop) as runner:
        raise SystemExit(runner.run(check()))
