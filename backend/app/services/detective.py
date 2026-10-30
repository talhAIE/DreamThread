"""Grounded historical question answering over the authenticated user's dream archive."""

import json
import logging
import re
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import and_, delete, exists, func, insert, not_, or_, select, update

from app.db.models import tables
from app.errors import APIError
from app.providers.nebius import Nebius
from app.schemas.analysis import Citation
from app.schemas.detective import (
    ConversationDetail,
    ConversationList,
    ConversationMessage,
    ConversationMessages,
    ConversationRead,
    DetectiveOutput,
    DetectiveSubmission,
    DreamCitation,
    GroundedClaim,
)
from app.services.accounts import active_account
from app.services.admission import reserve_ai
from app.services.evidence import OutputInvalid, verify_citation
from app.services.retrieval import lexical_rank, terms
from app.workers.types import SourceChanged

PROMPTS = Path(__file__).parents[1] / "prompts"
MAX_CANDIDATES = 50
MAX_SOURCES = 5
MAX_EXCERPT_LENGTH = 1600
MAX_HISTORY = 6
SEARCH_META_TERMS = {
    "all",
    "appear",
    "appeared",
    "count",
    "date",
    "did",
    "earliest",
    "ever",
    "first",
    "how",
    "initial",
    "initially",
    "many",
    "mention",
    "mentioned",
    "time",
    "when",
    "what",
    "where",
    "which",
    "who",
    "why",
}
INSUFFICIENT_ANSWER = "I couldn't find enough information in your saved dreams to answer that."
REFLECTION_QUESTION = "What, if anything, does this detail mean to you?"
LIMITED_ANSWER = (
    "I found more matching entries than I can verify in this response, "
    "so I can't make a complete archive-wide claim."
)
FAILED_ANSWER = "I couldn't safely prepare an answer from your dream entries. Please try again."
STALE_ANSWER = "This answer is no longer available because its supporting dream evidence changed."
logger = logging.getLogger("dreamthread")
FIRST_QUERY = re.compile(r"\b(first|earliest|initial(?:ly)?)\b", re.IGNORECASE)
EXHAUSTIVE_QUERY = re.compile(
    r"\b(all|every|ever|count|how many|date range|between)\b", re.IGNORECASE
)


def _grounded_statement(quote, coverage):
    if coverage == "earliest_match":
        return (
            "The earliest saved entry matching all exact searched terms says "
            f"“{quote}”. This does not establish the earliest semantic occurrence."
        )
    if coverage == "literal_match_set":
        return (
            f"A saved entry matching all exact searched terms says “{quote}”. "
            "This literal-term result may omit semantically similar entries."
        )
    return (
        f"A matching saved entry says “{quote}”. "
        "This quotation does not establish archive-wide completeness."
    )


def _exact_term_pattern(term):
    return rf"(^|[^[:alnum:]]){term}([^[:alnum:]]|$)"


C = tables["app.conversations"]
M = tables["app.messages"]
D = tables["app.dreams"]
R = tables["app.dream_revisions"]
DM = tables["app.dream_motifs"]
E = tables["app.evidence"]
ME = tables["app.message_evidence"]
RUNS = tables["app.model_runs"]
J = tables["app.jobs"]


class DetectiveService:
    def __init__(self, db, settings, provider=None):
        self.db = db
        self.settings = settings
        self.provider = provider or Nebius(settings)
        self.prompt = (PROMPTS / "detective_v1.txt").read_text(encoding="utf-8")

    async def create_conversation(self, identity, title=None):
        async with self.db.tenant_transaction(identity.user_id) as c:
            await active_account(c, identity.user_id)
            row = (
                await c.execute(
                    insert(C)
                    .values(
                        id=uuid4(),
                        user_id=identity.user_id,
                        title=title or "Dream Detective",
                    )
                    .returning(C)
                )
            ).mappings().one()
            return self._serialize_conversation(row)

    async def list_conversations(self, identity, limit=50):
        async with self.db.tenant_transaction(identity.user_id) as c:
            await active_account(c, identity.user_id)
            rows = (
                await c.execute(
                    select(C)
                    .where(C.c.user_id == identity.user_id)
                    .order_by(C.c.updated_at.desc(), C.c.id.desc())
                    .limit(limit)
                )
            ).mappings().all()
            items = [self._serialize_conversation(row) for row in rows]
            return ConversationList(items=items, count=len(items))

    async def delete_conversation(self, identity, conversation_id):
        user_id = identity.user_id
        async with self.db.tenant_transaction(user_id) as c:
            await active_account(c, user_id)
            await self._owned_conversation(c, user_id, conversation_id, lock=True)
            message_ids = select(M.c.id).where(
                M.c.user_id == user_id,
                M.c.conversation_id == conversation_id,
            )
            evidence_ids = (
                await c.execute(
                    select(E.c.id)
                    .join(
                        ME,
                        and_(
                            ME.c.user_id == E.c.user_id,
                            ME.c.evidence_id == E.c.id,
                        ),
                    )
                    .where(ME.c.user_id == user_id, ME.c.message_id.in_(message_ids))
                )
            ).scalars().all()
            job_ids = (
                await c.execute(
                    select(J.c.id)
                    .where(
                        J.c.user_id == user_id,
                        J.c.kind == "detective_answer",
                        J.c.payload["conversation_id"].astext == str(conversation_id),
                    )
                    .order_by(J.c.id)
                    .with_for_update()
                )
            ).scalars().all()
            if job_ids:
                await c.execute(
                    update(J)
                    .where(J.c.user_id == user_id, J.c.id.in_(job_ids))
                    .values(payload={})
                )
                await c.execute(
                    update(J)
                    .where(
                        J.c.user_id == user_id,
                        J.c.id.in_(job_ids),
                        J.c.status.in_(("queued", "running")),
                    )
                    .values(
                        status="cancelled",
                        stage="cancelled",
                        error_code="conversation_deleted",
                        lease_token=None,
                        leased_until=None,
                        deadline_at=None,
                        stage_deadline_at=None,
                    )
                )
            await c.execute(
                delete(C).where(C.c.user_id == user_id, C.c.id == conversation_id)
            )
            if evidence_ids:
                evidence_links = [
                    tables[f"app.{name}"]
                    for name in (
                        "message_evidence",
                        "analysis_evidence",
                        "connection_evidence",
                        "summary_evidence",
                        "storyline_evidence",
                        "motif_evidence",
                    )
                ]
                unreferenced = [
                    not_(
                        exists(
                            select(link.c.evidence_id).where(
                                link.c.user_id == user_id,
                                link.c.evidence_id == E.c.id,
                            )
                        )
                    )
                    for link in evidence_links
                ]
                await c.execute(
                    delete(E).where(
                        E.c.user_id == user_id,
                        E.c.id.in_(evidence_ids),
                        *unreferenced,
                    )
                )

    async def submit(
        self,
        identity,
        conversation_id,
        question,
        idempotency_key,
        *,
        source_ids=(),
        from_date=None,
        to_date=None,
        motif_ids=(),
    ):
        user_id = identity.user_id
        async with self.db.tenant_transaction(user_id) as c:
            account = await active_account(c, user_id)
            await self._owned_conversation(c, user_id, conversation_id, lock=True)
            source_ids = sorted({UUID(str(value)) for value in source_ids}, key=str)
            motif_ids = sorted({UUID(str(value)) for value in motif_ids}, key=str)
            if from_date is not None and to_date is not None and from_date > to_date:
                raise APIError("invalid_date_range", "from_date must not follow to_date", 422)
            admission = await reserve_ai(
                c,
                identity,
                self.settings,
                route="detective.message",
                key=idempotency_key,
                body={
                    "conversation_id": str(conversation_id),
                    "question": question,
                    "source_ids": [str(value) for value in source_ids],
                    "from_date": from_date.isoformat() if from_date else None,
                    "to_date": to_date.isoformat() if to_date else None,
                    "motif_ids": [str(value) for value in motif_ids],
                },
            )
            job_id = admission.resource_id
            if admission.replayed:
                job = (
                    await c.execute(
                        select(J).where(
                            J.c.user_id == user_id,
                            J.c.id == job_id,
                            J.c.kind == "detective_answer",
                        )
                    )
                ).mappings().one_or_none()
                if job is None:
                    raise APIError("answer_unavailable", FAILED_ANSWER, 503)
                assistant_id = job["payload"].get("assistant_message_id")
                if assistant_id is None:
                    raise APIError("answer_unavailable", FAILED_ANSWER, 503)
                status = job["status"]
                return DetectiveSubmission(
                    job_id=job_id, message_id=UUID(assistant_id), status=status
                )

            user_message_id, assistant_message_id = uuid4(), uuid4()
            request_key = str(job_id)
            for message_id, role, body in (
                (user_message_id, "user", question),
                (assistant_message_id, "assistant", ""),
            ):
                await c.execute(
                    insert(M).values(
                        id=message_id,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        role=role,
                        body=body,
                        status="pending",
                        request_key=request_key,
                        context_versions={"job_id": str(job_id)} if role == "assistant" else {},
                    )
                )
            await c.execute(
                insert(J).values(
                    id=job_id,
                    user_id=user_id,
                    kind="detective_answer",
                    dedupe_key=request_key,
                    payload={
                        "conversation_id": str(conversation_id),
                        "user_message_id": str(user_message_id),
                        "assistant_message_id": str(assistant_message_id),
                        "source_ids": [str(value) for value in source_ids],
                        "from_date": from_date.isoformat() if from_date else None,
                        "to_date": to_date.isoformat() if to_date else None,
                        "motif_ids": [str(value) for value in motif_ids],
                    },
                    status="queued",
                    stage="queued",
                    attempts=0,
                    run_after=func.now(),
                    error_code=None,
                    lease_token=None,
                    leased_until=None,
                    heartbeat_at=None,
                    deadline_at=None,
                    stage_deadline_at=None,
                    data_epoch=account["data_epoch"],
                    input_versions={"prompt_version": "detective_v1"},
                )
            )
            await c.execute(
                update(C)
                .where(C.c.user_id == user_id, C.c.id == conversation_id)
                .values(updated_at=func.now())
            )
            return DetectiveSubmission(
                job_id=job_id, message_id=assistant_message_id, status="queued"
            )

    async def get_conversation_messages(
        self, identity, conversation_id, *, limit=50, offset=0
    ):
        if not 1 <= limit <= 100 or offset < 0:
            raise APIError("invalid_pagination", "Message page is invalid", 422)
        user_id = identity.user_id
        async with self.db.tenant_transaction(user_id) as c:
            await active_account(c, user_id)
            await self._owned_conversation(c, user_id, conversation_id)
            rows = (
                await c.execute(
                    select(M)
                    .where(
                        M.c.user_id == user_id,
                        M.c.conversation_id == conversation_id,
                    )
                    .order_by(M.c.created_at.asc(), M.c.id.asc())
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).mappings().all()
            has_more = len(rows) > limit
            rows = rows[:limit]
            citations = await self._message_citations(
                c, user_id, [row["id"] for row in rows]
            )
            items = []
            for record in rows:
                validated = await self._validated_message(
                    c, user_id, dict(record), citations.get(record["id"], [])
                )
                items.append(
                    self._serialize_message(validated, citations.get(record["id"], []))
                )
            return ConversationMessages(
                conversation_id=conversation_id,
                items=items,
                offset=offset,
                limit=limit,
                has_more=has_more,
            )

    async def get_message(self, identity, message_id):
        user_id = identity.user_id
        async with self.db.tenant_transaction(user_id) as c:
            await active_account(c, user_id)
            row = (
                await c.execute(
                    select(M).where(M.c.user_id == user_id, M.c.id == message_id)
                )
            ).mappings().one_or_none()
            if row is None:
                raise APIError("not_found", "Message not found", 404)
            citations = await self._message_citations(c, user_id, [message_id])
            validated = await self._validated_message(
                c, user_id, dict(row), citations.get(message_id, [])
            )
            return self._serialize_message(validated, citations.get(message_id, []))

    async def run_queued_job(self, job):
        user_id = job["user_id"]
        payload = job["payload"]
        conversation_id = UUID(payload["conversation_id"])
        async with self.db.tenant_transaction(user_id) as c:
            account = await active_account(c, user_id)
            current = (
                await c.execute(
                    select(J)
                    .where(
                        J.c.user_id == user_id,
                        J.c.id == job["id"],
                        J.c.status == "running",
                        J.c.lease_token == job["lease_token"],
                        J.c.leased_until > func.clock_timestamp(),
                        J.c.deadline_at > func.clock_timestamp(),
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if current is None or account["data_epoch"] != current["data_epoch"]:
                raise SourceChanged
            await self._owned_conversation(c, user_id, conversation_id)
            history = (
                await c.execute(
                    select(M.c.body)
                    .where(
                        M.c.user_id == user_id,
                        M.c.conversation_id == conversation_id,
                        M.c.role == "user",
                        M.c.status == "completed",
                    )
                    .order_by(M.c.created_at.desc(), M.c.id.desc())
                    .limit(MAX_HISTORY)
                )
            ).scalars().all()
            question_row = (
                await c.execute(
                    select(M.c.body).where(
                        M.c.user_id == user_id,
                        M.c.id == UUID(payload["user_message_id"]),
                        M.c.conversation_id == conversation_id,
                        M.c.role == "user",
                        M.c.status == "pending",
                    )
                )
            ).mappings().one_or_none()
            if question_row is None or not question_row["body"]:
                raise SourceChanged
            question = question_row["body"]

        prior_questions = list(reversed(history))
        sources, coverage_limited = await self._retrieve(
            user_id,
            conversation_id,
            question,
            prior_questions,
            source_ids=payload.get("source_ids", ()),
            from_date=(
                date.fromisoformat(payload["from_date"])
                if payload.get("from_date")
                else None
            ),
            to_date=(
                date.fromisoformat(payload["to_date"])
                if payload.get("to_date")
                else None
            ),
            motif_ids=payload.get("motif_ids", ()),
        )
        if not sources:
            claims, citations = [], []
            tentative_reflections = []
            answer = LIMITED_ANSWER if coverage_limited else INSUFFICIENT_ANSWER
            insufficient = True
        else:
            output, run_id = await self._generate(
                user_id, question, prior_questions, sources
            )
            claims, citations = self._verify_output(output, sources)
            insufficient = output.insufficient_evidence or not claims
            tentative_reflections = (
                [REFLECTION_QUESTION] if claims and not insufficient else []
            )
            answer = (
                LIMITED_ANSWER
                if insufficient and coverage_limited
                else INSUFFICIENT_ANSWER
                if insufficient
                else " ".join(claim["statement"] for claim in claims)
            )
            await self._mark_model_run(user_id, run_id, "validated")
        return await self._persist_turn(
            user_id,
            conversation_id,
            UUID(payload["user_message_id"]),
            str(job["id"]),
            answer,
            insufficient,
            coverage_limited,
            sources[0]["coverage"] if sources else "top_ranked",
            sources[0].get("coverage_terms", []) if sources else [],
            claims,
            citations,
            tentative_reflections=tentative_reflections,
            job=job,
        )

    async def get_conversation(self, identity, conversation_id):
        async with self.db.tenant_transaction(identity.user_id) as c:
            await active_account(c, identity.user_id)
            conversation = await self._owned_conversation(c, identity.user_id, conversation_id)
            rows = (
                await c.execute(
                    select(M)
                    .where(
                        M.c.user_id == identity.user_id,
                        M.c.conversation_id == conversation_id,
                        M.c.status != "pending",
                    )
                    .order_by(M.c.created_at.desc(), M.c.id.desc())
                    .limit(MAX_HISTORY + 1)
                )
            ).mappings().all()
            has_more = len(rows) > MAX_HISTORY
            rows = list(reversed(rows[:MAX_HISTORY]))
            citations = await self._message_citations(
                c, identity.user_id, [row["id"] for row in rows]
            )
            messages = []
            for record in rows:
                row = await self._validated_message(
                    c,
                    identity.user_id,
                    dict(record),
                    citations.get(record["id"], []),
                )
                messages.append(
                    self._serialize_message(row, citations.get(record["id"], []))
                )
            return ConversationDetail(
                **self._serialize_conversation(conversation).model_dump(),
                messages=messages,
                has_more=has_more,
            )

    async def ask(self, identity, conversation_id, question, idempotency_key):
        async with self.db.tenant_transaction(identity.user_id) as c:
            await active_account(c, identity.user_id)
            await self._owned_conversation(c, identity.user_id, conversation_id, lock=True)
            admission = await reserve_ai(
                c,
                identity,
                self.settings,
                route="detective.message",
                key=idempotency_key,
                body={"conversation_id": str(conversation_id), "question": question},
            )
            request_key = str(admission.resource_id)
            if admission.replayed:
                existing = (
                    await c.execute(
                        select(M).where(
                            M.c.user_id == identity.user_id,
                            M.c.conversation_id == conversation_id,
                            M.c.request_key == request_key,
                            M.c.role == "assistant",
                        )
                    )
                ).mappings().one_or_none()
                if existing is not None:
                    if existing["status"] == "pending":
                        raise APIError(
                            "message_in_progress", "Message is still being processed", 409
                        )
                    if existing["status"] == "completed":
                        citations = await self._message_citations(
                            c, identity.user_id, [existing["id"]]
                        )
                        message = await self._validated_message(
                            c,
                            identity.user_id,
                            dict(existing),
                            citations.get(existing["id"], []),
                        )
                        return self._serialize_message(
                            message, citations.get(message["id"], [])
                        )
                    if existing["status"] != "failed":
                        raise APIError("answer_unavailable", FAILED_ANSWER, 503)
                pending = (
                    await c.execute(
                        select(M).where(
                            M.c.user_id == identity.user_id,
                            M.c.conversation_id == conversation_id,
                            M.c.request_key == request_key,
                            M.c.role == "user",
                        )
                    )
                ).mappings().one_or_none()
                if pending is None:
                    raise APIError("message_in_progress", "Message is being processed", 409)
                if pending["status"] == "pending":
                    raise APIError("message_in_progress", "Message is being processed", 409)
                if pending["status"] != "failed":
                    raise APIError("answer_unavailable", FAILED_ANSWER, 503)
                user_message_id = pending["id"]
                claimed = (
                    await c.execute(
                        update(M)
                        .where(
                            M.c.user_id == identity.user_id,
                            M.c.conversation_id == conversation_id,
                            M.c.id == user_message_id,
                            M.c.role == "user",
                            M.c.status == "failed",
                            M.c.request_key == request_key,
                        )
                        .values(status="pending")
                        .returning(M.c.id)
                    )
                ).scalar_one_or_none()
                if claimed is None:
                    raise APIError(
                        "message_in_progress", "Message is being processed", 409
                    )
                history = []
            else:
                history = (
                    await c.execute(
                        select(M.c.role, M.c.body)
                        .where(
                            M.c.user_id == identity.user_id,
                            M.c.conversation_id == conversation_id,
                            M.c.role == "user",
                            M.c.status == "completed",
                        )
                        .order_by(M.c.created_at.desc(), M.c.id.desc())
                        .limit(MAX_HISTORY)
                    )
                ).mappings().all()
                user_message_id = uuid4()
                await c.execute(
                    insert(M).values(
                        id=user_message_id,
                        user_id=identity.user_id,
                        conversation_id=conversation_id,
                        role="user",
                        body=question,
                        status="pending",
                        request_key=request_key,
                        context_versions={},
                    )
                )

        stage = "retrieve"
        run_id = None
        try:
            sources, coverage_limited = await self._retrieve(
                identity.user_id,
                conversation_id,
                question,
                [row["body"] for row in reversed(history)],
            )
            if not sources:
                claims = []
                citations = []
                tentative_reflections = []
                answer = LIMITED_ANSWER if coverage_limited else INSUFFICIENT_ANSWER
                insufficient = True
            else:
                stage = "generate"
                output, run_id = await self._generate(
                    identity.user_id,
                    question,
                    [row["body"] for row in reversed(history)],
                    sources,
                )
                claims, citations = self._verify_output(output, sources)
                insufficient = output.insufficient_evidence or not claims
                tentative_reflections = (
                    [REFLECTION_QUESTION] if claims and not insufficient else []
                )
                answer = (
                    LIMITED_ANSWER
                    if insufficient and coverage_limited
                    else INSUFFICIENT_ANSWER
                    if insufficient
                    else " ".join(claim["statement"] for claim in claims)
                )
                await self._mark_model_run(identity.user_id, run_id, "validated")
            stage = "persist"
            return await self._persist_turn(
                identity.user_id,
                conversation_id,
                user_message_id,
                request_key,
                answer,
                insufficient,
                coverage_limited,
                sources[0]["coverage"] if sources else "top_ranked",
                sources[0].get("coverage_terms", []) if sources else [],
                claims,
                citations,
                tentative_reflections=tentative_reflections,
            )
        except Exception as error:
            if run_id is not None and isinstance(error, OutputInvalid):
                try:
                    await self._mark_model_run(identity.user_id, run_id, "invalid_output")
                except Exception as audit_error:
                    logger.error(
                        "detective_audit_failed error_type=%s",
                        type(audit_error).__name__,
                    )
            logger.error(
                "detective_turn_failed stage=%s error_type=%s",
                stage,
                type(error).__name__,
            )
            try:
                await self._fail_turn(
                    identity.user_id, conversation_id, user_message_id, request_key
                )
            except Exception as recovery_error:
                logger.error(
                    "detective_turn_recovery_failed error_type=%s",
                    type(recovery_error).__name__,
                )
            raise APIError("answer_unavailable", FAILED_ANSWER, 503) from None

    async def _owned_conversation(self, c, user_id, conversation_id, *, lock=False):
        query = select(C).where(C.c.user_id == user_id, C.c.id == conversation_id)
        if lock:
            query = query.with_for_update()
        conversation = (await c.execute(query)).mappings().one_or_none()
        if conversation is None:
            raise APIError("not_found", "Conversation not found", 404)
        return conversation

    @staticmethod
    def _serialize_conversation(row):
        return ConversationRead(
            id=row["id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def _retrieve(
        self,
        user_id,
        conversation_id,
        question,
        history,
        *,
        source_ids=(),
        from_date=None,
        to_date=None,
        motif_ids=(),
    ):
        first_query = bool(FIRST_QUERY.search(question))
        earliest_query = first_query
        exhaustive_query = bool(EXHAUSTIVE_QUERY.search(question)) and not earliest_query
        search_text = (
            question
            if first_query or exhaustive_query
            else "\n".join([*history, question])
        )
        query_terms = sorted(terms(search_text) - SEARCH_META_TERMS)[:12]
        source_ids = [UUID(str(value)) for value in source_ids]
        motif_ids = [UUID(str(value)) for value in motif_ids]
        if not query_terms and (earliest_query or not source_ids):
            return [], False
        patterns = (
            [R.c.text.op("~*")(_exact_term_pattern(term)) for term in query_terms]
            if earliest_query
            else [R.c.text.ilike(f"%{term[:64]}%") for term in query_terms]
        )
        filters = [
            D.c.user_id == user_id,
            D.c.lifecycle == "saved",
            D.c.deleted_at.is_(None),
            R.c.user_id == user_id,
        ]
        if source_ids:
            filters.append(D.c.id.in_(source_ids))
        if from_date is not None:
            filters.append(D.c.dream_date >= from_date)
        if to_date is not None:
            filters.append(D.c.dream_date <= to_date)
        if motif_ids:
            filters.append(
                exists(
                    select(DM.c.id).where(
                        DM.c.user_id == user_id,
                        DM.c.dream_id == D.c.id,
                        DM.c.revision_id == D.c.current_revision_id,
                        DM.c.motif_id.in_(motif_ids),
                        DM.c.confirmation_state == "confirmed",
                    )
                )
            )
        if query_terms:
            filters.append(
                and_(*patterns)
                if earliest_query or exhaustive_query
                else or_(*patterns)
            )
        async with self.db.tenant_transaction(user_id) as c:
            await active_account(c, user_id)
            await self._owned_conversation(c, user_id, conversation_id)
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
                        *filters,
                    )
                    .order_by(
                        D.c.dream_date.asc()
                        if earliest_query
                        else D.c.dream_date.desc(),
                        D.c.id.asc() if earliest_query else D.c.id.desc(),
                    )
                    .limit(
                        1
                        if earliest_query
                        else MAX_SOURCES + 1
                        if exhaustive_query
                        else MAX_CANDIDATES
                    )
                )
            ).mappings().all()
        if not rows:
            return [], False
        if exhaustive_query and len(rows) > MAX_SOURCES:
            return [], True

        rank_rows = [{**row, "text": f"{row['title']}\n{row['text']}"} for row in rows]
        scores = lexical_rank(search_text, rank_rows)
        ranked = sorted(
            (
                row
                for row in rows
                if scores[row["id"]] > 0 or (source_ids and not query_terms)
            ),
            key=lambda row: (
                scores[row["id"]],
                row["dream_date"],
                str(row["id"]),
            ),
            reverse=True,
        )
        limited = len(ranked) > MAX_SOURCES
        sources = [
            {
                **row,
                "excerpt": self._excerpt(row["text"], query_terms),
                "coverage": (
                    "literal_match_set"
                    if exhaustive_query
                    else "earliest_match"
                    if earliest_query
                    else "top_ranked"
                ),
                "coverage_terms": query_terms if earliest_query else [],
            }
            for row in ranked[:MAX_SOURCES]
        ]
        return sources, limited

    @staticmethod
    def _excerpt(text, query_terms):
        position = next(
            (
                match.start()
                for term in query_terms
                if (match := re.search(re.escape(term), text, flags=re.IGNORECASE))
            ),
            0,
        )
        start = max(0, position - MAX_EXCERPT_LENGTH // 3)
        excerpt = text[start : start + MAX_EXCERPT_LENGTH]
        return {"start": start, "text": excerpt}

    async def _generate(self, user_id, question, history, sources):
        run_ids = []

        async def record_call(**metadata):
            async with self.db.tenant_transaction(user_id) as c:
                await active_account(c, user_id)
                return_id = await c.scalar(
                    insert(RUNS)
                    .values(
                        user_id=user_id,
                        job_id=None,
                        prompt_version="detective_v1",
                        trace_stage=metadata["task"],
                        **metadata,
                    )
                    .returning(RUNS.c.id)
                )
                run_ids.append(return_id)
                return return_id

        context = [
            {
                "dream_id": str(row["id"]),
                "revision_id": str(row["revision_id"]),
                "source_hash": row["content_hash"],
                "title": row["title"],
                "dream_date": row["dream_date"].isoformat(),
                "offset": row["excerpt"]["start"],
                "excerpt": row["excerpt"]["text"],
                "coverage": row["coverage"],
            }
            for row in sources
        ]
        completion = await self.provider.complete(
            [
                {"role": "system", "content": self.prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"question": question, "previous_questions": history, "sources": context},
                        ensure_ascii=False,
                    ),
                },
            ],
            schema=DetectiveOutput.model_json_schema(),
            task="detective",
            record=record_call,
        )
        try:
            output = DetectiveOutput.model_validate_json(completion.text)
        except ValidationError:
            if completion.run_id:
                await self._mark_model_run(user_id, completion.run_id, "invalid_output")
            raise
        return output, completion.run_id

    @staticmethod
    def _verify_output(output, sources):
        source_map = {(row["id"], row["revision_id"]): row for row in sources}
        verified, unique = [], {}
        verified_claims = []
        for claim in output.claims:
            claim_keys = []
            claim_citations = {}
            for citation in claim.citations:
                source = source_map.get((citation.dream_id, citation.revision_id))
                if source is None:
                    continue
                try:
                    citation = verify_citation(
                        citation,
                        dream_id=source["id"],
                        revision_id=source["revision_id"],
                        source_hash=source["content_hash"],
                        text=source["text"],
                    )
                except OutputInvalid:
                    continue
                if not _is_verbatim_source_claim(claim.statement, citation, source["text"]):
                    continue
                key = (
                    citation.dream_id,
                    citation.revision_id,
                    citation.start,
                    citation.end,
                    citation.quote,
                )
                claim_citations[key] = (citation, source)
                claim_keys.append(key)
            if claim_keys and _single_sentence(claim.statement):
                unique.update(claim_citations)
                verified_claims.append(
                    {
                        "statement": _grounded_statement(
                            claim_citations[claim_keys[0]][0].quote,
                            claim_citations[claim_keys[0]][1].get(
                                "coverage", "top_ranked"
                            ),
                        ),
                        "citation_keys": claim_keys,
                    }
                )
        for citation, source in unique.values():
            verified.append(
                {
                    "key": (
                        citation.dream_id,
                        citation.revision_id,
                        citation.start,
                        citation.end,
                        citation.quote,
                    ),
                    "dream_id": source["id"],
                    "revision_id": source["revision_id"],
                    "title": source["title"],
                    "dream_date": source["dream_date"],
                    "start": citation.start,
                    "end": citation.end,
                    "quote": citation.quote,
                    "content_hash": source["content_hash"],
                }
            )
        return verified_claims, verified

    async def _mark_model_run(self, user_id, run_id, status):
        if run_id is None:
            return
        async with self.db.tenant_transaction(user_id) as c:
            await active_account(c, user_id)
            await c.execute(
                update(RUNS)
                .where(RUNS.c.user_id == user_id, RUNS.c.id == run_id)
                .values(status=status)
            )

    async def _persist_turn(
        self,
        user_id,
        conversation_id,
        user_message_id,
        request_key,
        answer,
        insufficient,
        coverage_limited,
        coverage,
        coverage_terms,
        claims,
        citations,
        *,
        job=None,
        tentative_reflections=(),
    ):
        async with self.db.tenant_transaction(user_id) as c:
            account = await active_account(c, user_id)
            current_job = None
            if job is not None:
                current_job = (
                    await c.execute(
                        select(J)
                        .where(
                            J.c.user_id == user_id,
                            J.c.id == job["id"],
                            J.c.status == "running",
                            J.c.lease_token == job["lease_token"],
                            J.c.leased_until > func.clock_timestamp(),
                            J.c.deadline_at > func.clock_timestamp(),
                        )
                        .with_for_update()
                    )
                ).mappings().one_or_none()
                if (
                    current_job is None
                    or account["data_epoch"] != current_job["data_epoch"]
                ):
                    raise SourceChanged
            await self._owned_conversation(c, user_id, conversation_id)
            pending_user = (
                await c.execute(
                    select(M)
                    .where(
                        M.c.user_id == user_id,
                        M.c.conversation_id == conversation_id,
                        M.c.id == user_message_id,
                        M.c.request_key == request_key,
                        M.c.role == "user",
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if pending_user is None or pending_user["status"] != "pending":
                raise APIError(
                    "message_in_progress", "Message is no longer being processed", 409
                )
            existing_assistant = (
                await c.execute(
                    select(M)
                    .where(
                        M.c.user_id == user_id,
                        M.c.conversation_id == conversation_id,
                        M.c.request_key == request_key,
                        M.c.role == "assistant",
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if existing_assistant is not None:
                if existing_assistant["status"] == "completed":
                    if current_job is not None:
                        completed = await c.execute(
                            update(J)
                            .where(
                                J.c.user_id == user_id,
                                J.c.id == current_job["id"],
                                J.c.status == "running",
                                J.c.lease_token == job["lease_token"],
                                J.c.leased_until > func.clock_timestamp(),
                                J.c.deadline_at > func.clock_timestamp(),
                            )
                            .values(
                                status="succeeded",
                                stage="ready",
                                error_code=None,
                                lease_token=None,
                                leased_until=None,
                                deadline_at=None,
                                stage_deadline_at=None,
                            )
                        )
                        if completed.rowcount != 1:
                            raise SourceChanged
                    citations_by_message = await self._message_citations(
                        c, user_id, [existing_assistant["id"]]
                    )
                    row = await self._validated_message(
                        c,
                        user_id,
                        dict(existing_assistant),
                        citations_by_message.get(existing_assistant["id"], []),
                    )
                    return self._serialize_message(
                        row, citations_by_message.get(row["id"], [])
                    )
                if existing_assistant["status"] != (
                    "pending" if job is not None else "failed"
                ):
                    raise APIError(
                        "message_in_progress", "Message is being processed", 409
                    )
                if (
                    job is not None
                    and existing_assistant["id"] != UUID(job["payload"]["assistant_message_id"])
                ):
                    raise SourceChanged
            if citations:
                dream_ids = {citation["dream_id"] for citation in citations}
                current_sources = (
                    await c.execute(
                        select(D.c.id, D.c.current_revision_id, R.c.content_hash)
                        .join(
                            R,
                            and_(
                                R.c.user_id == D.c.user_id,
                                R.c.dream_id == D.c.id,
                                R.c.id == D.c.current_revision_id,
                            ),
                        )
                        .where(
                            D.c.user_id == user_id,
                            D.c.id.in_(dream_ids),
                            D.c.lifecycle == "saved",
                            D.c.deleted_at.is_(None),
                        )
                        .with_for_update(of=D)
                    )
                ).mappings().all()
                current_by_dream = {row["id"]: row for row in current_sources}
                sources_are_current = len(current_by_dream) == len(dream_ids) and all(
                    citation["dream_id"] in current_by_dream
                    and current_by_dream[citation["dream_id"]]["current_revision_id"]
                    == citation["revision_id"]
                    and current_by_dream[citation["dream_id"]]["content_hash"]
                    == citation["content_hash"]
                    for citation in citations
                )
                if not sources_are_current:
                    answer = LIMITED_ANSWER if coverage_limited else INSUFFICIENT_ANSWER
                    insufficient = True
                    claims = []
                    citations = []
                    tentative_reflections = []
            updated_user = (
                await c.execute(
                    update(M)
                    .where(
                        M.c.user_id == user_id,
                        M.c.conversation_id == conversation_id,
                        M.c.id == user_message_id,
                        M.c.request_key == request_key,
                        M.c.role == "user",
                        M.c.status == "pending",
                    )
                    .values(status="completed")
                    .returning(M.c.id)
                )
            ).scalar_one_or_none()
            if updated_user is None:
                raise APIError(
                    "message_in_progress", "Message is no longer being processed", 409
                )
            assistant_id = (
                existing_assistant["id"] if existing_assistant is not None else uuid4()
            )
            saved_citations = []
            evidence_ids = {}
            for citation in citations:
                evidence_id = uuid4()
                await c.execute(
                    insert(E).values(
                        id=evidence_id,
                        user_id=user_id,
                        dream_id=citation["dream_id"],
                        revision_id=citation["revision_id"],
                        start=citation["start"],
                        end=citation["end"],
                        exact_quote=citation["quote"],
                        content_hash=citation["content_hash"],
                    )
                )
                evidence_ids[citation["key"]] = evidence_id
                saved_citations.append(
                    DreamCitation(
                        evidence_id=evidence_id,
                        **{key: value for key, value in citation.items() if key != "key"},
                    )
                )
            context_versions = {
                "prompt_version": "detective_v1",
                "insufficient_evidence": insufficient,
                "coverage_limited": coverage_limited,
                "coverage": coverage,
                "coverage_terms": coverage_terms if coverage == "earliest_match" else [],
                "claims": [
                    {
                        "statement": claim["statement"],
                        "evidence_ids": [
                            str(evidence_ids[key]) for key in claim["citation_keys"]
                        ],
                    }
                    for claim in claims
                ],
                "tentative_reflections": list(tentative_reflections),
                "sources": [
                    {
                        "dream_id": str(citation["dream_id"]),
                        "revision_id": str(citation["revision_id"]),
                        "content_hash": citation["content_hash"],
                    }
                    for citation in citations
                ],
            }
            if existing_assistant is None:
                assistant = (
                    await c.execute(
                        insert(M)
                        .values(
                            id=assistant_id,
                            user_id=user_id,
                            conversation_id=conversation_id,
                            role="assistant",
                            body=answer,
                            status="completed",
                            request_key=request_key,
                            context_versions=context_versions,
                        )
                        .returning(M)
                    )
                ).mappings().one()
            else:
                assistant = (
                    await c.execute(
                        update(M)
                        .where(
                            M.c.user_id == user_id,
                            M.c.conversation_id == conversation_id,
                            M.c.id == assistant_id,
                            M.c.request_key == request_key,
                            M.c.role == "assistant",
                            M.c.status == ("pending" if job is not None else "failed"),
                        )
                        .values(
                            body=answer,
                            status="completed",
                            context_versions=context_versions,
                        )
                        .returning(M)
                    )
                ).mappings().one_or_none()
                if assistant is None:
                    raise APIError(
                        "message_in_progress", "Message is no longer being processed", 409
                    )
            for citation in saved_citations:
                await c.execute(
                    insert(ME).values(
                        user_id=user_id,
                        message_id=assistant_id,
                        evidence_id=citation.evidence_id,
                    )
                )
            await c.execute(
                update(C)
                .where(C.c.user_id == user_id, C.c.id == conversation_id)
                .values(updated_at=func.now())
            )
            if current_job is not None:
                completed = await c.execute(
                    update(J)
                    .where(
                        J.c.user_id == user_id,
                        J.c.id == current_job["id"],
                        J.c.status == "running",
                        J.c.lease_token == job["lease_token"],
                        J.c.leased_until > func.clock_timestamp(),
                        J.c.deadline_at > func.clock_timestamp(),
                    )
                    .values(
                        status="succeeded",
                        stage="ready",
                        error_code=None,
                        lease_token=None,
                        leased_until=None,
                        deadline_at=None,
                        stage_deadline_at=None,
                    )
                )
                if completed.rowcount != 1:
                    raise SourceChanged
            return self._serialize_message(assistant, saved_citations)

    async def _fail_turn(self, user_id, conversation_id, user_message_id, request_key):
        async with self.db.tenant_transaction(user_id) as c:
            await active_account(c, user_id)
            await self._owned_conversation(c, user_id, conversation_id)
            failed_user = (
                await c.execute(
                    update(M)
                    .where(
                        M.c.user_id == user_id,
                        M.c.conversation_id == conversation_id,
                        M.c.id == user_message_id,
                        M.c.request_key == request_key,
                        M.c.role == "user",
                        M.c.status == "pending",
                    )
                    .values(status="failed")
                    .returning(M.c.id)
                )
            ).scalar_one_or_none()
            if failed_user is None:
                return
            assistant = (
                await c.execute(
                    select(M).where(
                        M.c.user_id == user_id,
                        M.c.conversation_id == conversation_id,
                        M.c.request_key == request_key,
                        M.c.role == "assistant",
                    )
                )
            ).mappings().one_or_none()
            if assistant is None:
                await c.execute(
                    insert(M).values(
                        id=uuid4(),
                        user_id=user_id,
                        conversation_id=conversation_id,
                        role="assistant",
                        body=FAILED_ANSWER,
                        status="failed",
                        request_key=request_key,
                        context_versions={"prompt_version": "detective_v1"},
                    )
                )
            elif assistant["status"] == "pending":
                await c.execute(
                    update(M)
                    .where(
                        M.c.user_id == user_id,
                        M.c.id == assistant["id"],
                        M.c.status == "pending",
                    )
                    .values(
                        body=FAILED_ANSWER,
                        status="failed",
                        context_versions={"prompt_version": "detective_v1"},
                    )
                )
            await c.execute(
                update(C)
                .where(C.c.user_id == user_id, C.c.id == conversation_id)
                .values(updated_at=func.now())
            )

    async def _message_citations(self, c, user_id, message_ids):
        if not message_ids:
            return {}
        rows = (
            await c.execute(
                select(
                    ME.c.message_id,
                    E.c.id.label("evidence_id"),
                    E.c.dream_id,
                    E.c.revision_id,
                    D.c.title,
                    D.c.dream_date,
                    E.c.start,
                    E.c.end,
                    E.c.exact_quote.label("quote"),
                    E.c.content_hash,
                    D.c.current_revision_id,
                    D.c.deleted_at,
                    R.c.text.label("source_text"),
                    R.c.content_hash.label("revision_hash"),
                )
                .join(
                    E,
                    and_(E.c.user_id == ME.c.user_id, E.c.id == ME.c.evidence_id),
                )
                .join(
                    D,
                    and_(D.c.user_id == E.c.user_id, D.c.id == E.c.dream_id),
                )
                .outerjoin(
                    R,
                    and_(
                        R.c.user_id == E.c.user_id,
                        R.c.dream_id == E.c.dream_id,
                        R.c.id == E.c.revision_id,
                    ),
                )
                .where(
                    ME.c.user_id == user_id,
                    ME.c.message_id.in_(message_ids),
                    E.c.user_id == user_id,
                    D.c.user_id == user_id,
                )
                .order_by(E.c.dream_id, E.c.start, E.c.id)
            )
        ).mappings().all()
        result = {}
        for row in rows:
            if (
                row["deleted_at"] is not None
                or row["current_revision_id"] != row["revision_id"]
                or row["source_text"] is None
                or row["revision_hash"] != row["content_hash"]
                or row["start"] != 0
                or row["end"] != len(row["source_text"])
                or row["quote"] != row["source_text"]
                or not _single_sentence(row["source_text"])
            ):
                continue
            citation = Citation(
                dream_id=row["dream_id"],
                revision_id=row["revision_id"],
                source_hash=row["content_hash"],
                start=row["start"],
                end=row["end"],
                quote=row["quote"],
            )
            try:
                verify_citation(
                    citation,
                    dream_id=row["dream_id"],
                    revision_id=row["revision_id"],
                    source_hash=row["revision_hash"],
                    text=row["source_text"],
                )
            except OutputInvalid:
                continue
            result.setdefault(row["message_id"], []).append(
                DreamCitation(
                    **{
                        key: row[key]
                        for key in (
                            "evidence_id",
                            "dream_id",
                            "revision_id",
                            "title",
                            "dream_date",
                            "start",
                            "end",
                            "quote",
                            "content_hash",
                        )
                    }
                )
            )
        return result

    async def _earliest_match_id(self, c, user_id, query_terms):
        if not query_terms or any(terms(term) != {term} for term in query_terms):
            return None
        matching_terms = [
            R.c.text.op("~*")(_exact_term_pattern(term)) for term in query_terms
        ]
        return await c.scalar(
            select(D.c.id)
            .join(
                R,
                and_(
                    R.c.user_id == D.c.user_id,
                    R.c.dream_id == D.c.id,
                    R.c.id == D.c.current_revision_id,
                ),
            )
            .where(
                D.c.user_id == user_id,
                D.c.lifecycle == "saved",
                D.c.deleted_at.is_(None),
                R.c.user_id == user_id,
                and_(*matching_terms),
            )
            .order_by(D.c.dream_date.asc(), D.c.id.asc())
            .limit(1)
        )

    async def _validated_message(self, c, user_id, row, citations):
        if row["role"] != "assistant" or row["status"] != "completed":
            return row
        context = row["context_versions"] or {}
        claims = context.get("claims", [])
        reflections = context.get("tentative_reflections", [])
        reflections_supported = (
            isinstance(reflections, list)
            and all(value == REFLECTION_QUESTION for value in reflections)
            and (not reflections or bool(claims))
        )
        expected_ids = {
            evidence_id for claim in claims for evidence_id in claim.get("evidence_ids", [])
        }
        citations_by_id = {str(citation.evidence_id): citation for citation in citations}
        available_ids = set(citations_by_id)
        if context.get("insufficient_evidence"):
            expected_body = (
                LIMITED_ANSWER
                if context.get("coverage_limited")
                else INSUFFICIENT_ANSWER
            )
            if (
                row["body"] == expected_body
                and not claims
                and not expected_ids
                and not citations
                and reflections_supported
            ):
                return row
            await c.execute(
                update(M)
                .where(M.c.user_id == user_id, M.c.id == row["id"])
                .values(status="stale", body="")
            )
            row["status"] = "stale"
            row["body"] = ""
            return row
        expected_body = " ".join(claim.get("statement", "") for claim in claims)
        coverage_supported = True
        if context.get("coverage") == "earliest_match":
            query_terms = context.get("coverage_terms", [])
            earliest_id = await self._earliest_match_id(c, user_id, query_terms)
            coverage_supported = earliest_id is not None and all(
                any(
                    evidence_id in citations_by_id
                    and citations_by_id[evidence_id].dream_id == earliest_id
                    for evidence_id in claim.get("evidence_ids", [])
                )
                for claim in claims
            )
        claims_supported = all(
            bool(claim.get("evidence_ids"))
            and any(
                evidence_id in citations_by_id
                and claim["statement"]
                == _grounded_statement(
                    citations_by_id[evidence_id].quote,
                    context.get("coverage", "top_ranked"),
                )
                for evidence_id in claim.get("evidence_ids", [])
            )
            for claim in claims
        )
        if (
            not claims
            or not coverage_supported
            or not claims_supported
            or expected_ids != available_ids
            or row["body"] != expected_body
            or not reflections_supported
        ):
            await c.execute(
                update(M)
                .where(M.c.user_id == user_id, M.c.id == row["id"])
                .values(status="stale", body="")
            )
            row["status"] = "stale"
            row["body"] = ""
        return row

    @staticmethod
    def _serialize_message(row, citations):
        context = row["context_versions"] or {}
        if row["status"] == "stale":
            citations = []
            claims = []
            reflections = []
            coverage_limited = False
        else:
            claims = context.get("claims", [])
            reflections = context.get("tentative_reflections", [])
            coverage_limited = context.get("coverage_limited", False)
        citation_by_id = {str(citation.evidence_id): citation for citation in citations}
        observations = [
            GroundedClaim(
                statement=claim["statement"],
                citations=[
                    citation_by_id[evidence_id]
                    for evidence_id in claim["evidence_ids"]
                    if evidence_id in citation_by_id
                ],
            )
            for claim in claims
        ]
        return ConversationMessage(
            id=row["id"],
            role=row["role"],
            body=STALE_ANSWER if row["status"] == "stale" else row["body"],
            status=row["status"],
            created_at=row["created_at"],
            insufficient_evidence=context.get("insufficient_evidence", False),
            coverage_limited=coverage_limited,
            citations=citations,
            observations=observations,
            tentative_reflections=reflections,
            claims=observations,
        )


def _single_sentence(value):
    return len(re.split(r"(?<=[.!?])\s+", value.strip())) == 1


def _is_verbatim_source_claim(statement, citation, source_text):
    return (
        _single_sentence(statement)
        and statement == source_text
        and citation.quote == source_text
        and citation.start == 0
        and citation.end == len(source_text)
    )
