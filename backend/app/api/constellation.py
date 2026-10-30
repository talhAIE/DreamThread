"""FastAPI routes for the Phase 10 Constellation."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from app.api.dependencies import CurrentIdentity
from app.schemas.constellation import (
    ConstellationGraph,
    MotifDetail,
    MotifResponse,
    StorylineDetail,
    StorylineListItem,
    StorylineRefreshAccepted,
)
from app.services.constellation import ConstellationService

router = APIRouter(tags=["Constellation"])


def _service(request: Request) -> ConstellationService:
    return ConstellationService(request.app.state.database)


@router.get("/constellation", response_model=ConstellationGraph)
async def graph(
    request: Request,
    identity: CurrentIdentity,
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
    types: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
):
    return await _service(request).get_graph(
        identity.user_id,
        from_date=from_date,
        to_date=to_date,
        types=types,
        limit=limit,
    )


@router.get(
    "/dreams/{dream_id}/constellation",
    response_model=list[MotifResponse],
    tags=["Constellation"],
)
async def get_constellation(
    dream_id: UUID,
    request: Request,
    identity: CurrentIdentity,
):
    """Return motifs linked to a given dream for the requesting user."""
    return await _service(request).get_constellation(identity.user_id, dream_id)


@router.get("/motifs/{motif_id}", response_model=MotifDetail)
async def motif_detail(request: Request, identity: CurrentIdentity, motif_id: UUID):
    return await _service(request).get_motif(identity.user_id, motif_id)


@router.get("/storylines", response_model=list[StorylineListItem])
async def list_storylines(request: Request, identity: CurrentIdentity):
    return await _service(request).list_storylines(identity.user_id)


@router.get("/storylines/{storyline_id}", response_model=StorylineDetail)
async def storyline_detail(
    request: Request,
    identity: CurrentIdentity,
    storyline_id: UUID,
):
    return await _service(request).get_storyline(identity.user_id, storyline_id)


@router.post(
    "/storylines/{storyline_id}/refresh",
    response_model=StorylineRefreshAccepted,
    status_code=202,
)
async def refresh_storyline(
    request: Request,
    identity: CurrentIdentity,
    storyline_id: UUID,
):
    return await _service(request).enqueue_storyline_refresh(
        identity.user_id, storyline_id
    )
