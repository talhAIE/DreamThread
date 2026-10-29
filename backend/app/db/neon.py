"""Neon-specific setup validation and SQL-created, narrowly privileged login roles."""

from psycopg import sql
from sqlalchemy import text

from app.db.connections import postgres_url
from app.db.roles import elevated_role_sql

LOGIN_GROUPS = {
    "DATABASE_URL": ("dreamthread_app", "dreamthread_runtime"),
    "MIGRATION_DATABASE_URL": ("dreamthread_migrator", "dreamthread_owner"),
    "QUEUE_DATABASE_URL": ("dreamthread_claimer", "dreamthread_queue"),
}


def neon_urls(values):
    urls = {
        key: postgres_url(values.get(key, ""), neon_required=True)
        for key in ("ADMIN_DATABASE_URL", *LOGIN_GROUPS)
    }
    admin = urls["ADMIN_DATABASE_URL"]
    if any("CHANGE_ME" in url.password or "YOUR_ENDPOINT" in url.host for url in urls.values()):
        raise ValueError("Replace Neon placeholders before provisioning")
    for key, (role, _) in LOGIN_GROUPS.items():
        url = urls[key]
        if (url.host, url.port or 5432, url.database) != (
            admin.host,
            admin.port or 5432,
            admin.database,
        ) or url.username != role:
            raise ValueError(
                "Neon URLs must use one direct endpoint/database and separate SQL logins"
            )
    if admin.username in {role for role, _ in LOGIN_GROUPS.values()}:
        raise ValueError("ADMIN_DATABASE_URL must use the separate Neon administrator")
    if len({urls[key].password for key in LOGIN_GROUPS}) != 3:
        raise ValueError("Use distinct private passwords for the three SQL logins")
    return urls


def provision_neon_logins(connection, urls):
    for key, (role, group) in LOGIN_GROUPS.items():
        existing = connection.scalar(
            text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:r)"), {"r": role}
        )
        if not existing:
            # psycopg safely quotes identifiers/password literals. Never interpolate secrets
            # into SQLAlchemy statement/error text; CLI errors are sanitized as a second fence.
            raw = connection.connection.driver_connection
            with raw.cursor() as cursor:
                cursor.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(urls[key].password)
                    )
                )
        unsafe = connection.scalar(text("SELECT " + elevated_role_sql(":role")), {"role": role})
        if unsafe:
            raise RuntimeError(
                "Existing SQL login has administrator privileges; use a restricted role"
            )
        parents = (
            connection.execute(
                text("""
            SELECT p.rolname FROM pg_auth_members m JOIN pg_roles p ON p.oid=m.roleid
            JOIN pg_roles child ON child.oid=m.member WHERE child.rolname=:role
        """),
                {"role": role},
            )
            .scalars()
            .all()
        )
        if any(parent != group for parent in parents):
            raise RuntimeError("SQL login has unexpected inherited roles")
        connection.execute(text(f"GRANT {group} TO {role}"))
        # CONNECT is often PUBLIC by default; explicitly grant it to avoid depending on that.
        connection.execute(
            text(f"""
            DO $$ BEGIN EXECUTE format('GRANT CONNECT ON DATABASE %I TO {role}',
                                       current_database()); END $$
        """)
        )
