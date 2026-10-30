from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response

from app.api.dependencies import CurrentIdentity
from app.schemas.detective import (
    ConversationCreate,
    ConversationDetail,
    ConversationList,
    ConversationMessage,
    ConversationMessages,
    ConversationRead,
    DetectiveQuestion,
    DetectiveSubmission,
)
from app.services.detective import DetectiveService

router = APIRouter(prefix="/conversations", tags=["Dream Detective"])
messages_router = APIRouter(tags=["Dream Detective"])
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


def service(request):
    return DetectiveService(request.app.state.database, request.app.state.settings)


@router.post("", response_model=ConversationRead, status_code=201)
async def create(
    request: Request,
    identity: CurrentIdentity,
    payload: ConversationCreate | None = None,
):
    return await service(request).create_conversation(
        identity, payload.title if payload is not None else None
    )


@router.get("", response_model=ConversationList)
async def listing(
    request: Request,
    identity: CurrentIdentity,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    return await service(request).list_conversations(identity, limit)


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def detail(request: Request, identity: CurrentIdentity, conversation_id: UUID):
    return await service(request).get_conversation(identity, conversation_id)


@router.delete("/{conversation_id}", status_code=204)
async def delete(request: Request, identity: CurrentIdentity, conversation_id: UUID):
    await service(request).delete_conversation(identity, conversation_id)
    return Response(status_code=204)


@router.get(
    "/{conversation_id}/messages",
    response_model=ConversationMessages,
)
async def list_messages(
    request: Request,
    identity: CurrentIdentity,
    conversation_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return await service(request).get_conversation_messages(
        identity, conversation_id, limit=limit, offset=offset
    )


@router.post(
    "/{conversation_id}/messages",
    response_model=DetectiveSubmission,
    status_code=202,
)
async def message(
    request: Request,
    identity: CurrentIdentity,
    conversation_id: UUID,
    payload: DetectiveQuestion,
    idempotency_key: IdempotencyKey,
):
    return await service(request).submit(
        identity,
        conversation_id,
        payload.question,
        idempotency_key,
        source_ids=payload.source_ids,
        from_date=payload.from_date,
        to_date=payload.to_date,
        motif_ids=payload.motif_ids,
    )


@messages_router.get("/messages/{message_id}", response_model=ConversationMessage)
async def get_message(request: Request, identity: CurrentIdentity, message_id: UUID):
    return await service(request).get_message(identity, message_id)
