"""Response contracts for the tenant-scoped Phase 10 constellation."""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.schemas.identity import Contract


class AssociationRead(Contract):
    id: UUID
    meaning: str
    source_dream_id: UUID | None
    version: int


class MotifResponse(Contract):
    id: UUID
    type: Literal["place", "person", "object", "event", "emotion"]
    canonical_label: str
    normalized_key: str
    dream_id: UUID
    revision_id: UUID
    dream_date: date
    confirmation_state: Literal["suggested", "confirmed"]
    origin: Literal["extracted", "user"]
    evidence_ids: list[UUID] = Field(default_factory=list)
    personal_associations: list[AssociationRead] = Field(default_factory=list)


class ConstellationNode(Contract):
    id: str
    kind: Literal["dream", "motif", "association"]
    entity_id: UUID
    label: str
    dream_date: date | None = None
    motif_type: Literal["place", "person", "object", "event", "emotion"] | None = None


class ConstellationEdge(Contract):
    id: str
    source: str
    target: str
    kind: Literal["mentions", "continuity", "personal_association"]
    status: Literal["observed", "suggested", "confirmed"]
    evidence_ids: list[UUID] = Field(default_factory=list)


class ConstellationGraph(Contract):
    nodes: list[ConstellationNode]
    edges: list[ConstellationEdge]
    truncated: bool
    archive_version: int


class MotifEvidenceRead(Contract):
    evidence_id: UUID
    dream_id: UUID
    revision_id: UUID
    start: int
    end: int
    quote: str
    content_hash: str


class MotifMentionRead(Contract):
    dream_id: UUID
    revision_id: UUID
    title: str
    dream_date: date
    confirmation_state: Literal["suggested", "confirmed"]
    origin: Literal["extracted", "user"]
    evidence: list[MotifEvidenceRead] = Field(default_factory=list)


class MotifDetail(Contract):
    id: UUID
    type: Literal["place", "person", "object", "event", "emotion"]
    canonical_label: str
    normalized_key: str
    mentions: list[MotifMentionRead]
    personal_associations: list[AssociationRead]


class StorylineListItem(Contract):
    id: UUID
    motif_id: UUID
    title: str
    status: Literal["active", "stale"]
    source_signature: str
    entry_count: int


class StorylineChapter(Contract):
    dream_id: UUID
    revision_id: UUID
    title: str
    dream_date: date
    text: str
    evidence: list[MotifEvidenceRead] = Field(default_factory=list)


class StorylineDetail(StorylineListItem):
    chapters: list[StorylineChapter]


class StorylineRefreshAccepted(Contract):
    job_id: UUID
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
