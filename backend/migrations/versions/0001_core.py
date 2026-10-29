"""Create the complete relational schema and enforce tenant boundaries."""

import os

from alembic import op
from sqlalchemy import text

from app.db.schema_v1 import build_metadata
from app.db.security_v1 import secure_tables

revision = "0001_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    raw = op.get_context().config.attributes.get("embedding_dimensions")
    if raw is None:
        raw = os.environ.get("EMBEDDING_DIMENSIONS")
    try:
        dimensions = int(raw)
        if not 1 <= dimensions <= 16000:
            raise ValueError
    except (TypeError, ValueError):
        raise RuntimeError("Set EMBEDDING_DIMENSIONS to the measured provider dimension") from None
    connection = op.get_bind()
    exists = connection.scalar(text(
        "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'vector')"
    ))
    if not exists:
        raise RuntimeError("Administrator must provision pgvector in extensions schema first")
    metadata = build_metadata(dimensions)
    metadata.create_all(connection)
    connection.execute(text(
        "INSERT INTO app.schema_configuration(id, embedding_dimensions) VALUES (1, :d)"
    ), {"d": dimensions})
    secure_tables(connection, metadata)


def downgrade():
    connection = op.get_bind()
    # Leave extension and cluster roles alone: they can be shared with other databases/services.
    build_metadata(None).drop_all(connection)
    connection.execute(text("DROP FUNCTION app.touch_updated_at()"))
    connection.execute(text("DROP FUNCTION app.reject_revision_update()"))
