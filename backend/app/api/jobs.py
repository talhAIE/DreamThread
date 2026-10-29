from uuid import UUID

from fastapi import APIRouter, Request

from app.api.dependencies import CurrentIdentity
from app.api.dreams import IdempotencyKey
from app.schemas.jobs import JobRetry, JobView
from app.services.jobs import JobService

router = APIRouter(prefix="/jobs", tags=["Jobs"])


@router.get("/{job_id}", response_model=JobView)
async def get(request: Request, identity: CurrentIdentity, job_id: UUID):
    return await JobService(request.app.state.database, request.app.state.settings).get(
        identity, job_id
    )


@router.post("/{job_id}/retry", response_model=JobView, status_code=202)
async def retry(
    request: Request,
    identity: CurrentIdentity,
    job_id: UUID,
    payload: JobRetry,
    idempotency_key: IdempotencyKey,
):
    return await JobService(request.app.state.database, request.app.state.settings).retry(
        identity,
        job_id,
        payload,
        idempotency_key,
    )
