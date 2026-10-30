"""Opt-in real private-storage/decoder/ASR probe using a synthetic local recording."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx

from app.config import Settings
from app.loop import create_loop
from app.providers.storage import PrivateStorage
from app.services.audio import speech


async def probe(path, expected):
    settings = Settings()
    data = path.read_bytes()
    if not data or len(data) > settings.audio_max_bytes:
        raise ValueError("invalid_probe_audio")
    storage = PrivateStorage(settings)
    await storage.provision()
    key = f"{uuid4()}/{uuid4()}/source"
    deleted = False
    try:
        await storage.put(key, data, "audio/wav")
        loaded = await storage.get(key)
        if hashlib.sha256(data).digest() != hashlib.sha256(loaded).digest():
            raise ValueError("storage_content_mismatch")
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=30) as client:
            public = await client.get(
                settings.supabase_url
                + "/storage/v1/object/public/"
                + settings.supabase_audio_bucket
                + "/"
                + key
            )
            if public.status_code not in {400, 401, 403, 404}:
                raise ValueError("private_access_not_verified")
        result = await speech(loaded, settings)
        if expected and expected.casefold() not in result["transcript"].casefold():
            raise ValueError("synthetic_transcript_mismatch")
        await storage.remove([key])
        deleted = True
        async with storage.client() as client:
            check = await client.get("object/" + settings.supabase_audio_bucket + "/" + key)
            if check.status_code not in {400, 404}:
                raise ValueError("storage_deletion_not_verified")
        print(
            json.dumps(
                {
                    "voice_live_verified": True,
                    "private_bucket": True,
                    "raw_object_deleted": True,
                    "asr_model": settings.asr_model,
                    "duration_seconds": result["duration_seconds"],
                    "transcript_characters": len(result["transcript"]),
                }
            )
        )
    finally:
        if not deleted:
            await storage.remove([key])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--expect", default="")
    args = parser.parse_args()
    if not args.live:
        parser.error(
            "Pass --live to write/delete a synthetic object and run local speech inference"
        )
    try:
        with asyncio.Runner(loop_factory=create_loop) as runner:
            runner.run(probe(args.file, args.expect))
    except Exception as error:
        print(
            json.dumps(
                {
                    "voice_live_verified": False,
                    "error_type": type(error).__name__,
                    "error_code": getattr(error, "code", "probe_failed"),
                }
            )
        )
        raise SystemExit(1) from None
