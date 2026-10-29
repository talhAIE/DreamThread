"""Administrator-only provisioning; passwords/login roles are configured separately."""

from sqlalchemy import text

ROLES = ("dreamthread_owner", "dreamthread_runtime", "dreamthread_queue")


def provision(connection):
    for role in ROLES:
        exists = connection.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:r)"),
                                   {"r": role})
        if not exists:
            connection.execute(text(f"CREATE ROLE {role} NOLOGIN"))
        connection.execute(text(
            f"ALTER ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS"
        ))
    connection.execute(text("CREATE SCHEMA IF NOT EXISTS extensions"))
    connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions"))
    extension_schema = connection.scalar(text("""
        SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
        WHERE e.extname='vector'
    """))
    if extension_schema != "extensions":
        raise RuntimeError("Existing vector extension must be in schema extensions")
    connection.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
    connection.execute(text("""
        GRANT USAGE ON SCHEMA extensions
        TO dreamthread_owner, dreamthread_runtime, dreamthread_queue
    """))
    # Quote the database identifier using PostgreSQL format, not string interpolation.
    connection.execute(text("""
        DO $$ BEGIN EXECUTE format('GRANT CREATE ON DATABASE %I TO dreamthread_owner',
                                   current_database()); END $$
    """))
