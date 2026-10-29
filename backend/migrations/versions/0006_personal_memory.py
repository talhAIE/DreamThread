"""Personal memory provenance/forgetting and durable follow-up versions."""

import sqlalchemy as sa
from alembic import op

revision = "0006_personal_memory"
down_revision = "0005_embedding_spaces"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "associations",
        sa.Column("origin", sa.String(20), nullable=False, server_default="legacy"),
        schema="app",
    )
    op.add_column("associations", sa.Column("proposal_hash", sa.String(64)), schema="app")
    op.add_column("associations", sa.Column("deleted_at", sa.DateTime(timezone=True)), schema="app")
    op.create_check_constraint(
        "ck_association_origin", "associations", "origin IN ('legacy','user','model')", schema="app"
    )
    op.create_index(
        "ix_association_proposal",
        "associations",
        ["user_id", "motif_id", "proposal_hash"],
        schema="app",
    )
    op.add_column(
        "followup_answers",
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        schema="app",
    )
    op.add_column(
        "followup_answers",
        sa.Column("question_text", sa.Text, nullable=False, server_default=""),
        schema="app",
    )
    op.add_column(
        "followup_answers",
        sa.Column("save_as_association", sa.Boolean, nullable=False, server_default="false"),
        schema="app",
    )
    op.create_check_constraint(
        "ck_followup_version", "followup_answers", "version >= 1", schema="app"
    )


def downgrade():
    op.drop_constraint("ck_followup_version", "followup_answers", schema="app")
    for name in ("save_as_association", "question_text", "version"):
        op.drop_column("followup_answers", name, schema="app")
    op.drop_index("ix_association_proposal", table_name="associations", schema="app")
    op.drop_constraint("ck_association_origin", "associations", schema="app")
    for name in ("deleted_at", "proposal_hash", "origin"):
        op.drop_column("associations", name, schema="app")
