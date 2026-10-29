from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from app.workers.types import WorkFailure, classify, json_payload, retry_after


@pytest.mark.parametrize(
    "status,retry",
    [(401, False), (403, False), (400, False), (422, False), (429, True), (500, True), (503, True)],
)
def test_provider_failure_classification_and_retry_after(status, retry):
    request = httpx.Request("POST", "https://provider.example")
    response = httpx.Response(status, headers={"Retry-After": "30"}, request=request)
    error = httpx.HTTPStatusError("private provider message", request=request, response=response)
    failure = classify(error)
    assert failure.retryable is retry
    assert failure.retry_after == (30 if retry else 0)
    assert "private" not in failure.code


def test_retry_date_output_limits_and_safe_codes():
    future = datetime.now(UTC) + timedelta(seconds=60)
    assert 55 <= retry_after(format_datetime(future)) <= 60
    assert retry_after("invalid") == 0
    assert WorkFailure("raw private provider text").code == "internal_error"
    with pytest.raises(WorkFailure):
        json_payload({"oversized": "x" * 131073})
    with pytest.raises(WorkFailure):
        json_payload({"non_json": float("nan")})
    assert classify(TimeoutError()).retryable
