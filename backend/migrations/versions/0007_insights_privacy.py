"""Store short-lived private exports in the tenant-protected database."""

import sqlalchemy as sa
from alembic import op

revision = "0007_insights_privacy"
down_revision = "0006_personal_memory"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("exports", sa.Column("content", sa.Text(), nullable=True), schema="app")


def downgrade():
    op.drop_column("exports", "content", schema="app")
