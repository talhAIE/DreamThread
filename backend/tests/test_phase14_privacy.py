from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import current_identity
from app.auth.identity import Identity
from app.config import Settings
from app.errors import APIError
from app.main import create_app
from app.schemas.privacy import ExportFormat
from app.services.privacy import PrivacyService
from app.services.rate_limits import enforce_user_request_limit


class FakeAuthenticator:
    async def verify(self, _authorization):
        raise AssertionError("The test overrides current_identity")

    async def close(self):
        return None


class FakeDatabase:
    async def close(self):
        return None


class LimitConnection:
    def __init__(self):
        self.values = [
            datetime(2026, 10, 30, 12, 0, tzinfo=UTC),
            None,
            45,
        ]
        self.statements = []

    async def scalar(self, statement):
        self.statements.append(str(statement))
        return self.values.pop(0)


class LimitDatabase:
    def __init__(self):
        from contextlib import asynccontextmanager

        self.connection = LimitConnection()

        @asynccontextmanager
        async def tenant_transaction(_user_id):
            yield self.connection

        self.tenant_transaction = tenant_transaction


class DeletionResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def mappings(self):
        return self

    def one_or_none(self):
        return self.rows[0] if self.rows else None

    def scalars(self):
        return self

    def all(self):
        return self.rows


class DeletionConnection:
    def __init__(self, user_id, job, storage_keys=()):
        self.user_id = user_id
        self.job = job
        self.storage_keys = list(storage_keys)
        self.statements = []
        self.scalar_statements = []
        self.job_update_params = []

    async def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if sql.startswith("UPDATE app.jobs"):
            from sqlalchemy.dialects import postgresql

            self.job_update_params.append(statement.compile(dialect=postgresql.dialect()).params)
        if "FROM app.accounts" in sql:
            return DeletionResult(
                [{"id": self.user_id, "deletion_state": "deleting", "data_epoch": 3}]
            )
        if "FROM app.jobs" in sql:
            return DeletionResult([self.job])
        if "FROM app.audio_assets" in sql:
            return DeletionResult(self.storage_keys)
        return DeletionResult()

    async def scalar(self, statement):
        sql = str(statement)
        self.scalar_statements.append(sql)
        if "app.cleanup_tasks" in str(statement):
            value = int(bool(self.storage_keys))
        else:
            value = datetime.now(UTC)
        self.scalar_statements.append(value)
        return value


class DeletionDatabase:
    def __init__(self, user_id, job, storage_keys=()):
        from contextlib import asynccontextmanager

        self.connection = DeletionConnection(user_id, job, storage_keys)

        @asynccontextmanager
        async def tenant_transaction(_user_id):
            yield self.connection

        self.tenant_transaction = tenant_transaction


def app_settings():
    return Settings(database_url="postgresql://user:password@localhost/dreamthread")


def test_export_text_keeps_exact_revision_text_and_excludes_internal_provider_fields():
    exported = ExportFormat(
        schema_version="1",
        exported_at=datetime(2026, 10, 30, tzinfo=UTC),
        archive_version=4,
        data_epoch=2,
        dreams=[
            {
                "title": "Night",
                "dream_date": "2026-10-29",
                "revisions": [
                    {
                        "revision": 1,
                        "recorded_at": datetime(2026, 10, 29, tzinfo=UTC),
                        "text": "Exact text 🌙",
                    }
                ],
            }
        ],
        motifs=[],
        associations=[],
        connection_decisions=[],
        storylines=[],
        conversations=[],
    )

    text = PrivacyService._text_export(exported)

    assert "Exact text 🌙" in text
    assert "schema_version" not in text
    assert "provider" not in text.lower()


def test_journal_deletion_requires_explicit_true_confirmation():
    user_id = uuid4()
    app = create_app(
        settings=app_settings(),
        database=FakeDatabase(),
        authenticator=FakeAuthenticator(),
    )
    app.dependency_overrides[current_identity] = lambda: Identity(user_id, False)

    with TestClient(app) as client:
        response = client.request("DELETE", "/api/v1/me/journal", json={"confirm": False})

    assert response.status_code == 422


@pytest.mark.anyio
async def test_per_user_rate_limit_is_atomic_and_returns_retry_after():
    database = LimitDatabase()

    with pytest.raises(APIError) as caught:
        await enforce_user_request_limit(database, uuid4(), 120)

    assert caught.value.status == 429
    assert caught.value.headers["Retry-After"] == "15"
    assert "ON CONFLICT" in database.connection.statements[1]


@pytest.mark.anyio
async def test_journal_deletion_is_tenant_scoped_and_preserves_account_preferences():
    user_id, job_id, lease_token = uuid4(), uuid4(), uuid4()
    job = {
        "id": job_id,
        "user_id": user_id,
        "status": "running",
        "lease_token": lease_token,
        "leased_until": datetime.now(UTC) + timedelta(minutes=5),
        "data_epoch": 3,
    }
    database = DeletionDatabase(user_id, job)

    await PrivacyService(database).run_journal_deletion(job)

    statements = database.connection.statements
    content_deletes = [sql for sql in statements if sql.startswith("DELETE FROM")]
    assert any("DELETE FROM app.dreams" in sql for sql in content_deletes)
    assert all("user_id" in sql for sql in content_deletes)
    assert not any('DELETE FROM app."accounts"' in sql for sql in content_deletes)
    assert not any('DELETE FROM app."preferences"' in sql for sql in content_deletes)
    assert any("UPDATE app.accounts SET deletion_state" in sql for sql in statements)


@pytest.mark.anyio
async def test_journal_deletion_stays_blocked_while_external_storage_cleanup_is_pending():
    user_id, job_id, lease_token = uuid4(), uuid4(), uuid4()
    job = {
        "id": job_id,
        "user_id": user_id,
        "status": "running",
        "lease_token": lease_token,
        "leased_until": datetime.now(UTC) + timedelta(minutes=5),
        "data_epoch": 3,
    }
    database = DeletionDatabase(user_id, job, ["user-key/audio-object"])

    await PrivacyService(database).run_journal_deletion(job)

    statements = database.connection.statements
    assert any("INSERT INTO app.cleanup_tasks" in sql for sql in statements)
    assert database.connection.scalar_statements[-1] == 1
    assert any(
        params.get("status") == "failed"
        and params.get("error_code") == "storage_cleanup_pending"
        for params in database.connection.job_update_params
    )
    assert not any(
        "UPDATE app.accounts SET deletion_state" in sql for sql in statements
    ), statements
