"""Persist worker deadlines and tenant-private stage artifacts."""

import sqlalchemy as sa
from alembic import op

from app.db.schema_v1 import build_metadata
from app.db.schema_v2 import extend_metadata
from app.db.schema_v3 import extend_metadata as extend_worker_metadata

revision = "0004_worker_checkpoints"
down_revision = "0003_identity_limits"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("deadline_at", "stage_deadline_at"):
        op.add_column("jobs", sa.Column(name, sa.DateTime(timezone=True)), schema="app")
    metadata = build_metadata(None)
    extend_metadata(metadata)
    extend_worker_metadata(metadata).create(op.get_bind())
    op.execute("ALTER TABLE app.job_artifacts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.job_artifacts FORCE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON app.job_artifacts FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.job_artifacts TO dreamthread_runtime")
    op.execute("GRANT UPDATE(deadline_at, stage_deadline_at) ON app.jobs TO dreamthread_queue")
    op.execute("""
        CREATE POLICY tenant_isolation ON app.job_artifacts TO dreamthread_runtime
        USING (user_id = nullif(current_setting('app.user_id', true), '')::uuid)
        WITH CHECK (user_id = nullif(current_setting('app.user_id', true), '')::uuid)
    """)
    op.execute("""
        CREATE TRIGGER touch_updated_at BEFORE UPDATE ON app.job_artifacts
        FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at()
    """)


def downgrade():
    op.drop_table("job_artifacts", schema="app")
    op.execute("REVOKE UPDATE(deadline_at, stage_deadline_at) ON app.jobs FROM dreamthread_queue")
    op.drop_column("jobs", "stage_deadline_at", schema="app")
    op.drop_column("jobs", "deadline_at", schema="app")
