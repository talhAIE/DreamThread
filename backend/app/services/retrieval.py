"""Tenant-first exact cosine + lexical/motif nomination, without identity inference."""

import math
import re
from collections import Counter

from sqlalchemy import and_, func, select, true

from app.db.models import tables
from app.providers.embeddings import configuration, fingerprint
from app.schemas.retrieval import CandidateResult
from app.services.accounts import active_account
from app.services.journal import current_source, owned_dream
from app.workers.types import WorkFailure

D, R, C, S, J, M, DM, A = (
    tables["app." + name]
    for name in (
        "dreams",
        "dream_revisions",
        "embedding_chunks",
        "embedding_spaces",
        "jobs",
        "motifs",
        "dream_motifs",
        "associations",
    )
)
STOP = set(
    "a an and are as at be been but by for from had has have i in is it its me my of on or "
    "that the their them then there they this to was we were with you your dream dreamed "
    "saw went felt very some something through into".split()
)


def terms(value):
    return {
        term
        for term in re.findall(r"[^\W_]+", value.casefold())
        if len(term) >= 2 and term not in STOP
    }


def lexical_rank(query, rows):
    query_terms = terms(query)
    documents = {row["id"]: terms(row["text"]) for row in rows}
    counts = Counter(term for document in documents.values() for term in document)
    return {
        key: sum(
            1 + math.log((len(rows) + 1) / (counts[term] + 1)) for term in document & query_terms
        )
        for key, document in documents.items()
    }


def snippet(text, query):
    matches = [text.casefold().find(term) for term in terms(query)]
    start = max(0, min((p for p in matches if p >= 0), default=0) - 60)
    return text[start : start + 240]


class RetrievalService:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    async def candidates(self, identity, dream_id):
        user = identity.user_id
        async with self.db.tenant_transaction(user) as c:
            # Serialize source/memory snapshot with publication and mutation; no network calls.
            account = await active_account(c, user)
            dream = await owned_dream(c, user, dream_id)
            source = await current_source(c, dream)
            rows = (
                (
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
                            D.c.user_id == user, D.c.lifecycle == "saved", D.c.deleted_at.is_(None)
                        )
                        .order_by(D.c.id)
                    )
                )
                .mappings()
                .all()
            )
            others = [row for row in rows if row["id"] != dream_id]
            lexical = lexical_rank(source["text"], others)
            scores = dict(lexical)
            reasons = {row["id"]: (["text_match"] if lexical[row["id"]] else []) for row in others}
            association_ids = {row["id"]: [] for row in others}
            # Current, non-rejected motif links only; no label-based identity merges are written.
            motifs = (
                (
                    await c.execute(
                        select(
                            DM.c.dream_id,
                            DM.c.motif_id,
                            DM.c.confirmation_state,
                            M.c.canonical_label,
                        )
                        .join(
                            M,
                            and_(
                                M.c.user_id == DM.c.user_id,
                                M.c.id == DM.c.motif_id,
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
                            DM.c.user_id == user,
                            DM.c.confirmation_state != "rejected",
                            D.c.lifecycle == "saved",
                            D.c.deleted_at.is_(None),
                        )
                    )
                )
                .mappings()
                .all()
            )
            primary_motifs = {row["motif_id"] for row in motifs if row["dream_id"] == dream_id}
            associations = (
                (
                    await c.execute(
                        select(A).where(
                            A.c.user_id == user,
                            A.c.status == "confirmed",
                            A.c.enabled.is_(True),
                            A.c.deleted_at.is_(None),
                        )
                    )
                )
                .mappings()
                .all()
            )
            matched_associations = {}
            for item in associations:
                if item["motif_id"] in primary_motifs or terms(item["meaning"]) & terms(
                    source["text"]
                ):
                    matched_associations.setdefault(item["motif_id"], []).append(item["id"])
            for row in motifs:
                key = row["dream_id"]
                if key not in scores:
                    continue
                if row["motif_id"] in primary_motifs or (
                    row["canonical_label"].casefold() in source["text"].casefold()
                ):
                    confirmed = row["confirmation_state"] == "confirmed"
                    scores[key] += 6 if confirmed else 2
                    reasons[key].append("confirmed_motif" if confirmed else "motif_mention")
                ids = matched_associations.get(row["motif_id"], [])
                if ids:
                    scores[key] += 8
                    reasons[key].append("confirmed_association")
                    association_ids[key].extend(ids)
            space = (
                (await c.execute(select(S).where(S.c.user_id == user, S.c.state == "active")))
                .mappings()
                .one_or_none()
            )
            degraded, indexed, semantic = [], 0, []
            try:
                target = configuration(self.settings)
                if space is None or space["fingerprint"] != fingerprint(target):
                    degraded.append("embedding_space_building")
            except WorkFailure as error:
                degraded.append(error.code)
            if space:
                # Materialize the tenant + space + current-source filter BEFORE cosine to ensure
                # even the SQL planner cannot compare vectors from different dimensions/spaces.
                valid = (
                    select(C.c.dream_id, C.c.revision_id, C.c.embedding, C.c.query_embedding)
                    .join(
                        D,
                        and_(
                            D.c.user_id == C.c.user_id,
                            D.c.id == C.c.dream_id,
                            D.c.current_revision_id == C.c.revision_id,
                        ),
                    )
                    .join(
                        R,
                        and_(
                            R.c.user_id == C.c.user_id,
                            R.c.id == C.c.revision_id,
                            R.c.content_hash == C.c.content_hash,
                        ),
                    )
                    .where(
                        C.c.user_id == user,
                        C.c.space_id == space["id"],
                        C.c.dimensions == space["dimensions"],
                        D.c.lifecycle == "saved",
                        D.c.deleted_at.is_(None),
                    )
                    .cte("valid_chunks")
                    .prefix_with("MATERIALIZED")
                )
                indexed = await c.scalar(select(func.count(func.distinct(valid.c.dream_id))))
                query = (
                    select(valid.c.query_embedding)
                    .where(valid.c.dream_id == dream_id)
                    .cte("query_chunks")
                    .prefix_with("MATERIALIZED")
                )
                similarity = 1 - valid.c.embedding.cosine_distance(query.c.query_embedding)
                semantic = (
                    (
                        await c.execute(
                            select(valid.c.dream_id, func.max(similarity).label("similarity"))
                            .select_from(valid.join(query, true()))
                            .where(valid.c.dream_id != dream_id)
                            .group_by(valid.c.dream_id)
                            .order_by(func.max(similarity).desc(), valid.c.dream_id)
                            .limit(8)
                        )
                    )
                    .mappings()
                    .all()
                )
                if await c.scalar(select(func.count()).select_from(query)) == 0:
                    degraded.append("query_embedding_missing")
            else:
                degraded.append("embedding_space_unavailable")
            if indexed < len(rows):
                degraded.append("embedding_coverage_incomplete")
            ranks = {
                row["dream_id"]: (index + 1, float(row["similarity"]))
                for index, row in enumerate(semantic)
            }
            lexical_order = sorted(
                (key for key in scores if scores[key] > 0), key=lambda key: (-scores[key], str(key))
            )[:12]
            pool = set(lexical_order) | set(ranks)
            # Preserve strong personal/motif specificity alongside bounded semantic nomination.
            fused = {key: scores[key] + (2 / ranks[key][0] if key in ranks else 0) for key in pool}
            ordered = sorted(pool, key=lambda key: (-fused[key], str(key)))[:12]
            by_id = {row["id"]: row for row in others}
            job = (
                (
                    await c.execute(
                        select(J.c.id, J.c.status)
                        .where(
                            J.c.user_id == user,
                            J.c.kind == "embedding",
                            J.c.payload["dream_id"].astext == str(dream_id),
                            J.c.payload["revision_id"].astext == str(source["id"]),
                        )
                        .order_by(J.c.created_at.desc(), J.c.id.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            return CandidateResult(
                dream_id=dream_id,
                revision_id=source["id"],
                archive_version=account["archive_version"],
                memory_version=account["memory_version"],
                degraded=bool(degraded),
                degraded_reasons=list(dict.fromkeys(degraded)),
                active_embedding_version=space["version"] if space else None,
                target_embedding_version=self.settings.embedding_version,
                indexed_dreams=indexed,
                saved_dreams=len(rows),
                embedding_job_id=job["id"] if job else None,
                embedding_job_status=job["status"] if job else None,
                candidates=[
                    dict(
                        dream_id=key,
                        revision_id=by_id[key]["revision_id"],
                        source_hash=by_id[key]["content_hash"],
                        dream_date=by_id[key]["dream_date"],
                        title=by_id[key]["title"],
                        snippet=snippet(by_id[key]["text"], source["text"]),
                        rank=index + 1,
                        score=fused[key],
                        semantic_rank=ranks.get(key, (None, None))[0],
                        cosine_similarity=ranks.get(key, (None, None))[1],
                        reasons=list(
                            dict.fromkeys(
                                reasons[key] + (["semantic_similarity"] if key in ranks else [])
                            )
                        ),
                        association_ids=list(dict.fromkeys(association_ids[key])),
                    )
                    for index, key in enumerate(ordered)
                ],
            )
