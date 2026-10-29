"""Unit and contract tests for Phase 9: Dream Continuity Engine."""

import hashlib
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dependencies import current_identity
from app.auth.identity import Identity
from app.config import Settings
from app.errors import APIError
from app.main import create_app
from app.schemas.continuity import (
    ComparisonProposal,
    ConnectionEvidenceListResponse,
    ConnectionEvidenceRead,
    ConnectionListResponse,
    ConnectionPatchRequest,
    ConnectionRead,
    ContinuityOutput,
)
from app.services.continuity import (
    ContinuityEngine,
    ContinuityService,
    ContinuityTools,
    canonical_pair,
    canonical_relation_key,
)
from app.services.evidence import OutputInvalid, verify_citation


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
                    for k, v in params.items():
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
                if isinstance(v, UUID) and (k == "id_1" or "id" in k) and k != "user_id" and k != "user_id_1":
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
    tools_a = ContinuityTools(mock_conn, user_a, settings)

    # Searching dreams as User A returns User A's dream
    search_results = await tools_a.search_dreams("house")
    assert len(search_results) == 1
    assert search_results[0]["id"] == str(d_a)

    # Attempting to fetch User B's dream ID as User A returns nothing
    b_dreams = await tools_a.get_dreams([str(d_b)])
    assert len(b_dreams) == 0


@pytest.mark.anyio
async def test_reanalysis_preserves_confirmed_and_rejected_decisions():
    """Requirement: Confirm/reject works and rejection survives duplicate delivery and reanalysis."""
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

    text1 = "I was running along a sandy beach."
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

    # Proposal where candidate quote is fabricated
    proposals = [
        ComparisonProposal(
            target_dream_id=d1,
            candidate_dream_id=d2,
            relation_key="theme:beach",
            similarity_observation="Both dreams are near the ocean.",
            quotes_target=["sandy beach"],
            quotes_candidate=["blue ocean wave"],  # DOES NOT EXIST in text2
        )
    ]

    res = await engine._verify_and_persist(mock_conn, user_id, d1, source1, proposals, None)
    # Entailment failure must result in zero saved connections
    assert len(res) == 0


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
