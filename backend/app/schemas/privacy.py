from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.schemas.identity import Contract


class ExportCreate(Contract):
    format: Literal["json", "text"]


class ExportAccepted(Contract):
    id: UUID
    job_id: UUID
    status: str


class ExportStatus(Contract):
    id: UUID
    format: Literal["json", "text"]
    status: str
    created_at: datetime
    expires_at: datetime | None
    download_url: str | None
    pending_storage_cleanup: bool = False


class JournalDeleteConfirmation(Contract):
    confirm: Literal[True]


class JournalDeleteAccepted(Contract):
    job_id: UUID
    status: str


class ExportFormat(Contract):
    schema_version: Literal["1"]
    exported_at: datetime
    archive_version: int = Field(ge=0)
    data_epoch: int = Field(ge=0)
    dreams: list[dict]
    motifs: list[dict]
    associations: list[dict]
    connection_decisions: list[dict]
    storylines: list[dict]
    conversations: list[dict]
