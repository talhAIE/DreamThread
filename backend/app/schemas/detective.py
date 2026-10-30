"""Contracts for tenant-scoped Dream Detective conversations."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.schemas.analysis import Citation, StrictOutput
from app.schemas.dreams import postgres_text
from app.schemas.identity import Contract


class ConversationCreate(Contract):
    title: str | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("title")
    @classmethod
    def supported_title(cls, value):
        if value is None:
            return value
        value = postgres_text(value).strip()
        if not value:
            raise ValueError("Title must not be blank")
        return value


class ConversationRead(Contract):
    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationList(Contract):
    items: list[ConversationRead]
    count: int


class DetectiveQuestion(Contract):
    question: str = Field(min_length=1, max_length=4000)
    source_ids: list[UUID] = Field(default_factory=list, max_length=20)
    from_date: date | None = None
    to_date: date | None = None
    motif_ids: list[UUID] = Field(default_factory=list, max_length=20)

    @field_validator("question")
    @classmethod
    def supported_question(cls, value):
        value = postgres_text(value)
        if not value.strip():
            raise ValueError("Question must not be blank")
        return value

    @field_validator("source_ids", "motif_ids")
    @classmethod
    def unique_scope_ids(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("Scope IDs must be unique")
        return values

    @model_validator(mode="after")
    def valid_date_range(self):
        if self.from_date is not None and self.to_date is not None:
            if self.from_date > self.to_date:
                raise ValueError("from_date must not follow to_date")
        return self


class DreamCitation(Contract):
    evidence_id: UUID
    dream_id: UUID
    revision_id: UUID
    title: str
    dream_date: date
    start: int
    end: int
    quote: str
    content_hash: str


class GroundedClaim(Contract):
    statement: str
    citations: list[DreamCitation]


class ConversationMessage(Contract):
    id: UUID
    role: Literal["user", "assistant"]
    body: str
    status: Literal["pending", "completed", "failed", "stale"]
    created_at: datetime
    insufficient_evidence: bool = False
    coverage_limited: bool = False
    citations: list[DreamCitation] = Field(default_factory=list)
    observations: list[GroundedClaim] = Field(default_factory=list)
    tentative_reflections: list[str] = Field(default_factory=list)
    claims: list[GroundedClaim] = Field(default_factory=list)


class DetectiveSubmission(Contract):
    job_id: UUID
    message_id: UUID
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]


class ConversationDetail(ConversationRead):
    messages: list[ConversationMessage]
    has_more: bool = False


class ConversationMessages(Contract):
    conversation_id: UUID
    items: list[ConversationMessage]
    offset: int
    limit: int
    has_more: bool


class DetectiveClaim(StrictOutput):
    statement: str = Field(min_length=1, max_length=800)
    citations: list[Citation] = Field(min_length=1, max_length=5)


class DetectiveOutput(StrictOutput):
    insufficient_evidence: bool
    claims: list[DetectiveClaim] = Field(max_length=8)

    @model_validator(mode="after")
    def evidence_matches_answer(self):
        if self.insufficient_evidence and self.claims:
            raise ValueError("Insufficient-evidence answers cannot contain claims")
        if not self.insufficient_evidence and not self.claims:
            raise ValueError("Supported answers need cited claims")
        return self
