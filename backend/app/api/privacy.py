from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response

from app.api.dependencies import CurrentIdentity
from app.errors import APIError
from app.schemas.privacy import (
    ExportAccepted,
    ExportCreate,
    ExportStatus,
    JournalDeleteAccepted,
    JournalDeleteConfirmation,
)
from app.services.privacy import PrivacyService

router = APIRouter(tags=["Privacy and exports"])
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


@router.post("/exports", response_model=ExportAccepted, status_code=202)
async def create_export(
    request: Request,
    identity: CurrentIdentity,
    payload: ExportCreate,
    idempotency_key: IdempotencyKey,
):
    return await PrivacyService(
        request.app.state.database, request.app.state.settings
    ).create_export(identity, payload, idempotency_key)


@router.get("/exports/{export_id}", response_model=ExportStatus)
async def get_export(request: Request, identity: CurrentIdentity, export_id: UUID):
    result = await PrivacyService(request.app.state.database).export_status(
        identity.user_id, export_id
    )
    if result.download_url:
        result = result.model_copy(
            update={"download_url": request.app.state.settings.api_prefix + result.download_url}
        )
    return result


@router.get("/exports/{export_id}/download")
async def download_export(request: Request, identity: CurrentIdentity, export_id: UUID):
    file_format, content, status = await PrivacyService(
        request.app.state.database
    ).download_export(identity.user_id, export_id)
    if status == "expired":
        raise APIError("export_expired", "Export has expired", 410)
    if status == "stale":
        raise APIError("export_stale", "Journal changed; request a new export", 409)
    suffix, media_type = (
        ("json", "application/json; charset=utf-8")
        if file_format == "json"
        else ("txt", "text/plain; charset=utf-8")
    )
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="dreamthread-{export_id}.{suffix}"'},
    )


@router.delete("/me/journal", response_model=JournalDeleteAccepted, status_code=202)
async def delete_journal(
    request: Request,
    identity: CurrentIdentity,
    payload: JournalDeleteConfirmation,
):
    if payload.confirm is not True:
        raise APIError("confirmation_required", "Explicit confirmation is required", 422)
    return await PrivacyService(request.app.state.database).request_journal_deletion(
        identity.user_id
    )


@router.get("/me/journal/deletion")
async def journal_deletion_status(request: Request, identity: CurrentIdentity):
    return await PrivacyService(request.app.state.database).deletion_status(identity.user_id)
