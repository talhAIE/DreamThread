"""Migrations only use a separate owner-capable connection, never runtime credentials."""

import os

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool, text

from app.db.models import metadata

load_dotenv()
config = context.config


def run(connection):
    connection.execute(text("SET ROLE dreamthread_owner"))
    connection.execute(text("SET search_path TO app, extensions, public"))
    connection.execute(text("CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION dreamthread_owner"))
    connection.commit()
    context.configure(connection=connection, target_metadata=metadata,
                      version_table_schema="app", include_schemas=True,
                      compare_type=True, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Use online migrations: embedding dimension and roles must be verified")
elif config.attributes.get("connection") is not None:
    run(config.attributes["connection"])
else:
    url = os.environ.get("MIGRATION_DATABASE_URL")
    if not url:
        raise RuntimeError("MIGRATION_DATABASE_URL is required; runtime URL is never a fallback")
    engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True)
    try:
        with engine.connect() as connection:
            run(connection)
    finally:
        engine.dispose()
