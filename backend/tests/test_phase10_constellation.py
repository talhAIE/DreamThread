"""Focused Phase 10 constellation service and HTTP tests."""

import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import current_identity
from app.auth.identity import Identity
from app.config import Settings
from app.errors import APIError
from app.main import create_app
from app.services.constellation import ConstellationService

NOW = datetime(2026, 10, 30, tzinfo=UTC)
URL = "postgresql+psycopg://dreamthread:test@localhost/dreamthread"


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def one_or_none(self):
        return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self, user_id, dreams=(), motifs=(), motif_evidence=(), associations=(),
                 connections=(), connection_evidence=(), fail_on=None, scripted_results=None):
        self.user_id = user_id
        self.dreams = list(dreams)
        self.motifs = list(motifs)
        self.motif_evidence = list(motif_evidence)
        self.associations = list(associations)
        self.connections = list(connections)
        self.connection_evidence = list(connection_evidence)
        self.fail_on = fail_on
        self.statements = []
        self.scripted_results = {
            token: list(results) for token, results in (scripted_results or {}).items()
        }

    async def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise SQLAlchemyError("database failure")
        for token, result_sets in self.scripted_results.items():
            if token in sql and result_sets:
                rows = result_sets.pop(0)
                if "user_id" in sql:
                    rows = [
                        row
                        for row in rows
                        if "user_id" not in row or row["user_id"] == self.user_id
                    ]
                return Result(rows)
        if "FROM app.accounts" in sql:
            return Result(
                [
                    {
                        "id": self.user_id,
                        "deletion_state": "active",
                        "archive_version": 7,
                        "data_epoch": 1,
                    }
                ]
            )
        if "FROM app.dream_motifs" in sql and "app.motifs" in sql:
            return Result(self.motifs)
        if "FROM app.motif_evidence" in sql:
            return Result(self.motif_evidence)
        if "FROM app.associations" in sql:
            return Result(self.associations)
        if "FROM app.connections" in sql:
            return Result(self.connections)
        if "FROM app.connection_evidence" in sql:
            return Result(self.connection_evidence)
        if "FROM app.dreams" in sql:
            return Result(
                row for row in self.dreams if row.get("user_id", self.user_id) == self.user_id
            )
        return Result()


class FakeDatabase:
    def __init__(self, user_id, **kwargs):
        self.connection = FakeConnection(user_id, **kwargs)
        self.tenant_users = []

    @asynccontextmanager
    async def tenant_transaction(self, user_id):
        self.tenant_users.append(user_id)
        yield self.connection

    async def close(self):
        return None


class FakeAuthenticator:
    async def verify(self, _authorization):
        raise AssertionError("The test overrides current_identity")

    async def close(self):
        return None


def graph_fixture(user_id):
    dream_id, revision_id, motif_id = uuid4(), uuid4(), uuid4()
    link_id, evidence_id, association_id = uuid4(), uuid4(), uuid4()
    dream_text = "The old house"
    content_hash = hashlib.sha256(dream_text.encode()).hexdigest()
    dream = {
        "id": dream_id,
        "user_id": user_id,
        "title": "The old house",
        "dream_date": date(2026, 1, 3),
        "current_revision_id": revision_id,
        "created_at": NOW,
        "text": dream_text,
        "content_hash": content_hash,
    }
    motif = {
        "link_id": link_id,
        "dream_id": dream_id,
        "revision_id": revision_id,
        "motif_id": motif_id,
        "origin": "extracted",
        "confirmation_state": "suggested",
        "type": "place",
        "canonical_label": "House",
        "normalized_key": "house",
    }
    return {
        "dream": dream,
        "motif": motif,
        "evidence_id": evidence_id,
        "association_id": association_id,
        "dream_id": dream_id,
        "revision_id": revision_id,
        "motif_id": motif_id,
        "link_id": link_id,
        "user_id": user_id,
    }


@pytest.mark.anyio
async def test_graph_returns_current_owned_motifs_evidence_and_associations():
    user_id = uuid4()
    data = graph_fixture(user_id)
    db = FakeDatabase(
        user_id,
        dreams=[data["dream"]],
        motifs=[data["motif"]],
        motif_evidence=[
            {
                "dream_motif_id": data["link_id"],
                "evidence_id": data["evidence_id"],
                "dream_id": data["dream_id"],
                "revision_id": data["revision_id"],
                "start": 0,
                "end": len(data["dream"]["text"]),
                "exact_quote": data["dream"]["text"],
                "content_hash": data["dream"]["content_hash"],
                "revision_hash": data["dream"]["content_hash"],
                "text": data["dream"]["text"],
            }
        ],
        associations=[
            {
                "id": data["association_id"],
                "motif_id": data["motif_id"],
                "meaning": "A place of safety",
                "source_dream_id": data["dream_id"],
                "version": 2,
            }
        ],
    )

    graph = await ConstellationService(db).get_graph(user_id)

    assert graph.archive_version == 7
    assert graph.truncated is False
    assert {node.kind for node in graph.nodes} == {"dream", "motif", "association"}
    mentions = next(edge for edge in graph.edges if edge.kind == "mentions")
    assert mentions.status == "observed"
    assert mentions.evidence_ids == [data["evidence_id"]]
    assert next(edge for edge in graph.edges if edge.kind == "personal_association")
    assert db.tenant_users == [user_id]
    assert any("app.dreams.user_id" in sql for sql in db.connection.statements)
    assert any("app.dream_motifs.user_id" in sql for sql in db.connection.statements)


@pytest.mark.anyio
async def test_graph_empty_history_is_a_valid_empty_graph():
    user_id = uuid4()
    graph = await ConstellationService(FakeDatabase(user_id)).get_graph(user_id)

    assert graph.nodes == []
    assert graph.edges == []
    assert graph.truncated is False
    assert graph.archive_version == 7


@pytest.mark.anyio
async def test_graph_includes_only_current_confirmed_connections_with_endpoint_evidence():
    user_id = uuid4()
    first, second = graph_fixture(user_id), graph_fixture(user_id)
    if str(first["dream_id"]) > str(second["dream_id"]):
        first, second = second, first
    connection_id = uuid4()
    valid_evidence_id, unrelated_evidence_id = uuid4(), uuid4()
    connection = {
        "id": connection_id,
        "dream_a_id": first["dream_id"],
        "dream_b_id": second["dream_id"],
        "status": "confirmed",
        "basis_versions": {
            "dream_a_revision_id": str(first["revision_id"]),
            "dream_b_revision_id": str(second["revision_id"]),
            "dream_a_source_hash": first["dream"]["content_hash"],
            "dream_b_source_hash": second["dream"]["content_hash"],
        },
    }
    db = FakeDatabase(
        user_id,
        dreams=[first["dream"], second["dream"]],
        connections=[connection],
        connection_evidence=[
            {
                "connection_id": connection_id,
                "evidence_id": valid_evidence_id,
                "dream_id": first["dream_id"],
                "revision_id": first["revision_id"],
                "start": 0,
                "end": len(first["dream"]["text"]),
                "exact_quote": first["dream"]["text"],
                "content_hash": first["dream"]["content_hash"],
                "revision_hash": first["dream"]["content_hash"],
                "text": first["dream"]["text"],
            },
            {
                "connection_id": connection_id,
                "evidence_id": unrelated_evidence_id,
                "dream_id": uuid4(),
            },
        ],
    )

    graph = await ConstellationService(db).get_graph(user_id)

    continuity = [edge for edge in graph.edges if edge.kind == "continuity"]
    assert len(continuity) == 1
    assert continuity[0].status == "confirmed"
    assert continuity[0].evidence_ids == [valid_evidence_id]


@pytest.mark.anyio
async def test_per_dream_constellation_hides_missing_and_foreign_dreams():
    user_id, owner_id = uuid4(), uuid4()
    dream_id = uuid4()
    foreign_dream = {
        "id": dream_id,
        "user_id": owner_id,
        "current_revision_id": uuid4(),
    }
    db = FakeDatabase(user_id, dreams=[foreign_dream])
    service = ConstellationService(db)

    with pytest.raises(APIError) as error:
        await service.get_constellation(user_id, dream_id)

    assert error.value.status == 404
    assert db.tenant_users == [user_id]


@pytest.mark.anyio
async def test_database_failure_is_not_converted_to_an_empty_graph():
    user_id = uuid4()
    db = FakeDatabase(user_id, fail_on="FROM app.dreams")

    with pytest.raises(SQLAlchemyError):
        await ConstellationService(db).get_graph(user_id)


def test_constellation_router_is_registered_and_returns_graph():
    user_id = uuid4()
    database = FakeDatabase(user_id)
    settings = Settings(
        _env_file=None,
        database_url=URL,
        embedding_dimensions=3,
        supabase_url="https://project.supabase.co",
        supabase_publishable_key="test-key",
    )
    app = create_app(settings, database, FakeAuthenticator())

    async def identity():
        return Identity(user_id)

    app.dependency_overrides[current_identity] = identity
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/constellation?from=2026-01-01&to=2026-12-31&types=place&limit=20"
        )
        missing_motif = client.get(f"/api/v1/motifs/{uuid4()}")
        invalid_motif_id = client.get("/api/v1/motifs/not-a-uuid")
        storylines = client.get("/api/v1/storylines")
        missing_storyline = client.get(f"/api/v1/storylines/{uuid4()}")
        missing_refresh = client.post(f"/api/v1/storylines/{uuid4()}/refresh")

    assert response.status_code == 200
    assert response.json() == {
        "nodes": [],
        "edges": [],
        "truncated": False,
        "archive_version": 7,
    }
    assert missing_motif.status_code == 404
    assert invalid_motif_id.status_code == 422
    assert storylines.status_code == 200 and storylines.json() == []
    assert missing_storyline.status_code == 404
    assert missing_refresh.status_code == 404
    assert database.tenant_users == [user_id] * 5


@pytest.mark.anyio
async def test_graph_rejects_reversed_date_range():
    user_id = uuid4()
    with pytest.raises(APIError) as error:
        await ConstellationService(FakeDatabase(user_id)).get_graph(
            user_id,
            from_date=date(2026, 2, 1),
            to_date=date(2026, 1, 1),
        )
    assert error.value.status == 422


@pytest.mark.anyio
async def test_motif_detail_returns_only_verified_current_evidence():
    user_id = uuid4()
    motif_id, dream_id, revision_id = uuid4(), uuid4(), uuid4()
    link_id, evidence_id = uuid4(), uuid4()
    text = "I saw a red bird."
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    db = FakeDatabase(
        user_id,
        scripted_results={
            "FROM app.motifs": [[
                {
                    "id": motif_id,
                    "user_id": user_id,
                    "type": "object",
                    "canonical_label": "Red bird",
                    "normalized_key": "red bird",
                }
            ]],
            "FROM app.dream_motifs JOIN app.dreams": [[
                {
                    "link_id": link_id,
                    "dream_id": dream_id,
                    "revision_id": revision_id,
                    "content_hash": content_hash,
                    "origin": "extracted",
                    "confirmation_state": "confirmed",
                    "title": "The bird",
                    "dream_date": date(2026, 1, 2),
                }
            ]],
            "FROM app.motif_evidence": [[
                {
                    "dream_motif_id": link_id,
                    "evidence_id": evidence_id,
                    "dream_id": dream_id,
                    "revision_id": revision_id,
                    "start": 0,
                    "end": len(text),
                    "exact_quote": text,
                    "content_hash": content_hash,
                    "text": text,
                    "revision_hash": content_hash,
                }
            ]],
            "FROM app.associations": [[]],
        },
    )

    detail = await ConstellationService(db).get_motif(user_id, motif_id)

    assert detail.id == motif_id
    assert detail.mentions[0].dream_id == dream_id
    assert detail.mentions[0].evidence[0].evidence_id == evidence_id
    assert detail.mentions[0].evidence[0].quote == text
    assert all(
        "user_id" in sql
        for sql in db.connection.statements
        if "SELECT" in sql and "app." in sql and "app.accounts" not in sql
    )


@pytest.mark.anyio
async def test_motif_and_storyline_details_hide_missing_or_foreign_records():
    user_id, owner_id = uuid4(), uuid4()
    motif_id, storyline_id = uuid4(), uuid4()
    db = FakeDatabase(
        user_id,
        scripted_results={
            "FROM app.motifs": [[
                {
                    "id": motif_id,
                    "user_id": owner_id,
                    "type": "object",
                    "canonical_label": "Private",
                    "normalized_key": "private",
                }
            ]],
        },
    )
    service = ConstellationService(db)

    with pytest.raises(APIError) as motif_error:
        await service.get_motif(user_id, motif_id)
    with pytest.raises(APIError) as storyline_error:
        await service.get_storyline(user_id, storyline_id)

    assert motif_error.value.status == storyline_error.value.status == 404
    assert all(
        "user_id" in sql
        for sql in db.connection.statements
        if "SELECT" in sql and "app." in sql and "app.accounts" not in sql
    )


@pytest.mark.anyio
async def test_storyline_list_detail_and_refresh_use_owner_scoped_ordered_data():
    user_id = uuid4()
    motif_id, storyline_id, first_dream_id, second_dream_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    first_revision_id, second_revision_id = uuid4(), uuid4()
    db = FakeDatabase(
        user_id,
        scripted_results={
            "FROM app.motifs JOIN app.dream_motifs": [[
                {"id": motif_id, "canonical_label": "Red bird"}
            ]],
            "FROM app.storylines": [
                [
                    {
                        "id": storyline_id,
                        "motif_id": motif_id,
                        "title": "Red bird",
                        "status": "active",
                        "source_signature": "signature",
                        "entry_count": 2,
                    }
                ],
                [
                    {
                        "id": storyline_id,
                        "motif_id": motif_id,
                        "title": "Red bird",
                        "status": "active",
                        "source_signature": "signature",
                        "canonical_label": "Red bird",
                    }
                ],
                [
                    {
                        "id": storyline_id,
                        "user_id": user_id,
                        "motif_id": motif_id,
                        "title": "Red bird",
                        "status": "active",
                        "source_signature": "signature",
                    }
                ],
            ],
            "FROM app.dreams JOIN app.dream_revisions": [[
                {
                    "dream_id": first_dream_id,
                    "revision_id": first_revision_id,
                    "title": "First",
                    "dream_date": date(2026, 1, 1),
                    "created_at": NOW,
                    "text": "A red bird.",
                },
                {
                    "dream_id": second_dream_id,
                    "revision_id": second_revision_id,
                    "title": "Second",
                    "dream_date": date(2026, 1, 2),
                    "created_at": NOW,
                    "text": "The bird returned.",
                },
            ]],
            "FROM app.evidence JOIN app.storyline_evidence": [[]],
            "FROM app.jobs": [[]],
        },
    )
    service = ConstellationService(db)

    listed = await service.list_storylines(user_id)
    detail = await service.get_storyline(user_id, storyline_id)
    accepted = await service.enqueue_storyline_refresh(user_id, storyline_id)

    assert listed[0].id == storyline_id
    assert [chapter.dream_id for chapter in detail.chapters] == [
        first_dream_id,
        second_dream_id,
    ]
    chapter_query = next(
        sql for sql in db.connection.statements if "FROM app.dreams JOIN app.dream_revisions" in sql
    )
    assert (
        "ORDER BY app.dreams.dream_date, app.dreams.created_at, app.dreams.id"
        in chapter_query
    )
    assert accepted.status == "queued"
    assert accepted.job_id


@pytest.mark.anyio
async def test_stale_storyline_hides_previously_cached_chapters():
    user_id, storyline_id, motif_id = uuid4(), uuid4(), uuid4()
    db = FakeDatabase(
        user_id,
        scripted_results={
            "FROM app.storylines": [
                [
                    {
                        "id": storyline_id,
                        "user_id": user_id,
                        "motif_id": motif_id,
                        "title": "House",
                        "status": "stale",
                        "source_signature": "old-source-signature",
                        "canonical_label": "House",
                    }
                ]
            ]
        },
    )

    result = await ConstellationService(db).get_storyline(user_id, storyline_id)

    assert result.status == "stale"
    assert result.entry_count == 0
    assert result.chapters == []
    assert not any(
        "FROM app.storyline_entries" in statement
        for statement in db.connection.statements
    )
    list_db = FakeDatabase(
        user_id,
        scripted_results={
            "FROM app.motifs JOIN app.dream_motifs": [
                [{"id": motif_id, "canonical_label": "House"}]
            ],
            "FROM app.storylines": [
                [
                    {
                        "id": storyline_id,
                        "motif_id": motif_id,
                        "title": "House",
                        "status": "stale",
                        "source_signature": "old-source-signature",
                        "entry_count": 2,
                    }
                ]
            ],
        },
    )
    listed = await ConstellationService(list_db).list_storylines(user_id)
    assert listed[0].entry_count == 0


@pytest.mark.anyio
async def test_motif_database_failure_is_not_reported_as_missing():
    user_id = uuid4()
    db = FakeDatabase(user_id, fail_on="FROM app.motifs")

    with pytest.raises(SQLAlchemyError):
        await ConstellationService(db).get_motif(user_id, uuid4())


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("operation", "failed_query"),
    [
        ("list_storylines", "FROM app.motifs JOIN app.dream_motifs"),
        ("get_storyline", "FROM app.storylines"),
        ("enqueue_storyline_refresh", "FROM app.storylines"),
    ],
)
async def test_storyline_database_failures_are_not_reported_as_empty_or_missing(
    operation, failed_query
):
    user_id = uuid4()
    db = FakeDatabase(user_id, fail_on=failed_query)
    service = ConstellationService(db)

    with pytest.raises(SQLAlchemyError):
        if operation == "list_storylines":
            await service.list_storylines(user_id)
        elif operation == "get_storyline":
            await service.get_storyline(user_id, uuid4())
        else:
            await service.enqueue_storyline_refresh(user_id, uuid4())
