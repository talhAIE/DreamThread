"""Tenant-scoped rolling request-window counters."""

from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    Uuid,
    func,
)


def extend_metadata(metadata):
    limits = Table(
        "api_rate_limits",
        metadata,
        Column("id", Uuid, primary_key=True, default=uuid4),
        Column("user_id", Uuid, nullable=False),
        Column("route", String(40), nullable=False),
        Column("window_start", DateTime(timezone=True), nullable=False),
        Column("request_count", Integer, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        UniqueConstraint("user_id", "id", name="uq_api_rate_limits_owner_id"),
        UniqueConstraint("user_id", "route", name="uq_api_rate_limits_route"),
        ForeignKeyConstraint(["user_id"], ["app.accounts.id"], ondelete="CASCADE"),
        CheckConstraint("request_count >= 1", name="ck_api_rate_limits_count"),
    )
    Index("ix_api_rate_limits_owner", limits.c.user_id)
    return limits
