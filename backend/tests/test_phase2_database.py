"""Real PostgreSQL checks. Creates/drops only its own uniquely named test database.

Set TEST_ADMIN_DATABASE_URL to a disposable cluster administrator, never production.
"""

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, insert, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.auth.identity import Identity
from app.config import Settings
from app.db import Database
from app.db.models import tables
from app.db.provision import provision
from app.errors import APIError
from app.loop import create_loop
from app.schemas.privacy import ExportCreate
from app.services.insights import InsightsService
from app.services.privacy import PrivacyService
from app.services.rate_limits import enforce_user_request_limit
from app.workers.queue import Queue

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
    database_created = False
    created_roles = []
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{db_name}"'))
            database_created = True
        owner = create_engine(local_url, hide_parameters=True)
        transaction_roles = []
        with owner.begin() as connection:
            for role in ("anon", "authenticated", "service_role"):
                if not connection.scalar(text(
                    "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:r)"
                ), {"r": role}):
                    connection.execute(text(f"CREATE ROLE {role} NOLOGIN"))
                    transaction_roles.append(role)
            for role in ("dreamthread_owner", "dreamthread_runtime", "dreamthread_queue"):
                exists = connection.scalar(
                    text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:r)"),
                    {"r": role},
                )
                if not exists:
                    transaction_roles.append(role)
            provision(connection)
            for key, role in names.items():
                connection.execute(text(f'CREATE ROLE "{role}" LOGIN PASSWORD \'test-only\''))
                transaction_roles.append(role)
                group = {
                    "migrator": "dreamthread_owner", "app": "dreamthread_runtime",
                    "queue": "dreamthread_queue", "api": "authenticated",
                }[key]
                connection.execute(text(f'GRANT {group} TO "{role}"'))
        created_roles.extend(transaction_roles)
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
        if database_created:
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
                for role in reversed(created_roles):
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
        assert revision == "0008_request_rate_limits"
        assert c.scalar(text("SELECT count(*) FROM app.accounts")) == 2
        assert c.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname='app'")) == 38
        assert c.scalar(text(
            "SELECT relforcerowsecurity FROM pg_class WHERE oid='app.api_rate_limits'::regclass"
        ))
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
                               expected_schema_revision="0006_personal_memory",
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
            db.expected = "0006_personal_memory"
            db.dimensions = 16001
            assert await db.probe() == "schema_mismatch"
        finally:
            await db.close()
    with asyncio.Runner(loop_factory=create_loop) as runner:
        runner.run(check())


def test_owner_connection_is_not_runtime_ready(cluster):
    async def check():
        url = cluster["admin"].url.render_as_string(hide_password=False)
        db = Database(Settings(_env_file=None, database_url=url,
                               expected_schema_revision="0006_personal_memory",
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
             "EXPECTED_SCHEMA_REVISION": "0006_personal_memory", "EMBEDDING_DIMENSIONS": "3"},
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
        assert c.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname='app'")) == 38


def test_phase12_insights_capsule_and_phase14_export_deletion_are_tenant_scoped(cluster):
    user, other_user = uuid4(), uuid4()
    dream_ids = [uuid4(), uuid4()]
    revision_ids = [uuid4(), uuid4()]
    motif_id = uuid4()
    evidence_id = uuid4()
    with cluster["app"].begin() as c:
        tenant(c, user)
        c.execute(insert(tables["app.accounts"]).values(id=user))
        c.execute(
            insert(tables["app.preferences"]).values(user_id=user, timezone="UTC")
        )
        c.execute(insert(tables["app.motifs"]).values(
            id=motif_id, user_id=user, type="place", canonical_label="House",
            normalized_key="house",
        ))
        for index, (dream_id, revision_id) in enumerate(
            zip(dream_ids, revision_ids, strict=True)
        ):
            text_value = f"The house has door {index}."
            content_hash = hashlib.sha256(text_value.encode()).hexdigest()
            c.execute(insert(tables["app.dreams"]).values(
                id=dream_id, user_id=user, title=f"House {index}",
                dream_date=date(2026, 10, 4 + index), lifecycle="saved",
                current_revision_id=None,
            ))
            c.execute(insert(tables["app.dream_revisions"]).values(
                id=revision_id, user_id=user, dream_id=dream_id, revision=1,
                text=text_value, content_hash=content_hash,
                reported_emotions=["afraid", "curious"] if index == 0 else [],
                recorded_at=datetime(2026, 10, 5 + index, 2, tzinfo=UTC),
            ))
            c.execute(text(
                "UPDATE app.dreams SET current_revision_id=:revision_id WHERE id=:dream_id"
            ), {"revision_id": revision_id, "dream_id": dream_id})
            c.execute(insert(tables["app.dream_motifs"]).values(
                user_id=user, dream_id=dream_id, revision_id=revision_id,
                motif_id=motif_id, origin="extracted", confirmation_state="suggested",
            ))
            if index == 0:
                quote = "house has door"
                start = text_value.index(quote)
                c.execute(insert(tables["app.evidence"]).values(
                    id=evidence_id, user_id=user, dream_id=dream_id,
                    revision_id=revision_id, start=start, end=start + len(quote),
                    exact_quote=quote, content_hash=content_hash,
                ))

    with cluster["app"].begin() as c:
        tenant(c, other_user)
        c.execute(insert(tables["app.accounts"]).values(id=other_user))
        other_dream, other_revision = uuid4(), uuid4()
        other_text = "Another tenant private dream."
        other_hash = hashlib.sha256(other_text.encode()).hexdigest()
        c.execute(insert(tables["app.dreams"]).values(
            id=other_dream, user_id=other_user, title="Other private dream",
            dream_date=date(2026, 10, 12), lifecycle="saved",
        ))
        c.execute(insert(tables["app.dream_revisions"]).values(
            id=other_revision, user_id=other_user, dream_id=other_dream, revision=1,
            text=other_text, content_hash=other_hash,
        ))
        c.execute(text(
            "UPDATE app.dreams SET current_revision_id=:revision_id WHERE id=:dream_id"
        ), {"revision_id": other_revision, "dream_id": other_dream})

    settings = Settings(
        database_url=cluster["urls"]["app"],
        expected_schema_revision="0008_request_rate_limits",
        embedding_dimensions=3,
    )
    db = Database(settings)
    identity = Identity(user, False)

    async def workflow():
        insights = InsightsService(db)
        result = await insights.get_insights(user, "2026-10", "UTC")
        assert result.dream_date_count == 2
        assert result.recorded_at_count == 2
        assert result.missing_emotion_dream_count == 1
        assert [(item.emotion, item.dream_count) for item in result.emotion_counts] == [
            ("afraid", 1),
            ("curious", 1),
        ]
        assert result.motif_counts[0].dream_count == 2

        accepted = await insights.request_refresh(user, "2026-10", "UTC")
        token = uuid4()
        async with db.tenant_transaction(user) as c:
            job = (
                await c.execute(select(tables["app.jobs"]).where(
                    tables["app.jobs"].c.id == accepted.job_id
                ))
            ).mappings().one()
            await c.execute(
                update(tables["app.jobs"])
                .where(tables["app.jobs"].c.id == accepted.job_id)
                .values(
                    status="running",
                    attempts=1,
                    lease_token=token,
                    leased_until=func.clock_timestamp() + timedelta(minutes=5),
                    deadline_at=func.clock_timestamp() + timedelta(minutes=10),
                )
            )
        job = dict(job, status="running", lease_token=token)
        await insights.run_refresh(job)
        capsule = await insights.get_capsule(user, "2026-10", "UTC")
        assert capsule.status == "ready"
        assert len(capsule.capsule.citations) == 1
        assert capsule.capsule.citations[0].evidence_id == evidence_id
        assert f"[evidence:{evidence_id}]" in capsule.capsule.retrospective

        privacy = PrivacyService(db, settings)
        export = await privacy.create_export(
            identity, ExportCreate(format="json"), "export-key"
        )
        token = uuid4()
        async with db.tenant_transaction(user) as c:
            job = (
                await c.execute(select(tables["app.jobs"]).where(
                    tables["app.jobs"].c.id == export.job_id
                ))
            ).mappings().one()
            await c.execute(
                update(tables["app.jobs"])
                .where(tables["app.jobs"].c.id == export.job_id)
                .values(
                    status="running",
                    attempts=1,
                    lease_token=token,
                    leased_until=func.clock_timestamp() + timedelta(minutes=5),
                    deadline_at=func.clock_timestamp() + timedelta(minutes=10),
                )
            )
        await privacy.run_export(dict(job, status="running", lease_token=token))
        status = await privacy.export_status(user, export.id)
        assert status.status == "ready"
        format_value, content, status_value = await privacy.download_export(user, export.id)
        assert (format_value, status_value) == ("json", "ready")
        exported = json.loads(content)
        assert [dream["title"] for dream in exported["dreams"]] == ["House 0", "House 1"]
        assert "Another tenant private dream." not in content

        async with db.tenant_transaction(user) as c:
            await c.execute(
                update(tables["app.accounts"])
                .where(tables["app.accounts"].c.id == user)
                .values(archive_version=tables["app.accounts"].c.archive_version + 1)
            )
        _, _, export_state = await privacy.download_export(user, export.id)
        assert export_state == "stale"

        pending_export = await privacy.create_export(
            identity, ExportCreate(format="json"), "pending-export-key"
        )
        token = uuid4()
        async with db.tenant_transaction(user) as c:
            pending_job = (
                await c.execute(select(tables["app.jobs"]).where(
                    tables["app.jobs"].c.id == pending_export.job_id
                ))
            ).mappings().one()
            await c.execute(
                update(tables["app.jobs"])
                .where(tables["app.jobs"].c.id == pending_export.job_id)
                .values(
                    status="running",
                    attempts=1,
                    lease_token=token,
                    leased_until=func.clock_timestamp() + timedelta(minutes=5),
                    deadline_at=func.clock_timestamp() + timedelta(minutes=10),
                )
            )
        deletion = await privacy.request_journal_deletion(user)
        await privacy.run_export(dict(pending_job, status="running", lease_token=token))
        token = uuid4()
        async with db.tenant_transaction(user) as c:
            job = (
                await c.execute(select(tables["app.jobs"]).where(
                    tables["app.jobs"].c.id == deletion.job_id
                ))
            ).mappings().one()
            await c.execute(
                update(tables["app.jobs"])
                .where(tables["app.jobs"].c.id == deletion.job_id)
                .values(
                    status="running",
                    attempts=1,
                    lease_token=token,
                    leased_until=func.clock_timestamp() + timedelta(minutes=5),
                    deadline_at=func.clock_timestamp() + timedelta(minutes=10),
                )
            )
        await privacy.run_journal_deletion(
            dict(job, status="running", lease_token=token, data_epoch=job["data_epoch"])
        )
        async with db.tenant_transaction(user) as c:
            assert await c.scalar(
                select(func.count()).select_from(tables["app.dreams"])
            ) == 0
            assert await c.scalar(
                select(tables["app.accounts"].c.deletion_state).where(
                    tables["app.accounts"].c.id == user
                )
            ) == "active"
            assert await c.scalar(
                select(func.count()).select_from(tables["app.exports"])
            ) == 0
        async with db.tenant_transaction(other_user) as c:
            assert await c.scalar(
                select(func.count()).select_from(tables["app.dreams"])
            ) == 1

    try:
        asyncio.run(workflow())
    finally:
        asyncio.run(db.close())


def test_postgres_enforces_shared_model_concurrency_and_atomic_user_rate_limit(cluster):
    user = uuid4()
    with cluster["app"].begin() as c:
        tenant(c, user)
        c.execute(insert(tables["app.accounts"]).values(id=user))
        c.execute(
            insert(tables["app.jobs"]),
            [
                {
                    "user_id": user,
                    "kind": "analysis",
                    "dedupe_key": f"global-limit:{index}:{uuid4()}",
                    "payload": {},
                    "data_epoch": 0,
                    "input_versions": {},
                }
                for index in range(2)
            ],
        )

    settings = Settings(
        database_url=cluster["urls"]["app"],
        queue_database_url=cluster["urls"]["queue"],
        expected_schema_revision="0008_request_rate_limits",
        embedding_dimensions=3,
        worker_concurrency=1,
    )
    db = Database(settings)
    first_queue, second_queue = Queue(settings), Queue(settings)

    async def workflow():
        try:
            first = await first_queue.claim()
            second = await second_queue.claim()
            assert first is not None
            assert second is None

            outcomes = await asyncio.gather(
                enforce_user_request_limit(db, user, 1),
                enforce_user_request_limit(db, user, 1),
                return_exceptions=True,
            )
            admitted = [item for item in outcomes if item is None]
            rejected = [item for item in outcomes if isinstance(item, Exception)]
            assert len(admitted) == 1
            assert len(rejected) == 1
            assert isinstance(rejected[0], APIError)
            assert rejected[0].status == 429
        finally:
            await first_queue.close()
            await second_queue.close()
            await db.close()

    asyncio.run(workflow())
