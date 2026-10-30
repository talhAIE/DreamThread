"""Bounded owner-scoped graph views over current dream and motif data."""

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import tables
from app.errors import APIError
from app.schemas.analysis import Citation
from app.schemas.constellation import (
    AssociationRead,
    ConstellationEdge,
    ConstellationGraph,
    ConstellationNode,
    MotifDetail,
    MotifEvidenceRead,
    MotifMentionRead,
    MotifResponse,
    StorylineChapter,
    StorylineDetail,
    StorylineListItem,
    StorylineRefreshAccepted,
)
from app.services.accounts import active_account
from app.services.evidence import OutputInvalid, verify_citation
from app.services.journal import owned_dream
from app.workers.types import SourceChanged

MAX_NODES = 100
MAX_EDGES = 200
MAX_MOTIF_ROWS = 1000
MAX_STORYLINES = 100

D = tables["app.dreams"]
DM = tables["app.dream_motifs"]
M = tables["app.motifs"]
ASSOC = tables["app.associations"]
ME = tables["app.motif_evidence"]
E = tables["app.evidence"]
CONN = tables["app.connections"]
CE = tables["app.connection_evidence"]
ST = tables["app.storylines"]
SE = tables["app.storyline_entries"]
STE = tables["app.storyline_evidence"]
J = tables["app.jobs"]
R = tables["app.dream_revisions"]


class ConstellationService:
    def __init__(self, db):
        self.db = db

    async def get_constellation(self, user_id: UUID, dream_id: UUID) -> list[MotifResponse]:
        async with self.db.tenant_transaction(user_id) as c:
            await active_account(c, user_id)
            dream = await owned_dream(c, user_id, dream_id)
            rows = (
                await c.execute(
                    select(
                        DM.c.id.label("link_id"),
                        DM.c.dream_id,
                        DM.c.revision_id,
                        DM.c.origin,
                        DM.c.confirmation_state,
                        M.c.id.label("motif_id"),
                        M.c.type,
                        M.c.canonical_label,
                        M.c.normalized_key,
                        D.c.dream_date,
                    )
                    .join(
                        D,
                        and_(
                            D.c.user_id == DM.c.user_id,
                            D.c.id == DM.c.dream_id,
                            D.c.current_revision_id == DM.c.revision_id,
                        ),
                    )
                    .join(
                        M,
                        and_(
                            M.c.user_id == DM.c.user_id,
                            M.c.id == DM.c.motif_id,
                        ),
                    )
                    .where(
                        DM.c.user_id == user_id,
                        DM.c.dream_id == dream_id,
                        DM.c.revision_id == dream["current_revision_id"],
                        DM.c.confirmation_state != "rejected",
                        D.c.lifecycle == "saved",
                        D.c.deleted_at.is_(None),
                    )
                    .order_by(M.c.type, M.c.normalized_key, M.c.id)
                )
            ).mappings().all()
            if not rows:
                return []

            link_ids = [row["link_id"] for row in rows]
            evidence_rows = (
                await c.execute(
                    select(ME.c.dream_motif_id, E.c.id.label("evidence_id"))
                    .join(
                        E,
                        and_(
                            E.c.user_id == ME.c.user_id,
                            E.c.id == ME.c.evidence_id,
                        ),
                    )
                    .where(
                        ME.c.user_id == user_id,
                        ME.c.dream_motif_id.in_(link_ids),
                        E.c.user_id == user_id,
                        E.c.dream_id == dream_id,
                        E.c.revision_id == dream["current_revision_id"],
                    )
                    .order_by(E.c.start, E.c.id)
                )
            ).mappings().all()
            evidence_by_link = {}
            for row in evidence_rows:
                evidence_by_link.setdefault(row["dream_motif_id"], []).append(
                    row["evidence_id"]
                )

            motif_ids = [row["motif_id"] for row in rows]
            associations = (
                await c.execute(
                    select(
                        ASSOC.c.id,
                        ASSOC.c.motif_id,
                        ASSOC.c.meaning,
                        ASSOC.c.source_dream_id,
                        ASSOC.c.version,
                    )
                    .where(
                        ASSOC.c.user_id == user_id,
                        ASSOC.c.motif_id.in_(motif_ids),
                        ASSOC.c.status == "confirmed",
                        ASSOC.c.enabled.is_(True),
                        ASSOC.c.deleted_at.is_(None),
                    )
                    .order_by(ASSOC.c.created_at, ASSOC.c.id)
                )
            ).mappings().all()
            associations_by_motif = {}
            for row in associations:
                associations_by_motif.setdefault(row["motif_id"], []).append(
                    AssociationRead(
                        id=row["id"],
                        meaning=row["meaning"],
                        source_dream_id=row["source_dream_id"],
                        version=row["version"],
                    )
                )

            return [
                MotifResponse(
                    id=row["motif_id"],
                    type=row["type"],
                    canonical_label=row["canonical_label"],
                    normalized_key=row["normalized_key"],
                    dream_id=row["dream_id"],
                    revision_id=row["revision_id"],
                    dream_date=row["dream_date"],
                    confirmation_state=row["confirmation_state"],
                    origin=row["origin"],
                    evidence_ids=evidence_by_link.get(row["link_id"], []),
                    personal_associations=associations_by_motif.get(row["motif_id"], []),
                )
                for row in rows
            ]

    async def get_motif(self, user_id: UUID, motif_id: UUID) -> MotifDetail:
        async with self.db.tenant_transaction(user_id) as c:
            await active_account(c, user_id)
            motif = (
                await c.execute(
                    select(M).where(M.c.user_id == user_id, M.c.id == motif_id)
                )
            ).mappings().one_or_none()
            if motif is None:
                raise APIError("not_found", "Motif not found", 404)
            rows = (
                await c.execute(
                    select(
                        DM.c.id.label("link_id"),
                        DM.c.dream_id,
                        DM.c.revision_id,
                        DM.c.origin,
                        DM.c.confirmation_state,
                        D.c.title,
                        D.c.dream_date,
                    )
                    .join(
                        D,
                        and_(
                            D.c.user_id == DM.c.user_id,
                            D.c.id == DM.c.dream_id,
                            D.c.current_revision_id == DM.c.revision_id,
                        ),
                    )
                    .where(
                        DM.c.user_id == user_id,
                        DM.c.motif_id == motif_id,
                        DM.c.confirmation_state != "rejected",
                        D.c.lifecycle == "saved",
                        D.c.deleted_at.is_(None),
                    )
                    .order_by(D.c.dream_date, D.c.created_at, D.c.id, DM.c.id)
                    .limit(MAX_MOTIF_ROWS + 1)
                )
            ).mappings().all()
            if len(rows) > MAX_MOTIF_ROWS:
                raise APIError("result_too_large", "Motif has too many active mentions", 422)
            evidence_by_link = await self._motif_evidence(c, user_id, rows)
            association_rows = (
                await c.execute(
                    select(ASSOC)
                    .where(
                        ASSOC.c.user_id == user_id,
                        ASSOC.c.motif_id == motif_id,
                        ASSOC.c.status == "confirmed",
                        ASSOC.c.enabled.is_(True),
                        ASSOC.c.deleted_at.is_(None),
                    )
                    .order_by(ASSOC.c.created_at, ASSOC.c.id)
                )
            ).mappings().all()
            return MotifDetail(
                id=motif["id"],
                type=motif["type"],
                canonical_label=motif["canonical_label"],
                normalized_key=motif["normalized_key"],
                mentions=[
                    MotifMentionRead(
                        dream_id=row["dream_id"],
                        revision_id=row["revision_id"],
                        title=row["title"],
                        dream_date=row["dream_date"],
                        confirmation_state=row["confirmation_state"],
                        origin=row["origin"],
                        evidence=evidence_by_link.get(row["link_id"], []),
                    )
                    for row in rows
                ],
                personal_associations=[
                    AssociationRead(
                        id=row["id"],
                        meaning=row["meaning"],
                        source_dream_id=row["source_dream_id"],
                        version=row["version"],
                    )
                    for row in association_rows
                ],
            )

    @staticmethod
    async def _motif_evidence(c, user_id, motif_rows):
        if not motif_rows:
            return {}
        link_ids = [row["link_id"] for row in motif_rows]
        rows = (
            await c.execute(
                select(
                    ME.c.dream_motif_id,
                    E.c.id.label("evidence_id"),
                    E.c.dream_id,
                    E.c.revision_id,
                    E.c.start,
                    E.c.end,
                    E.c.exact_quote,
                    E.c.content_hash,
                    R.c.text,
                    R.c.content_hash.label("revision_hash"),
                )
                .join(
                    E,
                    and_(E.c.user_id == ME.c.user_id, E.c.id == ME.c.evidence_id),
                )
                .join(
                    DM,
                    and_(
                        DM.c.user_id == ME.c.user_id,
                        DM.c.id == ME.c.dream_motif_id,
                        DM.c.dream_id == E.c.dream_id,
                        DM.c.revision_id == E.c.revision_id,
                    ),
                )
                .join(
                    R,
                    and_(
                        R.c.user_id == E.c.user_id,
                        R.c.dream_id == E.c.dream_id,
                        R.c.id == E.c.revision_id,
                    ),
                )
                .where(
                    ME.c.user_id == user_id,
                    ME.c.dream_motif_id.in_(link_ids),
                    E.c.user_id == user_id,
                    R.c.user_id == user_id,
                )
                .order_by(ME.c.dream_motif_id, E.c.start, E.c.id)
            )
        ).mappings().all()
        result = {}
        for row in rows:
            if row["content_hash"] != row["revision_hash"]:
                continue
            citation = Citation(
                dream_id=row["dream_id"],
                revision_id=row["revision_id"],
                source_hash=row["content_hash"],
                start=row["start"],
                end=row["end"],
                quote=row["exact_quote"],
            )
            try:
                verify_citation(
                    citation,
                    dream_id=row["dream_id"],
                    revision_id=row["revision_id"],
                    source_hash=row["revision_hash"],
                    text=row["text"],
                )
            except OutputInvalid:
                continue
            result.setdefault(row["dream_motif_id"], []).append(
                MotifEvidenceRead(
                    evidence_id=row["evidence_id"],
                    dream_id=row["dream_id"],
                    revision_id=row["revision_id"],
                    start=row["start"],
                    end=row["end"],
                    quote=row["exact_quote"],
                    content_hash=row["content_hash"],
                )
            )
        return result

    async def list_storylines(self, user_id: UUID) -> list[StorylineListItem]:
        async with self.db.tenant_transaction(user_id) as c:
            await active_account(c, user_id)
            motifs = (
                await c.execute(
                    select(M.c.id, M.c.canonical_label)
                    .join(
                        DM,
                        and_(
                            DM.c.user_id == M.c.user_id,
                            DM.c.motif_id == M.c.id,
                            DM.c.confirmation_state == "confirmed",
                        ),
                    )
                    .join(
                        D,
                        and_(
                            D.c.user_id == DM.c.user_id,
                            D.c.id == DM.c.dream_id,
                            D.c.current_revision_id == DM.c.revision_id,
                        ),
                    )
                    .where(
                        M.c.user_id == user_id,
                        D.c.lifecycle == "saved",
                        D.c.deleted_at.is_(None),
                    )
                    .distinct()
                    .order_by(M.c.id)
                )
            ).mappings().all()
            for motif in motifs:
                await c.execute(
                    pg_insert(ST)
                    .values(
                        user_id=user_id,
                        motif_id=motif["id"],
                        title=motif["canonical_label"],
                        status="stale",
                        source_signature="",
                    )
                    .on_conflict_do_nothing(index_elements=[ST.c.user_id, ST.c.motif_id])
                )
            rows = (
                await c.execute(
                    select(
                        ST.c.id,
                        ST.c.motif_id,
                        ST.c.title,
                        ST.c.status,
                        ST.c.source_signature,
                        func.count(SE.c.dream_id).label("entry_count"),
                    )
                    .join(M, and_(M.c.user_id == ST.c.user_id, M.c.id == ST.c.motif_id))
                    .outerjoin(SE, and_(SE.c.user_id == ST.c.user_id, SE.c.storyline_id == ST.c.id))
                    .where(ST.c.user_id == user_id)
                    .group_by(
                        ST.c.id,
                        ST.c.motif_id,
                        ST.c.title,
                        ST.c.status,
                        ST.c.source_signature,
                        M.c.canonical_label,
                    )
                    .order_by(M.c.canonical_label, ST.c.id)
                    .limit(MAX_STORYLINES + 1)
                )
            ).mappings().all()
            if len(rows) > MAX_STORYLINES:
                raise APIError("result_too_large", "Too many storylines to list", 422)
            return [
                StorylineListItem(
                    **{
                        **row,
                        "entry_count": 0 if row["status"] == "stale" else row["entry_count"],
                    }
                )
                for row in rows
            ]

    async def get_storyline(self, user_id: UUID, storyline_id: UUID) -> StorylineDetail:
        async with self.db.tenant_transaction(user_id) as c:
            await active_account(c, user_id)
            storyline = (
                await c.execute(
                    select(
                        ST.c.id,
                        ST.c.motif_id,
                        ST.c.title,
                        ST.c.status,
                        ST.c.source_signature,
                        M.c.canonical_label,
                    )
                    .join(M, and_(M.c.user_id == ST.c.user_id, M.c.id == ST.c.motif_id))
                    .where(ST.c.user_id == user_id, ST.c.id == storyline_id)
                )
            ).mappings().one_or_none()
            if storyline is None:
                raise APIError("not_found", "Storyline not found", 404)
            if storyline["status"] == "stale":
                return StorylineDetail(
                    id=storyline["id"],
                    motif_id=storyline["motif_id"],
                    title=storyline["title"],
                    status="stale",
                    source_signature=storyline["source_signature"],
                    entry_count=0,
                    chapters=[],
                )
            entries = (
                await c.execute(
                    select(
                        D.c.id.label("dream_id"),
                        D.c.current_revision_id.label("revision_id"),
                        D.c.title,
                        D.c.dream_date,
                        D.c.created_at,
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
                    .join(
                        SE,
                        and_(
                            SE.c.user_id == D.c.user_id,
                            SE.c.dream_id == D.c.id,
                            SE.c.revision_id == D.c.current_revision_id,
                            SE.c.storyline_id == storyline_id,
                        ),
                    )
                    .where(
                        D.c.user_id == user_id,
                        D.c.lifecycle == "saved",
                        D.c.deleted_at.is_(None),
                        R.c.user_id == user_id,
                        SE.c.user_id == user_id,
                    )
                    .order_by(D.c.dream_date, D.c.created_at, D.c.id)
                    .limit(MAX_MOTIF_ROWS + 1)
                )
            ).mappings().all()
            if len(entries) > MAX_MOTIF_ROWS:
                raise APIError("result_too_large", "Storyline exceeds the detail limit", 422)
            evidence_by_dream = await self._storyline_evidence(
                c, user_id, storyline_id, [row["dream_id"] for row in entries]
            )
            return StorylineDetail(
                id=storyline["id"],
                motif_id=storyline["motif_id"],
                title=storyline["title"],
                status=storyline["status"],
                source_signature=storyline["source_signature"],
                entry_count=len(entries),
                chapters=[
                    StorylineChapter(
                        dream_id=row["dream_id"],
                        revision_id=row["revision_id"],
                        title=row["title"],
                        dream_date=row["dream_date"],
                        text=row["text"],
                        evidence=evidence_by_dream.get(row["dream_id"], []),
                    )
                    for row in entries
                ],
            )

    @staticmethod
    async def _storyline_evidence(c, user_id, storyline_id, dream_ids):
        if not dream_ids:
            return {}
        rows = (
            await c.execute(
                select(
                    E.c.dream_id,
                    E.c.id.label("evidence_id"),
                    E.c.revision_id,
                    E.c.start,
                    E.c.end,
                    E.c.exact_quote,
                    E.c.content_hash,
                    R.c.text,
                    R.c.content_hash.label("revision_hash"),
                )
                .join(
                    STE,
                    and_(
                        STE.c.user_id == E.c.user_id,
                        STE.c.evidence_id == E.c.id,
                        STE.c.storyline_id == storyline_id,
                    ),
                )
                .join(
                    R,
                    and_(
                        R.c.user_id == E.c.user_id,
                        R.c.dream_id == E.c.dream_id,
                        R.c.id == E.c.revision_id,
                    ),
                )
                .join(
                    D,
                    and_(
                        D.c.user_id == E.c.user_id,
                        D.c.id == E.c.dream_id,
                        D.c.current_revision_id == E.c.revision_id,
                        D.c.lifecycle == "saved",
                        D.c.deleted_at.is_(None),
                    ),
                )
                .where(
                    E.c.user_id == user_id,
                    E.c.dream_id.in_(dream_ids),
                    STE.c.user_id == user_id,
                    R.c.user_id == user_id,
                    D.c.user_id == user_id,
                )
                .order_by(E.c.dream_id, E.c.start, E.c.id)
            )
        ).mappings().all()
        result = {}
        for row in rows:
            if row["content_hash"] != row["revision_hash"]:
                continue
            citation = Citation(
                dream_id=row["dream_id"],
                revision_id=row["revision_id"],
                source_hash=row["content_hash"],
                start=row["start"],
                end=row["end"],
                quote=row["exact_quote"],
            )
            try:
                verify_citation(
                    citation,
                    dream_id=row["dream_id"],
                    revision_id=row["revision_id"],
                    source_hash=row["revision_hash"],
                    text=row["text"],
                )
            except OutputInvalid:
                continue
            result.setdefault(row["dream_id"], []).append(
                MotifEvidenceRead(
                    evidence_id=row["evidence_id"],
                    dream_id=row["dream_id"],
                    revision_id=row["revision_id"],
                    start=row["start"],
                    end=row["end"],
                    quote=row["exact_quote"],
                    content_hash=row["content_hash"],
                )
            )
        return result

    async def enqueue_storyline_refresh(self, user_id: UUID, storyline_id: UUID):
        async with self.db.tenant_transaction(user_id) as c:
            account = await active_account(c, user_id)
            storyline = (
                await c.execute(
                    select(ST)
                    .where(ST.c.user_id == user_id, ST.c.id == storyline_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if storyline is None:
                raise APIError("not_found", "Storyline not found", 404)
            archive_version = account["archive_version"]
            dedupe = f"{storyline_id}:{archive_version}"
            existing = (
                await c.execute(
                    select(J).where(
                        J.c.user_id == user_id,
                        J.c.kind == "storyline_refresh",
                        J.c.dedupe_key == dedupe,
                    )
                )
            ).mappings().one_or_none()
            if existing is not None:
                if existing["status"] in {"failed", "cancelled"}:
                    await c.execute(
                        update(J)
                        .where(
                            J.c.user_id == user_id,
                            J.c.id == existing["id"],
                            J.c.status.in_(("failed", "cancelled")),
                        )
                        .values(
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
                        )
                    )
                    return StorylineRefreshAccepted(
                        job_id=existing["id"], status="queued"
                    )
                return StorylineRefreshAccepted(job_id=existing["id"], status=existing["status"])
            job_id = uuid4()
            await c.execute(
                insert(J).values(
                    id=job_id,
                    user_id=user_id,
                    kind="storyline_refresh",
                    dedupe_key=dedupe,
                    payload={"storyline_id": str(storyline_id)},
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
                    input_versions={
                        "archive_version": archive_version,
                        "motif_id": str(storyline["motif_id"]),
                    },
                )
            )
            return StorylineRefreshAccepted(job_id=job_id, status="queued")

    async def run_storyline_refresh(self, job):
        user_id, storyline_id = job["user_id"], UUID(job["payload"]["storyline_id"])
        async with self.db.tenant_transaction(user_id) as c:
            account = await active_account(c, user_id)
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
            if current_job is None:
                raise SourceChanged
            if (
                account["archive_version"] != current_job["input_versions"]["archive_version"]
                or account["data_epoch"] != current_job["data_epoch"]
            ):
                raise SourceChanged
            storyline = (
                await c.execute(
                    select(ST)
                    .where(ST.c.user_id == user_id, ST.c.id == storyline_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if storyline is None:
                raise SourceChanged
            motif_links = (
                await c.execute(
                    select(
                        DM.c.id.label("link_id"),
                        DM.c.dream_id,
                        DM.c.revision_id,
                        D.c.dream_date,
                        D.c.created_at,
                        R.c.content_hash,
                    )
                    .join(
                        D,
                        and_(
                            D.c.user_id == DM.c.user_id,
                            D.c.id == DM.c.dream_id,
                            D.c.current_revision_id == DM.c.revision_id,
                        ),
                    )
                    .join(
                        R,
                        and_(
                            R.c.user_id == DM.c.user_id,
                            R.c.dream_id == DM.c.dream_id,
                            R.c.id == DM.c.revision_id,
                        ),
                    )
                    .where(
                        DM.c.user_id == user_id,
                        DM.c.motif_id == storyline["motif_id"],
                        DM.c.confirmation_state == "confirmed",
                        D.c.lifecycle == "saved",
                        D.c.deleted_at.is_(None),
                    )
                    .order_by(D.c.dream_date, D.c.created_at, D.c.id)
                    .limit(MAX_MOTIF_ROWS + 1)
                )
            ).mappings().all()
            if len(motif_links) > MAX_MOTIF_ROWS:
                raise APIError("result_too_large", "Storyline exceeds the rebuild limit", 422)
            entry_by_dream = {row["dream_id"]: row for row in motif_links}
            dream_ids = set(entry_by_dream)
            connections = (
                await c.execute(
                    select(
                        CONN.c.id,
                        CONN.c.dream_a_id,
                        CONN.c.dream_b_id,
                        CONN.c.basis_versions,
                    )
                    .where(
                        CONN.c.user_id == user_id,
                        CONN.c.status == "confirmed",
                        or_(
                            CONN.c.dream_a_id.in_(dream_ids) if dream_ids else False,
                            CONN.c.dream_b_id.in_(dream_ids) if dream_ids else False,
                        ),
                    )
                    .order_by(CONN.c.dream_a_id, CONN.c.dream_b_id, CONN.c.id)
                    .limit(MAX_MOTIF_ROWS + 1)
                )
            ).mappings().all()
            if len(connections) > MAX_MOTIF_ROWS:
                raise APIError("result_too_large", "Storyline exceeds the rebuild limit", 422)
            endpoint_ids = {
                endpoint
                for connection in connections
                for endpoint in (connection["dream_a_id"], connection["dream_b_id"])
            }
            current_revisions = {
                row["id"]: str(row["current_revision_id"])
                for row in (
                    await c.execute(
                        select(D.c.id, D.c.current_revision_id).where(
                            D.c.user_id == user_id,
                            D.c.id.in_(endpoint_ids) if endpoint_ids else False,
                            D.c.lifecycle == "saved",
                            D.c.deleted_at.is_(None),
                        )
                    )
                ).mappings().all()
            }
            connections = [
                connection
                for connection in connections
                if (
                    connection["basis_versions"] or {}
                ).get("dream_a_revision_id")
                == current_revisions.get(connection["dream_a_id"])
                and (connection["basis_versions"] or {}).get("dream_b_revision_id")
                == current_revisions.get(connection["dream_b_id"])
            ]
            connection_ids = set()
            changed = True
            while changed:
                changed = False
                for connection in connections:
                    endpoints = {connection["dream_a_id"], connection["dream_b_id"]}
                    if endpoints & dream_ids:
                        if not endpoints.issubset(dream_ids):
                            dream_ids.update(endpoints)
                            changed = True
                        connection_ids.add(connection["id"])
            if dream_ids:
                connected_rows = (
                    await c.execute(
                        select(
                            D.c.id.label("dream_id"),
                            D.c.current_revision_id.label("revision_id"),
                            D.c.dream_date,
                            D.c.created_at,
                            R.c.content_hash,
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
                            D.c.user_id == user_id,
                            D.c.id.in_(dream_ids),
                            D.c.lifecycle == "saved",
                            D.c.deleted_at.is_(None),
                        )
                        .limit(MAX_MOTIF_ROWS + 1)
                    )
                ).mappings().all()
                if len(connected_rows) > MAX_MOTIF_ROWS:
                    raise APIError(
                        "result_too_large", "Storyline exceeds the rebuild limit", 422
                    )
                entry_by_dream.update({row["dream_id"]: row for row in connected_rows})
                if len(entry_by_dream) > MAX_MOTIF_ROWS:
                    raise APIError(
                        "result_too_large", "Storyline exceeds the rebuild limit", 422
                    )
            all_dream_ids = set(entry_by_dream)
            evidence_rows = []
            if motif_links:
                evidence_rows.extend(
                    (
                        await c.execute(
                            select(E.c.id, E.c.dream_id, E.c.revision_id)
                            .join(
                                ME,
                                and_(
                                    ME.c.user_id == E.c.user_id,
                                    ME.c.evidence_id == E.c.id,
                                ),
                            )
                            .join(
                                D,
                                and_(
                                    D.c.user_id == E.c.user_id,
                                    D.c.id == E.c.dream_id,
                                    D.c.current_revision_id == E.c.revision_id,
                                    D.c.lifecycle == "saved",
                                    D.c.deleted_at.is_(None),
                                ),
                            )
                            .where(
                                E.c.user_id == user_id,
                                ME.c.dream_motif_id.in_([r["link_id"] for r in motif_links]),
                                E.c.dream_id.in_(all_dream_ids),
                                D.c.user_id == user_id,
                            )
                        )
                    ).mappings().all()
                )
            if connection_ids:
                evidence_rows.extend(
                    (
                        await c.execute(
                            select(E.c.id, E.c.dream_id, E.c.revision_id)
                            .join(
                                CE,
                                and_(
                                    CE.c.user_id == E.c.user_id,
                                    CE.c.evidence_id == E.c.id,
                                ),
                            )
                            .join(
                                D,
                                and_(
                                    D.c.user_id == E.c.user_id,
                                    D.c.id == E.c.dream_id,
                                    D.c.current_revision_id == E.c.revision_id,
                                    D.c.lifecycle == "saved",
                                    D.c.deleted_at.is_(None),
                                ),
                            )
                            .where(
                                CE.c.user_id == user_id,
                                CE.c.connection_id.in_(connection_ids),
                                E.c.dream_id.in_(all_dream_ids),
                                D.c.user_id == user_id,
                            )
                        )
                    ).mappings().all()
                )
            signature_items = sorted(
                (
                    str(row["dream_id"]),
                    str(row["revision_id"]),
                    row["content_hash"],
                )
                for row in entry_by_dream.values()
            )
            signature = hashlib.sha256(
                json.dumps(signature_items, separators=(",", ":")).encode()
            ).hexdigest()
            await c.execute(
                update(ST)
                .where(ST.c.user_id == user_id, ST.c.id == storyline_id)
                .values(status="active", source_signature=signature)
            )
            await c.execute(
                delete(SE).where(
                    SE.c.user_id == user_id,
                    SE.c.storyline_id == storyline_id,
                )
            )
            await c.execute(
                delete(STE).where(STE.c.user_id == user_id, STE.c.storyline_id == storyline_id)
            )
            for dream_id, row in entry_by_dream.items():
                await c.execute(
                    insert(SE).values(
                        user_id=user_id,
                        storyline_id=storyline_id,
                        dream_id=dream_id,
                        revision_id=row["revision_id"],
                    )
                )
            for evidence_id in sorted({row["id"] for row in evidence_rows}, key=str):
                await c.execute(
                    insert(STE).values(
                        user_id=user_id,
                        storyline_id=storyline_id,
                        evidence_id=evidence_id,
                    )
                )
            completed = await c.execute(
                update(J)
                .where(
                    J.c.user_id == user_id,
                    J.c.id == job["id"],
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

    async def get_graph(
        self,
        user_id: UUID,
        *,
        from_date=None,
        to_date=None,
        types: list[str] | None = None,
        limit: int = 100,
    ) -> ConstellationGraph:
        if from_date is not None and to_date is not None and from_date > to_date:
            raise APIError("invalid_date_range", "from must not follow to", 422)
        limit = min(limit, MAX_NODES)
        async with self.db.tenant_transaction(user_id) as c:
            account = await active_account(c, user_id)
            dream_query = (
                select(
                    D.c.id,
                    D.c.title,
                    D.c.dream_date,
                    D.c.current_revision_id,
                    D.c.created_at,
                    R.c.content_hash,
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
                    D.c.user_id == user_id,
                    D.c.lifecycle == "saved",
                    D.c.deleted_at.is_(None),
                    D.c.current_revision_id.is_not(None),
                )
                .order_by(D.c.dream_date.desc(), D.c.created_at.desc(), D.c.id.desc())
                .limit(limit + 1)
            )
            if from_date is not None:
                dream_query = dream_query.where(D.c.dream_date >= from_date)
            if to_date is not None:
                dream_query = dream_query.where(D.c.dream_date <= to_date)
            dream_rows = (await c.execute(dream_query)).mappings().all()
            truncated = len(dream_rows) > limit
            dream_rows = dream_rows[:limit]
            dream_by_id = {row["id"]: row for row in dream_rows}
            current_hashes = {row["id"]: row["content_hash"] for row in dream_rows}
            dream_ids = list(dream_by_id)
            nodes, edges = [], []
            node_ids = set()

            def add_node(node):
                nonlocal truncated
                if node.id in node_ids:
                    return True
                if len(nodes) >= MAX_NODES:
                    truncated = True
                    return False
                node_ids.add(node.id)
                nodes.append(node)
                return True

            def add_edge(edge):
                nonlocal truncated
                if len(edges) >= MAX_EDGES:
                    truncated = True
                    return False
                edges.append(edge)
                return True

            for row in dream_rows:
                add_node(
                    ConstellationNode(
                        id=f"dream:{row['id']}",
                        kind="dream",
                        entity_id=row["id"],
                        label=row["title"],
                        dream_date=row["dream_date"],
                    )
                )
            if not dream_ids:
                return ConstellationGraph(
                    nodes=[],
                    edges=[],
                    truncated=truncated,
                    archive_version=account["archive_version"],
                )

            motif_query = (
                select(
                    DM.c.id.label("link_id"),
                    DM.c.dream_id,
                    DM.c.revision_id,
                    DM.c.motif_id,
                    DM.c.origin,
                    DM.c.confirmation_state,
                    M.c.type,
                    M.c.canonical_label,
                    M.c.normalized_key,
                )
                .join(
                    D,
                    and_(
                        D.c.user_id == DM.c.user_id,
                        D.c.id == DM.c.dream_id,
                        D.c.current_revision_id == DM.c.revision_id,
                    ),
                )
                .join(M, and_(M.c.user_id == DM.c.user_id, M.c.id == DM.c.motif_id))
                .where(
                    DM.c.user_id == user_id,
                    DM.c.dream_id.in_(dream_ids),
                    DM.c.confirmation_state != "rejected",
                    D.c.lifecycle == "saved",
                    D.c.deleted_at.is_(None),
                )
                .order_by(DM.c.dream_id, M.c.type, M.c.normalized_key, M.c.id)
                .limit(MAX_MOTIF_ROWS + 1)
            )
            if types:
                motif_query = motif_query.where(M.c.type.in_(types))
            motif_rows = (await c.execute(motif_query)).mappings().all()
            if len(motif_rows) > MAX_MOTIF_ROWS:
                truncated = True
                motif_rows = motif_rows[:MAX_MOTIF_ROWS]

            motif_evidence = await self._motif_evidence(c, user_id, motif_rows)
            evidence_by_link = {
                link_id: [item.evidence_id for item in evidence]
                for link_id, evidence in motif_evidence.items()
            }

            motif_nodes = {}
            for row in motif_rows:
                dream_node_id = f"dream:{row['dream_id']}"
                motif_node_id = f"motif:{row['motif_id']}"
                motif_node = ConstellationNode(
                    id=motif_node_id,
                    kind="motif",
                    entity_id=row["motif_id"],
                    label=row["canonical_label"],
                    motif_type=row["type"],
                )
                if not add_node(motif_node) or dream_node_id not in node_ids:
                    truncated = True
                    continue
                motif_nodes[row["motif_id"]] = row
                add_edge(
                    ConstellationEdge(
                        id=f"edge:mention:{row['link_id']}",
                        source=dream_node_id,
                        target=motif_node_id,
                        kind="mentions",
                        status="observed",
                        evidence_ids=evidence_by_link.get(row["link_id"], []),
                    )
                )

            accepted_motif_ids = list(motif_nodes)
            if accepted_motif_ids:
                association_rows = (
                    await c.execute(
                        select(
                            ASSOC.c.id,
                            ASSOC.c.motif_id,
                            ASSOC.c.meaning,
                            ASSOC.c.source_dream_id,
                        )
                        .where(
                            ASSOC.c.user_id == user_id,
                            ASSOC.c.motif_id.in_(accepted_motif_ids),
                            ASSOC.c.status == "confirmed",
                            ASSOC.c.enabled.is_(True),
                            ASSOC.c.deleted_at.is_(None),
                        )
                        .order_by(ASSOC.c.motif_id, ASSOC.c.created_at, ASSOC.c.id)
                    )
                ).mappings().all()
                for row in association_rows:
                    association_node_id = f"association:{row['id']}"
                    if not add_node(
                        ConstellationNode(
                            id=association_node_id,
                            kind="association",
                            entity_id=row["id"],
                            label=row["meaning"],
                        )
                    ):
                        continue
                    add_edge(
                        ConstellationEdge(
                            id=f"edge:association:{row['id']}",
                            source=f"motif:{row['motif_id']}",
                            target=association_node_id,
                            kind="personal_association",
                            status="confirmed",
                        )
                    )

            current_revisions = {
                row["id"]: str(row["current_revision_id"]) for row in dream_rows
            }
            connection_rows = (
                await c.execute(
                    select(
                        CONN.c.id,
                        CONN.c.dream_a_id,
                        CONN.c.dream_b_id,
                        CONN.c.status,
                        CONN.c.basis_versions,
                    )
                    .where(
                        CONN.c.user_id == user_id,
                        CONN.c.dream_a_id.in_(dream_ids),
                        CONN.c.dream_b_id.in_(dream_ids),
                        CONN.c.status.in_(("suggested", "confirmed")),
                    )
                    .order_by(CONN.c.dream_a_id, CONN.c.dream_b_id, CONN.c.id)
                    .limit(MAX_EDGES + 1)
                )
            ).mappings().all()
            if len(connection_rows) > MAX_EDGES:
                truncated = True
                connection_rows = connection_rows[:MAX_EDGES]
            eligible_connections = []
            for row in connection_rows:
                basis = row["basis_versions"] or {}
                if (
                    basis.get("dream_a_revision_id") != current_revisions[row["dream_a_id"]]
                    or basis.get("dream_b_revision_id") != current_revisions[row["dream_b_id"]]
                    or basis.get("dream_a_source_hash") != current_hashes[row["dream_a_id"]]
                    or basis.get("dream_b_source_hash") != current_hashes[row["dream_b_id"]]
                ):
                    continue
                eligible_connections.append(row)

            connection_evidence = []
            connection_ids = [row["id"] for row in eligible_connections]
            connection_endpoints = {
                row["id"]: {row["dream_a_id"], row["dream_b_id"]}
                for row in eligible_connections
            }
            if connection_ids:
                connection_evidence = (
                    await c.execute(
                        select(
                            CE.c.connection_id,
                            E.c.id.label("evidence_id"),
                            E.c.dream_id,
                            E.c.revision_id,
                            E.c.start,
                            E.c.end,
                            E.c.exact_quote,
                            E.c.content_hash,
                            R.c.text,
                            R.c.content_hash.label("revision_hash"),
                        )
                        .join(
                            E,
                            and_(
                                E.c.user_id == CE.c.user_id,
                                E.c.id == CE.c.evidence_id,
                            ),
                        )
                        .join(
                            R,
                            and_(
                                R.c.user_id == E.c.user_id,
                                R.c.dream_id == E.c.dream_id,
                                R.c.id == E.c.revision_id,
                            ),
                        )
                        .join(
                            D,
                            and_(
                                D.c.user_id == E.c.user_id,
                                D.c.id == E.c.dream_id,
                                D.c.current_revision_id == E.c.revision_id,
                                D.c.lifecycle == "saved",
                            ),
                        )
                        .where(
                            CE.c.user_id == user_id,
                            CE.c.connection_id.in_(connection_ids),
                            E.c.user_id == user_id,
                            D.c.deleted_at.is_(None),
                            D.c.current_revision_id == E.c.revision_id,
                            E.c.dream_id.in_(dream_ids),
                        )
                        .order_by(CE.c.connection_id, E.c.dream_id, E.c.start, E.c.id)
                    )
                ).mappings().all()
            evidence_by_connection = {}
            for row in connection_evidence:
                if row["dream_id"] not in connection_endpoints[row["connection_id"]]:
                    continue
                if row["content_hash"] != row["revision_hash"]:
                    continue
                citation = Citation(
                    dream_id=row["dream_id"],
                    revision_id=row["revision_id"],
                    source_hash=row["content_hash"],
                    start=row["start"],
                    end=row["end"],
                    quote=row["exact_quote"],
                )
                try:
                    verify_citation(
                        citation,
                        dream_id=row["dream_id"],
                        revision_id=row["revision_id"],
                        source_hash=row["revision_hash"],
                        text=row["text"],
                    )
                except OutputInvalid:
                    continue
                evidence_by_connection.setdefault(row["connection_id"], []).append(
                    row["evidence_id"]
                )
            for row in eligible_connections:
                evidence_ids = evidence_by_connection.get(row["id"], [])
                if not evidence_ids:
                    continue
                add_edge(
                    ConstellationEdge(
                        id=f"edge:continuity:{row['id']}",
                        source=f"dream:{row['dream_a_id']}",
                        target=f"dream:{row['dream_b_id']}",
                        kind="continuity",
                        status=row["status"],
                        evidence_ids=evidence_ids,
                    )
                )

            return ConstellationGraph(
                nodes=nodes,
                edges=edges,
                truncated=truncated,
                archive_version=account["archive_version"],
            )
