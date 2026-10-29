"""Opt-in, read-only hosted Neon readiness/role/TLS/tenant-context checks. No model calls."""

import argparse
import asyncio
import json
from uuid import uuid4

from sqlalchemy import text

from app.config import Settings
from app.db import Database
from app.db.connections import postgres_url
from app.loop import create_loop
from app.workers.queue import Queue


async def require_client_tls(connection):
    # Hosted proxies can terminate TLS before PostgreSQL. pg_stat_ssl reports
    # that backend hop; inspect libpq for the actual client-to-Neon connection.
    raw = await connection.get_raw_connection()
    if not raw.driver_connection.pgconn.ssl_in_use:
        raise ValueError("Client TLS required")


async def check():
    settings = Settings()
    postgres_url(settings.database_url.get_secret_value(), neon_required=True)
    if not settings.queue_database_url:
        raise ValueError("Queue URL required")
    postgres_url(settings.queue_database_url.get_secret_value(), neon_required=True)
    db, queue = Database(settings), Queue(settings)
    try:
        if await db.probe() != "ok":
            raise ValueError("Runtime readiness failed")
        await queue.check_role()
        async with db.engine.connect() as c:
            await require_client_tls(c)
            if await c.scalar(text("SELECT count(*) FROM app.accounts")) != 0:
                raise ValueError("Context-free tenant access denied by expected RLS")
            schema = await c.scalar(
                text("""
                SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
                WHERE e.extname='vector'
            """)
            )
            if schema != "extensions":
                raise ValueError("Vector schema mismatch")
        for user in (uuid4(), uuid4()):
            async with db.tenant_transaction(user) as c:
                current = await c.scalar(text("SELECT current_setting('app.user_id', true)"))
                if current != str(user):
                    raise ValueError("Tenant context mismatch")
        async with db.engine.connect() as c:
            if await c.scalar(text("SELECT nullif(current_setting('app.user_id', true), '')")):
                raise ValueError("Tenant context leaked")
        async with queue.engine.connect() as c:
            await require_client_tls(c)
            if await c.scalar(
                text("SELECT has_table_privilege(current_user, 'app.dream_revisions', 'SELECT')")
            ):
                raise ValueError("Queue can read private sources")
        print(
            json.dumps(
                {
                    "neon_live_verified": True,
                    "schema_revision": settings.expected_schema_revision,
                    "checks": ["runtime_role", "queue_role", "tls", "pgvector", "tenant_context"],
                }
            )
        )
    finally:
        await queue.close()
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Connect to the configured hosted Neon DB"
    )
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to run the read-only Neon check")
    try:
        with asyncio.Runner(loop_factory=create_loop) as runner:
            runner.run(check())
    except Exception:
        print(json.dumps({"neon_live_verified": False, "error_code": "neon_readiness_failed"}))
        raise SystemExit(1) from None
