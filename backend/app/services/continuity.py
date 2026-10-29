"""Dream Continuity Engine (Phase 9).

Tool-bounded connection discovery, citation verification, canonical pair ordering,
and decision-preserving persistence.
"""

import hashlib
import json
import re
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.models import tables
from app.errors import APIError
from app.providers.nebius import Nebius
from app.schemas.analysis import Citation
from app.schemas.continuity import (
    ComparisonProposal,
    ConnectionEvidenceListResponse,
    ConnectionEvidenceRead,
    ConnectionListResponse,
    ConnectionPatchRequest,
    ConnectionRead,
    ContinuityOutput,
)
from app.services.evidence import OutputInvalid, verify_citation
from app.services.journal import current_source, owned_dream
from app.services.retrieval import RetrievalService, lexical_rank, snippet
from app.workers.types import WorkFailure

PROMPTS = Path(__file__).parents[1] / "prompts"

CONN = tables["app.connections"]
DEC = tables["app.connection_decisions"]
CONN_EVID = tables["app.connection_evidence"]
EVID = tables["app.evidence"]
D = tables["app.dreams"]
R = tables["app.dream_revisions"]
M = tables["app.motifs"]
A = tables["app.associations"]


CONTINUITY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_dreams",
            "description": "Search the authenticated user's dream archive for relevant entries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query keywords or motifs"},
                    "limit": {"type": "integer", "description": "Max results to return (1-5)", "default": 3},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dreams",
            "description": "Read the full text and details of specific dream entries by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of dream UUIDs to inspect",
                    }
                },
                "required": ["ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_personal_associations",
            "description": "Read confirmed personal associations and meanings for recurring motifs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "motif_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional motif UUIDs",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_confirmed_connections",
            "description": "Read previously confirmed connections for a dream.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dream_id": {"type": "string", "description": "Dream UUID"},
                },
                "required": ["dream_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_comparison",
            "description": "Submit a structured connection proposal between two dreams with supporting quotes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_dream_id": {"type": "string"},
                    "candidate_dream_id": {"type": "string"},
                    "relation_key": {
                        "type": "string",
                        "description": "Stable identifier e.g. motif:house or recurring:flying",
                    },
                    "similarity_observation": {
                        "type": "string",
                        "description": "Observed factual similarities supported by quotes",
                    },
                    "difference_observation": {
                        "type": "string",
                        "description": "Observed differences or changes",
                    },
                    "tentative_reflection": {
                        "type": "string",
                        "description": "Tentative interpretation or reflective prompt",
                    },
                    "uncertainty_note": {
                        "type": "string",
                        "description": "Degree of confidence or ambiguity",
                    },
                    "quotes_target": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exact quotes from target dream",
                    },
                    "quotes_candidate": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exact quotes from candidate dream",
                    },
                },
                "required": [
                    "target_dream_id",
                    "candidate_dream_id",
                    "relation_key",
                    "similarity_observation",
                    "quotes_target",
                    "quotes_candidate",
                ],
            },
        },
    },
]


def canonical_relation_key(raw_key: str) -> str:
    cleaned = re.sub(r"[^\w:\-]", "_", raw_key.strip().lower())
    if not cleaned:
        cleaned = "continuity:similarity"
    return cleaned[:200]


def canonical_pair(id_a: UUID, id_b: UUID):
    if id_a == id_b:
        raise ValueError("self_link_rejected")
    return (id_a, id_b) if str(id_a) < str(id_b) else (id_b, id_a)


class ContinuityTools:
    """Bounded, read-only tools strictly scoped to the authenticated tenant."""

    def __init__(self, connection, user_id: UUID, settings):
        self.connection = connection
        self.user_id = user_id
        self.settings = settings

    async def search_dreams(self, query: str, filters: dict | None = None, limit: int = 3) -> list[dict]:
        limit = max(1, min(limit, 5))
        c = self.connection
        rows = (
            await c.execute(
                select(
                    D.c.id,
                    D.c.title,
                    D.c.dream_date,
                    R.c.id.label("revision_id"),
                    R.c.content_hash,
                    R.c.text,
                )
                .join(
                    R,
                    and_(
                        R.c.user_id == D.c.user_id,
                        R.c.dream_id == D.c.id,
                        R.c.id == D.c.current_revision_id,
                    ),
                )
                .where(
                    D.c.user_id == self.user_id,
                    D.c.lifecycle == "saved",
                    D.c.deleted_at.is_(None),
                )
                .order_by(D.c.dream_date.desc(), D.c.id.desc())
            )
        ).mappings().all()

        if not rows:
            return []

        scores = lexical_rank(query, rows)
        ranked = sorted(rows, key=lambda r: scores.get(r["id"], 0.0), reverse=True)
        return [
            {
                "id": str(r["id"]),
                "title": r["title"],
                "dream_date": str(r["dream_date"]),
                "snippet": snippet(r["text"], query),
            }
            for r in ranked[:limit]
        ]

    async def get_dreams(self, ids: list[str]) -> list[dict]:
        clean_ids = []
        for raw in ids[:10]:
            try:
                clean_ids.append(UUID(str(raw)))
            except ValueError:
                continue
        if not clean_ids:
            return []

        c = self.connection
        rows = (
            await c.execute(
                select(
                    D.c.id,
                    D.c.title,
                    D.c.dream_date,
                    R.c.id.label("revision_id"),
                    R.c.content_hash,
                    R.c.text,
                )
                .join(
                    R,
                    and_(
                        R.c.user_id == D.c.user_id,
                        R.c.dream_id == D.c.id,
                        R.c.id == D.c.current_revision_id,
                    ),
                )
                .where(
                    D.c.user_id == self.user_id,
                    D.c.id.in_(clean_ids),
                    D.c.lifecycle == "saved",
                    D.c.deleted_at.is_(None),
                )
            )
        ).mappings().all()

        return [
            {
                "id": str(r["id"]),
                "title": r["title"],
                "dream_date": str(r["dream_date"]),
                "revision_id": str(r["revision_id"]),
                "content_hash": r["content_hash"],
                "text": r["text"][:3000],
            }
            for r in rows
        ]

    async def get_personal_associations(self, motif_ids: list[str] | None = None) -> list[dict]:
        c = self.connection
        stmt = (
            select(M.c.canonical_label, A.c.meaning, A.c.id.label("association_id"))
            .join(M, and_(M.c.id == A.c.motif_id, M.c.user_id == A.c.user_id))
            .where(
                A.c.user_id == self.user_id,
                A.c.enabled == True,
                A.c.status == "confirmed",
                A.c.deleted_at.is_(None),
            )
        )
        if motif_ids:
            clean_motif_ids = []
            for raw in motif_ids[:10]:
                try:
                    clean_motif_ids.append(UUID(str(raw)))
                except ValueError:
                    pass
            if clean_motif_ids:
                stmt = stmt.where(M.c.id.in_(clean_motif_ids))

        rows = (await c.execute(stmt)).mappings().all()
        return [
            {
                "association_id": str(r["association_id"]),
                "motif": r["canonical_label"],
                "meaning": r["meaning"],
            }
            for r in rows
        ]

    async def get_confirmed_connections(self, dream_id: str) -> list[dict]:
        try:
            d_id = UUID(str(dream_id))
        except ValueError:
            return []
        c = self.connection
        rows = (
            await c.execute(
                select(
                    CONN.c.id,
                    CONN.c.dream_a_id,
                    CONN.c.dream_b_id,
                    CONN.c.relation_key,
                    CONN.c.observation,
                ).where(
                    CONN.c.user_id == self.user_id,
                    or_(CONN.c.dream_a_id == d_id, CONN.c.dream_b_id == d_id),
                    CONN.c.status == "confirmed",
                )
            )
        ).mappings().all()

        return [
            {
                "connection_id": str(r["id"]),
                "connected_dream_id": str(r["dream_b_id"] if r["dream_a_id"] == d_id else r["dream_a_id"]),
                "relation_key": r["relation_key"],
                "observation": r["observation"],
            }
            for r in rows
        ]

    async def execute_tool(self, name: str, args: dict) -> dict:
        if name == "search_dreams":
            results = await self.search_dreams(args.get("query", ""), limit=args.get("limit", 3))
            return {"results": results}
        elif name == "get_dreams":
            results = await self.get_dreams(args.get("ids", []))
            return {"dreams": results}
        elif name == "get_personal_associations":
            results = await self.get_personal_associations(args.get("motif_ids"))
            return {"associations": results}
        elif name == "get_confirmed_connections":
            results = await self.get_confirmed_connections(args.get("dream_id", ""))
            return {"confirmed_connections": results}
        elif name == "propose_comparison":
            return {"status": "proposal_accepted_for_validation", "proposal": args}
        return {"error": f"unknown_tool_{name}"}


class ContinuityEngine:
    """Orchestrates candidate comparison, bounded tool calling, verification, and persistence."""

    def __init__(self, db, settings, provider=None):
        self.db = db
        self.settings = settings
        self.provider = provider or Nebius(settings)

    async def compare(
        self,
        c,
        user_id: UUID,
        target_dream_id: UUID,
        candidate_limit: int | None = None,
        record_call=None,
    ) -> list[dict]:
        target_dream = await owned_dream(c, user_id, target_dream_id)
        target_source = await current_source(c, target_dream)

        retrieval = RetrievalService(self.db, self.settings)
        from app.auth.identity import Identity

        identity = Identity(user_id, False)
        candidate_res = await retrieval.candidates(identity, target_dream_id)

        if not candidate_res.candidates:
            return []

        limit = candidate_limit or self.settings.agent_max_candidate_dreams
        top_candidates = candidate_res.candidates[:limit]

        prompt_path = PROMPTS / "continuity_v1.txt"
        system_prompt = (
            prompt_path.read_text(encoding="utf-8")
            if prompt_path.is_file()
            else "You are the DreamThread Continuity Engine."
        )

        candidate_summaries = [
            {
                "id": str(cand.dream_id),
                "title": cand.title,
                "dream_date": str(cand.dream_date),
                "snippet": cand.snippet,
                "reasons": cand.reasons,
            }
            for cand in top_candidates
        ]

        messages = [
            {
                "role": "system",
                "content": system_prompt
                + "\nJSON Schema:\n"
                + json.dumps(ContinuityOutput.model_json_schema()),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "target_dream": {
                            "id": str(target_dream_id),
                            "title": target_dream["title"],
                            "dream_date": str(target_dream["dream_date"]),
                            "text": target_source["text"],
                        },
                        "historical_candidates": candidate_summaries,
                    },
                    ensure_ascii=False,
                ),
            },
        ]

        tools = ContinuityTools(c, user_id, self.settings)
        proposals: list[ComparisonProposal] = []
        rounds = 0
        tool_invocations = 0
        max_rounds = self.settings.agent_max_rounds
        max_tools = self.settings.agent_max_tool_calls

        async def dummy_record(**kwargs):
            return uuid4()

        recorder = record_call or dummy_record
        model_run_id = None

        while rounds < max_rounds:
            rounds += 1
            completion = await self.provider.complete(
                messages,
                schema=ContinuityOutput.model_json_schema() if rounds == max_rounds else None,
                task="continuity",
                record=recorder,
                model=self.settings.nebius_reasoning_model,
                tools=CONTINUITY_TOOLS if rounds < max_rounds else None,
            )
            model_run_id = completion.run_id

            if completion.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": completion.text or None,
                        "tool_calls": [
                            {
                                "id": getattr(tc, "id", str(uuid4())),
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for tc in completion.tool_calls
                        ],
                    }
                )

                for tc in completion.tool_calls:
                    if tool_invocations >= max_tools:
                        break
                    tool_invocations += 1
                    try:
                        args = json.loads(tc.function.arguments)
                    except Exception:
                        args = {}

                    if tc.function.name == "propose_comparison":
                        try:
                            proposal_obj = ComparisonProposal.model_validate(args)
                            proposals.append(proposal_obj)
                        except ValidationError:
                            pass

                    tool_result = await tools.execute_tool(tc.function.name, args)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": getattr(tc, "id", str(uuid4())),
                            "name": tc.function.name,
                            "content": json.dumps(tool_result),
                        }
                    )
                continue

            if completion.text:
                try:
                    output = ContinuityOutput.model_validate_json(completion.text)
                    proposals.extend(output.comparisons)
                except (ValidationError, ValueError):
                    pass
                break

        verified_connections = await self._verify_and_persist(
            c, user_id, target_dream_id, target_source, proposals, model_run_id
        )
        return verified_connections

    async def _verify_and_persist(
        self,
        c,
        user_id: UUID,
        target_dream_id: UUID,
        target_source: dict,
        proposals: list[ComparisonProposal],
        model_run_id: UUID | None,
    ) -> list[dict]:
        saved_connections = []

        for p in proposals:
            if p.target_dream_id == p.candidate_dream_id:
                continue

            try:
                candidate_dream = await owned_dream(c, user_id, p.candidate_dream_id)
                candidate_source = await current_source(c, candidate_dream)
            except Exception:
                continue

            try:
                dream_a_id, dream_b_id = canonical_pair(p.target_dream_id, p.candidate_dream_id)
            except ValueError:
                continue

            if dream_a_id == target_dream_id:
                src_a, src_b = target_source, candidate_source
                quotes_a, quotes_b = p.quotes_target, p.quotes_candidate
            else:
                src_a, src_b = candidate_source, target_source
                quotes_a, quotes_b = p.quotes_candidate, p.quotes_target

            hash_a = src_a.get("content_hash") or src_a.get("source_hash") or ""
            hash_b = src_b.get("content_hash") or src_b.get("source_hash") or ""
            rev_a = src_a.get("revision_id") or src_a.get("id")
            rev_b = src_b.get("revision_id") or src_b.get("id")

            verified_citations_a = []
            for q in quotes_a:
                q = q.strip()
                if not q:
                    continue
                start_idx = src_a["text"].find(q)
                if start_idx < 0:
                    continue
                end_idx = start_idx + len(q)
                citation = Citation(
                    dream_id=dream_a_id,
                    revision_id=rev_a,
                    source_hash=hash_a,
                    start=start_idx,
                    end=end_idx,
                    quote=q,
                )
                try:
                    v = verify_citation(
                        citation,
                        dream_id=dream_a_id,
                        revision_id=rev_a,
                        source_hash=hash_a,
                        text=src_a["text"],
                    )
                    verified_citations_a.append(v)
                except OutputInvalid:
                    pass

            verified_citations_b = []
            for q in quotes_b:
                q = q.strip()
                if not q:
                    continue
                start_idx = src_b["text"].find(q)
                if start_idx < 0:
                    continue
                end_idx = start_idx + len(q)
                citation = Citation(
                    dream_id=dream_b_id,
                    revision_id=rev_b,
                    source_hash=hash_b,
                    start=start_idx,
                    end=end_idx,
                    quote=q,
                )
                try:
                    v = verify_citation(
                        citation,
                        dream_id=dream_b_id,
                        revision_id=rev_b,
                        source_hash=hash_b,
                        text=src_b["text"],
                    )
                    verified_citations_b.append(v)
                except OutputInvalid:
                    pass

            # Claim entailment screening: must have at least one verified quote from each dream
            if not verified_citations_a or not verified_citations_b:
                continue

            rel_key = canonical_relation_key(p.relation_key)

            existing_decision = (
                await c.execute(
                    select(DEC.c.decision, DEC.c.version).where(
                        DEC.c.user_id == user_id,
                        DEC.c.dream_a_id == dream_a_id,
                        DEC.c.dream_b_id == dream_b_id,
                        DEC.c.relation_key == rel_key,
                    )
                )
            ).mappings().first()

            if existing_decision:
                status = existing_decision["decision"]
            else:
                status = "suggested"

            basis_versions = {
                "dream_a_revision_id": str(rev_a),
                "dream_b_revision_id": str(rev_b),
                "dream_a_source_hash": hash_a,
                "dream_b_source_hash": hash_b,
            }

            conn_row = (
                await c.execute(
                    select(CONN.c.id, CONN.c.status).where(
                        CONN.c.user_id == user_id,
                        CONN.c.dream_a_id == dream_a_id,
                        CONN.c.dream_b_id == dream_b_id,
                        CONN.c.relation_key == rel_key,
                    )
                )
            ).mappings().first()

            if conn_row:
                connection_id = conn_row["id"]
                await c.execute(
                    update(CONN)
                    .where(CONN.c.id == connection_id)
                    .values(
                        status=status,
                        observation=p.similarity_observation,
                        basis_versions=basis_versions,
                        model_run_id=model_run_id,
                    )
                )
            else:
                connection_id = uuid4()
                await c.execute(
                    insert(CONN).values(
                        id=connection_id,
                        user_id=user_id,
                        dream_a_id=dream_a_id,
                        dream_b_id=dream_b_id,
                        relation_key=rel_key,
                        status=status,
                        observation=p.similarity_observation,
                        basis_versions=basis_versions,
                        model_run_id=model_run_id,
                    )
                )

            all_citations = verified_citations_a + verified_citations_b
            for cit in all_citations:
                evid_id = uuid4()
                await c.execute(
                    insert(EVID)
                    .values(
                        id=evid_id,
                        user_id=user_id,
                        dream_id=cit.dream_id,
                        revision_id=cit.revision_id,
                        start=cit.start,
                        end=cit.end,
                        exact_quote=cit.quote,
                        content_hash=cit.source_hash,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            EVID.c.user_id,
                            EVID.c.dream_id,
                            EVID.c.revision_id,
                            EVID.c.start,
                            EVID.c.end,
                            EVID.c.content_hash,
                        ]
                    )
                )

                actual_evid_id = await c.scalar(
                    select(EVID.c.id).where(
                        EVID.c.user_id == user_id,
                        EVID.c.dream_id == cit.dream_id,
                        EVID.c.revision_id == cit.revision_id,
                        EVID.c.start == cit.start,
                        EVID.c.end == cit.end,
                        EVID.c.content_hash == cit.source_hash,
                    )
                )

                if actual_evid_id:
                    await c.execute(
                        insert(CONN_EVID)
                        .values(
                            id=uuid4(),
                            user_id=user_id,
                            connection_id=connection_id,
                            evidence_id=actual_evid_id,
                        )
                        .on_conflict_do_nothing(
                            index_elements=[
                                CONN_EVID.c.user_id,
                                CONN_EVID.c.connection_id,
                                CONN_EVID.c.evidence_id,
                            ]
                        )
                    )

            saved_connections.append(
                {
                    "id": connection_id,
                    "dream_a_id": dream_a_id,
                    "dream_b_id": dream_b_id,
                    "relation_key": rel_key,
                    "status": status,
                    "observation": p.similarity_observation,
                }
            )

        return saved_connections


class ContinuityService:
    """Service consumed by FastAPI endpoints and background worker."""

    def __init__(self, db, settings, provider=None):
        self.db = db
        self.settings = settings
        self.engine = ContinuityEngine(db, settings, provider)

    async def get_connections_for_dream(
        self, user_id: UUID, dream_id: UUID, include_rejected: bool = False
    ) -> ConnectionListResponse:
        async with self.db.tenant_transaction(user_id) as c:
            await owned_dream(c, user_id, dream_id)

            stmt = (
                select(
                    CONN.c.id,
                    CONN.c.dream_a_id,
                    CONN.c.dream_b_id,
                    CONN.c.relation_key,
                    CONN.c.status,
                    CONN.c.observation,
                    CONN.c.basis_versions,
                    CONN.c.model_run_id,
                    CONN.c.created_at,
                    CONN.c.updated_at,
                    DEC.c.decision,
                    DEC.c.version.label("decision_version"),
                )
                .outerjoin(
                    DEC,
                    and_(
                        DEC.c.user_id == CONN.c.user_id,
                        DEC.c.dream_a_id == CONN.c.dream_a_id,
                        DEC.c.dream_b_id == CONN.c.dream_b_id,
                        DEC.c.relation_key == CONN.c.relation_key,
                    ),
                )
                .where(
                    CONN.c.user_id == user_id,
                    or_(CONN.c.dream_a_id == dream_id, CONN.c.dream_b_id == dream_id),
                )
                .order_by(CONN.c.created_at.desc())
            )
            if not include_rejected:
                stmt = stmt.where(CONN.c.status != "rejected")

            rows = (await c.execute(stmt)).mappings().all()

            results = []
            for r in rows:
                other_id = r["dream_b_id"] if r["dream_a_id"] == dream_id else r["dream_a_id"]
                other_dream = (
                    await c.execute(
                        select(D.c.title, D.c.deleted_at).where(
                            D.c.user_id == user_id, D.c.id == other_id
                        )
                    )
                ).mappings().first()

                if not other_dream or other_dream["deleted_at"] is not None:
                    continue

                evidence_count = await c.scalar(
                    select(CONN_EVID.c.id)
                    .where(CONN_EVID.c.connection_id == r["id"])
                )

                results.append(
                    ConnectionRead(
                        id=r["id"],
                        dream_a_id=r["dream_a_id"],
                        dream_b_id=r["dream_b_id"],
                        connected_dream_id=other_id,
                        connected_dream_title=other_dream["title"],
                        relation_key=r["relation_key"],
                        status=r["status"],
                        observation=r["observation"],
                        basis_versions=r["basis_versions"] or {},
                        model_run_id=r["model_run_id"],
                        decision=r["decision"],
                        decision_version=r["decision_version"],
                        created_at=r["created_at"],
                        updated_at=r["updated_at"],
                        evidence_count=1 if evidence_count else 0,
                    )
                )

            return ConnectionListResponse(
                dream_id=dream_id,
                connections=results,
                count=len(results),
            )

    async def patch_connection(
        self, user_id: UUID, connection_id: UUID, patch: ConnectionPatchRequest
    ) -> ConnectionRead:
        async with self.db.tenant_transaction(user_id) as c:
            conn = (
                await c.execute(
                    select(
                        CONN.c.id,
                        CONN.c.dream_a_id,
                        CONN.c.dream_b_id,
                        CONN.c.relation_key,
                        CONN.c.status,
                        CONN.c.observation,
                        CONN.c.basis_versions,
                        CONN.c.model_run_id,
                        CONN.c.created_at,
                        CONN.c.updated_at,
                    ).where(CONN.c.id == connection_id, CONN.c.user_id == user_id)
                )
            ).mappings().first()

            if not conn:
                raise APIError("not_found", "Connection not found", 404)

            existing_dec = (
                await c.execute(
                    select(DEC.c.id, DEC.c.decision, DEC.c.version).where(
                        DEC.c.user_id == user_id,
                        DEC.c.dream_a_id == conn["dream_a_id"],
                        DEC.c.dream_b_id == conn["dream_b_id"],
                        DEC.c.relation_key == conn["relation_key"],
                    )
                )
            ).mappings().first()

            current_ver = existing_dec["version"] if existing_dec else 1
            if current_ver != patch.expected_version:
                raise APIError("version_conflict", "Decision version mismatch", 409)

            new_ver = current_ver + 1
            if existing_dec:
                await c.execute(
                    update(DEC)
                    .where(DEC.c.id == existing_dec["id"])
                    .values(decision=patch.decision, version=new_ver)
                )
            else:
                await c.execute(
                    insert(DEC).values(
                        id=uuid4(),
                        user_id=user_id,
                        dream_a_id=conn["dream_a_id"],
                        dream_b_id=conn["dream_b_id"],
                        relation_key=conn["relation_key"],
                        decision=patch.decision,
                        version=new_ver,
                    )
                )

            await c.execute(
                update(CONN)
                .where(CONN.c.id == connection_id)
                .values(status=patch.decision)
            )

            updated_conn = (
                await c.execute(
                    select(CONN).where(CONN.c.id == connection_id)
                )
            ).mappings().first()

            return ConnectionRead(
                id=updated_conn["id"],
                dream_a_id=updated_conn["dream_a_id"],
                dream_b_id=updated_conn["dream_b_id"],
                connected_dream_id=updated_conn["dream_b_id"],
                relation_key=updated_conn["relation_key"],
                status=updated_conn["status"],
                observation=updated_conn["observation"],
                basis_versions=updated_conn["basis_versions"] or {},
                model_run_id=updated_conn["model_run_id"],
                decision=patch.decision,
                decision_version=new_ver,
                created_at=updated_conn["created_at"],
                updated_at=updated_conn["updated_at"],
            )

    async def get_connection_evidence(
        self, user_id: UUID, connection_id: UUID
    ) -> ConnectionEvidenceListResponse:
        async with self.db.tenant_transaction(user_id) as c:
            conn = (
                await c.execute(
                    select(CONN.c.id).where(CONN.c.id == connection_id, CONN.c.user_id == user_id)
                )
            ).mappings().first()

            if not conn:
                raise APIError("not_found", "Connection not found", 404)

            rows = (
                await c.execute(
                    select(
                        EVID.c.id,
                        EVID.c.dream_id,
                        EVID.c.revision_id,
                        EVID.c.start,
                        EVID.c.end,
                        EVID.c.exact_quote,
                        EVID.c.content_hash,
                    )
                    .join(CONN_EVID, and_(CONN_EVID.c.evidence_id == EVID.c.id, CONN_EVID.c.user_id == user_id))
                    .where(CONN_EVID.c.connection_id == connection_id)
                    .order_by(EVID.c.dream_id, EVID.c.start)
                )
            ).mappings().all()

            evidence_items = [
                ConnectionEvidenceRead(
                    id=r["id"],
                    dream_id=r["dream_id"],
                    revision_id=r["revision_id"],
                    start=r["start"],
                    end=r["end"],
                    exact_quote=r["exact_quote"],
                    content_hash=r["content_hash"],
                )
                for r in rows
            ]

            return ConnectionEvidenceListResponse(
                connection_id=connection_id,
                evidence=evidence_items,
            )

    async def analyze_connections(self, user_id: UUID, dream_id: UUID) -> ConnectionListResponse:
        async with self.db.tenant_transaction(user_id) as c:
            await self.engine.compare(c, user_id, dream_id)
        return await self.get_connections_for_dream(user_id, dream_id)
