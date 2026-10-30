import asyncio
import hashlib
import io
import json
import wave
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.body_limit import BodyLimitMiddleware
from app.config import Settings
from app.errors import APIError, RequestContextMiddleware
from app.providers.storage import PrivateStorage, validate_key
from app.schemas.audio import UploadIntent
from app.schemas.dreams import DreamCreate
from app.services.audio import AudioService, upload_signature
from app.workers.types import WorkFailure


def settings(**values):
    return Settings(
        _env_file=None,
        database_url="postgresql://u:p@localhost/test",
        supabase_url="https://test.supabase.co",
        supabase_secret_key="sb_secret_test",
        **values,
    )


@pytest.mark.parametrize(
    "key",
    [
        "../other/source",
        "https://example.com/audio",
        "u/a/source",
        "",
        f"{uuid4()}/{uuid4()}/source/../source",
    ],
)
def test_storage_rejects_untrusted_keys_before_http(key):
    with pytest.raises(WorkFailure):
        validate_key(key)


def test_storage_key_is_bound_to_owner():
    owner, other, asset = uuid4(), uuid4(), uuid4()
    key = f"{owner}/{asset}/source"
    assert validate_key(key, owner) == key
    with pytest.raises(WorkFailure):
        validate_key(key, other)


def test_signed_upload_permission_binds_user_asset_and_expiry():
    s, owner, asset, deadline = settings(), uuid4(), uuid4(), 1234567
    token = upload_signature(s, owner, asset, deadline)
    assert token != upload_signature(s, uuid4(), asset, deadline)
    assert token != upload_signature(s, owner, uuid4(), deadline)
    assert token != upload_signature(s, owner, asset, deadline + 1)
    assert len(token) == 64


def test_expired_upload_cannot_touch_database_or_storage():
    service = AudioService(object(), settings(voice_enabled=True, voice_verified=True))
    from app.auth.identity import Identity

    with pytest.raises(APIError) as error:
        asyncio.run(service.upload(Identity(uuid4()), uuid4(), 1, "0" * 64, b"data", "audio/wav"))
    assert error.value.code == "upload_expired"


def test_storage_http_does_not_send_secret_to_redirect_target_and_bounds_download():
    calls = []

    def transport(request):
        calls.append(request)
        assert request.headers["apikey"] == "sb_secret_test"
        assert request.url.host == "test.supabase.co"
        return httpx.Response(302, headers={"Location": "https://attacker.invalid"})

    store = PrivateStorage(settings(), transport=httpx.MockTransport(transport))
    with pytest.raises(WorkFailure):
        asyncio.run(store.get(f"{uuid4()}/{uuid4()}/source"))
    assert len(calls) == 1
    store = PrivateStorage(
        settings(audio_max_bytes=1024),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 1025)),
    )
    with pytest.raises(WorkFailure) as error:
        asyncio.run(store.get(f"{uuid4()}/{uuid4()}/source"))
    assert error.value.code == "invalid_audio"


def test_storage_upload_is_immutable_and_deletion_is_idempotent():
    key, calls = f"{uuid4()}/{uuid4()}/source", []

    def transport(request):
        calls.append(request)
        if request.method == "POST":
            assert request.headers["x-upsert"] == "false"
            assert request.content == b"synthetic audio"
        else:
            assert json.loads(request.content) == {"prefixes": [key]}
        return httpx.Response(200, json=[])

    async def run():
        store = PrivateStorage(settings(), transport=httpx.MockTransport(transport))
        await store.put(key, b"synthetic audio", "audio/wav")
        await store.remove([key])
        await store.remove([key])

    asyncio.run(run())
    assert len(calls) == 3


def test_existing_public_bucket_is_rejected():
    store = PrivateStorage(
        settings(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"public": True})),
    )
    with pytest.raises(WorkFailure) as error:
        asyncio.run(store.provision())
    assert error.value.code == "storage_configuration"


def test_upload_body_limit_is_separate_from_json_and_checks_streamed_bytes():
    app = FastAPI()
    app.add_middleware(
        BodyLimitMiddleware, max_bytes=10, audio_prefix="/audio/", audio_max_bytes=20
    )
    app.add_middleware(RequestContextMiddleware)

    @app.put("/audio/{asset}/upload")
    async def upload(request: Request):
        return {"size": len(await request.body())}

    @app.post("/dreams")
    async def dream(request: Request):
        return {"size": len(await request.body())}

    with TestClient(app) as client:
        assert client.put("/audio/id/upload", content=b"x" * 15).status_code == 200
        assert client.post("/dreams", content=b"x" * 15).status_code == 413
        assert client.put("/audio/id/upload", content=b"x" * 21).status_code == 413
        assert (
            client.put("/audio/id/upload", content=iter([b"x" * 11, b"y" * 10])).status_code == 413
        )


def test_voice_save_requires_reviewed_text_and_exactly_one_owned_asset_reference():
    day = datetime.now(UTC).date()
    DreamCreate(dream_date=day, text="Reviewed wording", input_mode="voice", audio_asset_id=uuid4())
    for values in (
        {"input_mode": "voice"},
        {"input_mode": "text", "audio_asset_id": uuid4()},
        {"input_mode": "voice", "audio_asset_id": uuid4(), "text": ""},
    ):
        with pytest.raises(ValueError):
            DreamCreate(dream_date=day, text=values.pop("text", "Reviewed"), **values)


def test_raw_audio_status_never_publishes_transcript_before_reviewable_ready_state():
    owner, asset = uuid4(), uuid4()
    service = AudioService(object(), settings())
    row = {
        "id": asset,
        "user_id": owner,
        "lifecycle": "queued",
        "expires_at": datetime.now(UTC) + timedelta(minutes=1),
        "verified_metadata": {"transcript": "private", "sha256": hashlib.sha256(b"x").hexdigest()},
    }
    assert service.status(row).transcript_draft is None
    assert service.status(dict(row, lifecycle="ready")).transcript_draft == "private"


def test_real_decoder_rejects_oversized_duration_before_model_load(tmp_path):
    pytest.importorskip("av")
    from app.workers.speech_job import transcribe

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 32000)
    source = tmp_path / "source.wav"
    source.write_bytes(buffer.getvalue())
    with pytest.raises(ValueError, match="invalid_audio"):
        transcribe(source, "tiny", 1)


def test_upload_metadata_limits_reject_unknown_types_and_excess_size():
    for values in (
        {"content_type": "text/html", "size_bytes": 100},
        {"content_type": "audio/wav", "size_bytes": 10485761},
    ):
        with pytest.raises(ValueError):
            UploadIntent(**values)
