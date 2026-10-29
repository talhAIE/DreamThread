from fastapi import APIRouter, Request

from app.api.dependencies import CurrentIdentity
from app.schemas.identity import Me, MePatch, Preferences, PreferencesPatch
from app.services.accounts import AccountService

router = APIRouter(tags=["Account"])


@router.get("/me", response_model=Me)
async def get_me(request: Request, identity: CurrentIdentity):
    return await AccountService(request.app.state.database).read(identity)


@router.patch("/me", response_model=Me)
async def patch_me(request: Request, identity: CurrentIdentity, payload: MePatch):
    return await AccountService(request.app.state.database).patch(identity, payload)


@router.get("/me/preferences", response_model=Preferences)
async def get_preferences(request: Request, identity: CurrentIdentity):
    return await AccountService(request.app.state.database).read(identity, preferences=True)


@router.patch("/me/preferences", response_model=Preferences)
async def patch_preferences(request: Request, identity: CurrentIdentity, payload: PreferencesPatch):
    return await AccountService(request.app.state.database).patch(
        identity, payload, preferences=True,
    )
