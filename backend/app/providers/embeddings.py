"""Explicit embedding input contract; no guessed model prefixes or silent truncation."""

import hashlib
import json
import math
import struct
import time
from dataclasses import dataclass

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, OpenAIError

from app.workers.types import WorkFailure, retry_after


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def configuration(settings):
    if not settings.nebius_embedding_model or not settings.embedding_dimensions:
        raise WorkFailure("provider_not_configured")
    if (
        not settings.embedding_prefixes_verified
        or settings.embedding_document_prefix is None
        or settings.embedding_query_prefix is None
        or settings.embedding_dimensions > 16000
    ):
        raise WorkFailure("provider_configuration")
    return {
        "model_id": settings.nebius_embedding_model,
        "version": settings.embedding_version,
        "dimensions": settings.embedding_dimensions,
        "document_prefix": settings.embedding_document_prefix,
        "query_prefix": settings.embedding_query_prefix,
        "input_token_budget": settings.embedding_input_token_budget,
        "chunking": "unicode-utf8-contiguous-v1",
    }


def fingerprint(config):
    return digest(json.dumps(config, sort_keys=True, ensure_ascii=False))


@dataclass(frozen=True)
class Chunk:
    start: int
    end: int
    text: str


def split_source(text, config):
    # UTF-8 bytes upper-bound text tokens for supported byte-fallback tokenizers. Keep
    # headroom for service special tokens; callers must verify the actual model limit.
    prefix_size = max(len(config[k].encode("utf-8")) for k in ("document_prefix", "query_prefix"))
    budget = config["input_token_budget"] - prefix_size - 32
    if budget < 4:
        raise WorkFailure("provider_configuration")
    chunks, start, size = [], 0, 0
    for index, char in enumerate(text):
        count = len(char.encode("utf-8"))
        if size + count > budget:
            chunks.append(Chunk(start, index, text[start:index]))
            start, size = index, 0
        size += count
    if start < len(text):
        chunks.append(Chunk(start, len(text), text[start:]))
    if not chunks or len(chunks) > 256:
        raise WorkFailure("provider_input_too_large")
    return chunks


def vector(value, dimensions):
    if not isinstance(value, (list, tuple)):
        raise WorkFailure("invalid_output")
    if (
        len(value) != dimensions
        or any(type(x) not in {int, float} or not math.isfinite(x) for x in value)
        or not any(value)
        or sum(float(x) * float(x) for x in value) > 3e38
    ):
        raise WorkFailure(
            "embedding_dimension_mismatch" if len(value) != dimensions else "invalid_output"
        )
    # pgvector stores float32. Reject underflow-to-zero and overflow before insertion,
    # so a nominally nonzero provider vector cannot turn into undefined cosine distance.
    try:
        values = [struct.unpack("f", struct.pack("f", x))[0] for x in value]
    except (OverflowError, struct.error):
        raise WorkFailure("invalid_output") from None
    if not any(values) or not all(math.isfinite(x) for x in values):
        raise WorkFailure("invalid_output")
    return values


class Embeddings:
    def __init__(self, settings, *, transport=None):
        self.settings, self.transport = settings, transport

    async def encode(self, inputs, *, task, record):
        settings = self.settings
        config = configuration(settings)
        if not settings.nebius_api_key:
            raise WorkFailure("provider_not_configured")
        if any(len(value.encode("utf-8")) + 32 > config["input_token_budget"] for value in inputs):
            raise WorkFailure("provider_input_too_large")
        # Batch both count and aggregate bytes; service limits may be stricter and must be probed.
        vectors = []
        pending = list(inputs)
        while pending:
            batch, size = [], 0
            while pending and len(batch) < 16:
                count = len(pending[0].encode("utf-8"))
                if batch and size + count > settings.embedding_batch_byte_budget:
                    break
                if count > settings.embedding_batch_byte_budget:
                    raise WorkFailure("provider_input_too_large")
                batch.append(pending.pop(0))
                size += count
            started, usage, failure = time.perf_counter(), None, None
            async with AsyncOpenAI(
                api_key=settings.nebius_api_key.get_secret_value(),
                base_url=settings.nebius_base_url,
                max_retries=0,
                timeout=settings.model_timeout_seconds,
                http_client=httpx.AsyncClient(
                    transport=self.transport, trust_env=False, follow_redirects=False
                ),
            ) as client:
                try:
                    result = await client.embeddings.create(
                        model=config["model_id"], input=batch, encoding_format="float"
                    )
                    usage = result.usage
                    data = sorted(result.data, key=lambda row: row.index)
                    if [row.index for row in data] != list(range(len(batch))):
                        raise WorkFailure("invalid_output")
                    values = [vector(row.embedding, config["dimensions"]) for row in data]
                except (APITimeoutError, APIConnectionError):
                    failure = WorkFailure("provider_timeout", retryable=True)
                except APIStatusError as error:
                    status = error.status_code
                    failure = WorkFailure(
                        "provider_rate_limited"
                        if status == 429
                        else "provider_unavailable"
                        if status >= 500
                        else "provider_auth_failed"
                        if status in {401, 403}
                        else "provider_request_failed",
                        retryable=status == 429 or status >= 500,
                        retry_after=retry_after(error.response.headers.get("Retry-After")),
                    )
                except WorkFailure as error:
                    failure = error
                except (OpenAIError, TypeError, ValueError, AttributeError):
                    failure = WorkFailure("invalid_output")
            tokens = getattr(usage, "prompt_tokens", None)
            await record(
                provider="nebius",
                model_id=config["model_id"],
                task=task,
                latency_ms=(time.perf_counter() - started) * 1000,
                input_tokens=tokens if type(tokens) is int and tokens >= 0 else None,
                output_tokens=None,
                status="request_failed" if failure else "validated",
            )
            if failure:
                raise failure
            vectors.extend(values)
        return vectors
