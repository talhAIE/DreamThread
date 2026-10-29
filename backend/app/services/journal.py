"""Durable journal writes with no external AI calls or long-held inference locks."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, delete, insert, or_, select, update

from app.db.models import tables
from app.errors import APIError
from app.schemas.dreams import (
    AnalysisRequestResult,
    AnalysisResult,
    DreamCreate,
    DreamDetail,
    DreamPage,
    DreamSummary,
    Revision,
)
from app.schemas.pagination import decode_cursor, encode_cursor
from app.services.accounts import active_account
from app.services.admission import reserve_ai, reserve_resource
from app.services.invalidation import invalidate_history, invalidate_sources

D, R, J = tables["app.dreams"], tables["app.dream_revisions"], tables["app.jobs"]
A, KEYS = tables["app.accounts"], tables["app.idempotency_keys"]
ANALYSES = tables["app.analyses"]


def pipeline_version(settings):
    values = [
        settings.prompt_version,
        settings.embedding_version,
        settings.nebius_extraction_model,
        settings.nebius_reasoning_model,
        settings.nebius_embedding_model,
    ]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()[:32]


async def owned_dream(connection, user_id, dream_id):
    row = (
        (
            await connection.execute(
                select(D)
                .where(
                    D.c.user_id == user_id,
                    D.c.id == dream_id,
                    D.c.deleted_at.is_(None),
                )
                .with_for_update()
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise APIError("not_found", "Dream not found", 404)
    return row


async def current_source(connection, dream):
    return (
        (
            await connection.execute(
                select(R).where(
                    R.c.user_id == dream["user_id"],
                    R.c.dream_id == dream["id"],
                    R.c.id == dream["current_revision_id"],
                )
            )
        )
        .mappings()
        .one()
    )


def summary(dream, revision):
    return DreamSummary(
        **{
            key: dream[key]
            for key in (
                "id",
                "title",
                "dream_date",
                "version",
                "lifecycle",
                "input_mode",
                "created_at",
                "updated_at",
            )
        },
        revision_id=revision["id"],
        revision=revision["revision"],
        reported_emotions=revision["reported_emotions"],
        analysis_state="not_requested"
        if dream["lifecycle"] == "draft"
        else dream["analysis_state"],
    )


class JournalService:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.pipeline = pipeline_version(settings)

    async def _current_analysis(self, c, dream):
        if dream["lifecycle"] != "saved" or dream["analysis_state"] != "ready":
            return None
        memory_version = await c.scalar(
            select(A.c.memory_version).where(
                A.c.id == dream["user_id"],
            )
        )
        analysis = (
            (
                await c.execute(
                    select(ANALYSES).where(
                        ANALYSES.c.user_id == dream["user_id"],
                        ANALYSES.c.dream_id == dream["id"],
                        ANALYSES.c.revision_id == dream["current_revision_id"],
                        ANALYSES.c.pipeline_version == self.pipeline,
                        ANALYSES.c.memory_version == memory_version,
                        ANALYSES.c.state == "ready",
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if analysis is None:
            return None
        evidence, links, sources = (
            tables["app.evidence"],
            tables["app.analysis_evidence"],
            D.alias("source_dream"),
        )
        stale = await c.scalar(
            select(evidence.c.id)
            .join(
                links,
                and_(
                    links.c.user_id == evidence.c.user_id,
                    links.c.evidence_id == evidence.c.id,
                ),
            )
            .join(
                sources,
                and_(sources.c.user_id == evidence.c.user_id, sources.c.id == evidence.c.dream_id),
            )
            .where(
                links.c.user_id == dream["user_id"],
                links.c.analysis_id == analysis["id"],
                or_(
                    sources.c.deleted_at.is_not(None),
                    sources.c.current_revision_id != evidence.c.revision_id,
                ),
            )
            .limit(1)
        )
        return None if stale else analysis

    async def detail(self, c, dream):
        revision = await current_source(c, dream)
        if dream["analysis_state"] == "ready" and not await self._current_analysis(c, dream):
            dream = {**dream, "analysis_state": "stale"}
        job_id = await c.scalar(
            select(J.c.id)
            .where(
                J.c.user_id == dream["user_id"],
                J.c.kind == "analysis",
                J.c.payload["dream_id"].astext == str(dream["id"]),
                J.c.payload["revision_id"].astext == str(revision["id"]),
            )
            .order_by(J.c.created_at.desc(), J.c.id.desc())
            .limit(1)
        )
        return DreamDetail(
            **summary(dream, revision).model_dump(),
            text=revision["text"],
            content_hash=revision["content_hash"],
            analysis_job_id=job_id,
        )

    async def _revision(self, c, user_id, dream_id, number, text, emotions):
        revision_id = uuid4()
        await c.execute(
            insert(R).values(
                id=revision_id,
                user_id=user_id,
                dream_id=dream_id,
                revision=number,
                text=text,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                reported_emotions=emotions,
            )
        )
        return revision_id

    async def _queue(self, c, identity, account, dream, *, automatic):
        revision_id = dream["current_revision_id"]
        dedupe = f"{revision_id}:{self.pipeline}:{account['memory_version']}"
        existing = (
            (
                await c.execute(
                    select(J)
                    .where(
                        J.c.user_id == identity.user_id,
                        J.c.kind == "analysis",
                        J.c.dedupe_key == dedupe,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing:
            if existing["status"] in {"queued", "running"} and (
                existing["input_versions"].get("dream_version") == dream["version"]
            ):
                return existing
            if existing["status"] == "succeeded" and await self._current_analysis(c, dream):
                return existing
        superseded = update(J).where(
            J.c.user_id == identity.user_id,
            J.c.kind == "analysis",
            J.c.payload["dream_id"].astext == str(dream["id"]),
            J.c.status.in_(["queued", "running"]),
        )
        if existing:
            superseded = superseded.where(J.c.id != existing["id"])
        await c.execute(
            superseded.values(
                status="cancelled", stage="superseded", lease_token=None, leased_until=None
            )
        )
        generation = existing["input_versions"].get("submission", 0) + 1 if existing else 1
        planned = existing["id"] if existing else uuid4()
        work_body = {
            "dream_id": str(dream["id"]),
            "revision_id": str(revision_id),
            "pipeline_version": self.pipeline,
            "memory_version": account["memory_version"],
            "submission": generation,
        }
        deferred, run_after = False, datetime.now(UTC)
        admission_key = f"job-{planned.hex}-{generation}"
        try:
            async with c.begin_nested():
                admission = await reserve_ai(
                    c,
                    identity,
                    self.settings,
                    route="analysis.work",
                    key=admission_key,
                    body=work_body,
                )
        except APIError as error:
            if not automatic or error.status != 429:
                raise
            deferred = True
            run_after += timedelta(seconds=int(error.headers["Retry-After"]) + 1)
        job_id = existing["id"] if existing else (planned if deferred else admission.resource_id)
        if existing and not deferred:
            await c.execute(
                update(KEYS)
                .where(
                    KEYS.c.user_id == identity.user_id,
                    KEYS.c.route == "analysis.work",
                    KEYS.c.key == admission_key,
                )
                .values(result_resource_id=job_id)
            )
        versions = {
            "revision_id": str(revision_id),
            "pipeline_version": self.pipeline,
            "memory_version": account["memory_version"],
            "dream_version": dream["version"],
            "archive_version": account["archive_version"],
            "submission": generation,
            "admission": "deferred" if deferred else "reserved",
            "admission_key": admission_key,
            "admission_body": work_body,
        }
        values = dict(
            payload={"dream_id": str(dream["id"]), "revision_id": str(revision_id)},
            input_versions=versions,
            status="queued",
            stage="waiting_quota" if deferred else "queued",
            run_after=run_after,
            data_epoch=account["data_epoch"],
            attempts=0,
            error_code=None,
            lease_token=None,
            leased_until=None,
            heartbeat_at=None,
        )
        if existing:
            result = await c.execute(
                update(J).where(J.c.id == job_id).values(**values).returning(J)
            )
        else:
            result = await c.execute(
                insert(J)
                .values(
                    id=job_id,
                    user_id=identity.user_id,
                    kind="analysis",
                    dedupe_key=dedupe,
                    **values,
                )
                .returning(J)
            )
        await c.execute(
            update(ANALYSES)
            .where(
                ANALYSES.c.user_id == identity.user_id,
                ANALYSES.c.dream_id == dream["id"],
            )
            .values(state="stale")
        )
        await c.execute(update(D).where(D.c.id == dream["id"]).values(analysis_state="queued"))
        return result.mappings().one()

    async def _archive_changed(self, c, user_id, *, deleted=False):
        values = {"archive_version": A.c.archive_version + 1}
        if deleted:
            values.update(data_epoch=A.c.data_epoch + 1, memory_version=A.c.memory_version + 1)
        return (
            (await c.execute(update(A).where(A.c.id == user_id).values(**values).returning(A)))
            .mappings()
            .one()
        )

    async def create(self, identity, payload, key):
        async with self.db.tenant_transaction(identity.user_id) as c:
            await active_account(c, identity.user_id)
            admission = await reserve_resource(
                c,
                identity,
                self.settings,
                route="dream.create",
                key=key,
                body=payload.model_dump(mode="json"),
            )
            if admission.replayed:
                return await self.detail(
                    c, await owned_dream(c, identity.user_id, admission.resource_id)
                )
            dream_id = admission.resource_id
            await c.execute(
                insert(D).values(
                    id=dream_id,
                    user_id=identity.user_id,
                    title=payload.title,
                    dream_date=payload.dream_date,
                    lifecycle=payload.lifecycle,
                )
            )
            revision_id = await self._revision(
                c, identity.user_id, dream_id, 1, payload.text, payload.reported_emotions
            )
            await c.execute(
                update(D).where(D.c.id == dream_id).values(current_revision_id=revision_id)
            )
            account = await self._archive_changed(c, identity.user_id)
            await invalidate_history(c, identity.user_id)
            dream = await owned_dream(c, identity.user_id, dream_id)
            if payload.lifecycle == "saved":
                await self._queue(c, identity, account, dream, automatic=True)
            return await self.detail(c, dream)

    async def get(self, identity, dream_id):
        async with self.db.tenant_transaction(identity.user_id) as c:
            await active_account(c, identity.user_id)
            return await self.detail(c, await owned_dream(c, identity.user_id, dream_id))

    async def list(self, identity, params):
        async with self.db.tenant_transaction(identity.user_id) as c:
            await active_account(c, identity.user_id)
            columns = [D, R.c.id.label("revision_id"), R.c.revision, R.c.reported_emotions]
            query = (
                select(*columns)
                .join(
                    R,
                    and_(
                        R.c.user_id == D.c.user_id,
                        R.c.dream_id == D.c.id,
                        R.c.id == D.c.current_revision_id,
                    ),
                )
                .where(D.c.user_id == identity.user_id, D.c.deleted_at.is_(None))
            )
            if params.from_date:
                query = query.where(D.c.dream_date >= params.from_date)
            if params.to_date:
                query = query.where(D.c.dream_date <= params.to_date)
            if params.emotion:
                query = query.where(R.c.reported_emotions.contains([params.emotion]))
            if params.lifecycle:
                query = query.where(D.c.lifecycle == params.lifecycle)
            if params.keyword:
                escaped = (
                    params.keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                )
                query = query.where(
                    or_(
                        D.c.title.ilike(f"%{escaped}%", escape="\\"),
                        R.c.text.ilike(f"%{escaped}%", escape="\\"),
                    )
                )
            if params.cursor:
                day, last_id = decode_cursor(params.cursor)
                query = query.where(
                    or_(D.c.dream_date < day, and_(D.c.dream_date == day, D.c.id < last_id))
                )
            rows = (
                (
                    await c.execute(
                        query.order_by(D.c.dream_date.desc(), D.c.id.desc()).limit(params.limit + 1)
                    )
                )
                .mappings()
                .all()
            )
            more, rows = len(rows) > params.limit, rows[: params.limit]
            items = [
                summary(
                    row,
                    {
                        "id": row["revision_id"],
                        "revision": row["revision"],
                        "reported_emotions": row["reported_emotions"],
                    },
                )
                for row in rows
            ]
            for item, row in zip(items, rows, strict=True):
                if item.analysis_state == "ready" and not await self._current_analysis(c, row):
                    item.analysis_state = "stale"
            cursor = encode_cursor(items[-1].dream_date, items[-1].id) if more else None
            return DreamPage(items=items, next_cursor=cursor)

    async def patch(self, identity, dream_id, payload):
        async with self.db.tenant_transaction(identity.user_id) as c:
            await active_account(c, identity.user_id)
            dream = await owned_dream(c, identity.user_id, dream_id)
            if dream["version"] != payload.expected_version:
                raise APIError("version_conflict", "Dream changed; reload and retry", 409)
            revision = await current_source(c, dream)
            merged = dict(
                title=dream["title"],
                dream_date=dream["dream_date"],
                lifecycle=dream["lifecycle"],
                text=revision["text"],
                reported_emotions=revision["reported_emotions"],
            )
            changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
            if dream["lifecycle"] == "saved" and changes.get("lifecycle") == "draft":
                raise APIError("invalid_transition", "Saved dreams cannot become drafts", 422)
            merged.update(changes)
            try:
                validated = DreamCreate(**merged)
            except ValueError:
                raise APIError("invalid_dream", "Saved dreams need text", 422) from None
            if all(
                merged[key]
                == (revision[key] if key in {"text", "reported_emotions"} else dream[key])
                for key in changes
            ):
                return await self.detail(c, dream)
            await invalidate_sources(c, identity.user_id)
            revision_id = revision["id"]
            if (
                validated.text != revision["text"]
                or validated.reported_emotions != revision["reported_emotions"]
            ):
                revision_id = await self._revision(
                    c,
                    identity.user_id,
                    dream_id,
                    revision["revision"] + 1,
                    validated.text,
                    validated.reported_emotions,
                )
            await c.execute(
                update(D)
                .where(D.c.id == dream_id)
                .values(
                    title=validated.title,
                    dream_date=validated.dream_date,
                    lifecycle=validated.lifecycle,
                    current_revision_id=revision_id,
                    version=D.c.version + 1,
                )
            )
            await c.execute(
                delete(tables["app.dream_embeddings"]).where(
                    tables["app.dream_embeddings"].c.dream_id == dream_id,
                )
            )
            account = await self._archive_changed(c, identity.user_id)
            updated = await owned_dream(c, identity.user_id, dream_id)
            if validated.lifecycle == "saved":
                await self._queue(c, identity, account, updated, automatic=True)
                updated = await owned_dream(c, identity.user_id, dream_id)
            return await self.detail(c, updated)

    async def revision(self, identity, dream_id, revision_id):
        async with self.db.tenant_transaction(identity.user_id) as c:
            await active_account(c, identity.user_id)
            dream = await owned_dream(c, identity.user_id, dream_id)
            row = (
                (
                    await c.execute(
                        select(R).where(
                            R.c.user_id == identity.user_id,
                            R.c.dream_id == dream_id,
                            R.c.id == revision_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise APIError("not_found", "Revision not found", 404)
            return Revision(
                **{k: row[k] for k in Revision.model_fields if k != "is_current"},
                is_current=revision_id == dream["current_revision_id"],
            )

    async def analyze(self, identity, dream_id, payload, key):
        async with self.db.tenant_transaction(identity.user_id) as c:
            account = await active_account(c, identity.user_id)
            dream = await owned_dream(c, identity.user_id, dream_id)
            if dream["version"] != payload.expected_version:
                raise APIError("version_conflict", "Dream changed; reload and retry", 409)
            if dream["lifecycle"] != "saved":
                raise APIError("invalid_transition", "Save the draft before analysis", 422)
            admission = await reserve_resource(
                c,
                identity,
                self.settings,
                route="dream.analyze",
                key=key,
                body={"dream_id": str(dream_id), **payload.model_dump()},
            )
            if admission.replayed:
                job = (
                    (
                        await c.execute(
                            select(J).where(
                                J.c.user_id == identity.user_id,
                                J.c.id == admission.resource_id,
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if job is None:
                    raise APIError("not_found", "Analysis job not found", 404)
            else:
                job = await self._queue(c, identity, account, dream, automatic=False)
                await c.execute(
                    update(KEYS)
                    .where(
                        KEYS.c.user_id == identity.user_id,
                        KEYS.c.route == "dream.analyze",
                        KEYS.c.key == key,
                    )
                    .values(result_resource_id=job["id"])
                )
            return AnalysisRequestResult(
                dream_id=dream_id,
                revision_id=dream["current_revision_id"],
                job_id=job["id"],
                status=job["status"],
            )

    async def analysis(self, identity, dream_id):
        async with self.db.tenant_transaction(identity.user_id) as c:
            await active_account(c, identity.user_id)
            dream = await owned_dream(c, identity.user_id, dream_id)
            detail = await self.detail(c, dream)
            result = AnalysisResult(
                dream_id=dream_id,
                revision_id=dream["current_revision_id"],
                status=detail.analysis_state,
                analysis_job_id=detail.analysis_job_id,
            )
            if dream["lifecycle"] == "draft":
                return result
            analysis = await self._current_analysis(c, dream)
            if analysis:
                return result.model_copy(
                    update=dict(
                        status="ready",
                        analysis_id=analysis["id"],
                        extraction=analysis["extraction"],
                        reflections=analysis["reflections"],
                        followup=analysis["followup"],
                    )
                )
            return result

    async def remove(self, identity, dream_id):
        async with self.db.tenant_transaction(identity.user_id) as c:
            await active_account(c, identity.user_id)
            await owned_dream(c, identity.user_id, dream_id)
            await invalidate_sources(c, identity.user_id)
            await self._archive_changed(c, identity.user_id, deleted=True)
            await c.execute(delete(D).where(D.c.user_id == identity.user_id, D.c.id == dream_id))
