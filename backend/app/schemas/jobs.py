from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.identity import Contract


class JobRetry(Contract):
    expected_attempts: int = Field(ge=0)
    expected_submission: int = Field(ge=1)


class JobView(Contract):
    id: UUID
    kind: str
    status: str
    stage: str
    attempts: int
    max_attempts: int
    submission: int
    run_after: datetime
    error_code: str | None
    error_message: str | None
    retry_allowed: bool
    checkpoint_stages: list[str]
    created_at: datetime
    updated_at: datetime
