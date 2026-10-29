import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from app.config import Settings
from app.providers.embeddings import Embeddings, configuration, digest, split_source, vector
from app.services.indexing import EmbeddingHandler
from app.workers.types import SourceRef, WorkFailure


def settings(**overrides):
    values = dict(
        _env_file=None,
        database_url="postgresql+psycopg://test@localhost/test",
        nebius_api_key="test-only",
        nebius_embedding_model="test/embedding",
        embedding_dimensions=3,
        embedding_document_prefix="passage: ",
        embedding_query_prefix="query: ",
        embedding_prefixes_verified=True,
    )
    return Settings(**(values | overrides))


def test_unicode_chunks_reconstruct_exact_source_with_byte_caps():
    config = configuration(settings(embedding_input_token_budget=512))
    text = "🌙 خواب میں ایک گھر\n" * 1000
    chunks = split_source(text, config)
    assert len(chunks) > 1
    assert "".join(chunk.text for chunk in chunks) == text
    assert chunks == split_source(text, config)
    for index, chunk in enumerate(chunks):
        assert text[chunk.start : chunk.end] == chunk.text
        assert chunk.start == (chunks[index - 1].end if index else 0)
        assert len((config["document_prefix"] + chunk.text).encode()) + 32 <= 512
        assert len((config["query_prefix"] + chunk.text).encode()) + 32 <= 512
    assert chunks[-1].end == len(text)


@pytest.mark.parametrize(
    "value",
    [
        [1, 2],
        [0, 0, 0],
        [float("nan"), 1, 0],
        [True, 1, 2],
        None,
        [1e-300, 0, 0],
        [1e300, 0, 0],
    ],
)
def test_invalid_vectors_fail_before_database_insertion(value):
    with pytest.raises(WorkFailure):
        vector(value, 3)


def test_prefixes_must_be_explicitly_verified_including_empty_prefixes():
    with pytest.raises(WorkFailure):
        configuration(settings(embedding_prefixes_verified=False))
    with pytest.raises(WorkFailure):
        configuration(settings(embedding_document_prefix=None))
    assert configuration(settings(embedding_document_prefix=""))["document_prefix"] == ""


def response(inputs, dimensions=3):
    return httpx.Response(
        200,
        json={
            "object": "list",
            "model": "test/embedding",
            "data": [
                {
                    "object": "embedding",
                    "index": index,
                    "embedding": [1.0] + [0.0] * (dimensions - 1),
                }
                for index, _ in enumerate(inputs)
            ],
            "usage": {"prompt_tokens": 12, "total_tokens": 12},
        },
    )


def test_real_sdk_sends_explicit_prefixes_and_batches_and_records_only_metadata():
    async def check():
        calls, runs = [], []

        def reply(request):
            data = json.loads(request.content)
            calls.append(data)
            return response(data["input"])

        async def record(**metadata):
            runs.append(metadata)

        provider = Embeddings(settings(), transport=httpx.MockTransport(reply))
        result = await provider.encode(
            ["passage: 🌙"] * 33, task="embedding_document", record=record
        )
        assert len(result) == 33 and [len(call["input"]) for call in calls] == [16, 16, 1]
        assert all(
            call["encoding_format"] == "float" and call["model"] == "test/embedding"
            for call in calls
        )
        assert len(runs) == 3 and all(run["input_tokens"] == 12 for run in runs)
        assert "passage" not in str(runs)

    asyncio.run(check())


def test_429_has_no_hidden_sdk_retry_and_retains_retry_after():
    async def check():
        calls, runs = [], []

        def reply(request):
            calls.append(request)
            return httpx.Response(
                429, headers={"Retry-After": "7"}, json={"error": {"message": "secret"}}
            )

        async def record(**metadata):
            runs.append(metadata)

        provider = Embeddings(settings(), transport=httpx.MockTransport(reply))
        with pytest.raises(WorkFailure) as error:
            await provider.encode(["query: house"], task="embedding_query", record=record)
        assert error.value.retryable and error.value.retry_after == 7
        assert len(calls) == 1 and runs[0]["status"] == "request_failed"
        assert "secret" not in str(runs)

    asyncio.run(check())


def test_dimension_mismatch_is_safe_failure_from_real_sdk():
    async def check():
        async def record(**metadata):
            assert metadata["status"] == "request_failed"

        provider = Embeddings(
            settings(), transport=httpx.MockTransport(lambda _: response(["x"], 2))
        )
        with pytest.raises(WorkFailure) as error:
            await provider.encode(["passage: house"], task="embedding_document", record=record)
        assert error.value.code == "embedding_dimension_mismatch"

    asyncio.run(check())


def test_handler_indexes_exact_chunks_and_distinct_query_document_inputs():
    async def check():
        calls = []

        def reply(request):
            data = json.loads(request.content)
            calls.append(data["input"])
            return response(data["input"])

        class Execution:
            original = {"text": "A house 🌙"}
            primary = SourceRef(uuid4(), uuid4(), digest(original["text"]), 1)

            async def set_stage(self, stage):
                assert stage == "embedding"

            async def record_call(self, **metadata):
                pass

        config = settings()
        result = await EmbeddingHandler(
            config, Embeddings(config, transport=httpx.MockTransport(reply))
        ).run(Execution())
        assert calls == [["passage: A house 🌙"], ["query: A house 🌙"]]
        assert result.chunks[0]["input_hash"] == digest(calls[0][0])
        assert result.chunks[0]["query_input_hash"] == digest(calls[1][0])

    asyncio.run(check())


def test_oversized_provider_input_is_rejected_without_request():
    async def check():
        async def record(**_):
            pytest.fail("Must reject before provider call")

        provider = Embeddings(
            settings(), transport=httpx.MockTransport(lambda _: pytest.fail("API call"))
        )
        with pytest.raises(WorkFailure) as error:
            await provider.encode(["x" * 3000], task="embedding_document", record=record)
        assert error.value.code == "provider_input_too_large"

    asyncio.run(check())
