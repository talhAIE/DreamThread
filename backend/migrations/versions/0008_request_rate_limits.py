"""Add a bounded tenant request-window counter."""

import sqlalchemy as sa
from alembic import op

revision = "0008_request_rate_limits"
down_revision = "0007_insights_privacy"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "api_rate_limits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("route", sa.String(length=40), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("request_count >= 1", name="ck_api_rate_limits_count"),
        sa.ForeignKeyConstraint(["user_id"], ["app.accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_api_rate_limits"),
        sa.UniqueConstraint("user_id", "id", name="uq_api_rate_limits_owner_id"),
        sa.UniqueConstraint("user_id", "route", name="uq_api_rate_limits_route"),
        schema="app",
    )
    op.create_index(
        "ix_api_rate_limits_owner",
        "api_rate_limits",
        ["user_id"],
        schema="app",
    )
    op.execute("ALTER TABLE app.api_rate_limits ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.api_rate_limits FORCE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON app.api_rate_limits FROM PUBLIC")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON app.api_rate_limits TO dreamthread_runtime"
    )
    op.execute("""
        CREATE POLICY tenant_isolation ON app.api_rate_limits TO dreamthread_runtime
        USING (user_id = nullif(current_setting('app.user_id', true), '')::uuid)
        WITH CHECK (user_id = nullif(current_setting('app.user_id', true), '')::uuid)
    """)
    op.execute("""
        CREATE TRIGGER touch_updated_at BEFORE UPDATE ON app.api_rate_limits
        FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at()
    """)


def downgrade():
    op.drop_table("api_rate_limits", schema="app")
