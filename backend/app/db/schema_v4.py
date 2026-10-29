"""Frozen Phase 7 versioned embedding spaces and source-aligned chunks."""

from uuid import uuid4

from pgvector.sqlalchemy import VECTOR
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB


def extend_metadata(metadata):
    def owned(name, *columns, constraints=()):
        table = Table(
            name,
            metadata,
            Column("id", Uuid, primary_key=True, default=uuid4),
            Column("user_id", Uuid, nullable=False),
            Column(
                "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
            ),
            Column(
                "updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()
            ),
            *columns,
            UniqueConstraint("user_id", "id", name=f"uq_{name}_owner_id"),
            ForeignKeyConstraint(["user_id"], ["app.accounts.id"], ondelete="CASCADE"),
            *constraints,
        )
        Index(f"ix_{name}_owner", table.c.user_id)
        return table

    spaces = owned(
        "embedding_spaces",
        Column("version", String(50), nullable=False),
        Column("model_id", String(200), nullable=False),
        Column("dimensions", Integer, nullable=False),
        Column("fingerprint", String(64), nullable=False),
        Column("configuration", JSONB, nullable=False),
        Column("state", String(20), nullable=False, server_default="building"),
        constraints=(
            UniqueConstraint("user_id", "version", name="uq_embedding_spaces_version"),
            UniqueConstraint("user_id", "id", "dimensions", name="uq_embedding_spaces_dimension"),
            CheckConstraint(
                "state IN ('building','active','retired')", name="ck_embedding_space_state"
            ),
            CheckConstraint("dimensions BETWEEN 1 AND 16000", name="ck_embedding_space_dimension"),
        ),
    )
    Index(
        "uq_embedding_spaces_active",
        spaces.c.user_id,
        unique=True,
        postgresql_where=text("state = 'active'"),
    )
    chunks = owned(
        "embedding_chunks",
        Column("space_id", Uuid, nullable=False),
        Column("dream_id", Uuid, nullable=False),
        Column("revision_id", Uuid, nullable=False),
        Column("chunk_index", Integer, nullable=False),
        Column("start", Integer, nullable=False),
        Column("end", Integer, nullable=False),
        Column("content_hash", String(64), nullable=False),
        Column("input_hash", String(64), nullable=False),
        Column("query_input_hash", String(64), nullable=False),
        Column("dimensions", Integer, nullable=False),
        Column("embedding", VECTOR(), nullable=False),
        Column("query_embedding", VECTOR(), nullable=False),
        constraints=(
            UniqueConstraint(
                "user_id",
                "space_id",
                "revision_id",
                "chunk_index",
                name="uq_embedding_chunks_source",
            ),
            ForeignKeyConstraint(
                ["user_id", "space_id", "dimensions"],
                [spaces.c.user_id, spaces.c.id, spaces.c.dimensions],
                ondelete="CASCADE",
            ),
            ForeignKeyConstraint(
                ["user_id", "dream_id", "revision_id"],
                [
                    "app.dream_revisions.user_id",
                    "app.dream_revisions.dream_id",
                    "app.dream_revisions.id",
                ],
                ondelete="CASCADE",
            ),
            CheckConstraint(
                'chunk_index >= 0 AND "start" >= 0 AND "end" > "start"',
                name="ck_embedding_chunk_span",
            ),
            CheckConstraint(
                "extensions.vector_dims(embedding) = dimensions AND "
                "extensions.vector_dims(query_embedding) = dimensions",
                name="ck_embedding_chunk_dimension",
            ),
        ),
    )
    return spaces, chunks
