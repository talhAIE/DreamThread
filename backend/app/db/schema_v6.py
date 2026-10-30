"""Private database-backed export payloads."""

from sqlalchemy import Column, Text


def extend_metadata(metadata):
    metadata.tables["app.exports"].append_column(Column("content", Text))
