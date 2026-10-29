from datetime import date
from uuid import UUID

from pydantic import Field

from app.schemas.identity import Contract


class IndexRequest(Contract):
    expected_version: int = Field(ge=1)


class IndexResult(Contract):
    dream_id: UUID
    job_id: UUID
    status: str


class Candidate(Contract):
    dream_id: UUID
    revision_id: UUID
    source_hash: str
    dream_date: date
    title: str
    snippet: str
    rank: int
    score: float
    semantic_rank: int | None
    cosine_similarity: float | None
    reasons: list[str]
    association_ids: list[UUID]


class CandidateResult(Contract):
    dream_id: UUID
    revision_id: UUID
    archive_version: int
    memory_version: int
    degraded: bool
    degraded_reasons: list[str]
    active_embedding_version: str | None
    target_embedding_version: str
    indexed_dreams: int
    saved_dreams: int
    embedding_job_id: UUID | None
    embedding_job_status: str | None
    candidates: list[Candidate]
    purpose: str = "Candidates for comparison; similarity does not establish identity or causation."
