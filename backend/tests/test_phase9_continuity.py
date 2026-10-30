"""Unit and contract tests for Phase 9: Dream Continuity Engine."""

import hashlib
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.errors import APIError
from app.schemas.continuity import (
    ComparisonProposal,
    ConnectionPatchRequest,
    ConnectionRead,
)
from app.services import continuity as continuity_module
from app.services.continuity import (
    ContinuityEngine,
    ContinuityService,
    ContinuityTools,
    canonical_pair,
    canonical_relation_key,
)
from app.services.evidence import OutputInvalid, verify_citation
from app.services.retrieval import MAX_CANDIDATE_POOL, ranked_current_sources
from app.workers.types import SourceChanged


def test_canonical_pair_ordering_and_self_link_rejection():
    u1 = UUID("11111111-1111-1111-1111-111111111111")
    u2 = UUID("22222222-2222-2222-2222-222222222222")

    a, b = canonical_pair(u1, u2)
    assert a == u1 and b == u2
    assert str(a) < str(b)

    # Inverted order should still produce canonical pair a < b
    a2, b2 = canonical_pair(u2, u1)
    assert a2 == u1 and b2 == u2
    assert str(a2) < str(b2)

    # Self-links must be rejected
    with pytest.raises(ValueError, match="self_link_rejected"):
        canonical_pair(u1, u1)


def test_canonical_relation_key_normalization():
    assert canonical_relation_key("motif:house") == "motif:house"
    assert canonical_relation_key("  Recurring: Flying High!  ") == "recurring:_flying_high_"
    assert canonical_relation_key("") == "continuity:similarity"
    assert canonical_relation_key("   ") == "continuity:similarity"
    assert len(canonical_relation_key("a" * 300)) == 200


def test_continuity_schema_validations():
    u1, u2 = uuid4(), uuid4()

    # Valid proposal
    valid_proposal = ComparisonProposal(
        target_dream_id=u1,
        candidate_dream_id=u2,
        relation_key="motif:house",
        similarity_observation="Both dreams involve entering a house with a locked wooden door.",
        difference_observation="The first house was wooden; the second was made of stone.",
        tentative_reflection="Could the recurring house represent security?",
        uncertainty_note="The dreamer expressed doubt about the door material.",
        quotes_target=["I entered a wooden house with a locked door."],
        quotes_candidate=["The stone house also had a heavy locked door."],
    )
    assert valid_proposal.relation_key == "motif:house"

    # Reject empty quotes
    with pytest.raises(ValidationError):
        ComparisonProposal(
            target_dream_id=u1,
            candidate_dream_id=u2,
            relation_key="motif:house",
            similarity_observation="Similar houses",
            quotes_target=[],
            quotes_candidate=["door"],
        )

    with pytest.raises(ValidationError):
        ComparisonProposal(
            target_dream_id=u1,
            candidate_dream_id=u2,
            relation_key="motif:house",
            similarity_observation="Similar houses",
            quotes_target=["house"],
            quotes_candidate=[],
        )

    # Reject patch with expected_version < 1
    with pytest.raises(ValidationError):
        ConnectionPatchRequest(decision="confirmed", expected_version=0)

    # Reject invalid decision value
    with pytest.raises(ValidationError):
        ConnectionPatchRequest(decision="maybe", expected_version=1)  # type: ignore

    # Valid patch
    patch = ConnectionPatchRequest(decision="confirmed", expected_version=1)
    assert patch.decision == "confirmed" and patch.expected_version == 1


def test_citation_verification_exact_quotes():
    dream_id = uuid4()
    revision_id = uuid4()
    text = "Last night I was flying over a dark blue ocean. The wind felt freezing cold."
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    from app.schemas.analysis import Citation

    # Exact matching quote
    quote = "flying over a dark blue ocean"
    start = text.index(quote)
    end = start + len(quote)

    citation = Citation(
        dream_id=dream_id,
        revision_id=revision_id,
        source_hash=source_hash,
        start=start,
        end=end,
        quote=quote,
    )

    verified = verify_citation(
        citation,
        dream_id=dream_id,
        revision_id=revision_id,
        source_hash=source_hash,
        text=text,
    )
    assert verified.start == start
    assert verified.end == end
    assert verified.quote == quote

    # Fabricated / hallucinated quote not present in source text
    fake_citation = Citation(
        dream_id=dream_id,
        revision_id=revision_id,
        source_hash=source_hash,
        start=0,
        end=10,
        quote="I was swimming in a warm lake",
    )
    with pytest.raises(OutputInvalid):
        verify_citation(
            fake_citation,
            dream_id=dream_id,
            revision_id=revision_id,
            source_hash=source_hash,
            text=text,
        )

    # Source hash mismatch
    with pytest.raises(OutputInvalid):
        verify_citation(
            citation,
            dream_id=dream_id,
            revision_id=revision_id,
            source_hash="wrong_hash",
            text=text,
        )


class MockDbCursor:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def one_or_none(self):
        return self._rows[0] if self._rows else None

    def one(self):
        if not self._rows:
            raise Exception("no_row")
        return self._rows[0]


class BoundedQueryConnection:
    def __init__(self, rows):
        self.rows = rows
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return MockDbCursor(self.rows)


@pytest.mark.anyio
async def test_continuity_search_bounds_pool_and_reports_truncation():
    user_id, target_id = uuid4(), uuid4()
    rows = [
        {
            "id": uuid4(),
            "title": f"House {index}",
            "dream_date": f"2026-01-{(index % 28) + 1:02d}",
            "revision_id": uuid4(),
            "content_hash": "current-hash",
            "text": "The old house stood by the river.",
            "lexical_score": 1.0,
        }
        for index in range(MAX_CANDIDATE_POOL + 1)
    ]
    connection = BoundedQueryConnection(rows)

    candidates, truncated = await ranked_current_sources(
        connection,
        user_id,
        "house",
        limit=MAX_CANDIDATE_POOL,
        exclude_dream_id=target_id,
        matching_only=True,
        recency_tiebreak=True,
    )

    statement = str(connection.statement.compile())
    parameters = connection.statement.compile().params
    assert len(candidates) == MAX_CANDIDATE_POOL
    assert truncated
    assert any(value == user_id for value in parameters.values())
    assert any(value == target_id for value in parameters.values())
    assert "app.dream_revisions.id = app.dreams.current_revision_id" in statement
    assert "app.dream_revisions.user_id = " in statement
    assert "app.dreams.lifecycle = " in statement
    assert "to_tsvector" in statement and "plainto_tsquery" in statement
    assert "ORDER BY" in statement
    assert "app.dreams.dream_date DESC, app.dreams.id DESC" in statement
    assert 257 in parameters.values()


@pytest.mark.anyio
async def test_continuity_search_orders_equal_scores_deterministically():
    connection = BoundedQueryConnection([])

    await ranked_current_sources(connection, uuid4(), "house", limit=3)

    statement = str(connection.statement.compile())
    assert "ORDER BY" in statement
    assert statement.endswith("app.dreams.id ASC\n LIMIT :param_5")


@pytest.mark.anyio
@pytest.mark.parametrize("change_during_call", [False, True])
async def test_continuity_model_calls_run_without_open_database_transactions(
    monkeypatch, change_during_call
):
    user_id, target_id, revision_id = uuid4(), uuid4(), uuid4()
    dream = {
        "id": target_id,
        "user_id": user_id,
        "title": "Target",
        "dream_date": "2026-01-01",
        "current_revision_id": revision_id,
    }
    source = {
        "id": revision_id,
        "revision_id": revision_id,
        "content_hash": "source-hash",
        "text": "A source dream.",
    }

    class Transaction:
        def __init__(self, database):
            self.database = database

        async def __aenter__(self):
            self.database.active_transactions += 1
            return object()

        async def __aexit__(self, *_args):
            self.database.active_transactions -= 1

    class Database:
        active_transactions = 0

        def tenant_transaction(self, _user_id):
            return Transaction(self)

    database = Database()
    archive = {"version": 1}

    async def active_account(_connection, _user_id):
        return {"archive_version": archive["version"]}

    async def owned_dream(_connection, _user_id, _dream_id):
        return dream

    async def current_source(_connection, _dream):
        return source

    class Retrieval:
        def __init__(self, *_args):
            pass

        async def candidates(self, *_args):
            return SimpleNamespace(
                revision_id=revision_id,
                archive_version=1,
                candidate_limit=MAX_CANDIDATE_POOL,
                candidate_search_truncated=False,
                degraded_reasons=[],
                candidates=[
                    SimpleNamespace(
                        dream_id=uuid4(),
                        title="Candidate",
                        dream_date="2025-01-01",
                        snippet="A candidate.",
                        reasons=[],
                    )
                ]
            )

    class Provider:
        calls = 0

        async def complete(self, *_args, **_kwargs):
            assert database.active_transactions == 0
            self.calls += 1
            if change_during_call:
                archive["version"] = 2
            if self.calls == 1:
                tool_call = SimpleNamespace(
                    id="tool-1",
                    function=SimpleNamespace(name="unknown_tool", arguments="{}"),
                )
                return SimpleNamespace(text=None, run_id=uuid4(), tool_calls=[tool_call])
            return SimpleNamespace(
                text='{"comparisons":[]}', run_id=uuid4(), tool_calls=[]
            )

    monkeypatch.setattr(continuity_module, "active_account", active_account)
    monkeypatch.setattr(continuity_module, "owned_dream", owned_dream)
    monkeypatch.setattr(continuity_module, "current_source", current_source)
    monkeypatch.setattr(continuity_module, "RetrievalService", Retrieval)
    settings = SimpleNamespace(
        agent_max_candidate_dreams=3,
        agent_max_rounds=2,
        agent_max_tool_calls=1,
        nebius_reasoning_model=None,
    )
    provider = Provider()

    if change_during_call:
        with pytest.raises(SourceChanged):
            await ContinuityEngine(database, settings, provider).compare(user_id, target_id)
    else:
        result = await ContinuityEngine(database, settings, provider).compare(user_id, target_id)
        assert result == []

    assert provider.calls == 2
    assert database.active_transactions == 0


class MockConnection:
    def __init__(self, user_id):
        self.user_id = user_id
        self.dreams = {}
        self.revisions = {}
        self.associations = []
        self.connections = {}
        self.decisions = {}
        self.evidence = {}
        self.connection_evidence = []
        self.calls = []

    async def execute(self, stmt):
        self.calls.append(stmt)
        sql = str(stmt).casefold()

        if "from app.accounts" in sql:
            return MockDbCursor(
                [{"id": self.user_id, "deletion_state": "active", "archive_version": 1}]
            )
        if "from app.dreams" in sql:
            params = {}
            if hasattr(stmt, "compile"):
                try:
                    params = stmt.compile().params
                except Exception:
                    pass

            filter_ids = None
            for k, v in params.items():
                if k == "id_1" or k.startswith("id_") or k == "param_1":
                    if isinstance(v, (UUID, list)):
                        filter_ids = {v} if isinstance(v, UUID) else set(v)
                elif "id" in k and k != "user_id_1" and k != "user_id":
                    if isinstance(v, UUID):
                        filter_ids = {v}

            matched = []
            for d_id, d in self.dreams.items():
                if d.get("user_id") == self.user_id and d.get("deleted_at") is None:
                    if filter_ids and d_id not in filter_ids:
                        continue
                    rev = self.revisions.get(d["current_revision_id"], {})
                    matched.append(
                        {
                            "id": d_id,
                            "user_id": d.get("user_id", self.user_id),
                            "title": d.get("title", ""),
                            "dream_date": d.get("dream_date"),
                            "current_revision_id": d.get("current_revision_id"),
                            "revision_id": d.get("current_revision_id"),
                            "content_hash": rev.get("content_hash", ""),
                            "text": rev.get("text", ""),
                            "lifecycle": d.get("lifecycle", "saved"),
                            "deleted_at": d.get("deleted_at"),
                            "version": d.get("version", 1),
                        }
                    )
            return MockDbCursor(matched)

        elif "from app.dream_revisions" in sql:
            matched = []
            rev_id_filter = None
            if hasattr(stmt, "compile"):
                try:
                    params = stmt.compile().params
                    for _k, v in params.items():
                        if isinstance(v, UUID) and v in self.revisions:
                            rev_id_filter = v
                except Exception:
                    pass
            for r_id, rev in self.revisions.items():
                if rev_id_filter and r_id != rev_id_filter:
                    continue
                matched.append(
                    {
                        "id": r_id,
                        "user_id": rev.get("user_id", self.user_id),
                        "dream_id": rev.get("dream_id"),
                        "text": rev.get("text", ""),
                        "content_hash": rev.get("content_hash", ""),
                    }
                )
            return MockDbCursor(matched)

        elif "from app.connection_decisions" in sql:
            matched = []
            for dec in self.decisions.values():
                if dec.get("user_id") == self.user_id:
                    matched.append(dec)
            return MockDbCursor(matched)

        elif "from app.connections" in sql:
            params = {}
            if hasattr(stmt, "compile"):
                try:
                    params = stmt.compile().params
                except Exception:
                    pass
            filter_conn_id = None
            for k, v in params.items():
                if (
                    isinstance(v, UUID)
                    and (k == "id_1" or "id" in k)
                    and k != "user_id"
                    and k != "user_id_1"
                ):
                    filter_conn_id = v
            matched = []
            for c in self.connections.values():
                if c.get("user_id") == self.user_id:
                    if filter_conn_id and c.get("id") != filter_conn_id:
                        continue
                    matched.append(c)
            return MockDbCursor(matched)

        elif "from app.associations" in sql:
            matched = [
                a for a in self.associations
                if a.get("user_id") == self.user_id and a.get("enabled")
            ]
            return MockDbCursor(matched)

        return MockDbCursor([])

    async def scalar(self, stmt):
        cursor = await self.execute(stmt)
        first = cursor.first()
        if first:
            if isinstance(first, dict):
                return next(iter(first.values()))
            return first
        return None


@pytest.mark.anyio
async def test_continuity_tools_scope_strictly_to_tenant():
    user_a = uuid4()
    user_b = uuid4()

    mock_conn = MockConnection(user_a)
    d_a = uuid4()
    rev_a = uuid4()
    text_a = "Dream about a wooden house on a hill."
    mock_conn.dreams[d_a] = {
        "id": d_a,
        "user_id": user_a,
        "title": "House on Hill",
        "dream_date": "2026-10-01",
        "current_revision_id": rev_a,
        "lifecycle": "saved",
        "deleted_at": None,
    }
    mock_conn.revisions[rev_a] = {
        "id": rev_a,
        "dream_id": d_a,
        "text": text_a,
        "content_hash": hashlib.sha256(text_a.encode()).hexdigest(),
    }

    # Dream belonging to user B
    d_b = uuid4()
    rev_b = uuid4()
    mock_conn.dreams[d_b] = {
        "id": d_b,
        "user_id": user_b,
        "title": "User B Secret Dream",
        "dream_date": "2026-10-01",
        "current_revision_id": rev_b,
        "lifecycle": "saved",
        "deleted_at": None,
    }
    mock_conn.revisions[rev_b] = {
        "id": rev_b,
        "dream_id": d_b,
        "text": "Secret user B content",
        "content_hash": hashlib.sha256(b"Secret").hexdigest(),
    }

    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://u:p@localhost/test",
    )
    search_conn = BoundedQueryConnection(
        [
            {
                "id": d_a,
                "title": "House on Hill",
                "dream_date": "2026-10-01",
                "revision_id": rev_a,
                "content_hash": mock_conn.revisions[rev_a]["content_hash"],
                "text": text_a,
                "lexical_score": 1.0,
            }
        ]
    )
    tools_a = ContinuityTools(search_conn, user_a, settings)

    search_results = await tools_a.search_dreams("house")
    assert len(search_results["results"]) == 1
    assert search_results["results"][0]["id"] == str(d_a)
    assert not search_results["candidate_limit_reached"]
    assert any(value == user_a for value in search_conn.statement.compile().params.values())

    # Attempting to fetch User B's dream ID as User A returns nothing
    tools_a.connection = mock_conn
    b_dreams = await tools_a.get_dreams([str(d_b)])
    assert len(b_dreams) == 0


@pytest.mark.anyio
async def test_reanalysis_preserves_confirmed_and_rejected_decisions():
    """Requirement: Confirmation and rejection persist across duplicate delivery and reanalysis."""
    user_id = uuid4()
    d1 = UUID("11111111-1111-1111-1111-111111111111")
    d2 = UUID("22222222-2222-2222-2222-222222222222")
    rel_key = "motif:house"

    text1 = "I walked through a blue door into an old house."
    text2 = "I saw the same blue door in a tall stone tower."

    rev1_id = uuid4()
    rev2_id = uuid4()

    source1 = {
        "dream_id": d1,
        "revision_id": rev1_id,
        "content_hash": hashlib.sha256(text1.encode()).hexdigest(),
        "text": text1,
        "user_id": user_id,
    }
    source2 = {
        "dream_id": d2,
        "revision_id": rev2_id,
        "content_hash": hashlib.sha256(text2.encode()).hexdigest(),
        "text": text2,
        "user_id": user_id,
    }

    mock_conn = MockConnection(user_id)
    mock_conn.dreams[d1] = {
        "id": d1,
        "user_id": user_id,
        "title": "Dream 1",
        "dream_date": "2026-10-01",
        "current_revision_id": rev1_id,
        "lifecycle": "saved",
        "deleted_at": None,
    }
    mock_conn.dreams[d2] = {
        "id": d2,
        "user_id": user_id,
        "title": "Dream 2",
        "dream_date": "2026-10-02",
        "current_revision_id": rev2_id,
        "lifecycle": "saved",
        "deleted_at": None,
    }
    mock_conn.revisions[rev1_id] = source1
    mock_conn.revisions[rev2_id] = source2

    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://u:p@localhost/test",
    )
    engine = ContinuityEngine(None, settings)

    # 1. Fresh comparison proposal without previous decision -> suggested
    proposals = [
        ComparisonProposal(
            target_dream_id=d1,
            candidate_dream_id=d2,
            relation_key=rel_key,
            similarity_observation="Both dreams feature a distinctive blue door.",
            quotes_target=["blue door into an old house"],
            quotes_candidate=["same blue door in a tall stone tower"],
        )
    ]

    res = await engine._verify_and_persist(mock_conn, user_id, d1, source1, proposals, None)
    assert len(res) == 1
    assert res[0]["status"] == "suggested"

    # 2. User confirms connection -> record in decisions
    mock_conn.decisions[(d1, d2, rel_key)] = {
        "user_id": user_id,
        "dream_a_id": d1,
        "dream_b_id": d2,
        "relation_key": rel_key,
        "decision": "confirmed",
        "version": 2,
    }

    # 3. Reanalysis runs again -> must preserve 'confirmed' status
    res2 = await engine._verify_and_persist(mock_conn, user_id, d1, source1, proposals, None)
    assert len(res2) == 1
    assert res2[0]["status"] == "confirmed"

    # 4. User rejects connection -> record in decisions
    mock_conn.decisions[(d1, d2, rel_key)]["decision"] = "rejected"
    mock_conn.decisions[(d1, d2, rel_key)]["version"] = 3

    # 5. Reanalysis runs again -> must preserve 'rejected' status
    res3 = await engine._verify_and_persist(mock_conn, user_id, d1, source1, proposals, None)
    assert len(res3) == 1
    assert res3[0]["status"] == "rejected"


@pytest.mark.anyio
async def test_entailment_screening_rejects_unverified_quotes():
    """Quotes that do not exist in stored text must fail entailment screening."""
    user_id = uuid4()
    d1 = UUID("11111111-1111-1111-1111-111111111111")
    d2 = UUID("22222222-2222-2222-2222-222222222222")

    text1 = "I was running along a sandy beach, then returned to the sandy beach."
    text2 = "A red car was parked beside a tall tree."

    source1 = {
        "dream_id": d1,
        "revision_id": uuid4(),
        "content_hash": hashlib.sha256(text1.encode()).hexdigest(),
        "text": text1,
    }
    source2 = {
        "dream_id": d2,
        "revision_id": uuid4(),
        "content_hash": hashlib.sha256(text2.encode()).hexdigest(),
        "text": text2,
    }

    mock_conn = MockConnection(user_id)
    mock_conn.dreams[d1] = {
        "id": d1,
        "user_id": user_id,
        "current_revision_id": source1["revision_id"],
        "lifecycle": "saved",
        "deleted_at": None,
    }
    mock_conn.dreams[d2] = {
        "id": d2,
        "user_id": user_id,
        "current_revision_id": source2["revision_id"],
        "lifecycle": "saved",
        "deleted_at": None,
    }
    mock_conn.revisions[source1["revision_id"]] = source1
    mock_conn.revisions[source2["revision_id"]] = source2

    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://u:p@localhost/test",
    )
    engine = ContinuityEngine(None, settings)

    # One proposal has a fabricated quote; the other has an ambiguous repeated quote.
    proposals = [
        ComparisonProposal(
            target_dream_id=d1,
            candidate_dream_id=d2,
            relation_key="theme:beach",
            similarity_observation="Both dreams are near the ocean.",
            quotes_target=["sandy beach"],
            quotes_candidate=["blue ocean wave"],  # DOES NOT EXIST in text2
        ),
        ComparisonProposal(
            target_dream_id=d1,
            candidate_dream_id=d2,
            relation_key="theme:ambiguous",
            similarity_observation="Both dreams mention a beach.",
            quotes_target=["sandy beach"],
            quotes_candidate=["red car"],
        ),
    ]

    res = await engine._verify_and_persist(mock_conn, user_id, d1, source1, proposals, None)
    # Entailment failure must result in zero saved connections
    assert len(res) == 0


@pytest.mark.anyio
async def test_continuity_rejects_quotes_from_a_stale_candidate_revision():
    user_id, target_id, candidate_id = uuid4(), uuid4(), uuid4()
    target_revision, old_revision, current_revision = uuid4(), uuid4(), uuid4()
    target_text = "A silver key rested beside the open gate."
    old_text = "I crossed a silver gate at dawn."
    current_text = "A blue boat drifted beneath the bridge."
    target_source = {
        "id": target_revision,
        "revision_id": target_revision,
        "content_hash": hashlib.sha256(target_text.encode()).hexdigest(),
        "text": target_text,
    }
    connection = MockConnection(user_id)
    connection.dreams[target_id] = {
        "id": target_id,
        "user_id": user_id,
        "current_revision_id": target_revision,
        "lifecycle": "saved",
        "deleted_at": None,
    }
    connection.dreams[candidate_id] = {
        "id": candidate_id,
        "user_id": user_id,
        "current_revision_id": current_revision,
        "lifecycle": "saved",
        "deleted_at": None,
    }
    connection.revisions[target_revision] = {
        **target_source,
        "dream_id": target_id,
        "user_id": user_id,
    }
    connection.revisions[old_revision] = {
        "id": old_revision,
        "revision_id": old_revision,
        "dream_id": candidate_id,
        "user_id": user_id,
        "content_hash": hashlib.sha256(old_text.encode()).hexdigest(),
        "text": old_text,
    }
    connection.revisions[current_revision] = {
        "id": current_revision,
        "revision_id": current_revision,
        "dream_id": candidate_id,
        "user_id": user_id,
        "content_hash": hashlib.sha256(current_text.encode()).hexdigest(),
        "text": current_text,
    }

    engine = ContinuityEngine(
        None,
        Settings(
            _env_file=None,
            database_url="postgresql+psycopg://user:pass@localhost/test?sslmode=require",
        ),
    )
    proposals = [
        ComparisonProposal(
            target_dream_id=target_id,
            candidate_dream_id=candidate_id,
            relation_key="motif:gate",
            similarity_observation="Both dreams mention a silver gate.",
            quotes_target=["silver key"],
            quotes_candidate=["silver gate"],
        )
    ]

    result = await engine._verify_and_persist(
        connection, user_id, target_id, target_source, proposals, None
    )

    assert result == []
    assert connection.connections == {}


def test_connection_read_and_patch_schema_models():
    u1, u2 = uuid4(), uuid4()
    c_id = uuid4()
    from datetime import UTC, datetime

    now = datetime.now(UTC)

    conn_read = ConnectionRead(
        id=c_id,
        dream_a_id=u1,
        dream_b_id=u2,
        connected_dream_id=u2,
        connected_dream_title="Dream 2",
        relation_key="motif:flying",
        status="suggested",
        observation="Both feature soaring over mountains.",
        basis_versions={"dream_a_revision_id": str(uuid4())},
        created_at=now,
        updated_at=now,
    )
    assert conn_read.status == "suggested"
    assert conn_read.connected_dream_id == u2

    # Patch model
    patch = ConnectionPatchRequest(decision="rejected", expected_version=1)
    assert patch.decision == "rejected"


class FakeDbForService:
    def __init__(self, connection):
        self.connection = connection

    def tenant_transaction(self, user_id):
        class TxContext:
            def __init__(self, conn):
                self.conn = conn

            async def __aenter__(self):
                return self.conn

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        return TxContext(self.connection)


@pytest.mark.anyio
async def test_patch_connection_version_conflict():
    user_id = uuid4()
    conn_id = uuid4()
    d1 = uuid4()
    d2 = uuid4()
    from datetime import UTC, datetime

    now = datetime.now(UTC)

    mock_conn = MockConnection(user_id)
    mock_conn.connections[conn_id] = {
        "id": conn_id,
        "user_id": user_id,
        "dream_a_id": d1,
        "dream_b_id": d2,
        "relation_key": "motif:lake",
        "status": "suggested",
        "observation": "Both feature a dark lake.",
        "basis_versions": {},
        "model_run_id": None,
        "created_at": now,
        "updated_at": now,
    }
    # Existing decision at version 2
    mock_conn.decisions[(d1, d2, "motif:lake")] = {
        "id": uuid4(),
        "user_id": user_id,
        "dream_a_id": d1,
        "dream_b_id": d2,
        "relation_key": "motif:lake",
        "decision": "confirmed",
        "version": 2,
    }

    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://u:p@localhost/test",
    )
    service = ContinuityService(FakeDbForService(mock_conn), settings)

    # Calling patch with stale version 1 must raise APIError 409 version_conflict
    with pytest.raises(APIError) as err:
        await service.patch_connection(
            user_id, conn_id, ConnectionPatchRequest(decision="rejected", expected_version=1)
        )
    assert err.value.status == 409
    assert err.value.code == "version_conflict"

    # Calling with non-existent connection ID raises 404
    with pytest.raises(APIError) as err_404:
        await service.patch_connection(
            user_id, uuid4(), ConnectionPatchRequest(decision="confirmed", expected_version=1)
        )
    assert err_404.value.status == 404


class EvidenceResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def one_or_none(self):
        return self.rows[0] if self.rows else None

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class EvidenceConnection:
    def __init__(self, user_id, *, mode="valid"):
        self.user_id = user_id
        self.mode = mode
        self.dream_a, self.dream_b = uuid4(), uuid4()
        self.revision_a, self.revision_b = uuid4(), uuid4()
        self.evidence_id = uuid4()
        self.text = "A silver river crossed the quiet forest."
        self.content_hash = hashlib.sha256(self.text.encode()).hexdigest()
        self.statements = []

    async def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if "FROM app.accounts" in sql:
            return EvidenceResult([{"id": self.user_id, "deletion_state": "active"}])
        if "FROM app.connections" in sql:
            if self.mode == "cross_tenant":
                return EvidenceResult([])
            status = "stale" if self.mode == "stale_connection" else "confirmed"
            return EvidenceResult(
                [
                    {
                        "id": uuid4(),
                        "dream_a_id": self.dream_a,
                        "dream_b_id": self.dream_b,
                        "basis_versions": {
                            "dream_a_revision_id": str(self.revision_a),
                            "dream_b_revision_id": str(self.revision_b),
                            "dream_a_source_hash": (
                                "0" * 64
                                if self.mode == "basis_hash_mismatch"
                                else self.content_hash
                            ),
                            "dream_b_source_hash": self.content_hash,
                        },
                        "status": status,
                    }
                ]
            )
        if "FROM app.dreams" in sql and "app.evidence" not in sql:
            if self.mode == "deleted":
                return EvidenceResult(
                    [
                        {
                            "id": self.dream_a,
                            "current_revision_id": self.revision_a,
                            "content_hash": self.content_hash,
                        }
                    ]
                )
            revision_b = uuid4() if self.mode == "changed_revision" else self.revision_b
            return EvidenceResult(
                [
                    {
                        "id": self.dream_a,
                        "current_revision_id": self.revision_a,
                        "content_hash": (
                            hashlib.sha256(b"changed source").hexdigest()
                            if self.mode == "modified_source"
                            else self.content_hash
                        ),
                    },
                    {
                        "id": self.dream_b,
                        "current_revision_id": revision_b,
                        "content_hash": self.content_hash,
                    },
                ]
            )
        if "FROM app.evidence" in sql and "app.connection_evidence" in sql:
            start = 100 if self.mode == "invalid_offsets" else self.text.index("silver")
            quote = "not in source" if self.mode == "quote_mismatch" else "silver river"
            content_hash = "0" * 64 if self.mode == "hash_mismatch" else self.content_hash
            revision_hash = (
                hashlib.sha256(b"changed source").hexdigest()
                if self.mode == "modified_source"
                else self.content_hash
            )
            source_text = "A changed source." if self.mode == "modified_source" else self.text
            return EvidenceResult(
                [
                    {
                        "id": self.evidence_id,
                        "dream_id": self.dream_a,
                        "revision_id": self.revision_a,
                        "start": start,
                        "end": start + len(quote),
                        "exact_quote": quote,
                        "content_hash": content_hash,
                        "text": source_text,
                        "revision_hash": revision_hash,
                    }
                ]
            )
        return EvidenceResult([])


class EvidenceDatabase:
    def __init__(self, connection):
        self.connection = connection

    def tenant_transaction(self, _user_id):
        class Transaction:
            async def __aenter__(inner_self):
                return self.connection

            async def __aexit__(inner_self, *_args):
                return None

        return Transaction()


@pytest.mark.anyio
async def test_connection_evidence_returns_only_current_verified_source():
    user_id = uuid4()
    connection = EvidenceConnection(user_id)
    service = ContinuityService(
        EvidenceDatabase(connection),
        Settings(_env_file=None, database_url="postgresql+psycopg://test@localhost/test"),
    )

    result = await service.get_connection_evidence(user_id, uuid4())

    assert len(result.evidence) == 1
    assert result.evidence[0].exact_quote == "silver river"
    assert all("user_id" in statement for statement in connection.statements[1:])


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mode",
    [
        "modified_source",
        "deleted",
        "changed_revision",
        "basis_hash_mismatch",
        "invalid_offsets",
        "quote_mismatch",
        "hash_mismatch",
        "stale_connection",
        "cross_tenant",
    ],
)
async def test_connection_evidence_rejects_stale_or_invalid_sources(mode):
    user_id = uuid4()
    connection = EvidenceConnection(user_id, mode=mode)
    service = ContinuityService(
        EvidenceDatabase(connection),
        Settings(_env_file=None, database_url="postgresql+psycopg://test@localhost/test"),
    )

    with pytest.raises(APIError) as error:
        await service.get_connection_evidence(user_id, uuid4())
    assert error.value.status in {404, 409}
