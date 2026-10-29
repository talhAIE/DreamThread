import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError

from app.config import Settings
from app.db.connections import connect_args, postgres_url
from app.db.neon import LOGIN_GROUPS, neon_urls, provision_neon_logins
from app.db.provision import provision
from app.db.roles import unsafe_login
from app.workers.queue import Queue

HOST = "ep-test-example.us-east-2.aws.neon.tech"
ADMIN = (
    f"postgresql://neondb_owner:private-admin@{HOST}/neondb?sslmode=require&channel_binding=require"
)


@pytest.mark.parametrize("encrypted", [True, False])
def test_hosted_tls_check_uses_client_connection(encrypted):
    from scripts.check_neon import require_client_tls

    raw = SimpleNamespace(
        driver_connection=SimpleNamespace(pgconn=SimpleNamespace(ssl_in_use=encrypted))
    )
    connection = SimpleNamespace(get_raw_connection=AsyncMock(return_value=raw))
    if encrypted:
        asyncio.run(require_client_tls(connection))
    else:
        with pytest.raises(ValueError, match="Client TLS required"):
            asyncio.run(require_client_tls(connection))


def urls():
    admin = postgres_url(ADMIN)
    return {
        "ADMIN_DATABASE_URL": ADMIN,
        **{
            key: admin.set(username=role, password="unique-" + role).render_as_string(
                hide_password=False
            )
            for key, (role, _) in LOGIN_GROUPS.items()
        },
    }


def test_neon_urls_normalize_driver_and_require_verified_tls_with_certifi():
    url = postgres_url(ADMIN, neon_required=True)
    assert url.drivername == "postgresql+psycopg"
    assert url.query["sslmode"] == "verify-full" and url.query["channel_binding"] == "require"
    args = connect_args(url.render_as_string(hide_password=False), 20)
    assert args["connect_timeout"] == 20 and Path(args["sslrootcert"]).is_file()
    explicit = url.update_query_dict({"sslrootcert": "/custom/ca.pem"})
    assert "sslrootcert" not in connect_args(explicit.render_as_string(hide_password=False))
    assert "sslrootcert" not in connect_args("postgresql+psycopg://test@localhost/test")


@pytest.mark.parametrize(
    "value",
    [
        ADMIN.replace("ep-test-example", "ep-test-example-pooler"),
        ADMIN.replace("sslmode=require", "sslmode=disable"),
        ADMIN.replace("neondb_owner:private-admin@", "neondb_owner@"),
        "https://neondb_owner:private-admin@example.com/neondb",
        "postgresql://user:private-admin@localhost/neondb",
    ],
)
def test_unsafe_neon_url_rejected_without_credential_disclosure(value):
    with pytest.raises(ValueError) as error:
        postgres_url(value, neon_required=True)
    assert "private-admin" not in str(error.value)


def test_neon_settings_normalize_before_production_tls_validation_and_hide_secrets():
    config = Settings(_env_file=None, database_url=ADMIN)
    assert "sslmode=verify-full" in config.database_url.get_secret_value()
    assert "private-admin" not in repr(config)
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None, database_url=ADMIN.replace("ep-test-example", "ep-test-example-pooler")
        )


@pytest.mark.parametrize("change", ["endpoint", "database", "login", "passwords", "admin_login"])
def test_neon_setup_rejects_mixed_projects_roles_and_reused_passwords(change):
    values = urls()
    if change == "endpoint":
        values["DATABASE_URL"] = values["DATABASE_URL"].replace("ep-test-example", "ep-other")
    elif change == "database":
        values["DATABASE_URL"] = values["DATABASE_URL"].replace("/neondb?", "/other?")
    elif change == "login":
        values["DATABASE_URL"] = values["DATABASE_URL"].replace("dreamthread_app:", "neondb_owner:")
    elif change == "passwords":
        parsed = postgres_url(values["QUEUE_DATABASE_URL"]).set(password="unique-dreamthread_app")
        values["QUEUE_DATABASE_URL"] = parsed.render_as_string(hide_password=False)
    else:
        values["ADMIN_DATABASE_URL"] = values["DATABASE_URL"]
    with pytest.raises(ValueError):
        neon_urls(values)


def generator():
    path = Path(__file__).parents[1] / "scripts/configure_neon.py"
    spec = importlib.util.spec_from_file_location("configure_neon", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.configure


def test_configure_neon_generates_private_urls_idempotently_without_overwriting_other_settings(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("ADMIN_DATABASE_URL", raising=False)
    path = tmp_path / ".env"
    path.write_text(
        f"ADMIN_DATABASE_URL='{ADMIN}'\nNEBIUS_API_KEY='preserve-this-secret'\nPROMPT_VERSION=v2\n"
    )
    assert generator()(path) == 3
    first = dotenv_values(path)
    parsed = neon_urls(first)
    assert len({parsed[key].password for key in LOGIN_GROUPS}) == 3
    assert all(len(parsed[key].password) >= 40 for key in LOGIN_GROUPS)
    assert first["NEBIUS_API_KEY"] == "preserve-this-secret"
    assert generator()(path) == 3
    assert dotenv_values(path) == first


def test_generator_does_not_replace_existing_other_project_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_DATABASE_URL", raising=False)
    path = tmp_path / ".env"
    values = urls()
    values["DATABASE_URL"] = values["DATABASE_URL"].replace("ep-test-example", "ep-other")
    original = "\n".join(f"{key}='{value}'" for key, value in values.items())
    path.write_text(original)
    with pytest.raises(ValueError):
        generator()(path)
    assert path.read_text() == original


def test_neon_compose_has_no_local_db_or_admin_secrets():
    compose = (Path(__file__).parents[1] / "compose.yaml").read_text(encoding="utf-8-sig")
    assert "  db:" not in compose and "POSTGRES_PASSWORD" not in compose
    assert "ADMIN_DATABASE_URL:" not in compose and "MIGRATION_DATABASE_URL:" not in compose
    assert "local-runtime-only" not in compose and "  api:" in compose and "  worker:" in compose


def test_managed_role_guard_checks_inherited_neon_and_shared_data_privileges():
    async def check():
        connection = AsyncMock()
        connection.scalar.return_value = True
        assert await unsafe_login(connection)
        sql = str(connection.scalar.call_args.args[0])
        assert "neon_superuser" in sql and "pg_read_all_data" in sql and "pg_write_all_data" in sql
        assert "rolcreaterole" in sql and "rolreplication" in sql

    asyncio.run(check())


def test_queue_rejects_inherited_neon_admin_even_when_direct_attributes_are_safe():
    class Begin:
        async def __aenter__(self):
            connection = AsyncMock()
            connection.scalar.side_effect = [
                True,
                True,
            ]  # queue membership, inherited administration
            return connection

        async def __aexit__(self, *args):
            pass

    class Engine:
        def connect(self):
            return Begin()

    async def check():
        queue = object.__new__(Queue)
        queue.engine = Engine()
        with pytest.raises(RuntimeError, match="restricted queue role"):
            await queue.check_role()

    asyncio.run(check())


def test_provision_avoids_unavailable_superuser_role_alterations():
    class Connection:
        statements = []

        def scalar(self, statement, parameters=None):
            sql = str(statement)
            if "EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:r)" in sql:
                return False
            if "n.nspname FROM pg_extension" in sql:
                return "extensions"
            return False

        def execute(self, statement, parameters=None):
            self.statements.append(str(statement))

    connection = Connection()
    provision(connection)
    sql = " ".join(connection.statements)
    assert "CREATE ROLE dreamthread_runtime NOLOGIN" in sql
    assert "ALTER ROLE" not in sql and "BYPASSRLS" not in sql
    assert "CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions" in sql


def test_sql_login_creation_quotes_passwords_and_grants_separate_groups():
    statements, ddl = [], []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query):
            ddl.append(query.as_string())

    class Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class Connection:
        connection = SimpleNamespace(driver_connection=SimpleNamespace(cursor=Cursor))

        def scalar(self, statement, parameters=None):
            return False

        def execute(self, statement, parameters=None):
            statements.append(str(statement))
            return Result()

    parsed = neon_urls(urls())
    parsed["DATABASE_URL"] = parsed["DATABASE_URL"].set(password="synthetic'quoted-password")
    provision_neon_logins(Connection(), parsed)
    assert len(ddl) == 3
    assert 'CREATE ROLE "dreamthread_app" LOGIN PASSWORD' in ddl[0]
    assert "synthetic''quoted-password" in ddl[0]
    assert "GRANT dreamthread_runtime TO dreamthread_app" in statements
    assert "GRANT dreamthread_owner TO dreamthread_migrator" in statements
    assert "GRANT dreamthread_queue TO dreamthread_claimer" in statements


def test_neon_provision_rejects_existing_administrator_login_without_resetting_password():
    class Connection:
        calls = 0

        def scalar(self, statement, parameters=None):
            self.calls += 1
            return True  # role exists, then elevated attributes/membership found

        def execute(self, *args):
            pytest.fail("Must not grant rights or reset a password")

    with pytest.raises(RuntimeError, match="administrator privileges"):
        provision_neon_logins(Connection(), neon_urls(urls()))
