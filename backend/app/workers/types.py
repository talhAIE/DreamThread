"""Trusted worker contracts. No raw provider exceptions cross the API boundary."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas.analysis import VerifiedEvidence


class LeaseLost(Exception):
    pass


class SourceChanged(Exception):
    pass


class WorkFailure(Exception):
    def __init__(self, code, *, retryable=False, retry_after=0):
        allowed = {
            "inference_not_implemented",
            "provider_timeout",
            "provider_rate_limited",
            "provider_unavailable",
            "provider_auth_failed",
            "provider_request_failed",
            "invalid_output",
            "internal_error",
            "unsupported_job",
            "stage_timeout",
            "provider_not_configured",
            "provider_configuration",
            "provider_input_too_large",
            "embedding_dimension_mismatch",
            "embedding_version_conflict",
            "storage_key_invalid",
            "storage_configuration",
            "storage_unavailable",
            "invalid_audio",
            "speech_timeout",
            "speech_unavailable",
            "source_changed",
        }
        self.code = code if code in allowed else "internal_error"
        self.retryable, self.retry_after = retryable, retry_after


@dataclass(frozen=True)
class SourceRef:
    dream_id: UUID
    revision_id: UUID
    content_hash: str
    dream_version: int

    def json(self):
        return {k: str(v) if isinstance(v, UUID) else v for k, v in asdict(self).items()}

    @classmethod
    def parse(cls, data):
        return cls(
            UUID(data["dream_id"]),
            UUID(data["revision_id"]),
            data["content_hash"],
            data["dream_version"],
        )


class AnalysisPayload(BaseModel):
    """Transport shape only; Phase 6 must validate meanings/quotes before returning it."""

    model_config = ConfigDict(extra="forbid")
    extraction: dict = Field(default_factory=dict)
    reflections: list = Field(default_factory=list, max_length=3)
    followup: str | None = Field(default=None, max_length=1000)


@dataclass(frozen=True)
class Publication:
    analysis: AnalysisPayload
    sources: tuple[SourceRef, ...] = ()
    archive_bound: bool = False
    evidence: tuple[VerifiedEvidence, ...] = ()
    require_evidence: bool = False


def snapshot_hash(job, sources, archive_bound):
    snapshot = {
        "epoch": job["data_epoch"],
        "pipeline": job["input_versions"]["pipeline_version"],
        "memory": job["input_versions"]["memory_version"],
        "sources": sorted([s.json() for s in sources], key=lambda s: s["dream_id"]),
    }
    if archive_bound:
        snapshot["archive"] = job["input_versions"]["archive_version"]
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


def json_payload(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > 131072:
            raise ValueError
        return json.loads(encoded)
    except (TypeError, ValueError, UnicodeError):
        raise WorkFailure("invalid_output") from None


def retry_after(value):
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = (
                parsedate_to_datetime(value).astimezone(UTC) - datetime.now(UTC)
            ).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0
    return min(604800, max(0, seconds))


def classify(error):
    if isinstance(error, WorkFailure):
        return error
    if isinstance(error, (TimeoutError, httpx.TimeoutException, httpx.NetworkError)):
        return WorkFailure("provider_timeout", retryable=True)
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status == 429 or status >= 500:
            return WorkFailure(
                "provider_rate_limited" if status == 429 else "provider_unavailable",
                retryable=True,
                retry_after=retry_after(error.response.headers.get("Retry-After")),
            )
        return WorkFailure(
            "provider_auth_failed" if status in {401, 403} else "provider_request_failed"
        )
    if isinstance(error, ValidationError):
        return WorkFailure("invalid_output")
    return WorkFailure("internal_error", retryable=True)
