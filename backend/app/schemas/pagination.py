"""Stable opaque cursors for future dream listings; always order date DESC, id DESC."""

import base64
import binascii
import json
from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.errors import APIError


class PageParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=256)


def encode_cursor(dream_date: date, dream_id: UUID) -> str:
    raw = json.dumps([dream_date.isoformat(), str(dream_id)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[date, UUID]:
    try:
        if len(cursor) > 256:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) != 2:
            raise ValueError
        return date.fromisoformat(values[0]), UUID(values[1])
    except (ValueError, TypeError, binascii.Error):
        raise APIError("invalid_cursor", "Invalid pagination cursor", 422) from None
