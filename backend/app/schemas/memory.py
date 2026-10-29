from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.schemas.dreams import postgres_text
from app.schemas.identity import Contract, Patch

MotifType = Literal["place", "person", "object", "event", "emotion"]
Decision = Literal["suggested", "confirmed", "rejected"]


class TextContract(Contract):
    @field_validator("*", mode="after")
    @classmethod
    def validate_text(cls, value):
        return postgres_text(value) if isinstance(value, str) else value


class NewMotif(TextContract):
    type: MotifType
    label: str = Field(min_length=1, max_length=120)

    @field_validator("label")
    @classmethod
    def nonblank_label(cls, value):
        if not value.strip():
            raise ValueError("Label must not be blank")
        return value


class MemoryCreate(TextContract):
    motif_id: UUID | None = None
    motif: NewMotif | None = None
    meaning: str = Field(min_length=1, max_length=1000)
    source_dream_id: UUID | None = None
    status: Decision = "confirmed"
    enabled: bool = Field(default=True, strict=True)

    @model_validator(mode="after")
    def one_motif(self):
        if (self.motif_id is None) == (self.motif is None):
            raise ValueError("Supply either motif_id or a new motif")
        if not self.meaning.strip():
            raise ValueError("Meaning must not be blank")
        return self


class MemoryPatch(Patch):
    meaning: str | None = Field(default=None, min_length=1, max_length=1000)
    enabled: bool | None = Field(default=None, strict=True)
    status: Decision | None = None

    @field_validator("meaning")
    @classmethod
    def valid_meaning(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Meaning must not be blank")
        return postgres_text(value)


class MemoryView(Contract):
    id: UUID
    motif_id: UUID
    meaning: str
    status: Decision
    enabled: bool
    origin: str
    version: int
    source_dream_id: UUID | None
    created_at: datetime
    updated_at: datetime


class MemoryPage(Contract):
    items: list[MemoryView]
    next_after: UUID | None
    memory_version: int
    archive_version: int


class MotifView(Contract):
    id: UUID
    type: MotifType
    label: str
    identity_confirmed: bool
    current_mentions: int
    origin: Literal["automatic_mention", "user_identity", "legacy"]


class MotifPage(Contract):
    items: list[MotifView]
    next_after: UUID | None


class DetailPatch(TextContract):
    expected_version: int = Field(ge=1)
    detail_key: str = Field(pattern=r"^detail_[a-f0-9]{40}$")
    removed: bool = Field(default=False, strict=True)
    label: str | None = Field(default=None, min_length=1, max_length=120)
    type: MotifType | None = None
    motif_id: UUID | None = None

    @model_validator(mode="after")
    def valid_change(self):
        if self.label is not None and not self.label.strip():
            raise ValueError("Label must not be blank")
        if self.removed and any(
            value is not None for value in (self.label, self.type, self.motif_id)
        ):
            raise ValueError("Removal cannot include replacement fields")
        if not self.removed and not any(
            value is not None for value in (self.label, self.type, self.motif_id)
        ):
            raise ValueError("Supply a correction, an identity, or removed=true")
        return self


class DetailResult(Contract):
    dream_id: UUID
    revision_id: UUID
    dream_version: int
    detail_key: str
    removed: bool
    corrected_value: dict
    memory_version: int
    analysis_job_id: UUID | None


class FollowupPut(TextContract):
    expected_version: int = Field(ge=1)
    question_version: str = Field(pattern=r"^[a-f0-9]{40}$")
    expected_answer_version: int = Field(default=0, ge=0)
    answer: str | None = Field(default=None, min_length=1, max_length=1000)
    skipped: bool = Field(default=False, strict=True)
    save_as_association: bool = Field(default=False, strict=True)
    motif_id: UUID | None = None

    @model_validator(mode="after")
    def valid_answer(self):
        if self.skipped:
            if self.answer is not None or self.save_as_association or self.motif_id is not None:
                raise ValueError("Skipping cannot save an answer or association")
        elif self.answer is None or not self.answer.strip():
            raise ValueError("Supply an answer or skipped=true")
        if self.save_as_association != (self.motif_id is not None):
            raise ValueError("Saving personal meaning requires an explicit motif_id")
        return self


class FollowupAnswer(Contract):
    id: UUID
    version: int
    question_version: str
    answer: str | None
    skipped: bool
    save_as_association: bool
    association_id: UUID | None


class FollowupView(Contract):
    dream_id: UUID
    revision_id: UUID
    dream_version: int
    question_version: str | None
    question: str | None
    status: Literal["available", "answered", "skipped", "not_available"]
    latest_answer: FollowupAnswer | None
