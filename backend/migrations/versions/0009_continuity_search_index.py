"""Index current-source full-text candidate filtering."""

import sqlalchemy as sa
from alembic import op

revision = "0009_continuity_search_index"
down_revision = "0008_request_rate_limits"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_dream_revisions_text_search",
        "dream_revisions",
        [sa.text("to_tsvector('simple'::regconfig, text)")],
        schema="app",
        postgresql_using="gin",
    )


def downgrade():
    op.drop_index(
        "ix_dream_revisions_text_search",
        table_name="dream_revisions",
        schema="app",
    )
