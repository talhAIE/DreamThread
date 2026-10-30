import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import current_identity
from app.auth.identity import Identity
from app.config import Settings
from app.errors import APIError
from app.main import create_app
from app.schemas.detective import ConversationMessage, DetectiveSubmission, DreamCitation
from app.services import detective as detective_module
from app.services.detective import DetectiveService
from app.workers.handlers import DurableJobHandler
from app.workers.runner import Worker
from app.workers.types import WorkFailure

NOW = datetime(2026, 10, 30, tzinfo=UTC)
URL = "postgresql+psycopg://user:password@localhost/test"


class FakeResult:
    def __init__(self, rows=None, scalar=None, rowcount=None):
        self.rows = rows or []
        self.value = scalar
        self.rowcount = len(self.rows) if rowcount is None else rowcount

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def one(self):
        assert len(self.rows) == 1
        return self.rows[0]

    def one_or_none(self):
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.rows[0][0] if self.rows else self.value

    def scalars(self):
        return FakeScalars(self.rows, self.value)


class FakeScalars:
    def __init__(self, rows, value=None):
        self.rows = rows
        self.value = value

    def all(self):
        return [
            row[0] if isinstance(row, tuple) else next(iter(row.values()))
            if isinstance(row, dict)
            else row
            for row in self.rows
        ]


class FakeConnection:
    def __init__(self, user_id, conversations=(), dreams=()):
        self.user_id = user_id
        self.conversations = [dict(row) for row in conversations]
        self.dreams = [dict(row) for row in dreams]
        self.messages = []
        self.evidence = []
        self.message_evidence = []
        self.model_runs = []
        self.jobs = []
        self.statements = []
        self.fail_failed_assistant_once = False
        self.fail_sql_once = None

    @staticmethod
    def values(statement):
        return {
            getattr(column, "name", column): getattr(bind, "value", NOW)
            for column, bind in (getattr(statement, "_values", None) or {}).items()
        }

    @staticmethod
    def params(statement):
        return statement.compile().params

    async def execute(self, statement):
        self.statements.append(statement)
        sql = str(statement.compile())
        params = self.params(statement)
        if self.fail_sql_once and self.fail_sql_once in sql:
            self.fail_sql_once = None
            raise RuntimeError("simulated database failure")

        if sql.startswith("SELECT") and "FROM app.accounts" in sql:
            return FakeResult(
                [
                    {
                        "id": self.user_id,
                        "deletion_state": "active",
                        "data_epoch": 1,
                        "archive_version": 1,
                        "is_anonymous": False,
                    }
                ]
            )
        if sql.startswith("SELECT") and "FROM app.jobs" in sql:
            rows = [row for row in self.jobs if row["user_id"] == self.user_id]
            row_ids = {row["id"] for row in rows}
            requested_id = next(
                (value for value in params.values() if value in row_ids), None
            )
            if requested_id is not None:
                rows = [row for row in rows if row["id"] == requested_id]
            return FakeResult([row for row in rows if self._matches_where(statement, row)])
        if sql.startswith("SELECT") and "FROM app.conversations" in sql:
            assert "conversations.user_id" in sql
            matches = [
                row for row in self.conversations if row["user_id"] == self.user_id
            ]
            all_ids = {row["id"] for row in self.conversations}
            requested_id = next((value for value in params.values() if value in all_ids), None)
            if requested_id is not None:
                matches = [row for row in matches if row["id"] == requested_id]
            return FakeResult(matches)
        if sql.startswith("SELECT") and "FROM app.messages" in sql:
            assert "messages.user_id" in sql
            conv_ids = {row["id"] for row in self.conversations if row["user_id"] == self.user_id}
            requested_conv = next(
                (value for value in params.values() if value in conv_ids), None
            )
            rows = [
                row
                for row in self.messages
                if row["user_id"] == self.user_id
                and (requested_conv is None or row["conversation_id"] == requested_conv)
            ]
            if "user" in params.values():
                rows = [row for row in rows if row["role"] == "user"]
            if "assistant" in params.values():
                rows = [row for row in rows if row["role"] == "assistant"]
            request_keys = {row["request_key"] for row in rows if row.get("request_key")}
            requested_key = next(
                (value for value in params.values() if value in request_keys), None
            )
            if requested_key is not None:
                rows = [row for row in rows if row.get("request_key") == requested_key]
            row_ids = {row["id"] for row in rows}
            requested_id = next(
                (value for value in params.values() if value in row_ids), None
            )
            if requested_id is not None:
                rows = [row for row in rows if row["id"] == requested_id]
            rows = [row for row in rows if self._matches_where(statement, row)]
            descending = "messages.created_at DESC" in sql
            rows.sort(
                key=lambda row: (row["created_at"], str(row["id"])),
                reverse=descending,
            )
            limit_clause = getattr(statement, "_limit_clause", None)
            offset_clause = getattr(statement, "_offset_clause", None)
            limit = getattr(limit_clause, "value", None)
            offset = getattr(offset_clause, "value", None) or 0
            if limit is not None:
                rows = rows[offset : offset + limit]
            return FakeResult(rows)
        if (
            sql.startswith("SELECT")
            and "FROM app.dreams" in sql
            and "app.dream_revisions" in sql
            and "FOR UPDATE" in sql
        ):
            dream_ids = {row["id"] for row in self.dreams}
            requested_ids = {
                value
                for param in params.values()
                for value in (param if isinstance(param, (tuple, list, set)) else (param,))
                if value in dream_ids
            }
            rows = [
                {
                    "id": row["id"],
                    "current_revision_id": row.get("current_revision_id", row["revision_id"]),
                    "content_hash": row["content_hash"],
                }
                for row in self.dreams
                if row["user_id"] == self.user_id
                and row["id"] in requested_ids
                and row.get("deleted_at") is None
                and row.get("lifecycle", "saved") == "saved"
            ]
            return FakeResult(rows)
        if sql.startswith("SELECT") and "FROM app.dreams" in sql:
            assert "dreams.user_id" in sql
            exact_term_prefix = "(^|[^[:alnum:]])"
            exact_term_suffix = "([^[:alnum:]]|$)"
            exact_terms = [
                value[len(exact_term_prefix) : -len(exact_term_suffix)]
                for value in params.values()
                if isinstance(value, str)
                and value.startswith(exact_term_prefix)
                and value.endswith(exact_term_suffix)
            ]
            patterns = [
                value.strip("%").casefold()
                for value in params.values()
                if isinstance(value, str) and value.startswith("%") and value.endswith("%")
            ]
            rows = [
                row
                for row in self.dreams
                if row["user_id"] == self.user_id
                and (
                    all(
                        re.search(
                            rf"(?<![^\W_]){re.escape(term)}(?![^\W_])",
                            row["text"],
                            flags=re.IGNORECASE,
                        )
                        for term in exact_terms
                    )
                    if exact_terms
                    else any(
                        pattern in (row["text"] + row["title"]).casefold()
                        for pattern in patterns
                    )
                )
            ]
            if "dreams.id IN" in sql:
                scoped_ids = {
                    item
                    for value in params.values()
                    for item in (value if isinstance(value, (tuple, list, set)) else ())
                    if item in {dream["id"] for dream in self.dreams}
                }
                if scoped_ids:
                    rows = [row for row in rows if row["id"] in scoped_ids]
            if "dreams.dream_date >=" in sql:
                from_date = next(
                    value
                    for value in params.values()
                    if isinstance(value, date)
                )
                rows = [row for row in rows if row["dream_date"] >= from_date]
            if "dreams.dream_date <=" in sql:
                to_date = next(
                    value
                    for value in params.values()
                    if isinstance(value, date)
                )
                rows = [row for row in rows if row["dream_date"] <= to_date]
            if "app.dream_motifs" in sql:
                motif_ids = {
                    item
                    for value in params.values()
                    for item in (value if isinstance(value, (tuple, list, set)) else ())
                    if item not in {dream["id"] for dream in self.dreams}
                }
                rows = [
                    row
                    for row in rows
                    if set(row.get("motif_ids", ())) & motif_ids
                ]
            if "dreams.dream_date ASC" in sql:
                rows.sort(key=lambda row: (row["dream_date"], str(row["id"])))
            else:
                rows.sort(
                    key=lambda row: (row["dream_date"], str(row["id"])),
                    reverse=True,
                )
            limit_clause = getattr(statement, "_limit_clause", None)
            offset_clause = getattr(statement, "_offset_clause", None)
            limit = getattr(limit_clause, "value", None) or 50
            offset = getattr(offset_clause, "value", None) or 0
            return FakeResult(rows[offset : offset + limit])
        if sql.startswith("SELECT") and "FROM app.message_evidence" in sql:
            message_ids = {
                item
                for value in params.values()
                for item in (value if isinstance(value, (list, tuple, set)) else [value])
            }
            rows = []
            for link in self.message_evidence:
                evidence = next(
                    (
                        item
                        for item in self.evidence
                        if item["id"] == link["evidence_id"]
                        and item["user_id"] == self.user_id
                    ),
                    None,
                )
                dream = next(
                    (
                        item
                        for item in self.dreams
                        if evidence is not None
                        and item["id"] == evidence["dream_id"]
                        and item["user_id"] == self.user_id
                    ),
                    None,
                )
                if (
                    link["user_id"] == self.user_id
                    and link["message_id"] in message_ids
                    and evidence is not None
                    and dream is not None
                ):
                    source_revision = evidence["revision_id"] == dream.get(
                        "current_revision_id", evidence["revision_id"]
                    )
                    source_text = dream.get("text") if source_revision else None
                    revision_hash = (
                        dream.get("content_hash", evidence["content_hash"])
                        if source_revision
                        else None
                    )
                    rows.append(
                        {
                            "message_id": link["message_id"],
                            "evidence_id": evidence["id"],
                            "dream_id": evidence["dream_id"],
                            "revision_id": evidence["revision_id"],
                            "title": dream["title"],
                            "dream_date": dream["dream_date"],
                            "start": evidence["start"],
                            "end": evidence["end"],
                            "quote": evidence["exact_quote"],
                            "content_hash": evidence["content_hash"],
                            "current_revision_id": dream.get(
                                "current_revision_id", evidence["revision_id"]
                            ),
                            "deleted_at": dream.get("deleted_at"),
                            "source_text": source_text,
                            "revision_hash": revision_hash,
                        }
                    )
            return FakeResult(rows)
        if (
            sql.startswith("SELECT")
            and "FROM app.evidence" in sql
            and "app.message_evidence" in sql
        ):
            message_ids = {
                item
                for value in params.values()
                for item in (value if isinstance(value, (list, tuple, set)) else [value])
            }
            return FakeResult(
                [
                    {"id": item["id"]}
                    for item in self.evidence
                    if item["user_id"] == self.user_id
                    and any(
                        link["user_id"] == self.user_id
                        and link["message_id"] in message_ids
                        and link["evidence_id"] == item["id"]
                        for link in self.message_evidence
                    )
                ]
            )

        table = getattr(statement, "table", None)
        table_name = table.name if table is not None else ""
        if sql.startswith("INSERT"):
            values = self.values(statement)
            if (
                self.fail_failed_assistant_once
                and table_name == "messages"
                and values.get("role") == "assistant"
                and values.get("status") == "failed"
            ):
                self.fail_failed_assistant_once = False
                raise RuntimeError("simulated failure")
            values.setdefault("created_at", NOW)
            if table_name == "conversations":
                values.setdefault("updated_at", NOW)
                self.conversations.append(values)
                return FakeResult([values])
            if table_name == "messages":
                values["created_at"] = NOW + timedelta(microseconds=len(self.messages))
                self.messages.append(values)
                return FakeResult([values] if "RETURNING" in sql else [])
            if table_name == "evidence":
                self.evidence.append(values)
                return FakeResult([values])
            if table_name == "message_evidence":
                self.message_evidence.append(values)
                return FakeResult()
            if table_name == "model_runs":
                values.setdefault("id", uuid4())
                self.model_runs.append(values)
                return FakeResult(scalar=values["id"])
            if table_name == "jobs":
                self.jobs.append(values)
                return FakeResult([values] if "RETURNING" in sql else [])
        if sql.startswith("UPDATE"):
            values = self.values(statement)
            updated = []
            if table_name == "messages":
                for row in self.messages:
                    if (
                        row["user_id"] == self.user_id
                        and self._matches_where(statement, row)
                    ):
                        row.update(values)
                        updated.append(dict(row))
            if table_name == "model_runs":
                for row in self.model_runs:
                    if (
                        row["user_id"] == self.user_id
                        and self._matches_where(statement, row)
                    ):
                        row.update(values)
                        updated.append(dict(row))
            if table_name == "jobs":
                for row in self.jobs:
                    if (
                        row["user_id"] == self.user_id
                        and self._matches_where(statement, row)
                    ):
                        row.update(values)
                        updated.append(dict(row))
            if "RETURNING" not in sql:
                return FakeResult(rowcount=len(updated))
            if (
                len(getattr(statement, "_returning", ())) == 1
                and getattr(statement._returning[0], "name", None) == "id"
            ):
                return FakeResult([(row["id"],) for row in updated])
            return FakeResult(updated)
        if sql.startswith("DELETE"):
            if table_name == "conversations":
                self.conversations = [
                    row
                    for row in self.conversations
                    if not (row["user_id"] == self.user_id and self._matches_where(statement, row))
                ]
            return FakeResult()
        raise AssertionError(f"Unexpected SQL in test fake: {sql}")

    @classmethod
    def _matches_where(cls, statement, row):
        def matches(clause):
            if hasattr(clause, "clauses"):
                return all(matches(child) for child in clause.clauses)
            left = getattr(clause, "left", None)
            right = getattr(clause, "right", None)
            column = getattr(left, "name", None)
            expected = getattr(right, "value", None)
            if column is None or column not in row:
                return True
            actual = row[column]
            operator = getattr(getattr(clause, "operator", None), "__name__", "")
            if operator in {"eq", "is_"}:
                return actual == expected
            if operator in {"ne", "is_not"}:
                return actual != expected
            if operator == "in_op":
                return actual in (expected or ())
            if operator == "not_in_op":
                return actual not in (expected or ())
            return True

        return all(matches(clause) for clause in statement._where_criteria)

    async def scalar(self, statement):
        result = await self.execute(statement)
        if result.rows and isinstance(result.rows[0], dict):
            return result.rows[0].get("id")
        return result.scalar_one()


class FakeDatabase:
    def __init__(self, user_id, conversations=(), dreams=()):
        self.connection = FakeConnection(user_id, conversations, dreams)
        self.tenant_users = []
        self.transaction_lock = asyncio.Lock()

    @asynccontextmanager
    async def tenant_transaction(self, user_id):
        async with self.transaction_lock:
            self.tenant_users.append(user_id)
            self.connection.user_id = user_id
            snapshot = deepcopy(
                (
                    self.connection.conversations,
                    self.connection.messages,
                    self.connection.evidence,
                    self.connection.message_evidence,
                    self.connection.model_runs,
                    self.connection.jobs,
                )
            )
            try:
                yield self.connection
            except BaseException:
                (
                    self.connection.conversations,
                    self.connection.messages,
                    self.connection.evidence,
                    self.connection.message_evidence,
                    self.connection.model_runs,
                    self.connection.jobs,
                ) = snapshot
                raise

    async def close(self):
        return None

    async def probe(self):
        return "ok"


class FakeProvider:
    def __init__(self, output):
        self.output = output
        self.calls = []

    async def complete(self, messages, *, schema, task, record):
        self.calls.append(messages)
        run_id = await record(
            provider="nebius",
            model_id="nvidia/nemotron-test",
            task=task,
            latency_ms=1.0,
            input_tokens=10,
            output_tokens=10,
            status="received",
        )
        return SimpleNamespace(text=json.dumps(self.output), run_id=run_id)


class FailedProvider:
    async def complete(self, *args, **kwargs):
        raise WorkFailure("provider_timeout", retryable=True)


class UnexpectedProvider:
    async def complete(self, *args, **kwargs):
        raise RuntimeError("private provider details must not escape")


def source(user_id, text, title="Dream"):
    return {
        "id": uuid4(),
        "user_id": user_id,
        "title": title,
        "dream_date": date(2026, 10, 1),
        "revision_id": uuid4(),
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text": text,
    }


def citation(dream, quote, *, start=None):
    start = dream["text"].index(quote) if start is None else start
    return {
        "dream_id": str(dream["id"]),
        "revision_id": str(dream["revision_id"]),
        "source_hash": dream["content_hash"],
        "start": start,
        "end": start + len(quote),
        "quote": quote,
    }


def make_service(user_id, db, provider=None):
    settings = Settings(
        _env_file=None,
        database_url=URL,
        embedding_dimensions=3,
        supabase_url="https://project.supabase.co",
        supabase_publishable_key="test-key",
    )
    return DetectiveService(db, settings, provider=provider)


@pytest.mark.anyio
async def test_detective_applies_source_date_and_motif_scope_before_ranking():
    user_id = uuid4()
    conversation_id = uuid4()
    conversation = {
        "id": conversation_id,
        "user_id": user_id,
        "title": "Scoped search",
        "created_at": NOW,
        "updated_at": NOW,
    }
    house_motif, forest_motif = uuid4(), uuid4()
    matching = source(user_id, "A silver river flows beneath the house.")
    matching.update(
        current_revision_id=matching["revision_id"],
        dream_date=date(2026, 1, 15),
        motif_ids=[house_motif],
    )
    wrong_motif = source(user_id, "A silver river flows beneath the trees.")
    wrong_motif.update(
        current_revision_id=wrong_motif["revision_id"],
        dream_date=date(2026, 1, 15),
        motif_ids=[forest_motif],
    )
    outside_dates = source(user_id, "A silver river flows beneath the house.")
    outside_dates.update(
        current_revision_id=outside_dates["revision_id"],
        dream_date=date(2026, 2, 1),
        motif_ids=[house_motif],
    )
    outside_source_ids = source(user_id, "A silver river flows beneath the house.")
    outside_source_ids.update(
        current_revision_id=outside_source_ids["revision_id"],
        dream_date=date(2026, 1, 15),
        motif_ids=[house_motif],
    )
    foreign_tenant = source(uuid4(), "A silver river flows beneath the house.")
    foreign_tenant.update(
        current_revision_id=foreign_tenant["revision_id"],
        dream_date=date(2026, 1, 15),
        motif_ids=[house_motif],
    )

    class ScopedConnection(FakeConnection):
        async def execute(self, statement):
            sql = str(statement.compile())
            if (
                sql.startswith("SELECT")
                and "FROM app.dreams JOIN app.dream_revisions" in sql
            ):
                self.statements.append(statement)
                return FakeResult([matching])
            return await super().execute(statement)

    db = FakeDatabase(
        user_id,
        [conversation],
        [matching, wrong_motif, outside_dates, outside_source_ids, foreign_tenant],
    )
    db.connection = ScopedConnection(
        user_id,
        [conversation],
        [matching, wrong_motif, outside_dates, outside_source_ids, foreign_tenant],
    )
    service = make_service(user_id, db)

    results, coverage_limited = await service._retrieve(
        user_id,
        conversation_id,
        "silver river",
        [],
        source_ids=[matching["id"], wrong_motif["id"], outside_dates["id"]],
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 31),
        motif_ids=[house_motif],
    )

    assert [row["id"] for row in results] == [matching["id"]]
    assert not coverage_limited
    query = next(
        str(statement.compile())
        for statement in db.connection.statements
        if str(statement.compile()).startswith("SELECT")
        and "FROM app.dreams" in str(statement.compile())
    )
    assert "dreams.id IN" in query
    assert "dreams.dream_date >=" in query and "dreams.dream_date <=" in query
    assert "app.dream_motifs" in query


def install_admission(monkeypatch, *, replayed=False, resource_id=None):
    resource_id = resource_id or uuid4()

    async def reserve(*args, **kwargs):
        return SimpleNamespace(resource_id=resource_id, replayed=replayed)

    monkeypatch.setattr(detective_module, "reserve_ai", reserve)
    return resource_id


@pytest.mark.anyio
async def test_unexpected_provider_exception_is_safely_reported_and_failed(monkeypatch, caplog):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Provider error",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, "A green forest appeared.")
    db = FakeDatabase(user_id, [conversation], [dream])
    install_admission(monkeypatch)

    with pytest.raises(APIError) as error:
        await make_service(user_id, db, UnexpectedProvider()).ask(
            Identity(user_id),
            conversation["id"],
            "What appeared in the forest?",
            "unexpected_1",
        )

    assert error.value.status == 503
    assert error.value.message == detective_module.FAILED_ANSWER
    assert "private provider details" not in error.value.message
    assert [row["status"] for row in db.connection.messages] == ["failed", "failed"]
    assert "private provider details" not in caplog.text


@pytest.mark.anyio
async def test_persistence_exception_rolls_back_partial_evidence_and_fails_turn(monkeypatch):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Persistence failure",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, "A moon appeared above the sea.")
    quote = dream["text"]
    provider = FakeProvider(
        {
            "insufficient_evidence": False,
            "claims": [{"statement": quote, "citations": [citation(dream, quote)]}],
        }
    )
    db = FakeDatabase(user_id, [conversation], [dream])
    db.connection.fail_sql_once = "INSERT INTO app.evidence"
    install_admission(monkeypatch)

    with pytest.raises(APIError) as error:
        await make_service(user_id, db, provider).ask(
            Identity(user_id), conversation["id"], "Tell me about the moon.", "persist_fail_1"
        )

    assert error.value.status == 503
    assert db.connection.evidence == []
    assert db.connection.message_evidence == []
    assert [row["status"] for row in db.connection.messages] == ["failed", "failed"]


@pytest.mark.anyio
async def test_source_change_during_generation_yields_insufficient_answer(monkeypatch):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Concurrent source change",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, "A moon appeared above the sea.")
    dream["current_revision_id"] = dream["revision_id"]

    class SourceChangingProvider(FakeProvider):
        async def complete(self, *args, **kwargs):
            result = await super().complete(*args, **kwargs)
            db.connection.dreams[0]["current_revision_id"] = uuid4()
            return result

    quote = dream["text"]
    provider = SourceChangingProvider(
        {
            "insufficient_evidence": False,
            "claims": [{"statement": quote, "citations": [citation(dream, quote)]}],
        }
    )
    db = FakeDatabase(user_id, [conversation], [dream])
    install_admission(monkeypatch)

    answer = await make_service(user_id, db, provider).ask(
        Identity(user_id),
        conversation["id"],
        "Tell me about the moon.",
        "concurrent_source_1",
    )

    assert answer.insufficient_evidence
    assert answer.body == detective_module.INSUFFICIENT_ANSWER
    assert answer.citations == []
    assert db.connection.evidence == []


@pytest.mark.anyio
async def test_serialization_exception_rolls_back_then_marks_turn_failed(monkeypatch):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Serialization failure",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, "A moon appeared above the sea.")
    quote = dream["text"]
    provider = FakeProvider(
        {
            "insufficient_evidence": False,
            "claims": [{"statement": quote, "citations": [citation(dream, quote)]}],
        }
    )
    db = FakeDatabase(user_id, [conversation], [dream])
    install_admission(monkeypatch)
    service = make_service(user_id, db, provider)

    def fail_serialization(*_args):
        raise TypeError("internal serialization value")

    monkeypatch.setattr(DetectiveService, "_serialize_message", staticmethod(fail_serialization))
    with pytest.raises(APIError) as error:
        await service.ask(
            Identity(user_id), conversation["id"], "What happened?", "serialize_fail_1"
        )

    assert error.value.status == 503
    assert "internal serialization value" not in error.value.message
    assert db.connection.evidence == []
    assert db.connection.message_evidence == []
    assert [row["status"] for row in db.connection.messages] == ["failed", "failed"]


@pytest.mark.anyio
async def test_replay_does_not_duplicate_failed_turn_and_new_key_can_retry(monkeypatch):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Retry",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, "A moon appeared above the sea.")
    db = FakeDatabase(user_id, [conversation], [dream])
    first_key_id = install_admission(monkeypatch)
    with pytest.raises(APIError):
        await make_service(user_id, db, UnexpectedProvider()).ask(
            Identity(user_id), conversation["id"], "Tell me about the moon.", "retry_old_1"
        )
    assert len(db.connection.messages) == 2

    install_admission(monkeypatch, replayed=True, resource_id=first_key_id)
    with pytest.raises(APIError) as replay_error:
        await make_service(user_id, db, UnexpectedProvider()).ask(
            Identity(user_id), conversation["id"], "Tell me about the moon.", "retry_old_1"
        )
    assert replay_error.value.status == 503
    assert len(db.connection.messages) == 2

    quote = dream["text"]
    install_admission(monkeypatch)
    answer = await make_service(
        user_id,
        db,
        FakeProvider(
            {
                "insufficient_evidence": False,
                "claims": [{"statement": quote, "citations": [citation(dream, quote)]}],
            }
        ),
    ).ask(Identity(user_id), conversation["id"], "Tell me about the moon.", "retry_new_1")

    assert answer.status == "completed"
    assert len(db.connection.messages) == 4
    assert [row["status"] for row in db.connection.messages] == [
        "failed",
        "failed",
        "completed",
        "completed",
    ]


@pytest.mark.anyio
async def test_replay_does_not_process_a_turn_already_pending(monkeypatch):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Recover pending",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, "A moon appeared above the sea.")
    db = FakeDatabase(user_id, [conversation], [dream])
    resource_id = uuid4()
    db.connection.messages.append(
        {
            "id": uuid4(),
            "user_id": user_id,
            "conversation_id": conversation["id"],
            "role": "user",
            "body": "What appeared?",
            "status": "pending",
            "request_key": str(resource_id),
            "created_at": NOW,
            "context_versions": {},
        }
    )
    install_admission(monkeypatch, replayed=True, resource_id=resource_id)
    provider = FakeProvider(
        {"insufficient_evidence": False, "claims": []}
    )

    with pytest.raises(APIError) as error:
        await make_service(user_id, db, provider).ask(
            Identity(user_id),
            conversation["id"],
            "Tell me about the moon.",
            "recover_pending_1",
        )

    assert error.value.status == 409
    assert provider.calls == []
    assert sum(row["role"] == "user" for row in db.connection.messages) == 1
    assert sum(row["role"] == "assistant" for row in db.connection.messages) == 0
    assert db.connection.messages[0]["status"] == "pending"


@pytest.mark.anyio
async def test_concurrent_duplicate_cannot_overwrite_completing_turn(monkeypatch):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Concurrent retry",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, "A moon appeared above the sea.")
    db = FakeDatabase(user_id, [conversation], [dream])
    request_id = uuid4()
    reserve_calls = 0

    async def reserve(*args, **kwargs):
        nonlocal reserve_calls
        reserve_calls += 1
        return SimpleNamespace(
            resource_id=request_id,
            replayed=reserve_calls > 1,
        )

    monkeypatch.setattr(detective_module, "reserve_ai", reserve)
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    class BlockingProvider(FakeProvider):
        async def complete(self, *args, **kwargs):
            provider_started.set()
            await release_provider.wait()
            return await super().complete(*args, **kwargs)

    text = dream["text"]
    provider = BlockingProvider(
        {
            "insufficient_evidence": False,
            "claims": [{"statement": text, "citations": [citation(dream, text)]}],
        }
    )
    service = make_service(user_id, db, provider)
    identity = Identity(user_id)
    first = asyncio.create_task(
        service.ask(identity, conversation["id"], "Tell me about the moon.", "same_key_1")
    )
    await asyncio.wait_for(provider_started.wait(), timeout=2)

    with pytest.raises(APIError) as duplicate_error:
        await service.ask(
            identity, conversation["id"], "Tell me about the moon.", "same_key_1"
        )
    assert duplicate_error.value.status == 409

    release_provider.set()
    completed = await first
    before_late_failure = completed.body
    user_row = next(row for row in db.connection.messages if row["role"] == "user")
    assistant_row = next(row for row in db.connection.messages if row["role"] == "assistant")
    await service._fail_turn(
        user_id, conversation["id"], user_row["id"], str(request_id)
    )

    replay = await service.ask(
        identity, conversation["id"], "Tell me about the moon.", "same_key_1"
    )

    assert replay.id == completed.id
    assert replay.body == before_late_failure
    assert assistant_row["status"] == "completed"
    assert assistant_row["body"] == before_late_failure
    assert sum(row["role"] == "user" for row in db.connection.messages) == 1
    assert sum(row["role"] == "assistant" for row in db.connection.messages) == 1
    assert len(provider.calls) == 1


@pytest.mark.anyio
async def test_failed_turn_can_retry_same_key_without_duplicate_messages(monkeypatch):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Retry same key",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, "A moon appeared above the sea.")
    db = FakeDatabase(user_id, [conversation], [dream])
    request_id = uuid4()
    reserve_calls = 0

    async def reserve(*args, **kwargs):
        nonlocal reserve_calls
        reserve_calls += 1
        return SimpleNamespace(resource_id=request_id, replayed=reserve_calls > 1)

    monkeypatch.setattr(detective_module, "reserve_ai", reserve)
    identity = Identity(user_id)
    with pytest.raises(APIError):
        await make_service(user_id, db, UnexpectedProvider()).ask(
            identity, conversation["id"], "Tell me about the moon.", "retry_same_1"
        )

    initial_ids = {row["role"]: row["id"] for row in db.connection.messages}
    text = dream["text"]
    answer = await make_service(
        user_id,
        db,
        FakeProvider(
            {
                "insufficient_evidence": False,
                "claims": [{"statement": text, "citations": [citation(dream, text)]}],
            }
        ),
    ).ask(identity, conversation["id"], "Tell me about the moon.", "retry_same_1")

    assert answer.status == "completed"
    assert {row["role"]: row["id"] for row in db.connection.messages} == initial_ids
    assert [row["status"] for row in db.connection.messages] == ["completed", "completed"]
    assert sum(row["role"] == "assistant" for row in db.connection.messages) == 1


@pytest.mark.anyio
async def test_conversations_create_list_retrieve_and_enforce_ownership():
    user_id, other_user = uuid4(), uuid4()
    owned = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "My dreams",
        "created_at": NOW,
        "updated_at": NOW,
    }
    foreign = {**owned, "id": uuid4(), "user_id": other_user}
    db = FakeDatabase(user_id, [owned, foreign])
    service = make_service(user_id, db)
    identity = Identity(user_id)

    created = await service.create_conversation(identity, "New thread")
    listed = await service.list_conversations(identity)
    detail = await service.get_conversation(identity, owned["id"])

    assert created.title == "New thread"
    assert listed.count == 2 and owned["id"] in {item.id for item in listed.items}
    assert detail.messages == []
    with pytest.raises(APIError) as error:
        await service.get_conversation(Identity(other_user), owned["id"])
    assert error.value.status == 404


@pytest.mark.anyio
async def test_conversation_deletion_is_tenant_scoped_and_missing_is_not_found():
    user_id, other_user = uuid4(), uuid4()
    owned = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Owned",
        "created_at": NOW,
        "updated_at": NOW,
    }
    foreign = {**owned, "id": uuid4(), "user_id": other_user}
    db = FakeDatabase(user_id, [owned, foreign])
    service = make_service(user_id, db)

    with pytest.raises(APIError) as error:
        await service.delete_conversation(Identity(user_id), foreign["id"])
    assert error.value.status == 404

    await service.delete_conversation(Identity(user_id), owned["id"])

    assert owned not in db.connection.conversations
    assert foreign in db.connection.conversations
    assert any(
        "jobs.user_id" in str(statement.compile())
        and "app.jobs" in str(statement.compile())
        for statement in db.connection.statements
    )


@pytest.mark.anyio
async def test_conversation_deletion_redacts_all_job_payloads_and_blocks_active_worker():
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Private",
        "created_at": NOW,
        "updated_at": NOW,
    }
    db = FakeDatabase(user_id, [conversation])
    service = make_service(user_id, db)
    target_jobs = []
    statuses = ("queued", "running", "succeeded", "failed", "cancelled")
    for status in statuses:
        job = {
            "id": uuid4(),
            "user_id": user_id,
            "kind": "detective_answer",
            "status": status,
            "stage": status,
            "attempts": 1,
            "lease_token": uuid4() if status == "running" else None,
            "leased_until": NOW if status == "running" else None,
            "deadline_at": NOW if status == "running" else None,
            "stage_deadline_at": NOW if status == "running" else None,
            "payload": {
                "conversation_id": str(conversation["id"]),
                "question": "Private question content",
                "user_message_id": str(uuid4()),
                "assistant_message_id": str(uuid4()),
            },
        }
        target_jobs.append(job)
        db.connection.jobs.append(job)
    foreign_job = {
        **target_jobs[0],
        "id": uuid4(),
        "user_id": uuid4(),
        "payload": {"conversation_id": str(conversation["id"]), "question": "Other tenant"},
    }
    db.connection.jobs.append(foreign_job)
    active_job = deepcopy(next(job for job in target_jobs if job["status"] == "running"))

    await service.delete_conversation(Identity(user_id), conversation["id"])

    for job, original_status in zip(target_jobs, statuses, strict=True):
        assert job["payload"] == {}
        if original_status in {"queued", "running"}:
            assert job["status"] == "cancelled"
            assert job["error_code"] == "conversation_deleted"
        else:
            assert job["status"] == original_status
    assert foreign_job["payload"]["question"] == "Other tenant"

    with pytest.raises(detective_module.SourceChanged):
        await service._persist_turn(
            user_id,
            conversation["id"],
            uuid4(),
            str(active_job["id"]),
            "An answer",
            False,
            False,
            "top_ranked",
            [],
            [],
            [],
            job=active_job,
        )


@pytest.mark.anyio
async def test_message_lookup_is_tenant_scoped_and_returns_pending_message():
    user_id, other_user = uuid4(), uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Owned",
        "created_at": NOW,
        "updated_at": NOW,
    }
    message = {
        "id": uuid4(),
        "user_id": user_id,
        "conversation_id": conversation["id"],
        "role": "assistant",
        "body": "",
        "status": "pending",
        "created_at": NOW,
        "request_key": "pending-key",
        "context_versions": {},
    }
    foreign_message = {**message, "id": uuid4(), "user_id": other_user}
    db = FakeDatabase(user_id, [conversation])
    db.connection.messages.extend([message, foreign_message])
    service = make_service(user_id, db)

    result = await service.get_message(Identity(user_id), message["id"])

    assert result.id == message["id"]
    assert result.status == "pending"
    assert result.citations == []
    with pytest.raises(APIError) as error:
        await service.get_message(Identity(user_id), foreign_message["id"])
    assert error.value.status == 404


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["get_message", "delete_conversation", "submit"])
async def test_new_message_endpoints_surface_database_failures(monkeypatch, operation):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Database failure",
        "created_at": NOW,
        "updated_at": NOW,
    }
    db = FakeDatabase(user_id, [conversation])
    service = make_service(user_id, db)
    if operation == "get_message":
        db.connection.fail_sql_once = "FROM app.messages"
    elif operation == "delete_conversation":
        db.connection.fail_sql_once = "FROM app.evidence"
    else:
        db.connection.fail_sql_once = "INSERT INTO app.jobs"

        async def reservation(*args, **kwargs):
            return SimpleNamespace(resource_id=uuid4(), replayed=False)

        monkeypatch.setattr(detective_module, "reserve_ai", reservation)

    with pytest.raises(RuntimeError, match="simulated database failure"):
        if operation == "get_message":
            await service.get_message(Identity(user_id), uuid4())
        elif operation == "delete_conversation":
            await service.delete_conversation(Identity(user_id), conversation["id"])
        else:
            await service.submit(
                Identity(user_id),
                conversation["id"],
                "Tell me about the moon.",
                "db_failure",
            )


@pytest.mark.anyio
async def test_message_submission_enqueues_once_and_replays_status(monkeypatch):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Queued",
        "created_at": NOW,
        "updated_at": NOW,
    }
    db = FakeDatabase(user_id, [conversation])
    request_id = uuid4()
    replay = False

    async def reserve(*args, **kwargs):
        return SimpleNamespace(resource_id=request_id, replayed=replay)

    monkeypatch.setattr(detective_module, "reserve_ai", reserve)
    service = make_service(user_id, db)
    identity = Identity(user_id)

    accepted = await service.submit(
        identity, conversation["id"], "Tell me about the moon.", "queued_1"
    )
    replay = True
    repeated = await service.submit(
        identity, conversation["id"], "Tell me about the moon.", "queued_1"
    )

    assert accepted.status == "queued"
    assert accepted.job_id == repeated.job_id == request_id
    assert accepted.message_id == repeated.message_id
    assert len(db.connection.jobs) == 1
    assert len(db.connection.messages) == 2
    assert {row["role"] for row in db.connection.messages} == {"user", "assistant"}
    assert "question" not in db.connection.jobs[0]["payload"]
    assert db.connection.messages[0]["body"] == "Tell me about the moon."


@pytest.mark.anyio
async def test_durable_worker_reads_question_from_private_message(monkeypatch):
    user_id, conversation_id, job_id = uuid4(), uuid4(), uuid4()
    user_message_id, assistant_message_id, lease_token = uuid4(), uuid4(), uuid4()
    question = "What changed in the house?"
    conversation = {
        "id": conversation_id,
        "user_id": user_id,
        "title": "Queued",
        "created_at": NOW,
        "updated_at": NOW,
    }
    job = {
        "id": job_id,
        "user_id": user_id,
        "kind": "detective_answer",
        "status": "running",
        "lease_token": lease_token,
        "leased_until": NOW + timedelta(minutes=5),
        "deadline_at": NOW + timedelta(minutes=10),
        "data_epoch": 1,
        "payload": {
            "conversation_id": str(conversation_id),
            "user_message_id": str(user_message_id),
            "assistant_message_id": str(assistant_message_id),
            "source_ids": [],
            "from_date": None,
            "to_date": None,
            "motif_ids": [],
        },
    }
    db = FakeDatabase(user_id, [conversation])
    db.connection.jobs.append(job)
    db.connection.messages.extend(
        [
            {
                "id": user_message_id,
                "user_id": user_id,
                "conversation_id": conversation_id,
                "role": "user",
                "body": question,
                "status": "pending",
                "created_at": NOW,
                "request_key": str(job_id),
                "context_versions": {},
            },
            {
                "id": assistant_message_id,
                "user_id": user_id,
                "conversation_id": conversation_id,
                "role": "assistant",
                "body": "",
                "status": "pending",
                "created_at": NOW,
                "request_key": str(job_id),
                "context_versions": {},
            },
        ]
    )
    service = make_service(user_id, db)
    received = []

    async def retrieve(_user_id, _conversation_id, actual_question, *_args, **_kwargs):
        received.append(actual_question)
        return [], False

    async def persist(*_args, **_kwargs):
        return "persisted"

    monkeypatch.setattr(service, "_retrieve", retrieve)
    monkeypatch.setattr(service, "_persist_turn", persist)

    assert await service.run_queued_job(job) == "persisted"
    assert received == [question]


@pytest.mark.anyio
async def test_durable_worker_failure_is_retryable_then_terminal_without_downgrade():
    user_id = uuid4()
    conversation_id, job_id = uuid4(), uuid4()
    user_message_id, assistant_message_id, lease_token = uuid4(), uuid4(), uuid4()
    conversation = {
        "id": conversation_id,
        "user_id": user_id,
        "title": "Queued",
        "created_at": NOW,
        "updated_at": NOW,
    }
    db = FakeDatabase(user_id, [conversation])
    settings = make_service(user_id, db).settings
    job = {
        "id": job_id,
        "user_id": user_id,
        "kind": "detective_answer",
        "status": "running",
        "lease_token": lease_token,
        "attempts": 1,
        "payload": {
            "conversation_id": str(conversation_id),
            "user_message_id": str(user_message_id),
            "assistant_message_id": str(assistant_message_id),
        },
    }
    db.connection.jobs.append(dict(job))
    db.connection.messages.extend(
        [
            {
                "id": user_message_id,
                "user_id": user_id,
                "conversation_id": conversation_id,
                "role": "user",
                "body": "Tell me about the moon.",
                "status": "pending",
                "created_at": NOW,
                "request_key": str(job_id),
                "context_versions": {},
            },
            {
                "id": assistant_message_id,
                "user_id": user_id,
                "conversation_id": conversation_id,
                "role": "assistant",
                "body": "",
                "status": "pending",
                "created_at": NOW,
                "request_key": str(job_id),
                "context_versions": {},
            },
        ]
    )
    handler = DurableJobHandler("detective_answer", settings, db)

    await handler.fail(job, WorkFailure("provider_timeout", retryable=True))

    assert db.connection.jobs[0]["status"] == "queued"
    assert [row["status"] for row in db.connection.messages] == ["pending", "pending"]

    db.connection.jobs[0].update(
        status="running",
        attempts=settings.job_max_attempts,
        lease_token=lease_token,
    )
    terminal_job = {**job, "attempts": settings.job_max_attempts}
    await handler.fail(terminal_job, WorkFailure("provider_timeout", retryable=True))
    assert db.connection.jobs[0]["status"] == "failed"
    assert [row["status"] for row in db.connection.messages] == ["failed", "failed"]

    db.connection.jobs[0]["status"] = "succeeded"
    db.connection.messages[1].update(status="completed", body="A completed answer.")
    await handler.fail(terminal_job, WorkFailure("provider_timeout", retryable=True))
    assert db.connection.messages[1]["status"] == "completed"
    assert db.connection.messages[1]["body"] == "A completed answer."


@pytest.mark.anyio
async def test_worker_dispatches_durable_api_jobs_without_dream_analysis_prepare():
    user_id = uuid4()
    db = FakeDatabase(user_id)
    settings = make_service(user_id, db).settings
    dispatched = []

    class Handler:
        async def run(self, job):
            dispatched.append(job["kind"])

    worker = Worker(
        settings,
        db,
        queue=object(),
        handlers={
            "detective_answer": Handler(),
            "storyline_refresh": Handler(),
        },
    )

    async def unexpected_prepare(_job):
        raise AssertionError("Durable API jobs must not use dream-backed analysis prepare")

    worker.store.prepare = unexpected_prepare
    for kind in ("detective_answer", "storyline_refresh"):
        await worker._execute(
            {"id": uuid4(), "kind": kind, "user_id": user_id, "payload": {}},
            {},
        )

    assert dispatched == ["detective_answer", "storyline_refresh"]


@pytest.mark.anyio
async def test_answer_quotes_exact_owned_sources_and_persists_evidence(monkeypatch):
    user_id, other_user = uuid4(), uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Dreams",
        "created_at": NOW,
        "updated_at": NOW,
    }
    first = source(user_id, "I walked across a silver bridge beneath the moon.", "Bridge")
    second = source(user_id, "The silver bridge swayed over a dark river.", "River")
    foreign = source(other_user, "I crossed the silver bridge with a dragon.", "Foreign")
    quote_one = first["text"]
    quote_two = second["text"]
    provider = FakeProvider(
        {
            "insufficient_evidence": False,
            "claims": [
                {
                    "statement": quote_one,
                    "citations": [citation(first, quote_one)],
                },
                {
                    "statement": quote_two,
                    "citations": [citation(second, quote_two)],
                },
            ],
        }
    )
    db = FakeDatabase(user_id, [conversation], [first, second, foreign])
    service = make_service(user_id, db, provider)
    install_admission(monkeypatch)

    answer = await service.ask(
        Identity(user_id), conversation["id"], "How many dreams mention the silver bridge?", "key_1"
    )

    assert answer.status == "completed"
    assert answer.citations and len(answer.citations) == 2
    assert answer.citations[0].quote == quote_one
    assert answer.citations[1].quote == quote_two
    assert len(db.connection.evidence) == 2
    assert len(db.connection.message_evidence) == 2
    assert [row["status"] for row in db.connection.messages] == ["completed", "completed"]
    assert db.connection.model_runs[0]["status"] == "validated"
    detail = await service.get_conversation(Identity(user_id), conversation["id"])
    assert len(detail.messages) == 2
    assert len(detail.messages[-1].citations) == 2
    assert len(detail.messages[-1].claims) == 2
    assert [len(claim.citations) for claim in detail.messages[-1].claims] == [1, 1]
    prompt_body = provider.calls[0][1]["content"]
    assert "Foreign" not in prompt_body
    assert str(foreign["id"]) not in prompt_body
    assert '"coverage": "literal_match_set"' in prompt_body
    assert all(tenant_user == user_id for tenant_user in db.tenant_users)


@pytest.mark.anyio
async def test_insufficient_evidence_is_persisted_without_calling_model(monkeypatch):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Empty archive",
        "created_at": NOW,
        "updated_at": NOW,
    }
    provider = FakeProvider({})
    db = FakeDatabase(user_id, [conversation])
    service = make_service(user_id, db, provider)
    install_admission(monkeypatch)

    answer = await service.ask(
        Identity(user_id), conversation["id"], "Which city appeared?", "empty_1"
    )

    assert answer.insufficient_evidence
    assert answer.body == detective_module.INSUFFICIENT_ANSWER
    assert answer.citations == []
    assert provider.calls == []
    assert [row["status"] for row in db.connection.messages] == ["completed", "completed"]


@pytest.mark.anyio
async def test_exhaustive_question_discloses_when_matches_exceed_safe_limit(monkeypatch):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Many meteors",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dreams = [source(user_id, f"I saw meteors near location {number}.") for number in range(6)]
    provider = FakeProvider({})
    db = FakeDatabase(user_id, [conversation], dreams)
    service = make_service(user_id, db, provider)
    install_admission(monkeypatch)

    answer = await service.ask(
        Identity(user_id), conversation["id"], "How many dreams mention meteors?", "many_1"
    )

    assert answer.insufficient_evidence
    assert answer.coverage_limited
    assert answer.body == detective_module.LIMITED_ANSWER
    assert provider.calls == []


@pytest.mark.anyio
async def test_first_mention_uses_earliest_matching_saved_entry(monkeypatch):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "First mention",
        "created_at": NOW,
        "updated_at": NOW,
    }
    newer = source(user_id, "A forest appeared near the river.", "Later forest")
    newer["dream_date"] = date(2026, 1, 1)
    earlier = source(user_id, "I walked through a forest of pine trees.", "First forest")
    earlier["dream_date"] = date(2020, 1, 1)
    quote = earlier["text"]
    provider = FakeProvider(
        {
            "insufficient_evidence": False,
            "claims": [
                {
                    "statement": quote,
                    "citations": [citation(earlier, quote)],
                }
            ],
        }
    )
    db = FakeDatabase(user_id, [conversation], [newer, earlier])
    db.connection.messages.append(
        {
            "id": uuid4(),
            "user_id": user_id,
            "conversation_id": conversation["id"],
            "role": "user",
            "body": "Did I cross a bridge?",
            "status": "completed",
            "created_at": NOW,
            "context_versions": {},
        }
    )
    service = make_service(user_id, db, provider)
    install_admission(monkeypatch)

    answer = await service.ask(
        Identity(user_id), conversation["id"], "When did I first mention a forest?", "first_1"
    )

    assert answer.citations[0].dream_id == earlier["id"]
    assert answer.citations[0].dream_date == date(2020, 1, 1)
    assert answer.body == detective_module._grounded_statement(quote, "earliest_match")
    prompt_body = provider.calls[0][1]["content"]
    assert str(earlier["id"]) in prompt_body
    assert str(newer["id"]) not in prompt_body
    assert '"coverage": "earliest_match"' in prompt_body
    assert '"coverage": "literal_match_set"' not in prompt_body


@pytest.mark.anyio
async def test_earliest_exact_term_search_rejects_substrings_and_keeps_exact_tokens():
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Exact terms",
        "created_at": NOW,
        "updated_at": NOW,
    }
    older_substring = source(user_id, "A wolfish shadow crossed the river.")
    older_substring["dream_date"] = date(2020, 1, 1)
    exact_match = source(user_id, "A wolf crossed the river.")
    exact_match["dream_date"] = date(2025, 1, 1)
    db = FakeDatabase(user_id, [conversation], [older_substring, exact_match])
    service = make_service(user_id, db)

    sources, _ = await service._retrieve(
        user_id, conversation["id"], "first wolf river", []
    )

    assert [row["id"] for row in sources] == [exact_match["id"]]
    assert sources[0]["coverage"] == "earliest_match"


@pytest.mark.anyio
async def test_earliest_exact_river_term_matches_and_selects_earliest_exact_result():
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Exact river",
        "created_at": NOW,
        "updated_at": NOW,
    }
    substring = source(user_id, "Water ran along the riverbank.")
    substring["dream_date"] = date(2019, 1, 1)
    later_exact = source(user_id, "Water ran along the river.")
    later_exact["dream_date"] = date(2021, 1, 1)
    earliest_exact = source(user_id, "I saw a river.")
    earliest_exact["dream_date"] = date(2020, 1, 1)
    db = FakeDatabase(user_id, [conversation], [substring, later_exact, earliest_exact])
    service = make_service(user_id, db)

    sources, _ = await service._retrieve(
        user_id, conversation["id"], "first river", []
    )

    assert [row["id"] for row in sources] == [earliest_exact["id"]]


@pytest.mark.anyio
async def test_qualified_earliest_match_is_supported_only_by_ordered_earliest_result(
    monkeypatch,
):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Qualified earliest",
        "created_at": NOW,
        "updated_at": NOW,
    }
    earlier = source(
        user_id, "The earliest matching saved dream contains a blue moon."
    )
    earlier["dream_date"] = date(2020, 1, 1)
    later = source(
        user_id, "A later matching saved dream contains a blue moon."
    )
    later["dream_date"] = date(2025, 1, 1)
    provider = FakeProvider(
        {
            "insufficient_evidence": False,
            "claims": [
                {
                    "statement": earlier["text"],
                    "citations": [citation(earlier, earlier["text"])],
                }
            ],
        }
    )
    db = FakeDatabase(user_id, [conversation], [later, earlier])
    install_admission(monkeypatch)

    service = make_service(user_id, db, provider)
    answer = await service.ask(
        Identity(user_id),
        conversation["id"],
        "What was the earliest ever saved dream with a blue moon?",
        "qualified_earliest_1",
    )

    assert not answer.insufficient_evidence
    assert answer.citations[0].dream_id == earlier["id"]
    assert answer.citations[0].dream_date == date(2020, 1, 1)
    assert '"coverage": "earliest_match"' in provider.calls[0][1]["content"]
    history = await service.get_conversation(Identity(user_id), conversation["id"])
    historical_answer = history.messages[-1]
    assert historical_answer.status == "completed"
    assert historical_answer.body == detective_module._grounded_statement(
        earlier["text"], "earliest_match"
    )


@pytest.mark.anyio
async def test_single_earliest_result_is_not_marked_as_exhaustive():
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Single result",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, "A silver moon rose above the hills.")
    db = FakeDatabase(user_id, [conversation], [dream])
    service = make_service(user_id, db)

    sources, coverage_limited = await service._retrieve(
        user_id, conversation["id"], "When did the silver moon first appear?", []
    )

    assert not coverage_limited
    assert len(sources) == 1
    assert sources[0]["id"] == dream["id"]
    assert sources[0]["coverage"] == "earliest_match"
    assert "complete_match_set" not in sources[0]


@pytest.mark.anyio
async def test_incomplete_earliest_search_returns_only_qualified_exact_term_match(
    monkeypatch,
):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "First time",
        "created_at": NOW,
        "updated_at": NOW,
    }
    text = "This was the first time I saw a red moon."
    dream = source(user_id, text)
    provider = FakeProvider(
        {
            "insufficient_evidence": False,
            "claims": [{"statement": text, "citations": [citation(dream, text)]}],
        }
    )
    db = FakeDatabase(user_id, [conversation], [dream])
    install_admission(monkeypatch)

    answer = await make_service(user_id, db, provider).ask(
        Identity(user_id),
        conversation["id"],
        "When was the first time I saw a red moon?",
        "first_claim_1",
    )

    assert not answer.insufficient_evidence
    assert answer.body == detective_module._grounded_statement(text, "earliest_match")
    assert "earliest semantic occurrence" in answer.body
    assert answer.claims[0].statement == answer.body
    assert answer.citations[0].quote == text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "query",
    [
        "What was the first time I ever saw a wolf?",
        "What was the earliest ever dream with a wolf?",
    ],
)
async def test_first_ever_and_earliest_ever_use_earliest_coverage(monkeypatch, query):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Mixed earliest intent",
        "created_at": NOW,
        "updated_at": NOW,
    }
    newer = source(user_id, "A wolf appeared beneath the moon.", "Later")
    newer["dream_date"] = date(2025, 1, 1)
    earlier = source(user_id, "A wolf crossed the silent forest.", "Earlier")
    earlier["dream_date"] = date(2020, 1, 1)
    provider = FakeProvider(
        {
            "insufficient_evidence": False,
            "claims": [
                {
                    "statement": earlier["text"],
                    "citations": [citation(earlier, earlier["text"])],
                }
            ],
        }
    )
    db = FakeDatabase(user_id, [conversation], [newer, earlier])
    install_admission(monkeypatch)

    answer = await make_service(user_id, db, provider).ask(
        Identity(user_id), conversation["id"], query, "mixed_earliest_1"
    )

    assert answer.citations[0].dream_id == earlier["id"]
    assert '"coverage": "earliest_match"' in provider.calls[0][1]["content"]
    assert '"coverage": "literal_match_set"' not in provider.calls[0][1]["content"]


@pytest.mark.anyio
async def test_literal_search_does_not_claim_semantic_archive_completeness(monkeypatch):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Literal search",
        "created_at": NOW,
        "updated_at": NOW,
    }
    exact = source(user_id, "A wolf stood near the river.")
    semantic = source(user_id, "A canine stood near the stream.")
    provider = FakeProvider(
        {
            "insufficient_evidence": False,
            "claims": [
                {
                    "statement": exact["text"],
                    "citations": [citation(exact, exact["text"])],
                }
            ],
        }
    )
    db = FakeDatabase(user_id, [conversation], [exact, semantic])
    install_admission(monkeypatch)

    answer = await make_service(user_id, db, provider).ask(
        Identity(user_id),
        conversation["id"],
        "How many dreams ever mention a wolf?",
        "literal_scope_1",
    )

    assert answer.citations[0].dream_id == exact["id"]
    assert answer.body == detective_module._grounded_statement(
        exact["text"], "literal_match_set"
    )
    assert "literal-term result may omit semantically similar entries" in answer.body
    assert semantic["text"] not in answer.body


@pytest.mark.anyio
@pytest.mark.parametrize(
    "source_text",
    [
        "This was the sole time I saw a wolf.",
        "This was the only occasion I saw a wolf.",
        "I never saw a wolf in any other dream.",
        "I always saw a wolf in every dream.",
    ],
)
async def test_archive_assertions_are_returned_only_as_qualified_source_quotes(
    monkeypatch, source_text
):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Attributed archive assertion",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, source_text)
    provider = FakeProvider(
        {
            "insufficient_evidence": False,
            "claims": [
                {
                    "statement": source_text,
                    "citations": [citation(dream, source_text)],
                }
            ],
        }
    )
    db = FakeDatabase(user_id, [conversation], [dream])
    install_admission(monkeypatch)

    answer = await make_service(user_id, db, provider).ask(
        Identity(user_id),
        conversation["id"],
        "What happened with the wolf?",
        "qualified_quote_1",
    )

    expected = detective_module._grounded_statement(source_text, "top_ranked")
    assert answer.body == expected
    assert answer.claims[0].statement == expected
    assert answer.citations[0].quote == source_text
    assert "does not establish archive-wide completeness" in answer.body


@pytest.mark.anyio
@pytest.mark.parametrize(
    "citation_factory",
    [
        lambda dream: {**citation(dream, "moon"), "dream_id": str(uuid4())},
        lambda dream: {**citation(dream, "moon"), "quote": "a fabricated moon"},
    ],
)
async def test_fabricated_or_inexact_citations_are_rejected(monkeypatch, citation_factory):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Validation",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, "A moon appeared above the sea.")
    provider = FakeProvider(
        {
            "insufficient_evidence": False,
            "claims": [
                {
                    "statement": "The dream mentioned a moon.",
                    "citations": [citation_factory(dream)],
                }
            ],
        }
    )
    db = FakeDatabase(user_id, [conversation], [dream])
    service = make_service(user_id, db, provider)
    install_admission(monkeypatch)

    answer = await service.ask(
        Identity(user_id), conversation["id"], "What happened to the moon?", "invalid_1"
    )

    assert answer.insufficient_evidence
    assert answer.body == detective_module.INSUFFICIENT_ANSWER
    assert answer.citations == []
    assert db.connection.evidence == []
    assert [row["status"] for row in db.connection.messages] == ["completed", "completed"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "statement",
    ["A dragon attacked the castle.", "The enormous moon destroyed the city."],
)
async def test_unrelated_or_exaggerated_claims_become_insufficient(monkeypatch, statement):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Quote validation",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, "A moon appeared above the sea.")
    quote = "A moon appeared above the sea."
    provider = FakeProvider(
        {
            "insufficient_evidence": False,
            "claims": [{"statement": statement, "citations": [citation(dream, quote)]}],
        }
    )
    db = FakeDatabase(user_id, [conversation], [dream])
    install_admission(monkeypatch)

    answer = await make_service(user_id, db, provider).ask(
        Identity(user_id), conversation["id"], "What appeared?", "unsupported_1"
    )

    assert answer.insufficient_evidence
    assert answer.body == detective_module.INSUFFICIENT_ANSWER
    assert answer.claims == []
    assert answer.citations == []
    assert db.connection.evidence == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("dream_text", "claim"),
    [
        (
            "My brother said, 'I stole the money,' but I did not steal it.",
            "I stole the money.",
        ),
        ("I did not see a wolf.", "I saw a wolf."),
        ("I saw a wolf, but it may have been a shadow.", "I saw a wolf."),
        ("There was a moonlit castle.", "The castle was destroyed."),
    ],
)
async def test_contextual_or_partial_claims_are_not_supported(
    monkeypatch, dream_text, claim
):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Context validation",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, dream_text)
    provider = FakeProvider(
        {
            "insufficient_evidence": False,
            "claims": [{"statement": claim, "citations": [citation(dream, dream_text)]}],
        }
    )
    db = FakeDatabase(user_id, [conversation], [dream])
    install_admission(monkeypatch)

    answer = await make_service(user_id, db, provider).ask(
        Identity(user_id),
        conversation["id"],
        f"Tell me about {next(term for term in ('money', 'wolf', 'castle') if term in dream_text)}",
        "context_validation_1",
    )

    assert answer.insufficient_evidence
    assert answer.body == detective_module.INSUFFICIENT_ANSWER
    assert answer.claims == []
    assert answer.citations == []


@pytest.mark.anyio
async def test_exact_complete_source_sentence_preserves_attribution_and_negation(monkeypatch):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Verbatim support",
        "created_at": NOW,
        "updated_at": NOW,
    }
    text = "My brother said, 'I stole the money,' but I did not steal it."
    dream = source(user_id, text)
    provider = FakeProvider(
        {
            "insufficient_evidence": False,
            "claims": [{"statement": text, "citations": [citation(dream, text)]}],
        }
    )
    db = FakeDatabase(user_id, [conversation], [dream])
    install_admission(monkeypatch)

    answer = await make_service(user_id, db, provider).ask(
        Identity(user_id), conversation["id"], "Tell me about the money", "verbatim_support_1"
    )

    assert not answer.insufficient_evidence
    assert answer.body == detective_module._grounded_statement(text, "top_ranked")
    assert answer.claims[0].statement == answer.body
    assert answer.citations[0].quote == text


@pytest.mark.anyio
async def test_only_individually_supported_claims_are_returned(monkeypatch):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Partial support",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, "A moon appeared above the quiet sea.")
    supported = dream["text"]
    quote = dream["text"]
    provider = FakeProvider(
        {
            "insufficient_evidence": False,
            "claims": [
                {"statement": supported, "citations": [citation(dream, quote)]},
                {
                    "statement": "A ship sank beneath the sea.",
                    "citations": [citation(dream, quote)],
                },
            ],
        }
    )
    db = FakeDatabase(user_id, [conversation], [dream])
    install_admission(monkeypatch)

    answer = await make_service(user_id, db, provider).ask(
        Identity(user_id), conversation["id"], "Tell me about the moon.", "partial_1"
    )

    assert len(provider.calls) == 1
    assert not answer.insufficient_evidence
    assert answer.body == detective_module._grounded_statement(supported, "top_ranked")
    assert [claim.statement for claim in answer.claims] == [answer.body]
    assert len(answer.claims[0].citations) == 1


@pytest.mark.anyio
async def test_multi_sentence_claim_is_not_accepted_as_one_claim(monkeypatch):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Atomic claims",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, "The moon appeared. A ship crossed the sea.")
    quote = dream["text"]
    provider = FakeProvider(
        {
            "insufficient_evidence": False,
            "claims": [{"statement": quote, "citations": [citation(dream, quote)]}],
        }
    )
    db = FakeDatabase(user_id, [conversation], [dream])
    install_admission(monkeypatch)

    answer = await make_service(user_id, db, provider).ask(
        Identity(user_id), conversation["id"], "What happened?", "atomic_1"
    )

    assert answer.insufficient_evidence
    assert answer.claims == []
    assert answer.body == detective_module.INSUFFICIENT_ANSWER


@pytest.mark.anyio
async def test_cross_user_conversation_and_dream_access_is_denied(monkeypatch):
    user_id, other_user = uuid4(), uuid4()
    foreign_conversation = {
        "id": uuid4(),
        "user_id": other_user,
        "title": "Private",
        "created_at": NOW,
        "updated_at": NOW,
    }
    foreign_dream = source(other_user, "A silver bridge was underwater.")
    provider = FakeProvider({})
    db = FakeDatabase(user_id, [foreign_conversation], [foreign_dream])
    service = make_service(user_id, db, provider)
    install_admission(monkeypatch)

    with pytest.raises(APIError) as error:
        await service.ask(
            Identity(user_id), foreign_conversation["id"], "Tell me about the bridge", "foreign_1"
        )

    assert error.value.status == 404
    assert provider.calls == []
    assert db.connection.messages == []


def add_historical_answer(db, conversation, dreams, *, quote, index=0):
    message_id = uuid4()
    evidence_id = uuid4()
    source_dream = dreams[0]
    message = {
        "id": message_id,
        "user_id": db.connection.user_id,
        "conversation_id": conversation["id"],
        "role": "assistant",
        "body": detective_module._grounded_statement(quote, "top_ranked"),
        "status": "completed",
        "request_key": f"historical_{index}",
        "created_at": NOW + timedelta(microseconds=index),
        "context_versions": {
            "coverage": "top_ranked",
            "claims": [
                {
                    "statement": detective_module._grounded_statement(
                        quote, "top_ranked"
                    ),
                    "evidence_ids": [str(evidence_id)],
                }
            ],
            "insufficient_evidence": False,
        },
    }
    evidence = {
        "id": evidence_id,
        "user_id": db.connection.user_id,
        "dream_id": source_dream["id"],
        "revision_id": source_dream["revision_id"],
        "start": source_dream["text"].index(quote),
        "end": source_dream["text"].index(quote) + len(quote),
        "exact_quote": quote,
        "content_hash": source_dream["content_hash"],
    }
    db.connection.messages.append(message)
    db.connection.evidence.append(evidence)
    db.connection.message_evidence.append(
        {
            "user_id": db.connection.user_id,
            "message_id": message_id,
            "evidence_id": evidence_id,
        }
    )
    return message, evidence


@pytest.mark.anyio
@pytest.mark.parametrize("invalid_reason", ["deleted", "revision_changed", "inaccessible"])
async def test_historical_answer_is_invalidated_when_its_source_is_invalid(
    invalid_reason,
):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Historical answer",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, "A moon appeared above the sea.")
    dream["current_revision_id"] = dream["revision_id"]
    db = FakeDatabase(user_id, [conversation], [dream])
    message, evidence = add_historical_answer(
        db, conversation, [dream], quote="A moon appeared above the sea."
    )
    if invalid_reason == "deleted":
        db.connection.dreams.clear()
    elif invalid_reason == "revision_changed":
        db.connection.dreams[0]["current_revision_id"] = uuid4()
    else:
        db.connection.message_evidence.clear()

    detail = await make_service(user_id, db).get_conversation(
        Identity(user_id), conversation["id"]
    )

    answer = next(item for item in detail.messages if item.id == message["id"])
    assert answer.status == "stale"
    assert answer.body == detective_module.STALE_ANSWER
    assert answer.citations == []
    assert answer.claims == []
    stored = next(row for row in db.connection.messages if row["id"] == message["id"])
    assert stored["status"] == "stale"
    assert stored["body"] == ""


@pytest.mark.anyio
async def test_canonical_historical_insufficient_answer_remains_unchanged():
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Canonical insufficient",
        "created_at": NOW,
        "updated_at": NOW,
    }
    db = FakeDatabase(user_id, [conversation])
    message = {
        "id": uuid4(),
        "user_id": user_id,
        "conversation_id": conversation["id"],
        "role": "assistant",
        "body": detective_module.INSUFFICIENT_ANSWER,
        "status": "completed",
        "request_key": "canonical_insufficient",
        "created_at": NOW,
        "context_versions": {
            "insufficient_evidence": True,
            "coverage_limited": False,
            "claims": [],
        },
    }
    db.connection.messages.append(message)

    detail = await make_service(user_id, db).get_conversation(
        Identity(user_id), conversation["id"]
    )

    answer = next(item for item in detail.messages if item.id == message["id"])
    assert answer.status == "completed"
    assert answer.body == detective_module.INSUFFICIENT_ANSWER
    assert answer.claims == []
    assert answer.citations == []


@pytest.mark.anyio
async def test_historical_insufficient_answer_with_unexpected_claim_is_stale():
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Unexpected insufficient claim",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, "The moon appeared above the sea.")
    dream["current_revision_id"] = dream["revision_id"]
    db = FakeDatabase(user_id, [conversation], [dream])
    message, _ = add_historical_answer(
        db, conversation, [dream], quote=dream["text"]
    )
    message["context_versions"]["insufficient_evidence"] = True

    detail = await make_service(user_id, db).get_conversation(
        Identity(user_id), conversation["id"]
    )

    answer = next(item for item in detail.messages if item.id == message["id"])
    assert answer.status == "stale"
    assert answer.body == detective_module.STALE_ANSWER
    assert answer.claims == []
    assert answer.citations == []


@pytest.mark.anyio
async def test_historical_insufficient_answer_with_citations_is_stale():
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Unexpected insufficient citation",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, "The moon appeared above the sea.")
    dream["current_revision_id"] = dream["revision_id"]
    db = FakeDatabase(user_id, [conversation], [dream])
    message, _ = add_historical_answer(
        db, conversation, [dream], quote=dream["text"]
    )
    message["body"] = detective_module.INSUFFICIENT_ANSWER
    message["context_versions"]["insufficient_evidence"] = True
    message["context_versions"]["claims"] = []

    detail = await make_service(user_id, db).get_conversation(
        Identity(user_id), conversation["id"]
    )

    answer = next(item for item in detail.messages if item.id == message["id"])
    assert answer.status == "stale"
    assert answer.body == detective_module.STALE_ANSWER
    assert answer.claims == []
    assert answer.citations == []


@pytest.mark.anyio
async def test_historical_insufficient_factual_text_with_deleted_source_is_stale():
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Deleted source with insufficient flag",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, "A silver moon rose above the hills.")
    dream["current_revision_id"] = dream["revision_id"]
    db = FakeDatabase(user_id, [conversation], [dream])
    message, _ = add_historical_answer(
        db, conversation, [dream], quote=dream["text"]
    )
    message["context_versions"]["insufficient_evidence"] = True
    db.connection.dreams.clear()

    detail = await make_service(user_id, db).get_conversation(
        Identity(user_id), conversation["id"]
    )

    answer = next(item for item in detail.messages if item.id == message["id"])
    assert answer.status == "stale"
    assert answer.body == detective_module.STALE_ANSWER
    assert answer.claims == []
    assert answer.citations == []


@pytest.mark.anyio
async def test_historical_unqualified_archive_claim_is_invalidated():
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Historical unsupported claim",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, "This was the sole time I saw a wolf.")
    dream["current_revision_id"] = dream["revision_id"]
    db = FakeDatabase(user_id, [conversation], [dream])
    message, _ = add_historical_answer(
        db, conversation, [dream], quote=dream["text"]
    )
    message["body"] = dream["text"]
    message["context_versions"]["claims"][0]["statement"] = dream["text"]

    detail = await make_service(user_id, db).get_conversation(
        Identity(user_id), conversation["id"]
    )

    answer = next(item for item in detail.messages if item.id == message["id"])
    assert answer.status == "stale"
    assert answer.body == detective_module.STALE_ANSWER
    assert answer.claims == []
    assert answer.citations == []


@pytest.mark.anyio
async def test_historical_earliest_answer_is_stale_if_a_earlier_exact_match_exists():
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Historical earliest assertion",
        "created_at": NOW,
        "updated_at": NOW,
    }
    later = source(user_id, "A wolf crossed the river.")
    later["dream_date"] = date(2025, 1, 1)
    earlier = source(user_id, "I watched a wolf near a river.")
    earlier["dream_date"] = date(2020, 1, 1)
    later["current_revision_id"] = later["revision_id"]
    earlier["current_revision_id"] = earlier["revision_id"]
    db = FakeDatabase(user_id, [conversation], [later, earlier])
    message, _ = add_historical_answer(db, conversation, [later], quote=later["text"])
    grounded = detective_module._grounded_statement(later["text"], "earliest_match")
    message["body"] = grounded
    message["context_versions"]["coverage"] = "earliest_match"
    message["context_versions"]["coverage_terms"] = ["river", "wolf"]
    message["context_versions"]["claims"][0]["statement"] = grounded

    detail = await make_service(user_id, db).get_conversation(
        Identity(user_id), conversation["id"]
    )

    answer = next(item for item in detail.messages if item.id == message["id"])
    assert answer.status == "stale"
    assert answer.body == detective_module.STALE_ANSWER
    assert answer.claims == []
    assert answer.citations == []


@pytest.mark.anyio
async def test_answer_with_one_valid_and_one_invalid_citation_is_stale():
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Multiple sources",
        "created_at": NOW,
        "updated_at": NOW,
    }
    first = source(user_id, "The moon appeared in a blue sky.")
    first["current_revision_id"] = first["revision_id"]
    second = source(user_id, "A boat crossed the calm sea.")
    second["current_revision_id"] = second["revision_id"]
    db = FakeDatabase(user_id, [conversation], [first, second])
    msg, first_evidence = add_historical_answer(
        db, conversation, [first], quote="The moon appeared in a blue sky."
    )
    second_evidence = {
        **first_evidence,
        "id": uuid4(),
        "dream_id": second["id"],
        "revision_id": second["revision_id"],
        "start": 0,
        "end": len(second["text"]),
        "exact_quote": second["text"],
        "content_hash": second["content_hash"],
    }
    db.connection.evidence.append(second_evidence)
    db.connection.message_evidence.append(
        {
            "user_id": user_id,
            "message_id": msg["id"],
            "evidence_id": second_evidence["id"],
        }
    )
    msg["context_versions"]["claims"][0]["evidence_ids"].append(
        str(second_evidence["id"])
    )
    db.connection.dreams[1]["current_revision_id"] = uuid4()

    detail = await make_service(user_id, db).get_conversation(
        Identity(user_id), conversation["id"]
    )
    answer = next(item for item in detail.messages if item.id == msg["id"])

    assert answer.status == "stale"
    assert answer.body == detective_module.STALE_ANSWER
    assert answer.citations == []


@pytest.mark.anyio
async def test_historical_stale_answer_does_not_hide_or_corrupt_valid_answer():
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "History",
        "created_at": NOW,
        "updated_at": NOW,
    }
    valid_dream = source(user_id, "The moon appeared.")
    valid_dream["current_revision_id"] = valid_dream["revision_id"]
    stale_dream = source(user_id, "A boat crossed the sea.")
    stale_dream["current_revision_id"] = uuid4()
    db = FakeDatabase(user_id, [conversation], [valid_dream, stale_dream])
    stale_message, _ = add_historical_answer(
        db, conversation, [stale_dream], quote="A boat crossed the sea.", index=0
    )
    valid_message, _ = add_historical_answer(
        db, conversation, [valid_dream], quote="The moon appeared.", index=1
    )

    detail = await make_service(user_id, db).get_conversation(
        Identity(user_id), conversation["id"]
    )

    by_id = {message.id: message for message in detail.messages}
    assert by_id[stale_message["id"]].status == "stale"
    assert by_id[stale_message["id"]].body == detective_module.STALE_ANSWER
    assert by_id[valid_message["id"]].status == "completed"
    assert by_id[valid_message["id"]].body == detective_module._grounded_statement(
        "The moon appeared.", "top_ranked"
    )
    assert len(by_id[valid_message["id"]].citations) == 1


@pytest.mark.anyio
async def test_provider_failure_is_safe_and_marks_turn_failed(monkeypatch):
    user_id = uuid4()
    conversation = {
        "id": uuid4(),
        "user_id": user_id,
        "title": "Provider error",
        "created_at": NOW,
        "updated_at": NOW,
    }
    dream = source(user_id, "I saw a green forest.")
    db = FakeDatabase(user_id, [conversation], [dream])
    service = make_service(user_id, db, FailedProvider())
    install_admission(monkeypatch)

    with pytest.raises(APIError) as error:
        await service.ask(
            Identity(user_id), conversation["id"], "Tell me about the green forest", "provider_1"
        )

    assert error.value.status == 503
    assert error.value.message == detective_module.FAILED_ANSWER
    assert [row["status"] for row in db.connection.messages] == ["failed", "failed"]


def test_stale_answers_do_not_expose_claims_or_evidence():
    citation_row = DreamCitation(
        evidence_id=uuid4(),
        dream_id=uuid4(),
        revision_id=uuid4(),
        title="Old source",
        dream_date=date(2020, 1, 1),
        start=0,
        end=4,
        quote="moon",
        content_hash="a" * 64,
    )
    message = DetectiveService._serialize_message(
        {
            "id": uuid4(),
            "role": "assistant",
            "body": "",
            "status": "stale",
            "created_at": NOW,
            "context_versions": {
                "claims": [
                    {
                        "statement": "The moon appeared.",
                        "evidence_ids": [str(citation_row.evidence_id)],
                    }
                ]
            },
        },
        [citation_row],
    )

    assert message.status == "stale"
    assert message.body == detective_module.STALE_ANSWER
    assert message.claims == []
    assert message.citations == []


def test_conversation_http_contract_and_existing_routers():
    user_id = uuid4()

    class FakeAuthenticator:
        async def verify(self, _authorization):
            return Identity(user_id)

        async def close(self):
            return None

    app = create_app(
        Settings(
            _env_file=None,
            database_url=URL,
            embedding_dimensions=3,
            supabase_url="https://project.supabase.co",
            supabase_publishable_key="test-key",
        ),
        FakeDatabase(user_id),
        FakeAuthenticator(),
    )
    async def authenticated_identity():
        return Identity(user_id)

    app.dependency_overrides[current_identity] = authenticated_identity
    with TestClient(app) as client:
        created = client.post("/api/v1/conversations", json={"title": "HTTP test"})
        assert created.status_code == 201
        conversation_id = created.json()["id"]
        listed = client.get("/api/v1/conversations")
        assert listed.status_code == 200 and listed.json()["count"] == 1
        detail = client.get(f"/api/v1/conversations/{conversation_id}")
        assert detail.status_code == 200 and detail.json()["messages"] == []
        paths = set(client.get("/openapi.json").json()["paths"])
        assert "/api/v1/conversations/{conversation_id}/messages" in paths
        assert "/api/v1/messages/{message_id}" in paths
        assert "/api/v1/dreams" in paths
        assert any(path.startswith("/api/v1/connections/") for path in paths)


def test_queued_message_routes_return_202_and_surface_validation_and_database_errors(
    monkeypatch,
):
    user_id = uuid4()
    conversation_id, message_id, job_id = uuid4(), uuid4(), uuid4()
    response_message = ConversationMessage(
        id=message_id,
        role="assistant",
        body="",
        status="pending",
        created_at=NOW,
    )

    class RouteService:
        async def submit(self, identity, requested_conversation, question, key, **scope):
            assert identity.user_id == user_id
            assert key == "request-1"
            assert scope["source_ids"] == []
            if requested_conversation != conversation_id:
                raise APIError("not_found", "Conversation not found", 404)
            if question == "database failure":
                raise SQLAlchemyError("private database detail")
            return DetectiveSubmission(
                job_id=job_id, message_id=message_id, status="queued"
            )

        async def get_conversation_messages(
            self, identity, requested_conversation, *, limit, offset
        ):
            assert identity.user_id == user_id
            assert requested_conversation == conversation_id
            assert (limit, offset) == (2, 1)
            return {
                "conversation_id": conversation_id,
                "items": [response_message],
                "offset": offset,
                "limit": limit,
                "has_more": True,
            }

        async def get_message(self, identity, requested_message):
            assert identity.user_id == user_id
            if requested_message != message_id:
                raise APIError("not_found", "Message not found", 404)
            return response_message

        async def delete_conversation(self, identity, requested_conversation):
            assert identity.user_id == user_id
            if requested_conversation != conversation_id:
                raise APIError("not_found", "Conversation not found", 404)

    class FakeAuthenticator:
        async def verify(self, _authorization):
            return Identity(user_id)

        async def close(self):
            return None

    app = create_app(
        Settings(
            _env_file=None,
            database_url=URL,
            embedding_dimensions=3,
            supabase_url="https://project.supabase.co",
            supabase_publishable_key="test-key",
        ),
        FakeDatabase(user_id),
        FakeAuthenticator(),
    )
    async def authenticated_identity():
        return Identity(user_id)

    app.dependency_overrides[current_identity] = authenticated_identity
    monkeypatch.setattr("app.api.conversations.service", lambda _request: RouteService())
    with TestClient(app) as client:
        submitted = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "request-1"},
            json={"question": "What happened?"},
        )
        assert submitted.status_code == 202
        assert submitted.json()["job_id"] == str(job_id)
        page = client.get(
            f"/api/v1/conversations/{conversation_id}/messages?limit=2&offset=1"
        )
        assert page.status_code == 200 and page.json()["has_more"] is True
        assert client.get(f"/api/v1/messages/{message_id}").status_code == 200
        assert client.get(f"/api/v1/messages/{uuid4()}").status_code == 404
        assert (
            client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                headers={"Idempotency-Key": "request-1"},
                json={"question": ""},
            ).status_code
            == 422
        )
        missing_conversation = client.post(
            f"/api/v1/conversations/{uuid4()}/messages",
            headers={"Idempotency-Key": "request-1"},
            json={"question": "What happened?"},
        )
        assert missing_conversation.status_code == 404
        database_error = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": "request-1"},
            json={"question": "database failure"},
        )
        assert database_error.status_code == 503
        assert "private database detail" not in database_error.text
        assert client.delete(f"/api/v1/conversations/{conversation_id}").status_code == 204
