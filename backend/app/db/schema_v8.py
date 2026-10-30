"""Indexed current-revision full-text retrieval for continuity candidates."""

from sqlalchemy import Index, func, literal_column


def extend_metadata(metadata):
    revisions = metadata.tables["app.dream_revisions"]
    Index(
        "ix_dream_revisions_text_search",
        func.to_tsvector(literal_column("'simple'::regconfig"), revisions.c.text),
        postgresql_using="gin",
        _table=revisions,
    )
