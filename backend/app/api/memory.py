from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response

from app.api.dependencies import CurrentIdentity
from app.schemas.memory import (
    Decision,
    MemoryCreate,
    MemoryPage,
    MemoryPatch,
    MemoryView,
    MotifPage,
    MotifType,
)
from app.services.memory import MemoryService

router = APIRouter(tags=["Personal memory"])
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


def service(request):
    return MemoryService(request.app.state.database, request.app.state.settings)


@router.get("/motifs", response_model=MotifPage)
async def motifs(
    request: Request,
    identity: CurrentIdentity,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    after: UUID | None = None,
    type: MotifType | None = None,
):
    return await service(request).motifs(identity, limit, after, type)


@router.get("/memory", response_model=MemoryPage)
async def memory(
    request: Request,
    identity: CurrentIdentity,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    after: UUID | None = None,
    status: Decision | None = None,
):
    return await service(request).list(identity, limit, after, status)


@router.post("/memory", response_model=MemoryView, status_code=201)
async def create(
    request: Request, identity: CurrentIdentity, payload: MemoryCreate, idempotency_key: Key
):
    return await service(request).create(identity, payload, idempotency_key)


@router.patch("/memory/{memory_id}", response_model=MemoryView)
async def patch(request: Request, identity: CurrentIdentity, memory_id: UUID, payload: MemoryPatch):
    return await service(request).patch(identity, memory_id, payload)


@router.delete("/memory/{memory_id}", status_code=204)
async def delete(
    request: Request,
    identity: CurrentIdentity,
    memory_id: UUID,
    expected_version: Annotated[int, Query(ge=1)],
):
    await service(request).delete(identity, memory_id, expected_version)
    return Response(status_code=204)
