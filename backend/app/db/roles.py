"""Reject inherited managed-service administration as well as direct role attributes."""

from sqlalchemy import text


def elevated_role_sql(role_expression="current_user"):
    # role_expression is a source constant, never client/model input.
    return f"""
        EXISTS (SELECT 1 FROM pg_roles p WHERE p.rolname = {role_expression}
                AND (p.rolsuper OR p.rolbypassrls OR p.rolcreaterole OR p.rolcreatedb
                     OR p.rolreplication))
        OR EXISTS (SELECT 1 FROM pg_roles p
                   WHERE p.rolname IN ('neon_superuser', 'pg_read_all_data', 'pg_write_all_data')
                     AND pg_has_role({role_expression}, p.oid, 'MEMBER'))
    """


async def unsafe_login(connection):
    return await connection.scalar(text("SELECT " + elevated_role_sql()))
