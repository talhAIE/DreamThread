"""Frozen Phase 8 provenance, forgetting tombstones and versioned follow-up answers."""

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Index, Integer, String, Text


def extend_metadata(metadata):
    associations = metadata.tables["app.associations"]
    associations.append_column(
        Column("origin", String(20), nullable=False, server_default="legacy")
    )
    associations.append_column(Column("proposal_hash", String(64)))
    associations.append_column(Column("deleted_at", DateTime(timezone=True)))
    associations.append_constraint(
        CheckConstraint(
            "origin IN ('legacy','user','model')",
            name="ck_association_origin",
        )
    )
    Index(
        "ix_association_proposal",
        associations.c.user_id,
        associations.c.motif_id,
        associations.c.proposal_hash,
    )
    answers = metadata.tables["app.followup_answers"]
    answers.append_column(Column("version", Integer, nullable=False, server_default="1"))
    answers.append_column(Column("question_text", Text, nullable=False, server_default=""))
    answers.append_column(
        Column("save_as_association", Boolean, nullable=False, server_default="false")
    )
    answers.append_constraint(CheckConstraint("version >= 1", name="ck_followup_version"))
