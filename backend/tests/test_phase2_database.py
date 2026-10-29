"""Real PostgreSQL checks. Creates/drops only its own uniquely named test database.

Set TEST_ADMIN_DATABASE_URL to a disposable cluster administrator, never production.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.config import Settings
from app.db import Database
from app.db.models import tables
from app.db.provision import provision
from app.loop import create_loop

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def cluster():
    url = os.environ.get("TEST_ADMIN_DATABASE_URL")
    if not url:
        pytest.skip("TEST_ADMIN_DATABASE_URL is required for isolated migration/RLS tests")
    suffix = uuid4().hex[:12]
    db_name = "dt_test_" + suffix
    names = {key: "dt_" + key + "_" + suffix for key in ("migrator", "app", "queue", "api")}
    admin = create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    local_url = make_url(url).set(database=db_name)
    owner = None
    engines = []
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{db_name}"'))
        owner = create_engine(local_url, hide_parameters=True)
        with owner.begin() as connection:
            for role in ("anon", "authenticated", "service_role"):
                if not connection.scalar(text(
                    "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:r)"
                ), {"r": role}):
                    connection.execute(text(f"CREATE ROLE {role} NOLOGIN"))
            provision(connection)
            for key, role in names.items():
                connection.execute(text(f'CREATE ROLE "{role}" LOGIN PASSWORD \'test-only\''))
                group = {
                    "migrator": "dreamthread_owner", "app": "dreamthread_runtime",
                    "queue": "dreamthread_queue", "api": "authenticated",
                }[key]
                connection.execute(text(f'GRANT {group} TO "{role}"'))
        urls = {key: local_url.set(username=role, password="test-only")
                for key, role in names.items()}
        migrator = create_engine(urls["migrator"], hide_parameters=True)
        engines.append(migrator)

        def migrate(target="head", direction="upgrade", dimension=3):
            config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
            config.attributes["embedding_dimensions"] = dimension  # synthetic test vectors only
            with migrator.connect() as connection:
                config.attributes["connection"] = connection
                getattr(command, direction)(config, target)

        migrate("0001_core")
        user_a, user_b = uuid4(), uuid4()
        app = create_engine(urls["app"], pool_size=1, max_overflow=0, hide_parameters=True)
        engines.append(app)
        for user in (user_a, user_b):
            with app.begin() as connection:
                tenant(connection, user)
                connection.execute(insert(tables["app.accounts"]).values(id=user))
        migrate()  # upgrade an existing database while preserving tenant rows
        yield {"admin": owner, "app": app, "urls": urls, "names": names,
               "a": user_a, "b": user_b, "migrate": migrate}
    finally:
        for engine in engines:
            engine.dispose()
        if owner:
            owner.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
            for role in names.values():
                connection.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        admin.dispose()


def tenant(connection, user):
    connection.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user)})
    connection.execute(text("SET LOCAL search_path TO app, extensions, public"))


def dream(connection, user, title="A house"):
    dream_id, revision = uuid4(), uuid4()
    connection.execute(insert(tables["app.dreams"]).values(
        id=dream_id, user_id=user, title=title, dream_date=date(2026, 10, 29)
    ))
    connection.execute(insert(tables["app.dream_revisions"]).values(
        id=revision, user_id=user, dream_id=dream_id, revision=1,
        text="A house 🌙", content_hash="hash",
    ))
    connection.execute(text("UPDATE app.dreams SET current_revision_id=:r WHERE id=:d"),
                       {"r": revision, "d": dream_id})
    return dream_id, revision


def test_schema_security_catalog_and_existing_upgrade(cluster):
    with cluster["admin"].connect() as c:
        revision = c.scalar(text("SELECT version_num FROM app.alembic_version"))
        assert revision == "0002_revision_guard"
        assert c.scalar(text("SELECT count(*) FROM app.accounts")) == 2
        assert c.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname='app'")) == 33
        assert c.scalar(text("""
            SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='app' AND c.relkind='r' AND c.relname NOT IN
            ('schema_configuration','alembic_version')
            AND NOT (c.relrowsecurity AND c.relforcerowsecurity)
        """)) == 0
        for key in ("app", "queue"):
            assert c.scalar(text("""
                SELECT NOT rolsuper AND NOT rolbypassrls AND NOT rolcreaterole
                FROM pg_roles WHERE rolname=:r
            """), {"r": cluster["names"][key]})


def test_no_context_and_pool_reuse(cluster):
    app = cluster["app"]
    with app.begin() as c:
        assert c.execute(text("SELECT id FROM app.accounts")).all() == []
        for name, table in tables.items():
            if "user_id" in table.c:
                assert c.execute(select(table.c.id)).all() == [], name
        tenant(c, cluster["a"])
        pid = c.scalar(text("SELECT pg_backend_pid()"))
        assert c.execute(text("SELECT id FROM app.accounts")).scalars().all() == [cluster["a"]]
    with app.begin() as c:
        assert c.scalar(text("SELECT pg_backend_pid()")) == pid
        assert c.execute(text("SELECT id FROM app.accounts")).all() == []
        tenant(c, cluster["b"])
        assert c.execute(text("SELECT id FROM app.accounts")).scalars().all() == [cluster["b"]]
    with pytest.raises(DBAPIError), app.begin() as c:
        tenant(c, cluster["a"])
        c.execute(text("SELECT 1/0"))
    with app.begin() as c:
        assert c.execute(text("SELECT id FROM app.accounts")).all() == []


def test_cross_tenant_reads_writes_and_foreign_keys(cluster):
    app, a, b = cluster["app"], cluster["a"], cluster["b"]
    with app.begin() as c:
        tenant(c, b)
        other_dream, other_revision = dream(c, b)
    with app.begin() as c:
        tenant(c, a)
        assert c.execute(text("SELECT id FROM app.dreams WHERE id=:d"),
                         {"d": other_dream}).all() == []
        assert c.execute(text("DELETE FROM app.dreams WHERE id=:d"),
                         {"d": other_dream}).rowcount == 0
        assert c.execute(text("UPDATE app.dreams SET title='no' WHERE id=:d"),
                         {"d": other_dream}).rowcount == 0
    with pytest.raises(DBAPIError), app.begin() as c:
        tenant(c, a)
        c.execute(insert(tables["app.dreams"]).values(
            user_id=b, title="forbidden", dream_date=date.today()
        ))
    # The inserted row is owned by A, so RLS passes; composite ownership FK must reject B's parent.
    with pytest.raises(DBAPIError), app.begin() as c:
        tenant(c, a)
        c.execute(insert(tables["app.dream_revisions"]).values(
            user_id=a, dream_id=other_dream, revision=2, text="x", content_hash="hash"
        ))
    with app.begin() as c:
        tenant(c, a)
        own, rev = dream(c, a)
    with pytest.raises(DBAPIError), app.begin() as c:
        tenant(c, a)
        c.execute(text("UPDATE app.dreams SET current_revision_id=:r WHERE id=:d"),
                  {"r": other_revision, "d": own})
    with pytest.raises(DBAPIError), app.begin() as c:
        tenant(c, a)
        c.execute(text("UPDATE app.dream_revisions SET text='changed' WHERE id=:r"), {"r": rev})


def test_same_tenant_wrong_revision_and_safe_cascade(cluster):
    app, a = cluster["app"], cluster["a"]
    with app.begin() as c:
        tenant(c, a)
        first, rev1 = dream(c, a)
        second, rev2 = dream(c, a)
    with pytest.raises(DBAPIError), app.begin() as c:
        tenant(c, a)
        c.execute(text("UPDATE app.dreams SET current_revision_id=:r WHERE id=:d"),
                  {"r": rev2, "d": first})
    with app.begin() as c:
        tenant(c, a)
        c.execute(text("DELETE FROM app.dreams WHERE id=:d"), {"d": second})
        assert not c.scalar(text("SELECT count(*) FROM app.dream_revisions WHERE id=:r"),
                            {"r": rev2})


def test_evidence_and_vector_dimension_constraints(cluster):
    app, a = cluster["app"], cluster["a"]
    with app.begin() as c:
        tenant(c, a)
        d, r = dream(c, a)
        c.execute(insert(tables["app.evidence"]).values(
            user_id=a, dream_id=d, revision_id=r, start=8, end=9,
            exact_quote="🌙", content_hash="hash",
        ))
        c.execute(insert(tables["app.dream_embeddings"]).values(
            user_id=a, dream_id=d, revision_id=r, model_id="synthetic-test-only",
            embedding_version="v1", embedding=[1, 0, 0], input_hash="hash",
        ))
    with pytest.raises(DBAPIError), app.begin() as c:
        tenant(c, a)
        c.execute(insert(tables["app.evidence"]).values(
            user_id=a, dream_id=d, revision_id=r, start=0, end=3,
            exact_quote="fabricated", content_hash="hash",
        ))
    with pytest.raises(DBAPIError), app.begin() as c:
        tenant(c, a)
        c.execute(insert(tables["app.dream_embeddings"]).values(
            user_id=a, dream_id=d, revision_id=r, model_id="synthetic-test-only",
            embedding_version="v2", embedding=[1, 0], input_hash="hash",
        ))


def test_queue_role_has_no_journal_access_and_limited_updates(cluster):
    with cluster["app"].begin() as c:
        tenant(c, cluster["b"])
        c.execute(insert(tables["app.jobs"]).values(
            user_id=cluster["b"], kind="analysis", dedupe_key="one", data_epoch=0,
        ))
    queue = create_engine(cluster["urls"]["queue"], hide_parameters=True)
    try:
        with queue.begin() as c:
            assert c.scalar(text("SELECT count(*) FROM app.jobs")) >= 1
            c.execute(text("UPDATE app.jobs SET attempts=attempts+1"))
        for statement in (
            "SELECT * FROM app.dreams", "UPDATE app.jobs SET user_id=gen_random_uuid()",
            "DELETE FROM app.jobs", "CREATE TABLE app.not_allowed(id int)",
        ):
            with pytest.raises(DBAPIError), queue.begin() as c:
                c.execute(text(statement))
    finally:
        queue.dispose()


def test_data_api_and_runtime_ddl_denied(cluster):
    api = create_engine(cluster["urls"]["api"], hide_parameters=True)
    try:
        with pytest.raises(DBAPIError), api.begin() as c:
            c.execute(text("SELECT * FROM app.accounts"))
    finally:
        api.dispose()
    for statement in ("CREATE TABLE app.forbidden(id int)", "TRUNCATE app.dreams",
                      "SET ROLE dreamthread_owner"):
        with pytest.raises(DBAPIError), cluster["app"].begin() as c:
            c.execute(text(statement))


def test_async_tenant_context_and_readiness(cluster):
    async def check():
        url = cluster["urls"]["app"].render_as_string(hide_password=False)
        db = Database(Settings(_env_file=None, database_url=url, db_pool_size=1,
                               expected_schema_revision="0002_revision_guard",
                               embedding_dimensions=3))
        try:
            assert await db.probe() == "ok"
            async with db.tenant_transaction(cluster["a"]) as c:
                assert (await c.execute(select(tables["app.accounts"].c.id))).scalars().all() == [
                    cluster["a"]
                ]
            async with db.engine.begin() as c:
                assert (await c.execute(text("SELECT id FROM app.accounts"))).all() == []
            db.expected = "wrong"
            assert await db.probe() == "schema_mismatch"
            db.expected = "0002_revision_guard"
            db.dimensions = 4
            assert await db.probe() == "schema_mismatch"
        finally:
            await db.close()
    with asyncio.Runner(loop_factory=create_loop) as runner:
        runner.run(check())


def test_owner_connection_is_not_runtime_ready(cluster):
    async def check():
        url = str(cluster["admin"].url)
        db = Database(Settings(_env_file=None, database_url=url,
                               expected_schema_revision="0002_revision_guard",
                               embedding_dimensions=3))
        try:
            assert await db.probe() == "unsafe_role"
        finally:
            await db.close()
    with asyncio.Runner(loop_factory=create_loop) as runner:
        runner.run(check())


def test_live_server_with_restricted_database_role(cluster):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory",
         "--host", "127.0.0.1", "--port", str(port), "--no-access-log",
         "--loop", "app.loop:create_loop"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "APP_ENV": "test",
             "DATABASE_URL": cluster["urls"]["app"].render_as_string(hide_password=False),
             "EXPECTED_SCHEMA_REVISION": "0002_revision_guard", "EMBEDDING_DIMENSIONS": "3"},
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=2) as response:
                    assert json.load(response)["status"] == "ready"
                    assert response.headers["X-Request-ID"]
                break
            except (URLError, TimeoutError):
                if process.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError("Live server did not become ready") from None
                time.sleep(0.1)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_migration_downgrade_rebuild_and_dimension_gate(cluster, monkeypatch):
    migrate = cluster["migrate"]
    migrate("0001_core", direction="downgrade")
    migrate()
    with cluster["admin"].connect() as c:
        assert c.scalar(text("SELECT count(*) FROM app.accounts")) == 2
    migrate("base", direction="downgrade")
    monkeypatch.delenv("EMBEDDING_DIMENSIONS", raising=False)
    with pytest.raises(RuntimeError, match="measured provider dimension"):
        migrate(dimension=None)
    migrate()
    migrate()  # already-at-head is safe
    with cluster["admin"].connect() as c:
        assert c.scalar(text("SELECT count(*) FROM app.accounts")) == 0
        assert c.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname='app'")) == 33
