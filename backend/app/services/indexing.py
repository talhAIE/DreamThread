"""Durable source indexing and complete-archive atomic vector-space activation."""

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.models import tables
from app.errors import APIError
from app.providers.embeddings import (
    Embeddings,
    configuration,
    digest,
    fingerprint,
    split_source,
    vector,
)
from app.services.accounts import active_account
from app.services.admission import reserve_ai, reserve_resource
from app.services.journal import owned_dream, pipeline_version
from app.workers.types import SourceChanged, WorkFailure

S, C, D, R, J = (
    tables["app." + name]
    for name in ("embedding_spaces", "embedding_chunks", "dreams", "dream_revisions", "jobs")
)


@dataclass(frozen=True)
class EmbeddingPublication:
    configuration: dict
    chunks: list


async def ensure_space(c, user_id, config):
    space = (
        (await c.execute(select(S).where(S.c.user_id == user_id, S.c.version == config["version"])))
        .mappings()
        .one_or_none()
    )
    if space and space["fingerprint"] != fingerprint(config):
        raise WorkFailure("embedding_version_conflict")
    if space:
        return space
    return (
        (
            await c.execute(
                insert(S)
                .values(
                    user_id=user_id,
                    version=config["version"],
                    model_id=config["model_id"],
                    dimensions=config["dimensions"],
                    fingerprint=fingerprint(config),
                    configuration=config,
                )
                .returning(S)
            )
        )
        .mappings()
        .one()
    )


async def activate_if_complete(c, user_id, space):
    # All writers take the account lock first, so archive changes cannot race this switch.
    indexed = exists(
        select(C.c.id).where(
            C.c.user_id == user_id,
            C.c.space_id == space["id"],
            C.c.revision_id == D.c.current_revision_id,
        )
    )
    missing = await c.scalar(
        select(D.c.id)
        .where(
            D.c.user_id == user_id,
            D.c.lifecycle == "saved",
            D.c.deleted_at.is_(None),
            ~indexed,
        )
        .limit(1)
    )
    if missing is None:
        await c.execute(
            update(S)
            .where(S.c.user_id == user_id, S.c.state == "active", S.c.id != space["id"])
            .values(state="retired")
        )
        await c.execute(update(S).where(S.c.id == space["id"]).values(state="active"))


class IndexService:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    async def enqueue(self, c, identity, account, dream, *, parent=None):
        config = configuration(self.settings)
        space = await ensure_space(c, identity.user_id, config)
        dedupe = f"{dream['current_revision_id']}:{fingerprint(config)}:{dream['version']}"
        existing = (
            (
                await c.execute(
                    select(J)
                    .where(
                        J.c.user_id == identity.user_id,
                        J.c.kind == "embedding",
                        J.c.dedupe_key == dedupe,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing and existing["status"] in {"queued", "running", "succeeded"}:
            # Old-pipeline jobs must be replaced, even if their model space is unchanged.
            if existing["input_versions"]["pipeline_version"] == pipeline_version(self.settings):
                if existing["status"] == "succeeded":
                    await activate_if_complete(c, identity.user_id, space)
                return existing
        submission = existing["input_versions"].get("submission", 0) + 1 if existing else 1
        job_id = existing["id"] if existing else uuid4()
        body = {
            "dream_id": str(dream["id"]),
            "revision_id": str(dream["current_revision_id"]),
            "space_id": str(space["id"]),
            "submission": submission,
        }
        admission_key = f"embedding-{job_id.hex}-{submission}"
        if parent is None:
            await reserve_ai(
                c, identity, self.settings, route="embedding.work", key=admission_key, body=body
            )
            keys = tables["app.idempotency_keys"]
            await c.execute(
                update(keys)
                .where(
                    keys.c.user_id == identity.user_id,
                    keys.c.route == "embedding.work",
                    keys.c.key == admission_key,
                )
                .values(result_resource_id=job_id)
            )
        # Automatic indexing is part of the already-admitted analysis workflow. It is
        # enqueued only after successful analysis, so deferred allowance was resolved.
        values = dict(
            payload={
                "dream_id": str(dream["id"]),
                "revision_id": str(dream["current_revision_id"]),
                "space_id": str(space["id"]),
            },
            input_versions={
                "pipeline_version": pipeline_version(self.settings),
                "memory_version": account["memory_version"],
                "archive_version": account["archive_version"],
                "dream_version": dream["version"],
                "submission": submission,
                "admission": "reserved",
                "admission_key": admission_key,
                "admission_body": body,
            },
            data_epoch=account["data_epoch"],
            status="queued",
            stage="queued",
            attempts=0,
            error_code=None,
            run_after=func.clock_timestamp(),
            lease_token=None,
            leased_until=None,
            deadline_at=None,
            stage_deadline_at=None,
        )
        if existing:
            statement = update(J).where(J.c.id == job_id).values(**values)
        else:
            statement = insert(J).values(
                id=job_id, user_id=identity.user_id, kind="embedding", dedupe_key=dedupe, **values
            )
        return (await c.execute(statement.returning(J))).mappings().one()

    async def request(self, identity, dream_id, expected_version, key):
        async with self.db.tenant_transaction(identity.user_id) as c:
            account = await active_account(c, identity.user_id)
            dream = await owned_dream(c, identity.user_id, dream_id)
            admission = await reserve_resource(
                c,
                identity,
                self.settings,
                route="dream.index",
                key=key,
                body={
                    "dream_id": str(dream_id),
                    "expected_version": expected_version,
                    "embedding_version": self.settings.embedding_version,
                },
            )
            if admission.replayed:
                job = (
                    (await c.execute(select(J).where(J.c.id == admission.resource_id)))
                    .mappings()
                    .one()
                )
            else:
                if dream["version"] != expected_version or dream["lifecycle"] != "saved":
                    raise APIError("version_conflict", "Reload the current saved dream", 409)
                try:
                    job = await self.enqueue(c, identity, account, dream)
                except WorkFailure as error:
                    raise APIError(
                        error.code, "Embedding configuration needs attention", 503
                    ) from None
                keys = tables["app.idempotency_keys"]
                await c.execute(
                    update(keys)
                    .where(
                        keys.c.user_id == identity.user_id,
                        keys.c.route == "dream.index",
                        keys.c.key == key,
                    )
                    .values(result_resource_id=job["id"])
                )
            return {"dream_id": dream_id, "job_id": job["id"], "status": job["status"]}

    async def publish(self, c, execution, publication, dream):
        config = configuration(self.settings)
        if publication.configuration != config:
            raise SourceChanged
        space = await ensure_space(c, execution.job["user_id"], config)
        if str(space["id"]) != execution.job["payload"].get("space_id"):
            raise SourceChanged
        text = execution.original["text"]
        expected = split_source(text, config)
        if len(expected) != len(publication.chunks):
            raise WorkFailure("invalid_output")
        values = []
        for index, (chunk, data) in enumerate(zip(expected, publication.chunks, strict=True)):
            if data["input_hash"] != digest(config["document_prefix"] + chunk.text) or (
                data["query_input_hash"] != digest(config["query_prefix"] + chunk.text)
            ):
                raise WorkFailure("invalid_output")
            values.append(
                dict(
                    user_id=execution.job["user_id"],
                    space_id=space["id"],
                    dream_id=dream["id"],
                    revision_id=dream["current_revision_id"],
                    chunk_index=index,
                    start=chunk.start,
                    end=chunk.end,
                    content_hash=execution.primary.content_hash,
                    input_hash=data["input_hash"],
                    query_input_hash=data["query_input_hash"],
                    dimensions=config["dimensions"],
                    embedding=vector(data["embedding"], config["dimensions"]),
                    query_embedding=vector(data["query_embedding"], config["dimensions"]),
                )
            )
        await c.execute(delete(C).where(C.c.space_id == space["id"], C.c.dream_id == dream["id"]))
        await c.execute(insert(C), values)
        await activate_if_complete(c, execution.job["user_id"], space)


class EmbeddingHandler:
    def __init__(self, settings, provider=None):
        self.settings = settings
        self.provider = provider or Embeddings(settings)

    async def run(self, execution):
        config = configuration(self.settings)
        text = execution.original["text"]
        if digest(text) != execution.primary.content_hash:
            raise SourceChanged
        chunks = split_source(text, config)
        await execution.set_stage("embedding")
        docs = [config["document_prefix"] + item.text for item in chunks]
        queries = [config["query_prefix"] + item.text for item in chunks]
        embeddings = await self.provider.encode(
            docs, task="embedding_document", record=execution.record_call
        )
        query_embeddings = (
            embeddings
            if docs == queries
            else await self.provider.encode(
                queries, task="embedding_query", record=execution.record_call
            )
        )
        if len(embeddings) != len(chunks) or len(query_embeddings) != len(chunks):
            raise WorkFailure("invalid_output")
        return EmbeddingPublication(
            config,
            [
                dict(
                    embedding=embedding,
                    query_embedding=query,
                    input_hash=digest(doc),
                    query_input_hash=digest(query_input),
                )
                for doc, query_input, embedding, query in zip(
                    docs, queries, embeddings, query_embeddings, strict=True
                )
            ],
        )
