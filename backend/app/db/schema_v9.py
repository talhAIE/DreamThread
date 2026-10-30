"""Not-before fencing for private object cleanup, including in-flight uploads."""

from sqlalchemy import Column, DateTime, func


def extend_metadata(metadata):
    metadata.tables["app.cleanup_tasks"].append_column(
        Column("not_before", DateTime(timezone=True), nullable=False, server_default=func.now())
    )
