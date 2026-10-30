from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError

from app.auth.identity import Identity, unauthorized
from app.errors import APIError
from app.services.accounts import AccountService
from app.services.rate_limits import enforce_user_request_limit

bearer = HTTPBearer(auto_error=False)


async def current_identity(
    request: Request,
    _credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Identity:
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        raise unauthorized()
    identity = await request.app.state.authenticator.verify(values[0])
    try:
        await AccountService(request.app.state.database).ensure(identity)
        await enforce_user_request_limit(
            request.app.state.database,
            identity.user_id,
            request.app.state.settings.user_request_rate_limit,
        )
    except SQLAlchemyError:
        raise APIError("database_unavailable", "Data service is unavailable", 503) from None
    return identity


CurrentIdentity = Annotated[Identity, Depends(current_identity)]
