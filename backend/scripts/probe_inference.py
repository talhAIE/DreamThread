"""Explicit, opt-in real Nebius capability check using the production validators.

Run from backend/: python scripts/probe_inference.py --live
Uses a synthetic dream only. Requires private .env settings; never prints raw output/secrets.
"""

import argparse
import asyncio
import hashlib
import json
from uuid import uuid4

import httpx
from openai import AsyncOpenAI

from app.config import Settings
from app.loop import create_loop
from app.providers.nebius import Nebius
from app.services.extraction import ExtractionPipeline
from app.workers.types import SourceRef, WorkFailure


class ProbeExecution:
    def __init__(self):
        text = "I walked through a forest and saw a house. I felt calm."
        self.primary = SourceRef(uuid4(), uuid4(), hashlib.sha256(text.encode()).hexdigest(), 1)
        self.original = {"text": text, "reported_emotions": ["calm"]}
        self.job = {"input_versions": {"submission": 1}}
        self.artifacts, self.runs = {}, []

    async def load_checkpoint(self, stage):
        return self.artifacts.get(stage)

    async def checkpoint(self, stage, output):
        self.artifacts[stage] = output

    async def set_stage(self, stage):
        pass

    async def personal_context(self):
        from app.services.personal_context import empty_context
        return empty_context()

    async def record_call(self, **metadata):
        run_id = uuid4()
        self.runs.append({"id": run_id, **metadata})
        return run_id

    async def mark_call(self, run_id, status):
        next(row for row in self.runs if row["id"] == run_id)["status"] = status


async def probe():
    settings = Settings()
    provider = Nebius(settings)
    provider.configured()
    async with AsyncOpenAI(
        api_key=settings.nebius_api_key.get_secret_value(),
        base_url=settings.nebius_base_url,
        max_retries=0,
        timeout=settings.model_timeout_seconds,
        http_client=httpx.AsyncClient(trust_env=False, follow_redirects=False),
    ) as client:
        catalog = await client.models.list()
        if settings.nebius_extraction_model not in {model.id for model in catalog.data}:
            raise WorkFailure("provider_configuration")
    execution = ProbeExecution()
    publication = await ExtractionPipeline(settings, provider).run(execution)
    if not publication.require_evidence or not publication.evidence:
        raise WorkFailure("invalid_output")
    print(
        json.dumps(
            {
                "live_verified": True,
                "model": settings.nebius_extraction_model,
                "output_mode": settings.nebius_output_mode,
                "evidence_count": len(publication.evidence),
                "requests": execution.runs,
            },
            default=str,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Make real, potentially billable API requests"
    )
    args = parser.parse_args()
    if not args.live:
        parser.error(
            "Pass --live only after configuring a real Nebius key and available NVIDIA model"
        )
    try:
        with asyncio.Runner(loop_factory=create_loop) as runner:
            runner.run(probe())
    except Exception as error:
        code = error.code if isinstance(error, WorkFailure) else "configuration_or_provider_failure"
        print(json.dumps({"live_verified": False, "error_code": code}))
        raise SystemExit(1) from None
