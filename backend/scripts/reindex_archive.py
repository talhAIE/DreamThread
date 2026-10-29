"""Queue a complete owned archive for the configured embedding version.

Run from backend: uv run python scripts/reindex_archive.py --user-id UUID
This queues potentially billable work; model calls run in the existing worker.
Use only a UUID obtained from the verified account record, never client-supplied tool text.
"""

import argparse
import asyncio
import hashlib
import json
from uuid import UUID

from sqlalchemy import select

from app.auth.identity import Identity
from app.config import Settings
from app.db import Database
from app.db.models import tables
from app.errors import APIError
from app.loop import create_loop
from app.services.indexing import IndexService
from app.workers.types import WorkFailure


async def reindex(user_id):
    settings = Settings()
    db = Database(settings)
    try:
        if await db.probe() != "ok":
            raise WorkFailure("provider_configuration")
        async with db.tenant_transaction(user_id) as c:
            account = (
                (
                    await c.execute(
                        select(tables["app.accounts"]).where(
                            tables["app.accounts"].c.id == user_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if account is None or account["deletion_state"] != "active":
                raise WorkFailure("provider_configuration")
            dreams = tables["app.dreams"]
            sources = (
                await c.execute(
                    select(dreams.c.id, dreams.c.version)
                    .where(
                        dreams.c.user_id == user_id,
                        dreams.c.lifecycle == "saved",
                        dreams.c.deleted_at.is_(None),
                    )
                    .order_by(dreams.c.id)
                )
            ).all()
        identity = Identity(user_id, account["is_anonymous"])
        submitted = 0
        for dream_id, version in sources:
            # Deterministic request keys make restarts replay admitted work. A new source version
            # or embedding version gets a new request. Retry failed jobs through the API.
            version_key = hashlib.sha256(settings.embedding_version.encode()).hexdigest()[:16]
            key = f"reindex-{dream_id.hex}-{version}-{version_key}"
            await IndexService(db, settings).request(identity, dream_id, version, key)
            submitted += 1
        print(
            json.dumps(
                {
                    "submitted_or_reused": submitted,
                    "saved_dreams": len(sources),
                    "embedding_version": settings.embedding_version,
                }
            )
        )
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, type=UUID)
    args = parser.parse_args()
    try:
        with asyncio.Runner(loop_factory=create_loop) as runner:
            runner.run(reindex(args.user_id))
    except (WorkFailure, APIError) as error:
        print(json.dumps({"queued_all": False, "error_code": error.code}))
        raise SystemExit(1) from None
