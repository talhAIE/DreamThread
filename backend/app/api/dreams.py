from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response

from app.api.dependencies import CurrentIdentity
from app.schemas.dreams import (
    AnalysisRequestResult,
    AnalysisResult,
    AnalyzeRequest,
    DreamCreate,
    DreamDetail,
    DreamPage,
    DreamPatch,
    DreamQuery,
    Revision,
)
from app.schemas.memory import DetailPatch, DetailResult, FollowupPut, FollowupView
from app.schemas.retrieval import CandidateResult, IndexRequest, IndexResult
from app.services.corrections import CorrectionService
from app.services.indexing import IndexService
from app.services.journal import JournalService
from app.services.retrieval import RetrievalService

router = APIRouter(prefix="/dreams", tags=["Journal"])
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


def service(request):
    return JournalService(request.app.state.database, request.app.state.settings)


@router.post("", response_model=DreamDetail, status_code=201)
async def create(
    request: Request,
    identity: CurrentIdentity,
    payload: DreamCreate,
    idempotency_key: IdempotencyKey,
):
    return await service(request).create(identity, payload, idempotency_key)


@router.get("", response_model=DreamPage)
async def listing(
    request: Request, identity: CurrentIdentity, params: Annotated[DreamQuery, Query()]
):
    return await service(request).list(identity, params)


@router.get("/{dream_id}", response_model=DreamDetail)
async def detail(request: Request, identity: CurrentIdentity, dream_id: UUID):
    return await service(request).get(identity, dream_id)


@router.patch("/{dream_id}", response_model=DreamDetail)
async def patch(request: Request, identity: CurrentIdentity, dream_id: UUID, payload: DreamPatch):
    return await service(request).patch(identity, dream_id, payload)


@router.get("/{dream_id}/revisions/{revision_id}", response_model=Revision)
async def revision(request: Request, identity: CurrentIdentity, dream_id: UUID, revision_id: UUID):
    return await service(request).revision(identity, dream_id, revision_id)


@router.post("/{dream_id}/analyze", response_model=AnalysisRequestResult, status_code=202)
async def analyze(
    request: Request,
    identity: CurrentIdentity,
    dream_id: UUID,
    payload: AnalyzeRequest,
    idempotency_key: IdempotencyKey,
):
    return await service(request).analyze(identity, dream_id, payload, idempotency_key)


@router.get("/{dream_id}/analysis", response_model=AnalysisResult)
async def analysis(request: Request, identity: CurrentIdentity, dream_id: UUID):
    return await service(request).analysis(identity, dream_id)


@router.delete("/{dream_id}", status_code=204)
async def remove(request: Request, identity: CurrentIdentity, dream_id: UUID):
    await service(request).remove(identity, dream_id)
    return Response(status_code=204)


@router.post("/{dream_id}/index", response_model=IndexResult, status_code=202)
async def index_dream(
    request: Request,
    identity: CurrentIdentity,
    dream_id: UUID,
    payload: IndexRequest,
    idempotency_key: IdempotencyKey,
):
    return await IndexService(request.app.state.database, request.app.state.settings).request(
        identity,
        dream_id,
        payload.expected_version,
        idempotency_key,
    )


@router.get("/{dream_id}/candidates", response_model=CandidateResult)
async def candidates(request: Request, identity: CurrentIdentity, dream_id: UUID):
    return await RetrievalService(
        request.app.state.database, request.app.state.settings
    ).candidates(
        identity,
        dream_id,
    )


@router.patch("/{dream_id}/details", response_model=DetailResult)
async def details(
    request: Request,
    identity: CurrentIdentity,
    dream_id: UUID,
    payload: DetailPatch,
    idempotency_key: IdempotencyKey,
):
    return await CorrectionService(request.app.state.database, request.app.state.settings).details(
        identity, dream_id, payload, idempotency_key
    )


@router.get("/{dream_id}/followup", response_model=FollowupView)
async def get_followup(request: Request, identity: CurrentIdentity, dream_id: UUID):
    return await CorrectionService(
        request.app.state.database, request.app.state.settings
    ).get_followup(identity, dream_id)


@router.put("/{dream_id}/followup", response_model=FollowupView)
async def put_followup(
    request: Request,
    identity: CurrentIdentity,
    dream_id: UUID,
    payload: FollowupPut,
    idempotency_key: IdempotencyKey,
):
    return await CorrectionService(
        request.app.state.database, request.app.state.settings
    ).put_followup(identity, dream_id, payload, idempotency_key)
