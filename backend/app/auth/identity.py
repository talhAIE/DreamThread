"""Validate user access tokens against the configured Supabase project's Auth service."""

import re
from dataclasses import dataclass
from uuid import UUID

import httpx

from app.config import Settings
from app.errors import APIError

BEARER = re.compile(r"^Bearer ([A-Za-z0-9._~+/-]+=*)$", re.IGNORECASE)


@dataclass(frozen=True)
class Identity:
    user_id: UUID
    is_anonymous: bool = False


def unauthorized():
    return APIError("unauthenticated", "A valid access token is required", 401,
                    headers={"WWW-Authenticate": "Bearer"})


class SupabaseIdentity:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            timeout=settings.auth_timeout_seconds, follow_redirects=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            trust_env=False,
        )

    async def verify(self, authorization: str | None) -> Identity:
        if not authorization or len(authorization) > 8192:
            raise unauthorized()
        match = BEARER.fullmatch(authorization)
        if not match:
            raise unauthorized()
        config = self.settings
        if not config.supabase_url or not config.supabase_publishable_key:
            raise APIError("auth_unavailable", "Authentication is unavailable", 503)
        try:
            response = await self.client.get(
                config.supabase_url + "/auth/v1/user",
                headers={"Authorization": "Bearer " + match[1],
                         "apikey": config.supabase_publishable_key.get_secret_value()},
                timeout=config.auth_timeout_seconds,
                follow_redirects=False,
            )
        except httpx.RequestError:
            raise APIError("auth_unavailable", "Authentication is unavailable", 503) from None
        if response.status_code in {401, 403}:
            raise unauthorized()
        if response.status_code != 200:
            raise APIError("auth_unavailable", "Authentication is unavailable", 503)
        try:
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("id"), str):
                raise ValueError
            user_id = UUID(data["id"])
            anonymous = data.get("is_anonymous", False)
            if type(anonymous) is not bool or data.get("role") != "authenticated":
                raise ValueError
            if data.get("deleted_at"):
                raise unauthorized()
        except (ValueError, KeyError, TypeError):
            raise APIError("auth_unavailable", "Authentication is unavailable", 503) from None
        if anonymous and not config.demo_enabled:
            raise APIError("anonymous_disabled", "Sample accounts are disabled", 403)
        # Never trust user_metadata for tenant identity, permissions or anonymous status.
        return Identity(user_id=user_id, is_anonymous=anonymous)

    async def close(self):
        await self.client.aclose()
