"""Frozen security DDL shared by initial migrations. No credentials in SQL."""

from sqlalchemy import text


def secure_tables(connection, metadata):
    for table in metadata.tables.values():
        name = table.name
        if name == "schema_configuration":
            connection.execute(text("REVOKE ALL ON app.schema_configuration FROM PUBLIC"))
            connection.execute(text(
                "GRANT SELECT ON app.schema_configuration TO dreamthread_runtime"
            ))
            continue
        connection.execute(text(f'ALTER TABLE app."{name}" ENABLE ROW LEVEL SECURITY'))
        connection.execute(text(f'ALTER TABLE app."{name}" FORCE ROW LEVEL SECURITY'))
        connection.execute(text(f'REVOKE ALL ON app."{name}" FROM PUBLIC'))
        connection.execute(text(
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON app."{name}" TO dreamthread_runtime'
        ))
        identity = "id" if name == "accounts" else "user_id"
        predicate = (
            f"{identity} = nullif(current_setting('app.user_id', true), '')::uuid"
        )
        connection.execute(text(
            f'CREATE POLICY tenant_isolation ON app."{name}" TO dreamthread_runtime '
            f'USING ({predicate}) WITH CHECK ({predicate})'
        ))
    connection.execute(text("REVOKE UPDATE ON app.dream_revisions FROM dreamthread_runtime"))
    connection.execute(text("GRANT SELECT ON app.jobs TO dreamthread_queue"))
    connection.execute(text("""
        GRANT UPDATE (status, lease_token, leased_until, heartbeat_at, attempts)
        ON app.jobs TO dreamthread_queue
    """))
    connection.execute(text("""
        CREATE POLICY queue_leasing ON app.jobs TO dreamthread_queue
        USING (true) WITH CHECK (true)
    """))
    for role in ("anon", "authenticated", "service_role"):
        exists = connection.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:r)"),
                                   {"r": role})
        if exists:
            connection.execute(text(f"REVOKE ALL ON SCHEMA app FROM {role}"))
            connection.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA app FROM {role}"))
            connection.execute(text(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA app REVOKE ALL ON TABLES FROM {role}"
            ))
    connection.execute(text("REVOKE ALL ON SCHEMA app FROM PUBLIC"))
    connection.execute(text(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA app REVOKE ALL ON TABLES FROM PUBLIC"
    ))
    connection.execute(text(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA app REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
    ))
    connection.execute(text("GRANT USAGE ON SCHEMA app TO dreamthread_runtime, dreamthread_queue"))
    connection.execute(text("GRANT SELECT ON app.alembic_version TO dreamthread_runtime"))
    connection.execute(text("""
        CREATE FUNCTION app.touch_updated_at() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN NEW.updated_at = now(); RETURN NEW; END $$
    """))
    connection.execute(text("""
        CREATE FUNCTION app.reject_revision_update() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN RAISE EXCEPTION 'dream revisions are immutable' USING ERRCODE='23000'; END $$
    """))
    connection.execute(text("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app FROM PUBLIC"))
    for table in metadata.tables.values():
        if "updated_at" in table.c:
            connection.execute(text(
                f'CREATE TRIGGER touch_updated_at BEFORE UPDATE ON app."{table.name}" '
                'FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at()'
            ))
    connection.execute(text("""
        CREATE TRIGGER immutable_revision BEFORE UPDATE ON app.dream_revisions
        FOR EACH ROW EXECUTE FUNCTION app.reject_revision_update()
    """))
