"""Nebius's OpenAI-compatible chat API. Worker owns retries; SDK retries are disabled."""

import json
import time
from dataclasses import dataclass

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, OpenAIError

from app.workers.types import WorkFailure, retry_after


@dataclass(frozen=True)
class Completion:
    text: str
    run_id: object
    tool_calls: list = ()


class Nebius:
    def __init__(self, settings, *, transport=None):
        self.settings, self.transport = settings, transport

    def configured(self, model=None):
        settings = self.settings
        chosen = model or settings.nebius_reasoning_model or settings.nebius_extraction_model
        if not settings.nebius_api_key or not chosen:
            raise WorkFailure("provider_not_configured")
        model_name = chosen.casefold()
        if not model_name.startswith("nvidia/") or "nemotron" not in model_name:
            raise WorkFailure("provider_configuration")

    async def complete(self, messages, schema=None, *, task, record, model=None, tools=None):
        self.configured(model=model)
        settings = self.settings
        chosen_model = model or settings.nebius_reasoning_model or settings.nebius_extraction_model
        # Deliberately conservative upper bound (one token per UTF-8 byte plus protocol headroom).
        schema_bytes = len(json.dumps(schema).encode()) if schema else 0
        tools_bytes = len(json.dumps(tools).encode()) if tools else 0
        estimated = (
            len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
            + schema_bytes
            + tools_bytes
            + settings.model_max_output_tokens
            + 512
        )
        if estimated > settings.model_context_token_budget:
            raise WorkFailure("provider_input_too_large")
        response_format = None
        if schema:
            response_format = {"type": "json_object"}
            if settings.nebius_output_mode == "json_schema":
                response_format = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": task,
                        "strict": True,
                        "schema": schema,
                    },
                }
        started, usage, failure, completion = time.perf_counter(), None, None, None
        async with AsyncOpenAI(
            api_key=settings.nebius_api_key.get_secret_value(),
            base_url=settings.nebius_base_url,
            timeout=settings.model_timeout_seconds,
            max_retries=0,
            http_client=httpx.AsyncClient(
                transport=self.transport, trust_env=False, follow_redirects=False
            ),
        ) as client:
            try:
                request_kwargs = {
                    "model": chosen_model,
                    "messages": messages,
                    "max_tokens": settings.model_max_output_tokens,
                    "temperature": 0,
                    "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}
                    if "nemotron-3-nano" in chosen_model.casefold()
                    else None,
                }
                if response_format:
                    request_kwargs["response_format"] = response_format
                if tools:
                    request_kwargs["tools"] = tools
                completion = await client.chat.completions.create(**request_kwargs)
                usage = completion.usage
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
            except (OpenAIError, ValueError, TypeError):
                failure = WorkFailure("invalid_output")
        run_id = await record(
            provider="nebius",
            model_id=chosen_model,
            task=task,
            latency_ms=(time.perf_counter() - started) * 1000,
            input_tokens=usage_count(usage, "prompt_tokens"),
            output_tokens=usage_count(usage, "completion_tokens"),
            status="request_failed" if failure else "received",
        )
        if failure:
            raise failure
        allowed_finish = {"stop", "tool_calls"} if tools else {"stop"}
        if (
            not completion.choices
            or completion.choices[0].finish_reason not in allowed_finish
            or (not tools and completion.choices[0].message.tool_calls)
            or completion.choices[0].message.refusal
        ):
            return Completion("", run_id)
        content = completion.choices[0].message.content
        tool_calls = list(completion.choices[0].message.tool_calls or [])
        return Completion(
            content if isinstance(content, str) and len(content) <= 131072 else "",
            run_id,
            tool_calls=tool_calls,
        )


def usage_count(usage, key):
    value = getattr(usage, key, None)
    return value if type(value) is int and value >= 0 else None
