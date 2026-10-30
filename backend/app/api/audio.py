from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response

from app.api.dependencies import CurrentIdentity
from app.schemas.audio import AudioStatus, UploadGranted, UploadIntent
from app.services.audio import AudioService

router = APIRouter(prefix="/audio", tags=["Voice capture"])


def service(request):
    return AudioService(request.app.state.database, request.app.state.settings)


@router.post("/upload-intents", response_model=UploadGranted, status_code=201)
async def intent(
    request: Request,
    identity: CurrentIdentity,
    payload: UploadIntent,
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
):
    return await service(request).intent(identity, payload, key)


@router.put("/{asset_id}/upload", status_code=204)
async def upload(
    request: Request,
    identity: CurrentIdentity,
    asset_id: UUID,
    deadline: Annotated[int, Query(gt=0)],
    signature: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
):
    await service(request).upload(
        identity,
        asset_id,
        deadline,
        signature,
        await request.body(),
        request.headers.get("content-type", ""),
    )
    return Response(status_code=204)


@router.post("/{asset_id}/complete", response_model=AudioStatus, status_code=202)
async def complete(request: Request, identity: CurrentIdentity, asset_id: UUID):
    return await service(request).complete(identity, asset_id)


@router.get("/{asset_id}", response_model=AudioStatus)
async def status(request: Request, identity: CurrentIdentity, asset_id: UUID):
    return await service(request).get(identity, asset_id)


@router.delete("/{asset_id}", status_code=204)
async def discard(request: Request, identity: CurrentIdentity, asset_id: UUID):
    await service(request).discard(identity, asset_id)
    return Response(status_code=204)
