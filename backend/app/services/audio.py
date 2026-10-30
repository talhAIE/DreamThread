"""Owner-scoped upload intents, reviewed transcripts and epoch-fenced ASR publication."""

import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, update

from app.db.models import tables
from app.errors import APIError
from app.providers.storage import PrivateStorage
from app.schemas.audio import AudioStatus, UploadGranted
from app.services.accounts import active_account
from app.services.admission import reserve_resource
from app.workers.types import LeaseLost, SourceChanged, WorkFailure

A, ASSETS, J, CLEANUP = (
    tables["app." + n] for n in ("accounts", "audio_assets", "jobs", "cleanup_tasks")
)


def upload_signature(settings, user_id, asset_id, deadline):
    message = f"dreamthread-audio-v1:{user_id}:{asset_id}:{deadline}".encode()
    return hmac.new(
        settings.supabase_secret_key.get_secret_value().encode(), message, hashlib.sha256
    ).hexdigest()


async def speech(data, settings):
    def run():
        with tempfile.TemporaryDirectory(prefix="dreamthread-asr-") as directory:
            source, output = Path(directory) / "source", Path(directory) / "result.json"
            source.write_bytes(data)
            # subprocess.run enforces a hard decoder/model deadline and kills the child;
            # SelectorEventLoop on Windows cannot directly spawn async subprocesses.
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "app.workers.speech_job",
                        str(source),
                        str(output),
                        "--model",
                        settings.asr_model,
                        "--max-seconds",
                        str(settings.audio_max_duration_seconds),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=settings.asr_timeout_seconds,
                    check=False,
                    cwd=Path(__file__).resolve().parents[2],
                    env={
                        key: value
                        for key, value in os.environ.items()
                        if key.upper()
                        in {
                            "PATH",
                            "SYSTEMROOT",
                            "WINDIR",
                            "TEMP",
                            "TMP",
                            "USERPROFILE",
                            "HOME",
                            "LOCALAPPDATA",
                            "APPDATA",
                            "HF_HOME",
                            "HF_HUB_CACHE",
                            "HF_HUB_OFFLINE",
                        }
                    },
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except subprocess.TimeoutExpired:
                raise WorkFailure("speech_timeout", retryable=True) from None
            if result.returncode or not output.exists() or output.stat().st_size > 131072:
                raise WorkFailure("speech_unavailable", retryable=True)
            value = json.loads(output.read_text(encoding="utf-8"))
            if "error_code" in value:
                raise WorkFailure(
                    value["error_code"], retryable=value["error_code"] != "invalid_audio"
                )
            return value

    return await asyncio.to_thread(run)


class AudioService:
    def __init__(self, db, settings, storage=None, transcriber=None):
        self.db, self.settings = db, settings
        self.storage = storage or PrivateStorage(settings)
        self.transcriber = transcriber or speech

    def enabled(self):
        if not self.settings.voice_enabled:
            raise APIError("voice_unavailable", "Voice capture is unavailable", 503)

    async def asset(self, c, user_id, asset_id):
        row = (
            (
                await c.execute(
                    select(ASSETS)
                    .where(ASSETS.c.user_id == user_id, ASSETS.c.id == asset_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise APIError("not_found", "Audio not found", 404)
        return row

    async def intent(self, identity, payload, key):
        self.enabled()
        if payload.size_bytes > self.settings.audio_max_bytes:
            raise APIError("invalid_audio", "Audio exceeds the upload limit", 413)
        async with self.db.tenant_transaction(identity.user_id) as c:
            account = await active_account(c, identity.user_id)
            admitted = await reserve_resource(
                c,
                identity,
                self.settings,
                route="audio.intent",
                key=key,
                body=payload.model_dump(mode="json"),
            )
            if admitted.replayed:
                asset = await self.asset(c, identity.user_id, admitted.resource_id)
                if asset["lifecycle"] != "pending" or asset["expires_at"] <= datetime.now(UTC):
                    raise APIError("intent_expired", "Create a new upload intent", 409)
                return self.granted(asset)
            asset_id = admitted.resource_id
            expiry = datetime.now(UTC) + timedelta(
                seconds=self.settings.audio_upload_intent_ttl_seconds
            )
            storage_key = f"{identity.user_id}/{asset_id}/source"
            metadata = {
                "data_epoch": account["data_epoch"],
                "content_type": payload.content_type,
                "size_bytes": payload.size_bytes,
            }
            await c.execute(
                insert(ASSETS).values(
                    id=asset_id,
                    user_id=identity.user_id,
                    storage_key=storage_key,
                    verified_metadata=metadata,
                    expires_at=expiry,
                )
            )
            # Persist the object key BEFORE any upload. Delay cleanup past every valid upload
            # plus the bounded in-flight storage request, even if the API crashes after PUT.
            await c.execute(
                insert(CLEANUP).values(
                    user_id=identity.user_id,
                    operation_key=f"audio:{asset_id}",
                    storage_keys=[storage_key],
                    state="queued",
                    not_before=expiry
                    + timedelta(seconds=self.settings.storage_timeout_seconds + 60),
                )
            )
            return self.granted({"id": asset_id, "user_id": identity.user_id, "expires_at": expiry})

    def granted(self, asset):
        deadline = int(asset["expires_at"].timestamp())
        token = upload_signature(self.settings, asset["user_id"], asset["id"], deadline)
        return UploadGranted(
            asset_id=asset["id"],
            expires_at=asset["expires_at"],
            upload_url=f"{self.settings.api_prefix}/audio/{asset['id']}/upload"
            f"?deadline={deadline}&signature={token}",
            max_bytes=self.settings.audio_max_bytes,
        )

    async def upload(self, identity, asset_id, deadline, signature, data, content_type):
        self.enabled()
        expected = upload_signature(self.settings, identity.user_id, asset_id, deadline)
        if deadline <= datetime.now(UTC).timestamp() or not hmac.compare_digest(
            signature, expected
        ):
            raise APIError("upload_expired", "Upload permission expired or is invalid", 403)
        async with self.db.tenant_transaction(identity.user_id) as c:
            account = await active_account(c, identity.user_id)
            asset = await self.asset(c, identity.user_id, asset_id)
            meta = asset["verified_metadata"]
            if asset["lifecycle"] != "pending" or meta["data_epoch"] != account["data_epoch"]:
                raise APIError("upload_conflict", "Upload cannot be used again", 409)
            if int(asset["expires_at"].timestamp()) != deadline:
                raise APIError("upload_expired", "Upload permission is invalid", 403)
            if len(data) != meta["size_bytes"] or content_type != meta["content_type"]:
                raise APIError("invalid_audio", "Audio does not match the upload intent", 422)
            await c.execute(
                update(ASSETS).where(ASSETS.c.id == asset_id).values(lifecycle="uploading")
            )
        try:
            await self.storage.put(asset["storage_key"], data, content_type)
        except Exception:
            # The pre-existing outbox guarantees later cleanup even on unknown PUT outcome.
            raise APIError("upload_failed", "Create a new upload intent and retry", 503) from None
        async with self.db.tenant_transaction(identity.user_id) as c:
            account = await active_account(c, identity.user_id)
            current = await self.asset(c, identity.user_id, asset_id)
            if (
                account["data_epoch"] != meta["data_epoch"]
                or current["lifecycle"] != "uploading"
                or current["expires_at"] <= datetime.now(UTC)
            ):
                raise APIError("upload_expired", "Create a new upload intent", 409)
            await c.execute(
                update(ASSETS)
                .where(ASSETS.c.id == asset_id)
                .values(
                    lifecycle="uploaded",
                    verified_metadata={**meta, "sha256": hashlib.sha256(data).hexdigest()},
                )
            )

    async def complete(self, identity, asset_id):
        self.enabled()
        async with self.db.tenant_transaction(identity.user_id) as c:
            account = await active_account(c, identity.user_id)
            asset = await self.asset(c, identity.user_id, asset_id)
            meta = asset["verified_metadata"]
            if asset["lifecycle"] in {"queued", "ready", "attached"}:
                return self.status(asset)
            if asset["lifecycle"] != "uploaded" or asset["expires_at"] <= datetime.now(UTC):
                raise APIError("upload_incomplete", "Upload expired or is incomplete", 409)
            job_id = uuid4()
            expiry = datetime.now(UTC) + timedelta(seconds=self.settings.audio_draft_ttl_seconds)
            await c.execute(
                insert(J).values(
                    id=job_id,
                    user_id=identity.user_id,
                    kind="transcription",
                    dedupe_key=str(asset_id),
                    payload={"asset_id": str(asset_id)},
                    data_epoch=account["data_epoch"],
                    input_versions={},
                )
            )
            meta = {**meta, "job_id": str(job_id)}
            await c.execute(
                update(ASSETS)
                .where(ASSETS.c.id == asset_id)
                .values(
                    lifecycle="queued",
                    verified_metadata=meta,
                    expires_at=expiry,
                )
            )
            await c.execute(
                update(CLEANUP)
                .where(
                    CLEANUP.c.user_id == identity.user_id,
                    CLEANUP.c.operation_key == f"audio:{asset_id}",
                )
                .values(not_before=expiry + timedelta(seconds=60))
            )
            return self.status(
                dict(asset, lifecycle="queued", verified_metadata=meta, expires_at=expiry)
            )

    def status(self, asset):
        meta = asset["verified_metadata"]
        return AudioStatus(
            id=asset["id"],
            status=asset["lifecycle"],
            expires_at=asset["expires_at"],
            job_id=meta.get("job_id"),
            transcript_draft=meta.get("transcript") if asset["lifecycle"] == "ready" else None,
            duration_seconds=meta.get("duration_seconds"),
            error_code=meta.get("error_code"),
        )

    async def get(self, identity, asset_id):
        self.enabled()
        async with self.db.tenant_transaction(identity.user_id) as c:
            await active_account(c, identity.user_id)
            asset = await self.asset(c, identity.user_id, asset_id)
            if asset["expires_at"] <= datetime.now(UTC) and asset["lifecycle"] != "attached":
                raise APIError("audio_expired", "Audio draft expired", 410)
            return self.status(asset)

    async def discard(self, identity, asset_id):
        async with self.db.tenant_transaction(identity.user_id) as c:
            await active_account(c, identity.user_id)
            asset = await self.asset(c, identity.user_id, asset_id)
            await c.execute(
                update(ASSETS)
                .where(ASSETS.c.id == asset_id)
                .values(
                    lifecycle="discarded",
                    verified_metadata={"data_epoch": asset["verified_metadata"]["data_epoch"]},
                )
            )
            if asset["lifecycle"] not in {"pending", "uploading"}:
                await c.execute(
                    update(CLEANUP)
                    .where(
                        CLEANUP.c.user_id == identity.user_id,
                        CLEANUP.c.operation_key == f"audio:{asset_id}",
                    )
                    .values(not_before=func.now())
                )

    async def run(self, job):
        asset_id = UUID(job["payload"]["asset_id"])
        async with self.db.tenant_transaction(job["user_id"]) as c:
            account = await active_account(c, job["user_id"])
            asset = await self.asset(c, job["user_id"], asset_id)
            if account["data_epoch"] != job["data_epoch"] or asset["lifecycle"] != "queued":
                raise SourceChanged
            meta = asset["verified_metadata"]
            await c.execute(
                update(J)
                .where(J.c.id == job["id"], J.c.lease_token == job["lease_token"])
                .values(
                    stage="transcribing",
                    stage_deadline_at=func.least(
                        J.c.deadline_at,
                        func.clock_timestamp()
                        + timedelta(seconds=self.settings.asr_timeout_seconds + 10),
                    ),
                )
            )
        data = await self.storage.get(asset["storage_key"])
        if len(data) != meta["size_bytes"] or hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise WorkFailure("invalid_audio")
        output = await self.transcriber(data, self.settings)
        if output.get("media_type") != meta["content_type"]:
            raise WorkFailure("invalid_audio")
        async with self.db.tenant_transaction(job["user_id"]) as c:
            account = await active_account(c, job["user_id"])
            asset = await self.asset(c, job["user_id"], asset_id)
            current = (
                (await c.execute(select(J).where(J.c.id == job["id"]).with_for_update()))
                .mappings()
                .one_or_none()
            )
            now = await c.scalar(select(func.clock_timestamp()))
            if (
                current is None
                or current["status"] != "running"
                or current["lease_token"] != job["lease_token"]
                or current["leased_until"] <= now
                or current["deadline_at"] <= now
            ):
                raise LeaseLost
            if (
                account["data_epoch"] != job["data_epoch"]
                or asset["lifecycle"] != "queued"
                or asset["expires_at"] <= now
            ):
                raise SourceChanged
            await c.execute(
                update(ASSETS)
                .where(ASSETS.c.id == asset_id)
                .values(
                    lifecycle="ready",
                    verified_metadata={**meta, **output},
                )
            )
            await c.execute(
                update(J)
                .where(J.c.id == job["id"])
                .values(
                    status="succeeded",
                    stage="ready",
                    error_code=None,
                    lease_token=None,
                    leased_until=None,
                    deadline_at=None,
                    stage_deadline_at=None,
                )
            )

    async def fail(self, job, failure):
        async with self.db.tenant_transaction(job["user_id"]) as c:
            account = (
                (await c.execute(select(A).where(A.c.id == job["user_id"]).with_for_update()))
                .mappings()
                .one_or_none()
            )
            if (
                not account
                or account["deletion_state"] != "active"
                or account["data_epoch"] != job["data_epoch"]
            ):
                return
            current = (
                (
                    await c.execute(
                        select(J)
                        .where(J.c.id == job["id"], J.c.user_id == job["user_id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                not current
                or current["status"] != "running"
                or current["lease_token"] != job["lease_token"]
            ):
                return
            retry = failure.retryable and current["attempts"] < self.settings.job_max_attempts
            await c.execute(
                update(J)
                .where(J.c.id == job["id"])
                .values(
                    status="queued" if retry else "failed",
                    stage="queued" if retry else "failed",
                    error_code=failure.code,
                    run_after=func.now() + timedelta(seconds=10),
                    lease_token=None,
                    leased_until=None,
                    deadline_at=None,
                    stage_deadline_at=None,
                )
            )
            if not retry:
                await c.execute(
                    update(ASSETS)
                    .where(
                        ASSETS.c.id == UUID(job["payload"]["asset_id"]),
                        ASSETS.c.user_id == job["user_id"],
                        ASSETS.c.lifecycle == "queued",
                    )
                    .values(
                        lifecycle="failed",
                        verified_metadata={
                            "data_epoch": job["data_epoch"],
                            "error_code": failure.code,
                        },
                    )
                )

    async def cancel(self, job):
        await self.fail(job, WorkFailure("source_changed"))

    async def maintain(self, job):
        async with self.db.tenant_transaction(job["user_id"]) as c:
            account = (
                (await c.execute(select(A).where(A.c.id == job["user_id"]).with_for_update()))
                .mappings()
                .one_or_none()
            )
            current = (
                (
                    await c.execute(
                        select(J)
                        .where(J.c.id == job["id"], J.c.user_id == job["user_id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if not current or not account or account["deletion_state"] != "active":
                return
            now = await c.scalar(select(func.clock_timestamp()))
            if current["status"] in {"succeeded", "failed", "cancelled"}:
                await c.execute(
                    update(J)
                    .where(J.c.id == job["id"])
                    .values(
                        stage="retention_cleaned",
                        payload={},
                    )
                )
                return
            expired = (
                current["status"] == "queued"
                or not current["leased_until"]
                or current["leased_until"] <= now
                or not current["deadline_at"]
                or current["deadline_at"] <= now
            )
            if current["attempts"] < self.settings.job_max_attempts or not expired:
                return
            await c.execute(
                update(J)
                .where(J.c.id == job["id"])
                .values(
                    status="failed",
                    stage="failed",
                    error_code="speech_timeout",
                    lease_token=None,
                    leased_until=None,
                    deadline_at=None,
                    stage_deadline_at=None,
                )
            )
            if "asset_id" in current["payload"]:
                await c.execute(
                    update(ASSETS)
                    .where(
                        ASSETS.c.user_id == job["user_id"],
                        ASSETS.c.id == UUID(current["payload"]["asset_id"]),
                        ASSETS.c.lifecycle == "queued",
                    )
                    .values(
                        lifecycle="failed",
                        verified_metadata={
                            "data_epoch": current["data_epoch"],
                            "error_code": "speech_timeout",
                        },
                    )
                )
