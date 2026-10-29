"""Profile versions, verified anonymous status and shared AI admission counters."""

import sqlalchemy as sa
from alembic import op

from app.db.schema_v1 import build_metadata
from app.db.schema_v2 import extend_metadata

revision = "0003_identity_limits"
down_revision = "0002_revision_guard"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("accounts", sa.Column("version", sa.Integer, nullable=False, server_default="1"),
                  schema="app")
    op.add_column("accounts", sa.Column("is_anonymous", sa.Boolean, nullable=False,
                                       server_default="false"), schema="app")
    op.add_column("preferences", sa.Column("version", sa.Integer, nullable=False,
                                          server_default="1"), schema="app")
    usage = extend_metadata(build_metadata(None))
    usage.create(op.get_bind())
    op.execute("ALTER TABLE app.ai_usage ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.ai_usage FORCE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON app.ai_usage FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.ai_usage TO dreamthread_runtime")
    op.execute("""
        CREATE POLICY tenant_isolation ON app.ai_usage TO dreamthread_runtime
        USING (user_id = nullif(current_setting('app.user_id', true), '')::uuid)
        WITH CHECK (user_id = nullif(current_setting('app.user_id', true), '')::uuid)
    """)
    op.execute("""
        CREATE TRIGGER touch_updated_at BEFORE UPDATE ON app.ai_usage
        FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at()
    """)


def downgrade():
    op.drop_table("ai_usage", schema="app")
    op.drop_column("preferences", "version", schema="app")
    op.drop_column("accounts", "is_anonymous", schema="app")
    op.drop_column("accounts", "version", schema="app")
