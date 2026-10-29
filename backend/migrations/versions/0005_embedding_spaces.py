"""Versioned tenant embedding spaces and deterministic source chunks."""

from alembic import op

from app.db.schema_v1 import build_metadata
from app.db.schema_v4 import extend_metadata

revision = "0005_embedding_spaces"
down_revision = "0004_worker_checkpoints"
branch_labels = None
depends_on = None


def upgrade():
    for table in extend_metadata(build_metadata(None)):
        table.create(op.get_bind())
        name = table.name
        op.execute(f"ALTER TABLE app.{name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{name} FORCE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON app.{name} FROM PUBLIC")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON app.{name} TO dreamthread_runtime")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON app.{name} TO dreamthread_runtime
            USING (user_id = nullif(current_setting('app.user_id', true), '')::uuid)
            WITH CHECK (user_id = nullif(current_setting('app.user_id', true), '')::uuid)
        """)
        op.execute(f"""
            CREATE TRIGGER touch_updated_at BEFORE UPDATE ON app.{name}
            FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at()
        """)


def downgrade():
    op.drop_table("embedding_chunks", schema="app")
    op.drop_table("embedding_spaces", schema="app")
