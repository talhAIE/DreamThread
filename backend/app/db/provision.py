"""Administrator-only provisioning; passwords/login roles are configured separately."""

from sqlalchemy import text

from app.db.roles import elevated_role_sql

ROLES = ("dreamthread_owner", "dreamthread_runtime", "dreamthread_queue")


def provision(connection):
    for role in ROLES:
        exists = connection.scalar(
            text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:r)"), {"r": role}
        )
        if not exists:
            connection.execute(text(f"CREATE ROLE {role} NOLOGIN"))
        # New SQL roles have safe attributes by default. Managed Neon administrators cannot
        # alter superuser/BYPASSRLS flags; reject unsafe existing roles instead of trying to
        # demote them or silently accepting inherited administration.
        unsafe = connection.scalar(text("SELECT " + elevated_role_sql(":role")), {"role": role})
        login = connection.scalar(
            text("SELECT rolcanlogin FROM pg_roles WHERE rolname=:role"), {"role": role}
        )
        if unsafe or login:
            raise RuntimeError("Existing DreamThread privilege group has unsafe attributes")
        cross_group = connection.scalar(
            text("""
            SELECT EXISTS(SELECT 1 FROM pg_roles p WHERE p.rolname = ANY(:groups)
                          AND p.rolname != :role AND pg_has_role(:role, p.oid, 'MEMBER'))
        """),
            {"groups": list(ROLES), "role": role},
        )
        if cross_group:
            raise RuntimeError("DreamThread privilege groups must not inherit each other")
    connection.execute(text("CREATE SCHEMA IF NOT EXISTS extensions"))
    connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions"))
    extension_schema = connection.scalar(
        text("""
        SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
        WHERE e.extname='vector'
    """)
    )
    if extension_schema != "extensions":
        raise RuntimeError("Existing vector extension must be in schema extensions")
    connection.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
    connection.execute(
        text("""
        GRANT USAGE ON SCHEMA extensions
        TO dreamthread_owner, dreamthread_runtime, dreamthread_queue
    """)
    )
    # Quote the database identifier using PostgreSQL format, not string interpolation.
    connection.execute(
        text("""
        DO $$ BEGIN EXECUTE format('GRANT CREATE ON DATABASE %I TO dreamthread_owner',
                                   current_database()); END $$
    """)
    )
