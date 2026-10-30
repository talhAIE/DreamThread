"""Schemas for Dream Continuity Engine (Phase 9)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.identity import Contract


class ContinuityAccepted(Contract):
    job_id: UUID
    status: str


class ConnectionEvidenceRead(Contract):
    id: UUID
    dream_id: UUID
    revision_id: UUID
    start: int
    end: int
    exact_quote: str
    content_hash: str


class ConnectionRead(Contract):
    id: UUID
    dream_a_id: UUID
    dream_b_id: UUID
    connected_dream_id: UUID
    connected_dream_title: str | None = None
    relation_key: str
    status: Literal["suggested", "confirmed", "rejected", "stale"]
    observation: str
    basis_versions: dict = Field(default_factory=dict)
    model_run_id: UUID | None = None
    decision: Literal["confirmed", "rejected"] | None = None
    decision_version: int | None = None
    created_at: datetime
    updated_at: datetime
    evidence_count: int = 0
    evidence: list[ConnectionEvidenceRead] = Field(default_factory=list)


class ConnectionListResponse(Contract):
    dream_id: UUID
    connections: list[ConnectionRead]
    count: int


class ConnectionPatchRequest(Contract):
    decision: Literal["confirmed", "rejected"]
    expected_version: int = Field(ge=1)


class ConnectionEvidenceListResponse(Contract):
    connection_id: UUID
    evidence: list[ConnectionEvidenceRead]


class ComparisonProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_dream_id: UUID
    candidate_dream_id: UUID
    relation_key: str = Field(min_length=1, max_length=200)
    similarity_observation: str = Field(min_length=1, max_length=2000)
    difference_observation: str | None = Field(default=None, max_length=2000)
    tentative_reflection: str | None = Field(default=None, max_length=2000)
    uncertainty_note: str | None = Field(default=None, max_length=500)
    quotes_target: list[str] = Field(min_length=1, max_length=5)
    quotes_candidate: list[str] = Field(min_length=1, max_length=5)


class ContinuityOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comparisons: list[ComparisonProposal] = Field(default_factory=list, max_length=5)
    notes: str | None = Field(default=None, max_length=1000)
