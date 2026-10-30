from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.dependencies import CurrentIdentity
from app.schemas.insights import (
    InsightsResult,
    TimeCapsule,
    TimeCapsuleList,
    TimeCapsuleRefreshAccepted,
)
from app.services.insights import InsightsService

router = APIRouter(tags=["Insights and time capsules"])


@router.get("/insights", response_model=InsightsResult)
async def insights(
    request: Request,
    identity: CurrentIdentity,
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    timezone: Annotated[str, Query(min_length=1, max_length=80)],
):
    return await InsightsService(request.app.state.database).get_insights(
        identity.user_id, month, timezone
    )


@router.get("/time-capsules", response_model=TimeCapsuleList)
async def list_time_capsules(
    request: Request,
    identity: CurrentIdentity,
    timezone: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
):
    return await InsightsService(request.app.state.database).list_capsules(
        identity.user_id, timezone
    )


@router.get("/time-capsules/{month}", response_model=TimeCapsule)
async def time_capsule(
    request: Request,
    identity: CurrentIdentity,
    month: str,
    timezone: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
):
    return await InsightsService(request.app.state.database).get_capsule(
        identity.user_id, month, timezone
    )


@router.post(
    "/time-capsules/{month}/refresh",
    response_model=TimeCapsuleRefreshAccepted,
    status_code=202,
)
async def refresh_time_capsule(
    request: Request,
    identity: CurrentIdentity,
    month: str,
    timezone: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
):
    return await InsightsService(request.app.state.database).request_refresh(
        identity.user_id, month, timezone
    )
