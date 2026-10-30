"""Atomic per-user request limiting shared by API replicas."""

from sqlalchemy import case, extract, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import tables
from app.errors import APIError

LIMITS = tables["app.api_rate_limits"]
WINDOW = 60


async def enforce_user_request_limit(db, user_id, limit):
    async with db.tenant_transaction(user_id) as c:
        window_start = await c.scalar(
            select(func.date_trunc("minute", func.clock_timestamp()))
        )
        statement = insert(LIMITS).values(
            user_id=user_id,
            route="api",
            window_start=window_start,
            request_count=1,
        )
        admitted = await c.scalar(
            statement.on_conflict_do_update(
                index_elements=[LIMITS.c.user_id, LIMITS.c.route],
                set_={
                    "window_start": window_start,
                    "request_count": case(
                        (
                            LIMITS.c.window_start == window_start,
                            LIMITS.c.request_count + 1,
                        ),
                        else_=1,
                    ),
                },
                where=or_(
                    LIMITS.c.window_start != window_start,
                    LIMITS.c.request_count < limit,
                ),
            ).returning(LIMITS.c.request_count)
        )
        if admitted is not None:
            return
        elapsed = await c.scalar(
            select(extract("epoch", func.clock_timestamp() - window_start))
        )
        retry_after = max(1, WINDOW - int(elapsed or 0))
        raise APIError(
            "rate_limit_exceeded",
            "Request rate limit exceeded",
            429,
            headers={"Retry-After": str(retry_after)},
        )
