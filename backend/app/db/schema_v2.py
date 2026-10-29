"""Frozen Phase 3 additions. Baseline migration snapshots remain unchanged."""

from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Table,
    UniqueConstraint,
    Uuid,
    func,
)


def extend_metadata(metadata):
    metadata.tables["app.accounts"].append_column(
        Column("version", Integer, nullable=False, server_default="1")
    )
    metadata.tables["app.accounts"].append_column(
        Column("is_anonymous", Boolean, nullable=False, server_default="false")
    )
    metadata.tables["app.preferences"].append_column(
        Column("version", Integer, nullable=False, server_default="1")
    )
    usage = Table(
        "ai_usage", metadata,
        Column("id", Uuid, primary_key=True, default=uuid4),
        Column("user_id", Uuid, nullable=False),
        Column("period", Date, nullable=False),
        Column("used", Integer, nullable=False, server_default="0"),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        UniqueConstraint("user_id", "id", name="uq_ai_usage_owner_id"),
        UniqueConstraint("user_id", "period", name="uq_ai_usage_period"),
        ForeignKeyConstraint(["user_id"], ["app.accounts.id"], ondelete="CASCADE"),
        CheckConstraint("used >= 0", name="ck_ai_usage_nonnegative"),
    )
    Index("ix_ai_usage_owner", usage.c.user_id)
    return usage
