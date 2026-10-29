'''FastAPI router for Constellation (Phase 10).'''

from fastapi import APIRouter, Request
from uuid import UUID

from app.api.dependencies import CurrentIdentity
from app.schemas.constellation import MotifResponse
from app.services.constellation import ConstellationService

router = APIRouter(tags=["Constellation"])

def _service(request: Request) -> ConstellationService:
    return ConstellationService(request.app.state.database)

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
