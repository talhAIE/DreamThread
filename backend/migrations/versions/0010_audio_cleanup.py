"""Fence object cleanup behind upload expiry and bounded in-flight storage requests."""

import sqlalchemy as sa
from alembic import op

revision = "0010_audio_cleanup"
down_revision = "0009_continuity_search_index"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "cleanup_tasks",
        sa.Column(
            "not_before", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema="app",
    )
    op.create_index("ix_cleanup_due", "cleanup_tasks", ["state", "not_before"], schema="app")
    op.execute(
        "GRANT SELECT (id, user_id, state, not_before) ON app.cleanup_tasks TO dreamthread_queue"
    )
    op.execute(
        "CREATE POLICY queue_cleanup_schedule ON app.cleanup_tasks "
        "FOR SELECT TO dreamthread_queue USING (true)"
    )


def downgrade():
    op.execute("DROP POLICY queue_cleanup_schedule ON app.cleanup_tasks")
    op.execute(
        "REVOKE SELECT (id, user_id, state, not_before) ON app.cleanup_tasks FROM dreamthread_queue"
    )
    op.drop_index("ix_cleanup_due", table_name="cleanup_tasks", schema="app")
    op.drop_column("cleanup_tasks", "not_before", schema="app")
