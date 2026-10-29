"""Conservative cache invalidation until the later evidence dependency engine exists."""

from sqlalchemy import update

from app.db.models import tables


async def invalidate_history(connection, user_id):
    # New entries can invalidate claims about counts, first occurrences and monthly history.
    messages = tables["app.messages"]
    await connection.execute(
        update(messages)
        .where(messages.c.user_id == user_id, messages.c.role == "assistant")
        .values(status="stale", body="")
    )
    summaries = tables["app.monthly_summaries"]
    await connection.execute(
        update(summaries).where(summaries.c.user_id == user_id).values(state="stale", summary={})
    )
    stories = tables["app.storylines"]
    await connection.execute(
        update(stories).where(stories.c.user_id == user_id).values(status="stale")
    )


async def invalidate_sources(connection, user_id):
    """Edits/deletion invalidate all owner-derived results, preserving decision records.

    Later phases may replace this with precise dependency traversal. Never serve these
    cached fields unless state is ready and every source revision is current.
    """
    await invalidate_history(connection, user_id)
    analyses = tables["app.analyses"]
    await connection.execute(
        update(analyses)
        .where(analyses.c.user_id == user_id)
        .values(state="stale", extraction={}, reflections=[], followup=None)
    )
    connections = tables["app.connections"]
    await connection.execute(
        update(connections).where(connections.c.user_id == user_id).values(observation="")
    )
    await connection.execute(
        update(connections)
        .where(connections.c.user_id == user_id, connections.c.status != "rejected")
        .values(status="stale")
    )
    dreams = tables["app.dreams"]
    await connection.execute(
        update(dreams)
        .where(dreams.c.user_id == user_id, dreams.c.lifecycle == "saved")
        .values(analysis_state="stale")
    )
    jobs = tables["app.jobs"]
    await connection.execute(
        update(jobs)
        .where(jobs.c.user_id == user_id, jobs.c.status.in_(["queued", "running"]))
        .values(
            status="cancelled",
            stage="superseded",
            lease_token=None,
            leased_until=None,
            error_code="source_changed",
        )
    )
