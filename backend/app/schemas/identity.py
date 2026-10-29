from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Emotion = Literal[
    "calm", "curious", "afraid", "joyful", "unsure", "sad", "anxious", "excited",
    "confused", "peaceful", "nostalgic", "awe",
]
DreamText = Annotated[str, Field(min_length=1, max_length=20000)]


def timezone_name(value: str) -> str:
    if value not in {"UTC"} and "/" not in value:
        raise ValueError("Use an IANA timezone")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Use an IANA timezone") from None
    return value


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class Me(Contract):
    id: UUID
    display_name: str
    timezone: str
    version: int
    is_anonymous: bool
    created_at: datetime
    updated_at: datetime


class Preferences(Contract):
    auto_connections: bool
    require_memory_confirmation: bool
    timezone: str
    theme: Literal["dark", "light", "system"]
    reduced_motion: bool
    version: int
    updated_at: datetime


class Patch(Contract):
    expected_version: int = Field(ge=1)

    @model_validator(mode="after")
    def changes_required(self):
        changes = self.model_fields_set - {"expected_version"}
        if not changes or any(getattr(self, name) is None for name in changes):
            raise ValueError("Supply at least one non-null update")
        return self

    @field_validator("timezone", check_fields=False)
    @classmethod
    def validate_timezone(cls, value):
        return timezone_name(value) if value is not None else value


class MePatch(Patch):
    display_name: str | None = Field(default=None, max_length=120)
    timezone: str | None = Field(default=None, max_length=80)


class PreferencesPatch(Patch):
    auto_connections: bool | None = Field(default=None, strict=True)
    require_memory_confirmation: bool | None = Field(default=None, strict=True)
    timezone: str | None = Field(default=None, max_length=80)
    theme: Literal["dark", "light", "system"] | None = None
    reduced_motion: bool | None = Field(default=None, strict=True)
