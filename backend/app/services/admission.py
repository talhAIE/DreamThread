"""Atomic, cross-process AI admission and idempotency, called BEFORE queue/model work.

Call inside the SAME tenant transaction that creates the resource/job. Rollback then
also rolls back quota/idempotency. Retrying a committed operation does not recharge it.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import tables
from app.errors import APIError
from app.services.accounts import active_account

KEY = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
KEYS, USAGE = tables["app.idempotency_keys"], tables["app.ai_usage"]


@dataclass(frozen=True)
class Admission:
    resource_id: UUID
    replayed: bool


async def reserve_ai(connection, identity, settings, *, route, key, body, units=1, now=None):
    return await _reserve(
        connection,
        identity,
        settings,
        route=route,
        key=key,
        body=body,
        units=units,
        now=now,
        charge=True,
    )


async def reserve_resource(connection, identity, settings, *, route, key, body):
    """Idempotency for durable user data. AI admission is separately charged per job."""
    return await _reserve(
        connection,
        identity,
        settings,
        route=route,
        key=key,
        body=body,
        units=1,
        now=None,
        charge=False,
    )


async def _reserve(connection, identity, settings, *, route, key, body, units, now, charge):
    if not key or not KEY.fullmatch(key):
        raise APIError("invalid_idempotency_key", "Supply a valid Idempotency-Key", 422)
    if route not in {
        "dream.create",
        "dream.analyze",
        "analysis.work",
        "detective.message",
        "job.retry",
        "embedding.work",
        "dream.index",
        "memory.create",
        "dream.details",
        "followup.answer",
    }:
        raise ValueError("Unsupported server-side operation")
    if type(units) is not int or units < 1:
        raise ValueError("Quota units must be positive integers")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("Admission clock must be timezone-aware")
    now = now.astimezone(UTC)
    await active_account(connection, identity.user_id)  # serializes first inserts/expiry per user
    fingerprint = hashlib.sha256(
        json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    predicate = (KEYS.c.user_id == identity.user_id) & (KEYS.c.route == route) & (KEYS.c.key == key)
    prior = (await connection.execute(select(KEYS).where(predicate))).mappings().one_or_none()
    if prior and prior["expires_at"] > now:
        if prior["request_hash"] != fingerprint:
            raise APIError("idempotency_conflict", "Key was used for another request", 409)
        return Admission(prior["result_resource_id"], replayed=True)
    if prior:
        await connection.execute(delete(KEYS).where(predicate))
    limit = settings.anonymous_ai_daily_limit if identity.is_anonymous else settings.ai_daily_limit
    limit = min(limit, settings.ai_daily_limit)
    statement = insert(USAGE).values(user_id=identity.user_id, period=now.date(), used=units)
    if not charge:
        admitted = 0
    elif units > limit:
        admitted = None
    else:
        admitted = await connection.scalar(
            statement.on_conflict_do_update(
                index_elements=[USAGE.c.user_id, USAGE.c.period],
                set_={"used": USAGE.c.used + units},
                where=USAGE.c.used + units <= limit,
            ).returning(USAGE.c.used)
        )
    if admitted is None:
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        raise APIError(
            "quota_exceeded",
            "Daily AI allowance reached",
            429,
            headers={"Retry-After": str(max(1, int((tomorrow - now).total_seconds())))},
        )
    resource = uuid4()
    await connection.execute(
        insert(KEYS).values(
            user_id=identity.user_id,
            route=route,
            key=key,
            request_hash=fingerprint,
            result_resource_id=resource,
            expires_at=now + timedelta(hours=24),
        )
    )
    return Admission(resource, replayed=False)
