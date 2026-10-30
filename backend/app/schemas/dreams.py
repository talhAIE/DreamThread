from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.schemas.identity import Contract, Emotion, Patch
from app.schemas.pagination import PageParams


def postgres_text(value):
    if value is not None:
        if "\x00" in value:
            raise ValueError("Text contains an unsupported character")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Text must be valid Unicode") from None
    return value


class DreamCreate(Contract):
    input_mode: Literal["text", "voice"] = "text"
    audio_asset_id: UUID | None = None
    title: str = Field(default="Untitled dream", min_length=1, max_length=160)
    dream_date: date
    text: str = Field(default="", max_length=20000)
    reported_emotions: list[Emotion] = Field(default_factory=list, max_length=12)
    lifecycle: Literal["draft", "saved"] = "saved"

    @field_validator("title", "text")
    @classmethod
    def supported_text(cls, value):
        return postgres_text(value)

    @field_validator("reported_emotions")
    @classmethod
    def unique_emotions(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("Emotion tags must be unique")
        return values

    @model_validator(mode="after")
    def saved_content(self):
        if (self.input_mode == "voice") != (self.audio_asset_id is not None):
            raise ValueError("Voice entries require an audio asset; text entries cannot attach one")
        if self.input_mode == "voice" and self.lifecycle != "saved":
            raise ValueError("Review the transcript before saving a voice entry")
        if self.lifecycle == "saved" and not self.text.strip():
            raise ValueError("Saved dreams need text")
        return self


class DreamPatch(Patch):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    dream_date: date | None = None
    text: str | None = Field(default=None, max_length=20000)
    reported_emotions: list[Emotion] | None = Field(default=None, max_length=12)
    lifecycle: Literal["draft", "saved"] | None = None

    @field_validator("title", "text")
    @classmethod
    def supported_text(cls, value):
        return postgres_text(value)

    @field_validator("reported_emotions")
    @classmethod
    def unique_emotions(cls, values):
        if values is not None:
            return DreamCreate.unique_emotions(values)
        return values


class AnalyzeRequest(Contract):
    expected_version: int = Field(ge=1)


class DreamQuery(PageParams):
    from_date: date | None = None
    to_date: date | None = None
    emotion: Emotion | None = None
    keyword: str | None = Field(default=None, max_length=200)
    lifecycle: Literal["draft", "saved"] | None = None

    @field_validator("keyword")
    @classmethod
    def supported_keyword(cls, value):
        return postgres_text(value)

    @model_validator(mode="after")
    def date_range(self):
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("from_date must not follow to_date")
        return self


class DreamSummary(Contract):
    id: UUID
    title: str
    dream_date: date
    version: int
    revision_id: UUID
    revision: int
    lifecycle: Literal["draft", "saved"]
    input_mode: Literal["text", "voice"]
    reported_emotions: list[Emotion]
    analysis_state: str
    created_at: datetime
    updated_at: datetime


class DreamDetail(DreamSummary):
    text: str
    content_hash: str
    analysis_job_id: UUID | None = None


class DreamPage(Contract):
    items: list[DreamSummary]
    next_cursor: str | None


class Revision(Contract):
    id: UUID
    dream_id: UUID
    revision: int
    text: str
    content_hash: str
    reported_emotions: list[Emotion]
    recorded_at: datetime
    is_current: bool


class AnalysisRequestResult(Contract):
    dream_id: UUID
    revision_id: UUID
    job_id: UUID
    status: str


class AnalysisResult(Contract):
    dream_id: UUID
    revision_id: UUID
    status: str
    analysis_job_id: UUID | None = None
    analysis_id: UUID | None = None
    extraction: dict | None = None
    reflections: list | None = None
    followup: str | None = None
    evidence: list[dict] | None = None
