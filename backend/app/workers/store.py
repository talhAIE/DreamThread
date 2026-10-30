"""Tenant-private job operations. Every publication is fenced in one short transaction."""

import random
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.auth.identity import Identity
from app.db.models import tables
from app.errors import APIError
from app.services.admission import reserve_ai
from app.services.evidence import OutputInvalid, verify_citation
from app.services.indexing import EmbeddingPublication, IndexService
from app.services.journal import pipeline_version
from app.services.personal_context import load_context, publish_mentions
from app.workers.types import (
    LeaseLost,
    SourceChanged,
    SourceRef,
    WorkFailure,
    json_payload,
    snapshot_hash,
)

A, D, R, J = (tables["app." + name] for name in ("accounts", "dreams", "dream_revisions", "jobs"))
ART, ANALYSES = tables["app.job_artifacts"], tables["app.analyses"]
STAGE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")


class Store:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    @asynccontextmanager
    async def locked(self, job, sources=(), *, leased=True):
        async with self.db.tenant_transaction(job["user_id"]) as c:
            account = (
                (await c.execute(select(A).where(A.c.id == job["user_id"]).with_for_update()))
                .mappings()
                .one_or_none()
            )
            if account is None:
                raise LeaseLost
            ids = {s.dream_id for s in sources}
            try:
                ids.add(UUID(job["payload"]["dream_id"]))
            except (KeyError, ValueError, TypeError):
                pass
            dreams = (
                {
                    row["id"]: row
                    for row in (
                        await c.execute(
                            select(D).where(D.c.id.in_(ids)).order_by(D.c.id).with_for_update()
                        )
                    ).mappings()
                }
                if ids
                else {}
            )
            current = (
                (await c.execute(select(J).where(J.c.id == job["id"]).with_for_update()))
                .mappings()
                .one_or_none()
            )
            now = await c.scalar(select(func.clock_timestamp()))
            if current is None:
                raise LeaseLost
            if leased and (
                current["status"] != "running"
                or current["lease_token"] != job["lease_token"]
                or current["leased_until"] is None
                or current["leased_until"] <= now
                or current["deadline_at"] is None
                or current["deadline_at"] <= now
            ):
                raise LeaseLost
            yield c, account, dreams, current, now

    def guard(self, account, dreams, current, sources=(), *, archive_bound=False):
        versions = current["input_versions"]
        try:
            primary = dreams[UUID(current["payload"]["dream_id"])]
            valid = (
                account["deletion_state"] == "active"
                and account["data_epoch"] == current["data_epoch"]
                and account["memory_version"] == versions["memory_version"]
                and versions["pipeline_version"] == pipeline_version(self.settings)
                and primary["deleted_at"] is None
                and primary["lifecycle"] == "saved"
                and str(primary["current_revision_id"]) == current["payload"]["revision_id"]
                and primary["version"] == versions["dream_version"]
            )
            if archive_bound:
                valid = valid and account["archive_version"] == versions["archive_version"]
            for source in sources:
                row = dreams[source.dream_id]
                valid = valid and (
                    row["deleted_at"] is None
                    and row["lifecycle"] == "saved"
                    and row["current_revision_id"] == source.revision_id
                    and row["version"] == source.dream_version
                )
            if not valid:
                raise SourceChanged
        except (KeyError, ValueError, TypeError):
            raise SourceChanged from None
        return primary

    async def check_hashes(self, c, sources):
        for source in sources:
            actual = await c.scalar(
                select(R.c.content_hash).where(
                    R.c.dream_id == source.dream_id,
                    R.c.id == source.revision_id,
                )
            )
            if actual != source.content_hash:
                raise SourceChanged

    async def prepare(self, job):
        async with self.locked(job) as (c, account, dreams, current, now):
            dream = self.guard(account, dreams, current)
            revision = await c.execute(
                select(R).where(R.c.id == dream["current_revision_id"], R.c.dream_id == dream["id"])
            )
            revision = revision.mappings().one()
            if current["input_versions"].get("admission") == "deferred":
                versions = dict(current["input_versions"])
                try:
                    async with c.begin_nested():
                        await reserve_ai(
                            c,
                            Identity(job["user_id"], account["is_anonymous"]),
                            self.settings,
                            route="analysis.work",
                            key=versions["admission_key"],
                            body=versions["admission_body"],
                        )
                        keys = tables["app.idempotency_keys"]
                        await c.execute(
                            update(keys)
                            .where(
                                keys.c.route == "analysis.work",
                                keys.c.key == versions["admission_key"],
                            )
                            .values(result_resource_id=job["id"])
                        )
                except APIError as error:
                    if error.status != 429:
                        raise
                    # Waiting for allowance is not a failed processing attempt.
                    await c.execute(
                        update(J)
                        .where(J.c.id == job["id"])
                        .values(
                            status="queued",
                            stage="waiting_quota",
                            attempts=J.c.attempts - 1,
                            run_after=now
                            + timedelta(seconds=int(error.headers["Retry-After"]) + 1),
                            lease_token=None,
                            leased_until=None,
                            deadline_at=None,
                            stage_deadline_at=None,
                        )
                    )
                    return None
                versions["admission"] = "reserved"
                current = dict(current, input_versions=versions)
                await c.execute(
                    update(J).where(J.c.id == job["id"]).values(input_versions=versions)
                )
            if current["input_versions"].get("admission") != "reserved":
                raise SourceChanged
            versions = {**current["input_versions"], "archive_version": account["archive_version"]}
            current = dict(current, input_versions=versions)
            await c.execute(
                update(J)
                .where(J.c.id == job["id"])
                .values(
                    input_versions=versions,
                    stage="preparing",
                    stage_deadline_at=now
                    + timedelta(seconds=self.settings.job_stage_timeout_seconds),
                )
            )
            ref = SourceRef(dream["id"], revision["id"], revision["content_hash"], dream["version"])
            return Execution(self, dict(current), ref, dict(revision))

    async def source(self, execution, dream_id):
        # Lock source identities in deterministic order together with the primary source.
        async with self.locked(
            execution.job, execution.sources + (SourceRef(dream_id, dream_id, "", 0),)
        ) as (c, account, dreams, current, _):
            self.guard(account, dreams, current, execution.sources)
            dream = dreams.get(dream_id)
            if dream is None or dream["deleted_at"] is not None or dream["lifecycle"] != "saved":
                raise SourceChanged
            revision = (
                (
                    await c.execute(
                        select(R).where(
                            R.c.id == dream["current_revision_id"], R.c.dream_id == dream_id
                        )
                    )
                )
                .mappings()
                .one()
            )
            ref = SourceRef(dream_id, revision["id"], revision["content_hash"], dream["version"])
            execution.add_sources((ref,))
            return dict(revision)

    async def stage(self, execution, stage):
        if stage not in {"extracting", "embedding", "retrieving", "comparing", "verifying"}:
            raise ValueError("Unsupported processing stage")
        async with self.locked(execution.job, execution.sources) as (
            c,
            account,
            dreams,
            current,
            now,
        ):
            dream = self.guard(account, dreams, current, execution.sources)
            if current["stage_deadline_at"] and current["stage_deadline_at"] <= now:
                raise WorkFailure("stage_timeout", retryable=True)
            await c.execute(
                update(J)
                .where(J.c.id == current["id"])
                .values(
                    stage=stage,
                    stage_deadline_at=now
                    + timedelta(seconds=self.settings.job_stage_timeout_seconds),
                )
            )
            if current["kind"] == "analysis":
                await c.execute(update(D).where(D.c.id == dream["id"]).values(analysis_state=stage))

    async def save_checkpoint(self, execution, stage, value, archive_bound):
        if not STAGE.fullmatch(stage):
            raise ValueError("Invalid checkpoint stage")
        value = json_payload(value)
        async with self.locked(execution.job, execution.sources) as (
            c,
            account,
            dreams,
            current,
            _,
        ):
            self.guard(account, dreams, current, execution.sources, archive_bound=archive_bound)
            await self.check_hashes(c, execution.sources)
            values = dict(
                snapshot_hash=snapshot_hash(current, execution.sources, archive_bound),
                sources=[s.json() for s in execution.sources],
                archive_bound=archive_bound,
                output=value,
            )
            await c.execute(
                insert(ART)
                .values(user_id=current["user_id"], job_id=current["id"], stage=stage, **values)
                .on_conflict_do_update(
                    index_elements=[ART.c.user_id, ART.c.job_id, ART.c.stage],
                    set_=values,
                )
            )
            execution.archive_bound |= archive_bound

    async def load_checkpoint(self, execution, stage):
        artifact = None
        # Source IDs in the artifact are private metadata; load before the guarded transaction.
        async with self.db.tenant_transaction(execution.job["user_id"]) as c:
            artifact = (
                (
                    await c.execute(
                        select(ART).where(ART.c.job_id == execution.job["id"], ART.c.stage == stage)
                    )
                )
                .mappings()
                .one_or_none()
            )
        if artifact is None:
            return None
        refs = tuple(SourceRef.parse(s) for s in artifact["sources"])
        async with self.locked(execution.job, execution.sources + refs) as (
            c,
            account,
            dreams,
            current,
            _,
        ):
            self.guard(account, dreams, current, execution.sources)
            try:
                self.guard(account, dreams, current, refs, archive_bound=artifact["archive_bound"])
                await self.check_hashes(c, refs)
                if artifact["snapshot_hash"] != snapshot_hash(
                    current, refs, artifact["archive_bound"]
                ):
                    raise SourceChanged
            except SourceChanged:
                await c.execute(delete(ART).where(ART.c.id == artifact["id"]))
                return None
            execution.add_sources(refs)
            execution.archive_bound |= artifact["archive_bound"]
            return artifact["output"]

    async def complete(self, execution, publication):
        if isinstance(publication, EmbeddingPublication):
            return await self.complete_embedding(execution, publication)
        refs = execution.sources + publication.sources
        values = json_payload(publication.analysis.model_dump(mode="json"))
        async with self.locked(execution.job, refs) as (c, account, dreams, current, now):
            if current["stage_deadline_at"] and current["stage_deadline_at"] <= now:
                raise WorkFailure("stage_timeout", retryable=True)
            dream = self.guard(
                account,
                dreams,
                current,
                refs,
                archive_bound=execution.archive_bound or publication.archive_bound,
            )
            await self.check_hashes(c, refs)
            records = publication.evidence
            if len({item.id for item in records}) != len(records):
                raise WorkFailure("invalid_output")
            if publication.require_evidence:
                ids = {str(item.id) for item in records}
                claims = values["extraction"].get("observations", []) + values["reflections"]
                if (
                    not records
                    or not claims
                    or any(
                        not claim.get("evidence_ids")
                        or not set(claim["evidence_ids"]).issubset(ids)
                        for claim in claims
                    )
                ):
                    raise WorkFailure("invalid_output")
            evidence_table, links = tables["app.evidence"], tables["app.analysis_evidence"]
            for item in records:
                ref = next(
                    (
                        s
                        for s in refs
                        if s.dream_id == item.dream_id and s.revision_id == item.revision_id
                    ),
                    None,
                )
                if ref is None:
                    raise WorkFailure("invalid_output")
                source_text = await c.scalar(
                    select(R.c.text).where(
                        R.c.id == ref.revision_id,
                        R.c.dream_id == ref.dream_id,
                    )
                )
                try:
                    verified = verify_citation(
                        item,
                        dream_id=ref.dream_id,
                        revision_id=ref.revision_id,
                        source_hash=ref.content_hash,
                        text=source_text,
                    )
                except OutputInvalid:
                    raise WorkFailure("invalid_output") from None
                await c.execute(
                    insert(evidence_table).values(
                        id=item.id,
                        user_id=current["user_id"],
                        dream_id=verified.dream_id,
                        revision_id=verified.revision_id,
                        start=verified.start,
                        end=verified.end,
                        exact_quote=verified.quote,
                        content_hash=verified.source_hash,
                    )
                )
            values.update(state="ready")
            if publication.require_evidence:
                await publish_mentions(c, current["user_id"], dream, values["extraction"])
            analysis_id = await c.scalar(
                insert(ANALYSES)
                .values(
                    user_id=current["user_id"],
                    dream_id=dream["id"],
                    revision_id=dream["current_revision_id"],
                    pipeline_version=current["input_versions"]["pipeline_version"],
                    memory_version=account["memory_version"],
                    **values,
                )
                .on_conflict_do_update(
                    index_elements=[
                        ANALYSES.c.user_id,
                        ANALYSES.c.revision_id,
                        ANALYSES.c.pipeline_version,
                        ANALYSES.c.memory_version,
                    ],
                    set_=values,
                )
                .returning(ANALYSES.c.id)
            )
            await c.execute(delete(links).where(links.c.analysis_id == analysis_id))
            for item in records:
                await c.execute(
                    insert(links).values(
                        user_id=current["user_id"], analysis_id=analysis_id, evidence_id=item.id
                    )
                )
            await c.execute(update(D).where(D.c.id == dream["id"]).values(analysis_state="ready"))
            if self.settings.nebius_embedding_model:
                # Configuration errors must not roll back a valid single-entry analysis.
                try:
                    async with c.begin_nested():
                        await IndexService(self.db, self.settings).enqueue(
                            c,
                            Identity(current["user_id"], account["is_anonymous"]),
                            account,
                            dream,
                            parent=current,
                        )
                except WorkFailure:
                    # Candidates disclose configuration status; explicit indexing can retry.
                    pass
            # Success and publication are one commit; no pre-commit acknowledgement.
            await c.execute(
                update(J)
                .where(J.c.id == current["id"])
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

    async def complete_embedding(self, execution, publication):
        async with self.locked(execution.job, execution.sources) as (
            c,
            account,
            dreams,
            current,
            now,
        ):
            if current["kind"] != "embedding":
                raise WorkFailure("invalid_output")
            if current["stage_deadline_at"] and current["stage_deadline_at"] <= now:
                raise WorkFailure("stage_timeout", retryable=True)
            dream = self.guard(account, dreams, current, execution.sources)
            await self.check_hashes(c, execution.sources)
            await IndexService(self.db, self.settings).publish(c, execution, publication, dream)
            # Continuity follows indexing and owns a separate fenced job. It must never
            # overwrite the validated single-dream analysis with an empty publication.
            preferences = tables["app.preferences"]
            auto = await c.scalar(select(preferences.c.auto_connections).where(
                preferences.c.user_id == execution.job["user_id"]
            ))
            if auto:
                await c.execute(insert(J).values(
                    user_id=execution.job["user_id"], kind="continuity",
                    dedupe_key=f"{dream['current_revision_id']}:{account['archive_version']}:"
                               f"{account['memory_version']}:{pipeline_version(self.settings)}",
                    payload={"dream_id": str(dream["id"]),
                             "revision_id": str(dream["current_revision_id"])},
                    data_epoch=account["data_epoch"], input_versions={
                        "archive_version": account["archive_version"],
                        "memory_version": account["memory_version"],
                    },
                ).on_conflict_do_nothing(index_elements=[J.c.user_id, J.c.kind, J.c.dedupe_key]))
            await c.execute(
                update(J)
                .where(J.c.id == current["id"])
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

    async def fail(self, job, failure, sources=()):
        async with self.locked(job, sources) as (c, account, dreams, current, now):
            dream = self.guard(account, dreams, current, sources)
            retry = failure.retryable and current["attempts"] < self.settings.job_max_attempts
            delay = min(
                self.settings.job_retry_max_seconds,
                self.settings.job_retry_base_seconds * 2 ** (current["attempts"] - 1),
            )
            delay = max(failure.retry_after, delay + random.uniform(0, delay * 0.25))
            await c.execute(
                update(J)
                .where(J.c.id == current["id"])
                .values(
                    status="queued" if retry else "failed",
                    stage="retry_wait" if retry else "failed",
                    error_code=failure.code,
                    run_after=now + timedelta(seconds=delay),
                    lease_token=None,
                    leased_until=None,
                    deadline_at=None,
                    stage_deadline_at=None,
                )
            )
            if current["kind"] == "analysis":
                await c.execute(
                    update(D)
                    .where(D.c.id == dream["id"])
                    .values(analysis_state="queued" if retry else "failed")
                )

    async def cancel(self, job):
        try:
            async with self.locked(job) as (c, _, dreams, current, _):
                await c.execute(
                    update(J)
                    .where(J.c.id == current["id"])
                    .values(
                        status="cancelled",
                        stage="superseded",
                        error_code="source_changed",
                        lease_token=None,
                        leased_until=None,
                        deadline_at=None,
                        stage_deadline_at=None,
                    )
                )
                target = dreams.get(UUID(current["payload"]["dream_id"]))
                if (
                    current["kind"] == "analysis"
                    and target
                    and str(target["current_revision_id"]) == current["payload"]["revision_id"]
                ):
                    await c.execute(
                        update(D).where(D.c.id == target["id"]).values(analysis_state="stale")
                    )
        except LeaseLost:
            pass

    async def maintain(self, candidate):
        async with self.locked(candidate, leased=False) as (c, account, dreams, current, now):
            expired = current["status"] == "running" and (
                current["leased_until"] is None
                or current["leased_until"] <= now
                or current["deadline_at"] is None
                or current["deadline_at"] <= now
            )
            exhausted = current["attempts"] >= self.settings.job_max_attempts
            if exhausted and (expired or current["status"] == "queued"):
                try:
                    primary = self.guard(account, dreams, current)
                except SourceChanged:
                    primary = None
                predicates = [
                    J.c.id == current["id"],
                    J.c.user_id == current["user_id"],
                    J.c.attempts == current["attempts"],
                    J.c.attempts >= self.settings.job_max_attempts,
                ]
                if current["status"] == "running":
                    predicates.extend(
                        (
                            J.c.status == "running",
                            or_(
                                J.c.leased_until.is_(None),
                                J.c.leased_until <= func.clock_timestamp(),
                                J.c.deadline_at.is_(None),
                                J.c.deadline_at <= func.clock_timestamp(),
                            ),
                        )
                    )
                else:
                    predicates.append(J.c.status == "queued")
                result = await c.execute(
                    update(J)
                    .where(*predicates)
                    .values(
                        status="failed" if primary else "cancelled",
                        stage="failed" if primary else "superseded",
                        error_code="lease_expired" if primary else "source_changed",
                        lease_token=None,
                        leased_until=None,
                        deadline_at=None,
                        stage_deadline_at=None,
                    )
                )
                if result.rowcount == 1 and primary and current["kind"] == "analysis":
                    await c.execute(
                        update(D).where(D.c.id == primary["id"]).values(analysis_state="failed")
                    )
            cutoff = now - timedelta(days=self.settings.checkpoint_retention_days)
            if (
                current["status"] in {"succeeded", "failed", "cancelled"}
                and current["updated_at"] < cutoff
            ):
                cleaned = await c.execute(
                    update(J)
                    .where(
                        J.c.id == current["id"],
                        J.c.user_id == current["user_id"],
                        J.c.status.in_(("succeeded", "failed", "cancelled")),
                        J.c.stage != "retention_cleaned",
                        J.c.updated_at < cutoff,
                    )
                    .values(stage="retention_cleaned")
                )
                if cleaned.rowcount == 1:
                    await c.execute(
                        delete(ART).where(
                            ART.c.user_id == current["user_id"],
                            ART.c.job_id == current["id"],
                        )
                    )
            keys = tables["app.idempotency_keys"]
            await c.execute(
                delete(keys).where(
                    keys.c.user_id == current["user_id"],
                    keys.c.expires_at < now,
                )
            )
            usage = tables["app.ai_usage"]
            await c.execute(
                delete(usage).where(
                    usage.c.user_id == current["user_id"],
                    usage.c.period < cutoff.date(),
                )
            )


class Execution:
    def __init__(self, store, job, primary, original):
        self.store, self.job, self.primary, self.original = store, job, primary, original
        self.sources = (primary,)
        self.archive_bound = False

    def add_sources(self, sources):
        self.sources = tuple(dict.fromkeys(self.sources + tuple(sources)))

    async def get_source(self, dream_id):
        return await self.store.source(self, UUID(str(dream_id)))

    async def personal_context(self):
        async with self.store.locked(self.job, self.sources) as (c, account, dreams, current, _):
            self.store.guard(account, dreams, current, self.sources)
            return await load_context(
                c, current["user_id"], self.primary.dream_id, self.primary.revision_id
            )

    async def set_stage(self, stage):
        await self.store.stage(self, stage)

    async def checkpoint(self, stage, output, *, archive_bound=False):
        await self.store.save_checkpoint(self, stage, output, archive_bound)

    async def load_checkpoint(self, stage):
        return await self.store.load_checkpoint(self, stage)

    async def record_call(self, **metadata):
        async with self.store.locked(self.job, self.sources) as (c, account, dreams, current, _):
            self.store.guard(account, dreams, current, self.sources)
            return await c.scalar(
                insert(tables["app.model_runs"])
                .values(
                    user_id=current["user_id"],
                    job_id=current["id"],
                    prompt_version=self.store.settings.prompt_version,
                    trace_stage=metadata["task"],
                    **metadata,
                )
                .returning(tables["app.model_runs"].c.id)
            )

    async def mark_call(self, run_id, status):
        if status not in {"validated", "invalid_output"}:
            raise ValueError("Invalid model-run status")
        async with self.store.locked(self.job, self.sources) as (c, account, dreams, current, _):
            self.store.guard(account, dreams, current, self.sources)
            runs = tables["app.model_runs"]
            await c.execute(
                update(runs)
                .where(runs.c.id == run_id, runs.c.job_id == current["id"])
                .values(status=status)
            )
