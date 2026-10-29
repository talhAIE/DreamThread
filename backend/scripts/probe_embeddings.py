"""Opt-in live embedding capability check. Uses synthetic text; prints metadata only."""

import argparse
import asyncio
import json

import httpx
from openai import AsyncOpenAI

from app.config import Settings
from app.loop import create_loop
from app.providers.embeddings import Embeddings, configuration, vector
from app.workers.types import WorkFailure


async def probe():
    settings = Settings()
    if (
        not settings.nebius_api_key
        or not settings.nebius_embedding_model
        or not settings.embedding_prefixes_verified
        or settings.embedding_document_prefix is None
        or settings.embedding_query_prefix is None
    ):
        raise WorkFailure("provider_not_configured")
    text = "A cedar house beside a forest."
    inputs = [settings.embedding_document_prefix + text, settings.embedding_query_prefix + text]
    if any(len(value.encode()) + 32 > settings.embedding_input_token_budget for value in inputs):
        raise WorkFailure("provider_input_too_large")
    async with AsyncOpenAI(
        api_key=settings.nebius_api_key.get_secret_value(),
        base_url=settings.nebius_base_url,
        max_retries=0,
        timeout=settings.model_timeout_seconds,
        http_client=httpx.AsyncClient(trust_env=False, follow_redirects=False),
    ) as client:
        catalog = await client.models.list()
        if settings.nebius_embedding_model not in {item.id for item in catalog.data}:
            raise WorkFailure("provider_configuration")
        result = await client.embeddings.create(
            model=settings.nebius_embedding_model,
            input=inputs,
            encoding_format="float",
        )
    if len(result.data) != 2 or {row.index for row in result.data} != {0, 1}:
        raise WorkFailure("invalid_output")
    measured = len(result.data[0].embedding)
    if not 1 <= measured <= 16000:
        raise WorkFailure("embedding_dimension_mismatch")
    for item in result.data:
        vector(item.embedding, measured)
    if settings.embedding_dimensions not in {None, measured}:
        raise WorkFailure("embedding_dimension_mismatch")
    settings = settings.model_copy(update={"embedding_dimensions": measured})
    configuration(settings)
    runs = []

    async def record(**metadata):
        runs.append(metadata)

    # Exercise the same adapter as the worker after measuring the actual dimension.
    await Embeddings(settings).encode(inputs, task="embedding_probe", record=record)
    print(
        json.dumps(
            {
                "live_verified": True,
                "model": settings.nebius_embedding_model,
                "measured_dimensions": measured,
                "embedding_version": settings.embedding_version,
                "requests": runs,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Make potentially billable real requests"
    )
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live only after verifying model prefixes and configuring credentials")
    try:
        with asyncio.Runner(loop_factory=create_loop) as runner:
            runner.run(probe())
    except Exception as error:
        code = error.code if isinstance(error, WorkFailure) else "configuration_or_provider_failure"
        print(json.dumps({"live_verified": False, "error_code": code}))
        raise SystemExit(1) from None
