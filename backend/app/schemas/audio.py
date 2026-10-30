from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.schemas.identity import Contract

AudioMime = Literal["audio/wav", "audio/webm", "audio/ogg", "audio/mpeg", "audio/mp4"]


class UploadIntent(Contract):
    content_type: AudioMime
    size_bytes: int = Field(gt=0, le=10485760)


class UploadGranted(Contract):
    asset_id: UUID
    upload_url: str
    upload_method: Literal["PUT"] = "PUT"
    expires_at: datetime
    max_bytes: int


class AudioStatus(Contract):
    id: UUID
    status: str
    expires_at: datetime
    job_id: UUID | None = None
    transcript_draft: str | None = None
    duration_seconds: float | None = None
    error_code: str | None = None
