"""FastAPI router for Dream Continuity Engine (Phase 9)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from app.api.dependencies import CurrentIdentity
from app.schemas.continuity import (
    ConnectionEvidenceListResponse,
    ConnectionListResponse,
    ConnectionPatchRequest,
    ConnectionRead,
)
from app.services.continuity import ContinuityService

router = APIRouter(tags=["Continuity"])


def service(request: Request) -> ContinuityService:
    provider = getattr(request.app.state, "nebius", None)
    return ContinuityService(
        request.app.state.database, request.app.state.settings, provider=provider
    )


@router.get("/dreams/{dream_id}/connections", response_model=ConnectionListResponse)
async def get_dream_connections(
    request: Request,
    dream_id: UUID,
    identity: CurrentIdentity,
    include_rejected: Annotated[bool, Query()] = False,
):
    """Retrieve verified connections for a given dream entry."""
    return await service(request).get_connections_for_dream(
        identity.user_id, dream_id, include_rejected=include_rejected
    )


@router.post("/dreams/{dream_id}/connections/analyze", response_model=ConnectionListResponse)
async def analyze_dream_connections(
    request: Request,
    dream_id: UUID,
    identity: CurrentIdentity,
):
    """Trigger continuity analysis connecting this dream with historical entries."""
    return await service(request).analyze_connections(identity.user_id, dream_id)


@router.patch("/connections/{connection_id}", response_model=ConnectionRead)
async def patch_connection(
    request: Request,
    connection_id: UUID,
    body: ConnectionPatchRequest,
    identity: CurrentIdentity,
):
    """Confirm or reject a suggested connection with optimistic concurrency control."""
    return await service(request).patch_connection(identity.user_id, connection_id, body)


@router.get("/connections/{connection_id}/evidence", response_model=ConnectionEvidenceListResponse)
async def get_connection_evidence(
    request: Request,
    connection_id: UUID,
    identity: CurrentIdentity,
):
    """Inspect verifiable text quotes and evidence citations supporting a connection."""
    return await service(request).get_connection_evidence(identity.user_id, connection_id)
