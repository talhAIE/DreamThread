"""Private Supabase object storage. Keys are server-generated, never arbitrary URLs."""

import re
from uuid import UUID

import httpx

from app.workers.types import WorkFailure

KEY = re.compile(r"^([0-9a-f-]{36})/([0-9a-f-]{36})/source$")


def validate_key(key, user_id=None):
    match = KEY.fullmatch(key)
    if not match:
        raise WorkFailure("storage_key_invalid")
    try:
        owner, asset = UUID(match[1]), UUID(match[2])
    except ValueError:
        raise WorkFailure("storage_key_invalid") from None
    if (
        str(owner) != match[1]
        or str(asset) != match[2]
        or (user_id is not None and owner != user_id)
    ):
        raise WorkFailure("storage_key_invalid")
    return key


class PrivateStorage:
    def __init__(self, settings, *, transport=None):
        self.settings, self.transport = settings, transport

    def client(self):
        s = self.settings
        if not s.supabase_url or not s.supabase_secret_key:
            raise WorkFailure("storage_unavailable", retryable=True)
        return httpx.AsyncClient(
            base_url=s.supabase_url + "/storage/v1/",
            headers={"apikey": s.supabase_secret_key.get_secret_value()},
            transport=self.transport,
            timeout=s.storage_timeout_seconds,
            trust_env=False,
            follow_redirects=False,
        )

    def check(self, response):
        if response.status_code not in {200, 201, 204}:
            raise WorkFailure(
                "storage_unavailable",
                retryable=response.status_code == 429 or response.status_code >= 500,
            )

    async def provision(self):
        s = self.settings
        async with self.client() as client:
            response = await client.get("bucket/" + s.supabase_audio_bucket)
            if response.status_code == 404 or (
                response.status_code == 400 and response.json().get("error") == "Bucket not found"
            ):
                response = await client.post(
                    "bucket",
                    json={
                        "id": s.supabase_audio_bucket,
                        "name": s.supabase_audio_bucket,
                        "public": False,
                        "file_size_limit": s.audio_max_bytes,
                        "allowed_mime_types": [
                            "audio/wav",
                            "audio/webm",
                            "audio/ogg",
                            "audio/mpeg",
                            "audio/mp4",
                        ],
                    },
                )
                self.check(response)
                response = await client.get("bucket/" + s.supabase_audio_bucket)
            self.check(response)
            data = response.json()
            if data.get("public") is not False:
                raise WorkFailure("storage_configuration")

    async def put(self, key, data, mime):
        validate_key(key)
        if len(data) > self.settings.audio_max_bytes:
            raise WorkFailure("invalid_audio")
        async with self.client() as client:
            response = await client.post(
                "object/" + self.settings.supabase_audio_bucket + "/" + key,
                content=data,
                headers={"Content-Type": mime, "x-upsert": "false"},
            )
            self.check(response)

    async def get(self, key):
        validate_key(key)
        async with self.client() as client:
            async with client.stream(
                "GET", "object/" + self.settings.supabase_audio_bucket + "/" + key
            ) as response:
                self.check(response)
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > self.settings.audio_max_bytes:
                        raise WorkFailure("invalid_audio")
                return bytes(data)

    async def remove(self, keys):
        if not keys:
            return
        for key in keys:
            validate_key(key)
        async with self.client() as client:
            response = await client.request(
                "DELETE", "object/" + self.settings.supabase_audio_bucket, json={"prefixes": keys}
            )
            self.check(response)
