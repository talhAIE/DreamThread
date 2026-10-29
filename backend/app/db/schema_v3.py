"""Frozen Phase 5 durable worker additions."""

from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Table,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB


def extend_metadata(metadata):
    jobs = metadata.tables["app.jobs"]
    jobs.append_column(Column("deadline_at", DateTime(timezone=True)))
    jobs.append_column(Column("stage_deadline_at", DateTime(timezone=True)))
    artifacts = Table(
        "job_artifacts",
        metadata,
        Column("id", Uuid, primary_key=True, default=uuid4),
        Column("user_id", Uuid, nullable=False),
        Column("job_id", Uuid, nullable=False),
        Column("stage", String(50), nullable=False),
        Column("snapshot_hash", String(64), nullable=False),
        Column("sources", JSONB, nullable=False),
        Column("archive_bound", Boolean, nullable=False, server_default="false"),
        Column("output", JSONB, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        UniqueConstraint("user_id", "id", name="uq_job_artifacts_owner_id"),
        UniqueConstraint("user_id", "job_id", "stage", name="uq_job_artifacts_stage"),
        ForeignKeyConstraint(["user_id"], ["app.accounts.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["user_id", "job_id"], [jobs.c.user_id, jobs.c.id], ondelete="CASCADE"
        ),
    )
    Index("ix_job_artifacts_owner", artifacts.c.user_id)
    return artifacts
