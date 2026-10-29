"""Strict provider contracts and application-assigned evidence identities."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.dreams import postgres_text


class StrictOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    @field_validator("*", mode="after")
    @classmethod
    def valid_strings(cls, value):
        return postgres_text(value) if isinstance(value, str) else value


class Citation(StrictOutput):
    dream_id: UUID
    revision_id: UUID
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    quote: str = Field(min_length=1, max_length=20000)


class Observation(StrictOutput):
    key: str = Field(pattern=r"^o[1-9][0-9]?$", max_length=3)
    type: Literal["place", "person", "object", "event", "emotion"]
    label: str = Field(min_length=1, max_length=120)
    citation: Citation


class Extraction(StrictOutput):
    observations: list[Observation] = Field(max_length=24)


class Reflection(StrictOutput):
    text: str = Field(min_length=1, max_length=600)
    kind: Literal["tentative_reflection"]
    observation_keys: list[str] = Field(min_length=1, max_length=8)


class ReflectionOutput(StrictOutput):
    possible_reflections: list[Reflection] = Field(min_length=1, max_length=3)
    followup_question: str | None = Field(max_length=300)


class VerifiedEvidence(Citation):
    id: UUID
