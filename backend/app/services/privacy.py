"""Private exports and journal-wide deletion workflows."""

import json
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import case, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.auth.identity import Identity
from app.db.models import tables
from app.errors import APIError
from app.schemas.privacy import (
    ExportAccepted,
    ExportFormat,
    ExportStatus,
    JournalDeleteAccepted,
)
from app.services.accounts import active_account
from app.services.admission import reserve_resource
from app.workers.types import LeaseLost

A = tables["app.accounts"]
D = tables["app.dreams"]
R = tables["app.dream_revisions"]
J = tables["app.jobs"]
EXPORTS = tables["app.exports"]
ART = tables["app.job_artifacts"]
ASSETS = tables["app.audio_assets"]
CLEANUP = tables["app.cleanup_tasks"]

EXPORT_TTL = timedelta(minutes=15)
EXPORT_TTL_SECONDS = int(EXPORT_TTL.total_seconds())


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"Unsupported export value: {type(value).__name__}")


def _records(rows, fields=None):
    return [
        {key: row[key] for key in (fields or row.keys())}
        for row in rows
    ]


class PrivacyService:
    def __init__(self, db, settings=None):
        self.db, self.settings = db, settings

    async def create_export(self, identity: Identity, payload, idempotency_key: str):
        async with self.db.tenant_transaction(identity.user_id) as c:
            account = await active_account(c, identity.user_id)
            admission = await reserve_resource(
                c,
                identity,
                self.settings,
                route="export.create",
                key=idempotency_key,
                body=payload.model_dump(mode="json"),
            )
            if admission.replayed:
                row = (
                    await c.execute(
                        select(EXPORTS).where(
                            EXPORTS.c.user_id == identity.user_id,
                            EXPORTS.c.id == admission.resource_id,
                        )
                    )
                ).mappings().one_or_none()
                if row is None:
                    raise APIError("not_found", "Export not found", 404)
                return ExportAccepted(id=row["id"], job_id=row["job_id"], status=row["status"])

            export_id, job_id = admission.resource_id, uuid4()
            await c.execute(
                insert(J).values(
                    id=job_id,
                    user_id=identity.user_id,
                    kind="export",
                    dedupe_key=f"export:{export_id}",
                    payload={"export_id": str(export_id)},
                    data_epoch=account["data_epoch"],
                    input_versions={
                        "archive_version": account["archive_version"],
                        "format": payload.format,
                    },
                )
            )
            now = await c.scalar(select(func.clock_timestamp()))
            await c.execute(
                insert(EXPORTS).values(
                    id=export_id,
                    user_id=identity.user_id,
                    format=payload.format,
                    job_id=job_id,
                    archive_version=account["archive_version"],
                    data_epoch=account["data_epoch"],
                    status="pending",
                    expires_at=now + EXPORT_TTL,
                )
            )
            return ExportAccepted(id=export_id, job_id=job_id, status="pending")

    async def export_status(self, user_id: UUID, export_id: UUID):
        async with self.db.tenant_transaction(user_id) as c:
            account = await active_account(c, user_id)
            row = (
                await c.execute(
                    select(EXPORTS).where(
                        EXPORTS.c.user_id == user_id,
                        EXPORTS.c.id == export_id,
                    ).with_for_update()
                )
            ).mappings().one_or_none()
            if row is None:
                raise APIError("not_found", "Export not found", 404)
            now = await c.scalar(select(func.clock_timestamp()))
            if (
                row["status"] == "ready"
                and (
                    row["expires_at"] <= now
                    or row["data_epoch"] != account["data_epoch"]
                    or row["archive_version"] != account["archive_version"]
                )
            ):
                status = (
                    "expired"
                    if row["expires_at"] <= now
                    else "stale"
                )
                await c.execute(
                    update(EXPORTS)
                    .where(EXPORTS.c.user_id == user_id, EXPORTS.c.id == export_id)
                    .values(status=status, content=None)
                )
                row = {**row, "status": status, "content": None}
            if row["status"] == "ready" and row["expires_at"] <= now:
                await c.execute(
                    update(EXPORTS)
                    .where(EXPORTS.c.user_id == user_id, EXPORTS.c.id == export_id)
                    .values(status="expired", content=None)
                )
                row = {**row, "status": "expired", "content": None}
            return ExportStatus(
                id=row["id"],
                format=row["format"],
                status=row["status"],
                created_at=row["created_at"],
                expires_at=row["expires_at"] if row["status"] == "ready" else None,
                download_url=f"/exports/{export_id}/download"
                if row["status"] == "ready"
                else None,
            )

    async def download_export(self, user_id: UUID, export_id: UUID):
        response = None
        async with self.db.tenant_transaction(user_id) as c:
            account = await active_account(c, user_id)
            row = (
                await c.execute(
                    select(EXPORTS).where(
                        EXPORTS.c.user_id == user_id,
                        EXPORTS.c.id == export_id,
                    ).with_for_update()
                )
            ).mappings().one_or_none()
            if row is None:
                raise APIError("not_found", "Export not found", 404)
            now = await c.scalar(select(func.clock_timestamp()))
            if (
                row["status"] == "ready"
                and row["expires_at"] <= now
            ):
                await c.execute(
                    update(EXPORTS)
                    .where(EXPORTS.c.user_id == user_id, EXPORTS.c.id == export_id)
                    .values(status="expired", content=None)
                )
                response = (row["format"], None, "expired")
            elif row["status"] != "ready" or row["content"] is None:
                raise APIError("export_unavailable", "Export is not available", 409)
            elif (
                account["deletion_state"] != "active"
                or account["data_epoch"] != row["data_epoch"]
                or account["archive_version"] != row["archive_version"]
            ):
                await c.execute(
                    update(EXPORTS)
                    .where(EXPORTS.c.user_id == user_id, EXPORTS.c.id == export_id)
                    .values(status="stale", content=None)
                )
                response = (row["format"], None, "stale")
            else:
                response = (row["format"], row["content"], "ready")
        return response

    async def _snapshot(self, c, user_id: UUID, account):
        dreams = (
            await c.execute(
                select(
                    D.c.id,
                    D.c.title,
                    D.c.dream_date,
                    D.c.current_revision_id,
                    D.c.version,
                    D.c.lifecycle,
                    D.c.input_mode,
                    D.c.analysis_state,
                    D.c.created_at,
                    D.c.updated_at,
                )
                .where(D.c.user_id == user_id, D.c.deleted_at.is_(None))
                .order_by(D.c.dream_date, D.c.created_at, D.c.id)
            )
        ).mappings().all()
        dream_ids = [row["id"] for row in dreams]
        revisions = (
            await c.execute(
                select(
                    R.c.id,
                    R.c.dream_id,
                    R.c.revision,
                    R.c.text,
                    R.c.content_hash,
                    R.c.reported_emotions,
                    R.c.recorded_at,
                    R.c.created_at,
                    R.c.updated_at,
                )
                .where(R.c.user_id == user_id, R.c.dream_id.in_(dream_ids) if dream_ids else False)
                .order_by(R.c.dream_id, R.c.revision, R.c.id)
            )
        ).mappings().all()
        revisions_by_dream = {}
        for row in revisions:
            revisions_by_dream.setdefault(row["dream_id"], []).append(
                {key: row[key] for key in row.keys() if key != "dream_id"}
            )

        dreams_export = []
        for row in dreams:
            item = {key: row[key] for key in row.keys()}
            item["revisions"] = revisions_by_dream.get(row["id"], [])
            dreams_export.append(item)

        motifs = tables["app.motifs"]
        associations = tables["app.associations"]
        decisions = tables["app.connection_decisions"]
        storylines = tables["app.storylines"]
        entries = tables["app.storyline_entries"]
        conversations = tables["app.conversations"]
        messages = tables["app.messages"]
        motif_rows = (
            await c.execute(
                select(
                    motifs.c.id,
                    motifs.c.type,
                    motifs.c.canonical_label,
                    motifs.c.normalized_key,
                )
                .where(motifs.c.user_id == user_id)
                .order_by(motifs.c.type, motifs.c.normalized_key, motifs.c.id)
            )
        ).mappings().all()
        association_rows = (
            await c.execute(
                select(
                    associations.c.id,
                    associations.c.motif_id,
                    associations.c.meaning,
                    associations.c.status,
                    associations.c.enabled,
                    associations.c.version,
                    associations.c.source_dream_id,
                    associations.c.origin,
                    associations.c.created_at,
                    associations.c.updated_at,
                )
                .where(associations.c.user_id == user_id, associations.c.deleted_at.is_(None))
                .order_by(associations.c.created_at, associations.c.id)
            )
        ).mappings().all()
        decision_rows = (
            await c.execute(
                select(
                    decisions.c.id,
                    decisions.c.dream_a_id,
                    decisions.c.dream_b_id,
                    decisions.c.relation_key,
                    decisions.c.decision,
                    decisions.c.version,
                    decisions.c.created_at,
                    decisions.c.updated_at,
                )
                .where(decisions.c.user_id == user_id)
                .order_by(
                    decisions.c.dream_a_id,
                    decisions.c.dream_b_id,
                    decisions.c.relation_key,
                )
            )
        ).mappings().all()
        storyline_rows = (
            await c.execute(
                select(
                    storylines.c.id,
                    storylines.c.motif_id,
                    storylines.c.title,
                    storylines.c.status,
                    storylines.c.source_signature,
                    storylines.c.created_at,
                    storylines.c.updated_at,
                )
                .where(storylines.c.user_id == user_id)
                .order_by(storylines.c.title, storylines.c.id)
            )
        ).mappings().all()
        entry_rows = (
            await c.execute(
                select(
                    entries.c.storyline_id,
                    entries.c.dream_id,
                    entries.c.revision_id,
                    entries.c.created_at,
                )
                .where(entries.c.user_id == user_id)
                .order_by(entries.c.storyline_id, entries.c.dream_id)
            )
        ).mappings().all()
        entries_by_story = {}
        for row in entry_rows:
            entries_by_story.setdefault(row["storyline_id"], []).append(
                {key: row[key] for key in row.keys() if key != "storyline_id"}
            )
        stories_export = []
        for row in storyline_rows:
            item = {key: row[key] for key in row.keys()}
            item["entries"] = entries_by_story.get(row["id"], [])
            stories_export.append(item)

        conversation_rows = (
            await c.execute(
                select(
                    conversations.c.id,
                    conversations.c.title,
                    conversations.c.created_at,
                    conversations.c.updated_at,
                )
                .where(conversations.c.user_id == user_id)
                .order_by(conversations.c.created_at, conversations.c.id)
            )
        ).mappings().all()
        conversation_ids = [row["id"] for row in conversation_rows]
        message_rows = (
            await c.execute(
                select(
                    messages.c.id,
                    messages.c.conversation_id,
                    messages.c.role,
                    messages.c.body,
                    messages.c.status,
                    messages.c.created_at,
                    messages.c.updated_at,
                )
                .where(
                    messages.c.user_id == user_id,
                    messages.c.conversation_id.in_(conversation_ids) if conversation_ids else False,
                )
                .order_by(messages.c.conversation_id, messages.c.created_at, messages.c.id)
            )
        ).mappings().all()
        messages_by_conversation = {}
        for row in message_rows:
            messages_by_conversation.setdefault(row["conversation_id"], []).append(
                {key: row[key] for key in row.keys() if key != "conversation_id"}
            )
        conversations_export = []
        for row in conversation_rows:
            item = {key: row[key] for key in row.keys()}
            item["messages"] = messages_by_conversation.get(row["id"], [])
            conversations_export.append(item)

        payload = ExportFormat(
            schema_version="1",
            exported_at=datetime.now(UTC),
            archive_version=account["archive_version"],
            data_epoch=account["data_epoch"],
            dreams=dreams_export,
            motifs=_records(motif_rows),
            associations=_records(association_rows),
            connection_decisions=_records(decision_rows),
            storylines=stories_export,
            conversations=conversations_export,
        )
        return account, payload

    async def run_export(self, job):
        user_id = job["user_id"]
        async with self.db.tenant_transaction(
            user_id, isolation_level="REPEATABLE READ"
        ) as c:
            account = (
                await c.execute(select(A).where(A.c.id == user_id).with_for_update())
            ).mappings().one_or_none()
            current = (
                await c.execute(
                    select(J).where(J.c.user_id == user_id, J.c.id == job["id"]).with_for_update()
                )
            ).mappings().one_or_none()
            now = await c.scalar(select(func.clock_timestamp()))
            if (
                account is None
                or current is None
                or current["status"] != "running"
                or current["lease_token"] != job["lease_token"]
                or current["leased_until"] is None
                or current["leased_until"] <= now
                or current["deadline_at"] is None
                or current["deadline_at"] <= now
            ):
                return
            if account["deletion_state"] != "active":
                return
            expected_epoch = job["data_epoch"]
            expected_archive = job["input_versions"]["archive_version"]
            if (
                account["data_epoch"] != expected_epoch
                or account["archive_version"] != expected_archive
            ):
                await self._requeue_export(c, current, account, now)
                return
            account, payload = await self._snapshot(c, user_id, account)

        if job["input_versions"]["format"] == "json":
            content = json.dumps(
                payload.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        else:
            content = self._text_export(payload)

        async with self.db.tenant_transaction(user_id) as c:
            account = (
                await c.execute(select(A).where(A.c.id == user_id).with_for_update())
            ).mappings().one_or_none()
            current = (
                await c.execute(
                    select(J).where(J.c.user_id == user_id, J.c.id == job["id"]).with_for_update()
                )
            ).mappings().one_or_none()
            now = await c.scalar(select(func.clock_timestamp()))
            if current is None or current["status"] != "running":
                return
            if (
                account is None
                or account["deletion_state"] != "active"
                or current["lease_token"] != job["lease_token"]
                or current["leased_until"] is None
                or current["leased_until"] <= now
            ):
                raise LeaseLost
            if (
                account["data_epoch"] != expected_epoch
                or account["archive_version"] != expected_archive
            ):
                await self._requeue_export(c, current, account, now)
                return

            export_id = UUID(current["payload"]["export_id"])
            updated = await c.execute(
                update(EXPORTS)
                .where(
                    EXPORTS.c.user_id == user_id,
                    EXPORTS.c.id == export_id,
                    EXPORTS.c.status == "pending",
                    EXPORTS.c.data_epoch == expected_epoch,
                    EXPORTS.c.archive_version == expected_archive,
                )
                .values(
                    content=content,
                    status="ready",
                    expires_at=now + EXPORT_TTL,
                )
            )
            if updated.rowcount != 1:
                raise LeaseLost
            await c.execute(
                update(J)
                .where(J.c.user_id == user_id, J.c.id == job["id"])
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

    async def _requeue_export(self, c, job, account, now):
        versions = dict(job["input_versions"])
        versions["archive_version"] = account["archive_version"]
        export_id = UUID(job["payload"]["export_id"])
        await c.execute(
            update(EXPORTS)
            .where(EXPORTS.c.user_id == job["user_id"], EXPORTS.c.id == export_id)
            .values(
                status="pending",
                content=None,
                archive_version=account["archive_version"],
                data_epoch=account["data_epoch"],
                expires_at=now + EXPORT_TTL,
            )
        )
        await c.execute(
            update(J)
            .where(J.c.user_id == job["user_id"], J.c.id == job["id"])
            .values(
                status="queued",
                stage="snapshot_changed",
                attempts=func.greatest(J.c.attempts - 1, 0),
                run_after=now,
                data_epoch=account["data_epoch"],
                input_versions=versions,
                lease_token=None,
                leased_until=None,
                deadline_at=None,
                stage_deadline_at=None,
            )
        )

    @staticmethod
    def _text_export(payload):
        lines = [
            "DreamThread journal export",
            f"Schema version: {payload.schema_version}",
            f"Exported at: {payload.exported_at.isoformat()}",
            "",
        ]
        for dream in payload.dreams:
            lines.extend(
                [
                    f"Dream: {dream['title'] or '(untitled)'}",
                    f"Date: {dream['dream_date'] or 'not set'}",
                ]
            )
            for revision in dream["revisions"]:
                lines.extend(
                    [
                        f"Revision {revision['revision']} ({revision['recorded_at'].isoformat()}):",
                        revision["text"],
                        "",
                    ]
                )
        for conversation in payload.conversations:
            lines.extend(["Conversation: " + (conversation["title"] or "(untitled)"), ""])
            for message in conversation["messages"]:
                lines.extend([f"{message['role']}: {message['body']}", ""])
        if payload.associations:
            lines.extend(["Personal associations", ""])
            for association in payload.associations:
                lines.append(
                    f"{association['status']} motif {association['motif_id']}: "
                    f"{association['meaning']}"
                )
        return "\n".join(lines)

    async def request_journal_deletion(self, user_id: UUID):
        async with self.db.tenant_transaction(user_id) as c:
            account = (
                await c.execute(select(A).where(A.c.id == user_id).with_for_update())
            ).mappings().one_or_none()
            if account is None or account["deletion_state"] == "deleted":
                raise APIError("account_unavailable", "Account is unavailable", 403)
            if account["deletion_state"] == "active":
                epoch = account["data_epoch"] + 1
                archive_version = account["archive_version"] + 1
                await c.execute(
                    update(A)
                    .where(A.c.id == user_id, A.c.deletion_state == "active")
                    .values(
                        deletion_state="deleting",
                        data_epoch=epoch,
                        archive_version=archive_version,
                    )
                )
            else:
                epoch = account["data_epoch"]
                archive_version = account["archive_version"]

            active_job = (
                await c.execute(
                    select(J)
                    .where(
                        J.c.user_id == user_id,
                        J.c.kind == "journal_delete",
                        J.c.data_epoch == epoch,
                        J.c.status.in_(("queued", "running")),
                    )
                    .order_by(J.c.created_at.desc(), J.c.id.desc())
                    .with_for_update()
                    .limit(1)
                )
            ).mappings().one_or_none()
            if active_job:
                return JournalDeleteAccepted(job_id=active_job["id"], status=active_job["status"])

            job_id = uuid4()
            await c.execute(
                insert(J).values(
                    id=job_id,
                    user_id=user_id,
                    kind="journal_delete",
                    dedupe_key=f"journal-delete:{epoch}:{job_id}",
                    payload={"data_epoch": epoch},
                    data_epoch=epoch,
                    input_versions={"archive_version": archive_version},
                )
            )
            await c.execute(
                update(J)
                .where(J.c.user_id == user_id, J.c.id != job_id)
                .values(
                    status=case(
                        (J.c.status.in_(("queued", "running")), "cancelled"),
                        else_=J.c.status,
                    ),
                    stage=case(
                        (J.c.status.in_(("queued", "running")), "journal_deleting"),
                        else_=J.c.stage,
                    ),
                    error_code=case(
                        (J.c.status.in_(("queued", "running")), "source_changed"),
                        else_=J.c.error_code,
                    ),
                    payload={},
                    lease_token=None,
                    leased_until=None,
                    deadline_at=None,
                    stage_deadline_at=None,
                )
            )
            await c.execute(
                update(tables["app.messages"])
                .where(tables["app.messages"].c.user_id == user_id)
                .values(body="", status="stale", context_versions={})
            )
            await c.execute(
                update(EXPORTS)
                .where(EXPORTS.c.user_id == user_id)
                .values(status="cancelled", content=None)
            )
            await c.execute(
                update(tables["app.monthly_summaries"])
                .where(tables["app.monthly_summaries"].c.user_id == user_id)
                .values(state="stale", summary={})
            )
            await c.execute(
                delete(tables["app.summary_evidence"]).where(
                    tables["app.summary_evidence"].c.user_id == user_id
                )
            )
            return JournalDeleteAccepted(job_id=job_id, status="queued")

    async def deletion_status(self, user_id: UUID):
        async with self.db.tenant_transaction(user_id) as c:
            account = (
                await c.execute(select(A).where(A.c.id == user_id))
            ).mappings().one_or_none()
            if account is None:
                raise APIError("account_unavailable", "Account is unavailable", 403)
            job = (
                await c.execute(
                    select(J)
                    .where(J.c.user_id == user_id, J.c.kind == "journal_delete")
                    .order_by(J.c.created_at.desc(), J.c.id.desc())
                    .limit(1)
                )
            ).mappings().one_or_none()
            if job is None:
                raise APIError("not_found", "Journal deletion not found", 404)
            pending = await c.scalar(
                select(func.count())
                .select_from(CLEANUP)
                .where(CLEANUP.c.user_id == user_id, CLEANUP.c.state != "complete")
            )
            return {
                "job_id": job["id"],
                "status": job["status"],
                "deletion_state": account["deletion_state"],
                "pending_storage_cleanup": pending or 0,
            }

    async def run_journal_deletion(self, job):
        user_id = job["user_id"]
        async with self.db.tenant_transaction(user_id) as c:
            account = (
                await c.execute(select(A).where(A.c.id == user_id).with_for_update())
            ).mappings().one_or_none()
            current = (
                await c.execute(
                    select(J).where(J.c.user_id == user_id, J.c.id == job["id"]).with_for_update()
                )
            ).mappings().one_or_none()
            now = await c.scalar(select(func.clock_timestamp()))
            if (
                account is None
                or account["deletion_state"] != "deleting"
                or account["data_epoch"] != job["data_epoch"]
                or current is None
                or current["status"] != "running"
                or current["lease_token"] != job["lease_token"]
                or current["leased_until"] is None
                or current["leased_until"] <= now
            ):
                return

            assets = (
                await c.execute(
                    select(ASSETS.c.storage_key).where(ASSETS.c.user_id == user_id)
                )
            ).scalars().all()
            if assets and self.settings and self.settings.supabase_secret_key:
                # New audio intents already persist their own cleanup fence before upload.
                # Do not create an immediate duplicate deletion that could race a PUT.
                registered = set((await c.execute(select(CLEANUP.c.operation_key).where(
                    CLEANUP.c.user_id == user_id
                ))).scalars().all())
                assets = [key for key in assets if len(key.split('/')) < 2 or
                          f"audio:{key.split('/')[1]}" not in registered]
            if assets:
                await c.execute(
                    pg_insert(CLEANUP)
                    .values(
                        user_id=user_id,
                        operation_key=f"journal-delete:{job['data_epoch']}",
                        storage_keys=list(assets),
                        state="queued",
                        not_before=now + timedelta(seconds=(
                            self.settings.audio_upload_intent_ttl_seconds
                            + self.settings.storage_timeout_seconds + 60
                        )) if self.settings else now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[CLEANUP.c.user_id, CLEANUP.c.operation_key]
                    )
                )

            ordered_tables = (
                "connection_evidence",
                "message_evidence",
                "analysis_evidence",
                "summary_evidence",
                "storyline_evidence",
                "motif_evidence",
                "messages",
                "conversations",
                "storyline_entries",
                "storylines",
                "connections",
                "connection_decisions",
                "associations",
                "embedding_chunks",
                "embedding_spaces",
                "dream_embeddings",
                "followup_answers",
                "detail_overrides",
                "dream_motifs",
                "analyses",
                "evidence",
                "dreams",
                "motifs",
                "monthly_summaries",
                "exports",
                "audio_assets",
                "idempotency_keys",
                "api_rate_limits",
                "ai_usage",
                "model_runs",
                "job_artifacts",
            )
            for name in ordered_tables:
                table = tables.get(f"app.{name}")
                if table is not None:
                    await c.execute(delete(table).where(table.c.user_id == user_id))
            await c.execute(
                delete(J).where(J.c.user_id == user_id, J.c.id != job["id"])
            )
            pending_cleanup = await c.scalar(
                select(func.count())
                .select_from(CLEANUP)
                .where(CLEANUP.c.user_id == user_id, CLEANUP.c.state != "complete")
            )
            if pending_cleanup:
                await c.execute(
                    update(J)
                    .where(J.c.user_id == user_id, J.c.id == job["id"])
                    .values(
                        status="failed",
                        stage="storage_cleanup_pending",
                        error_code="storage_cleanup_pending",
                        payload={},
                        lease_token=None,
                        leased_until=None,
                        deadline_at=None,
                        stage_deadline_at=None,
                    )
                )
                return
            await c.execute(
                update(A)
                .where(A.c.id == user_id, A.c.deletion_state == "deleting")
                .values(deletion_state="active")
            )
            await c.execute(
                update(J)
                .where(J.c.user_id == user_id, J.c.id == job["id"])
                .values(
                    status="succeeded",
                    stage="ready",
                    error_code=None,
                    payload={},
                    lease_token=None,
                    leased_until=None,
                    deadline_at=None,
                    stage_deadline_at=None,
                )
            )

    async def mark_export_failure(self, job, *, cancelled=False):
        export_id = job.get("payload", {}).get("export_id")
        if not export_id:
            return
        async with self.db.tenant_transaction(job["user_id"]) as c:
            await c.execute(
                update(EXPORTS)
                .where(
                    EXPORTS.c.user_id == job["user_id"],
                    EXPORTS.c.id == UUID(export_id),
                    EXPORTS.c.status.in_(("pending", "ready")),
                )
                .values(status="cancelled" if cancelled else "failed", content=None)
            )

    async def expire_export(self, candidate):
        async with self.db.tenant_transaction(candidate["user_id"]) as c:
            current = (
                await c.execute(
                    select(J).where(
                        J.c.user_id == candidate["user_id"],
                        J.c.id == candidate["id"],
                        J.c.status == "succeeded",
                        J.c.kind == "export",
                        J.c.stage == candidate["stage"],
                        J.c.attempts == candidate["attempts"],
                    ).with_for_update()
                )
            ).mappings().one_or_none()
            if current is None:
                return
            export_id = UUID(current["payload"]["export_id"])
            await c.execute(
                update(EXPORTS)
                .where(
                    EXPORTS.c.user_id == candidate["user_id"],
                    EXPORTS.c.id == export_id,
                    EXPORTS.c.expires_at <= func.clock_timestamp(),
                )
                .values(status="expired", content=None)
            )
            expired = await c.scalar(
                select(EXPORTS.c.id).where(
                    EXPORTS.c.user_id == candidate["user_id"],
                    EXPORTS.c.id == export_id,
                    EXPORTS.c.expires_at <= func.clock_timestamp(),
                )
            )
            if expired is not None:
                await c.execute(
                    update(J)
                    .where(J.c.user_id == candidate["user_id"], J.c.id == candidate["id"])
                    .values(stage="retention_cleaned")
                )
