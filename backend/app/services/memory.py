"""User-controlled memory, explicit identities and transactional invalidation/rebuilds."""

import hashlib
from uuid import uuid4

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.models import tables
from app.errors import APIError
from app.schemas.memory import MemoryPage, MemoryView, MotifPage, MotifView
from app.services.accounts import active_account
from app.services.admission import reserve_resource
from app.services.invalidation import invalidate_sources
from app.services.journal import JournalService, owned_dream

M, DM, A, ACC, D = (
    tables["app." + name]
    for name in (
        "motifs",
        "dream_motifs",
        "associations",
        "accounts",
        "dreams",
    )
)


def meaning_hash(meaning):
    normalized = " ".join(meaning.casefold().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def memory_view(row):
    return MemoryView.model_validate({key: row[key] for key in MemoryView.model_fields})


async def owned_motif(c, user_id, motif_id):
    row = (
        (await c.execute(select(M).where(M.c.user_id == user_id, M.c.id == motif_id)))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise APIError("not_found", "Motif not found", 404)
    return row


async def new_motif(c, user_id, kind, label):
    return await c.scalar(
        insert(M)
        .values(
            user_id=user_id, type=kind, canonical_label=label, normalized_key="user:" + uuid4().hex
        )
        .returning(M.c.id)
    )


async def confirm_source(c, user_id, motif_id, dream):
    if dream["lifecycle"] != "saved":
        raise APIError("invalid_source", "Choose a saved dream", 409)
    await c.execute(
        insert(DM)
        .values(
            user_id=user_id,
            motif_id=motif_id,
            dream_id=dream["id"],
            revision_id=dream["current_revision_id"],
            origin="user",
            confirmation_state="confirmed",
        )
        .on_conflict_do_update(
            index_elements=[DM.c.user_id, DM.c.revision_id, DM.c.motif_id],
            set_={"origin": "user", "confirmation_state": "confirmed"},
        )
    )


async def forget(c, association):
    # Retain only proposal identity/decision to suppress repeated suggestions. Original user
    # follow-up answers are separate source records, never reused as forgotten memory.
    await c.execute(
        update(A)
        .where(A.c.id == association["id"])
        .values(
            meaning="",
            proposal_hash=association["proposal_hash"] or meaning_hash(association["meaning"]),
            status="rejected",
            enabled=False,
            deleted_at=func.clock_timestamp(),
            version=A.c.version + 1,
        )
    )


async def propose_association(c, user_id, motif_id, meaning, source_dream_id=None):
    """Internal proposal writer. Rejected/deleted meanings cannot reappear on model retries.

    Never call this with a model-supplied user ID; verified tenant context is the boundary.
    Suggestions do not enter personal context until an explicit user confirmation.
    """
    await active_account(c, user_id)
    await owned_motif(c, user_id, motif_id)
    if source_dream_id:
        await owned_dream(c, user_id, source_dream_id)
    if not meaning.strip() or len(meaning) > 1000 or "\x00" in meaning:
        raise ValueError("Invalid proposal meaning")
    signature = meaning_hash(meaning)
    # Also respect legacy decisions, which may predate proposal_hash.
    prior = (
        (await c.execute(select(A).where(A.c.user_id == user_id, A.c.motif_id == motif_id)))
        .mappings()
        .all()
    )
    if any((row["proposal_hash"] or meaning_hash(row["meaning"])) == signature for row in prior):
        return None
    return await c.scalar(
        insert(A)
        .values(
            user_id=user_id,
            motif_id=motif_id,
            meaning=meaning,
            proposal_hash=signature,
            origin="model",
            status="suggested",
            enabled=True,
            source_dream_id=source_dream_id,
        )
        .returning(A.c.id)
    )


class MemoryService:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    async def changed(self, c, identity):
        account = (
            (
                await c.execute(
                    update(ACC)
                    .where(ACC.c.id == identity.user_id)
                    .values(
                        memory_version=ACC.c.memory_version + 1,
                        archive_version=ACC.c.archive_version + 1,
                    )
                    .returning(ACC)
                )
            )
            .mappings()
            .one()
        )
        await invalidate_sources(c, identity.user_id)
        dreams = (
            (
                await c.execute(
                    select(D)
                    .where(
                        D.c.user_id == identity.user_id,
                        D.c.lifecycle == "saved",
                        D.c.deleted_at.is_(None),
                    )
                    .order_by(D.c.id)
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        jobs = {}
        for dream in dreams:
            jobs[dream["id"]] = (
                await JournalService(self.db, self.settings)._queue(
                    c, identity, account, dream, automatic=True
                )
            )["id"]
        return account, jobs

    async def association(self, c, user_id, association_id):
        row = (
            (
                await c.execute(
                    select(A)
                    .where(
                        A.c.user_id == user_id, A.c.id == association_id, A.c.deleted_at.is_(None)
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise APIError("not_found", "Memory not found", 404)
        return row

    async def list(self, identity, limit=20, after=None, status=None):
        async with self.db.tenant_transaction(identity.user_id) as c:
            account = await active_account(c, identity.user_id)
            query = select(A).where(A.c.user_id == identity.user_id, A.c.deleted_at.is_(None))
            if after:
                query = query.where(A.c.id > after)
            if status:
                query = query.where(A.c.status == status)
            rows = (await c.execute(query.order_by(A.c.id).limit(limit + 1))).mappings().all()
            return MemoryPage(
                items=[memory_view(row) for row in rows[:limit]],
                next_after=rows[limit - 1]["id"] if len(rows) > limit else None,
                memory_version=account["memory_version"],
                archive_version=account["archive_version"],
            )

    async def motifs(self, identity, limit=20, after=None, kind=None):
        async with self.db.tenant_transaction(identity.user_id) as c:
            await active_account(c, identity.user_id)
            current = and_(
                D.c.current_revision_id == DM.c.revision_id,
                D.c.lifecycle == "saved",
                D.c.deleted_at.is_(None),
            )
            query = select(
                M,
                func.count(DM.c.id).filter(current).label("mentions"),
                func.count(DM.c.id)
                .filter(and_(current, DM.c.confirmation_state == "confirmed"))
                .label("confirmed"),
            )
            query = query.outerjoin(DM, and_(DM.c.user_id == M.c.user_id, DM.c.motif_id == M.c.id))
            query = query.outerjoin(
                D, and_(D.c.user_id == DM.c.user_id, D.c.id == DM.c.dream_id)
            ).where(M.c.user_id == identity.user_id)
            if after:
                query = query.where(M.c.id > after)
            if kind:
                query = query.where(M.c.type == kind)
            rows = (
                (await c.execute(query.group_by(M.c.id).order_by(M.c.id).limit(limit + 1)))
                .mappings()
                .all()
            )
            return MotifPage(
                items=[
                    MotifView(
                        id=row["id"],
                        type=row["type"],
                        label=row["canonical_label"],
                        current_mentions=row["mentions"],
                        identity_confirmed=bool(row["confirmed"])
                        or row["normalized_key"].startswith("user:"),
                        origin="automatic_mention"
                        if row["normalized_key"].startswith("mention:")
                        else "user_identity"
                        if row["normalized_key"].startswith("user:")
                        else "legacy",
                    )
                    for row in rows[:limit]
                ],
                next_after=rows[limit - 1]["id"] if len(rows) > limit else None,
            )

    async def create(self, identity, payload, key):
        async with self.db.tenant_transaction(identity.user_id) as c:
            await active_account(c, identity.user_id)
            admission = await reserve_resource(
                c,
                identity,
                self.settings,
                route="memory.create",
                key=key,
                body=payload.model_dump(mode="json"),
            )
            if admission.replayed:
                return memory_view(
                    await self.association(c, identity.user_id, admission.resource_id)
                )
            if payload.motif_id:
                await owned_motif(c, identity.user_id, payload.motif_id)
                motif_id = payload.motif_id
            else:
                motif_id = await new_motif(
                    c, identity.user_id, payload.motif.type, payload.motif.label
                )
            if payload.source_dream_id:
                dream = await owned_dream(c, identity.user_id, payload.source_dream_id)
                await confirm_source(c, identity.user_id, motif_id, dream)
            row = (
                (
                    await c.execute(
                        insert(A)
                        .values(
                            id=admission.resource_id,
                            user_id=identity.user_id,
                            motif_id=motif_id,
                            meaning=payload.meaning,
                            status=payload.status,
                            enabled=payload.enabled,
                            source_dream_id=payload.source_dream_id,
                            origin="user",
                            proposal_hash=meaning_hash(payload.meaning),
                        )
                        .returning(A)
                    )
                )
                .mappings()
                .one()
            )
            await self.changed(c, identity)
            return memory_view(row)

    async def patch(self, identity, association_id, payload):
        async with self.db.tenant_transaction(identity.user_id) as c:
            await active_account(c, identity.user_id)
            row = await self.association(c, identity.user_id, association_id)
            if row["version"] != payload.expected_version:
                raise APIError("version_conflict", "Reload current memory", 409)
            changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
            if all(row[key] == value for key, value in changes.items()):
                return memory_view(row)
            if changes.get("status") == "confirmed" and row["source_dream_id"]:
                # Confirm only an existing mention on the current source. A stale proposal
                # cannot silently assign identity to a newly edited revision.
                source_revision = await c.scalar(
                    select(D.c.current_revision_id).where(
                        D.c.user_id == identity.user_id,
                        D.c.id == row["source_dream_id"],
                        D.c.lifecycle == "saved",
                        D.c.deleted_at.is_(None),
                    )
                )
                if source_revision:
                    await c.execute(
                        update(DM)
                        .where(
                            DM.c.user_id == identity.user_id,
                            DM.c.dream_id == row["source_dream_id"],
                            DM.c.revision_id == source_revision,
                            DM.c.motif_id == row["motif_id"],
                        )
                        .values(confirmation_state="confirmed", origin="user")
                    )
            # Preserve the old rejection signature. Editing a rejected proposal must not
            # allow the original model proposal to return under the same identity.
            if "meaning" in changes and changes["meaning"] != row["meaning"]:
                if row["origin"] == "model" or row["status"] == "rejected":
                    await c.execute(
                        insert(A).values(
                            user_id=identity.user_id,
                            motif_id=row["motif_id"],
                            meaning="",
                            origin=row["origin"],
                            proposal_hash=row["proposal_hash"] or meaning_hash(row["meaning"]),
                            enabled=False,
                            status="rejected",
                            deleted_at=func.clock_timestamp(),
                        )
                    )
                changes["proposal_hash"] = meaning_hash(changes["meaning"])
            updated = (
                (
                    await c.execute(
                        update(A)
                        .where(A.c.id == association_id)
                        .values(**changes, version=A.c.version + 1)
                        .returning(A)
                    )
                )
                .mappings()
                .one()
            )
            await self.changed(c, identity)
            return memory_view(updated)

    async def delete(self, identity, association_id, expected_version):
        async with self.db.tenant_transaction(identity.user_id) as c:
            await active_account(c, identity.user_id)
            row = await self.association(c, identity.user_id, association_id)
            if row["version"] != expected_version:
                raise APIError("version_conflict", "Reload current memory", 409)
            await forget(c, row)
            await self.changed(c, identity)
