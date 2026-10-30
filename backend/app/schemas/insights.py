from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.schemas.identity import Contract


class MotifCount(Contract):
    motif_id: UUID
    type: str
    label: str
    dream_count: int


class EmotionCount(Contract):
    emotion: str
    dream_count: int


class InsightsResult(Contract):
    month: str
    timezone: str
    dream_date_count: int
    recorded_at_count: int
    missing_emotion_dream_count: int
    motif_counts: list[MotifCount]
    emotion_counts: list[EmotionCount]


class CapsuleCitation(Contract):
    evidence_id: UUID
    dream_id: UUID
    revision_id: UUID
    dream_date: date | None
    title: str
    quote: str
    start: int
    end: int


class CapsuleContent(Contract):
    insights: InsightsResult
    retrospective: str
    citations: list[CapsuleCitation] = Field(default_factory=list)


class TimeCapsule(Contract):
    id: UUID
    month: str
    timezone: str
    status: Literal["pending", "ready", "stale"]
    generated_at: datetime | None
    capsule: CapsuleContent | None


class TimeCapsuleList(Contract):
    items: list[TimeCapsule]


class TimeCapsuleRefreshAccepted(Contract):
    job_id: UUID
    status: str
    month: str
    timezone: str
