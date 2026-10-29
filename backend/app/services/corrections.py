"""Revision-bound user detail overrides and explicit follow-up consent."""

import hashlib
import json

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.models import tables
from app.errors import APIError
from app.schemas.analysis import Observation
from app.schemas.memory import DetailResult, FollowupAnswer, FollowupView
from app.services.accounts import active_account
from app.services.admission import reserve_resource
from app.services.evidence import verify_citation
from app.services.journal import JournalService, current_source, owned_dream
from app.services.memory import (
    MemoryService,
    confirm_source,
    forget,
    meaning_hash,
    new_motif,
    owned_motif,
)

D, OVERRIDES, F, A, E, DM, ACC = (
    tables["app." + name]
    for name in (
        "dreams",
        "detail_overrides",
        "followup_answers",
        "associations",
        "evidence",
        "dream_motifs",
        "accounts",
    )
)


def question_version(analysis):
    data = [str(analysis["id"]), analysis["memory_version"], analysis["followup"]]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()[:40]


class CorrectionService:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.journal, self.memory = JournalService(db, settings), MemoryService(db, settings)

    async def details(self, identity, dream_id, payload, key):
        async with self.db.tenant_transaction(identity.user_id) as c:
            account = await active_account(c, identity.user_id)
            dream = await owned_dream(c, identity.user_id, dream_id)
            admission = await reserve_resource(
                c,
                identity,
                self.settings,
                route="dream.details",
                key=key,
                body={"dream_id": str(dream_id), **payload.model_dump(mode="json")},
            )
            if admission.replayed:
                row = (
                    (
                        await c.execute(
                            select(OVERRIDES).where(OVERRIDES.c.id == admission.resource_id)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None or row["revision_id"] != dream["current_revision_id"]:
                    raise APIError("source_changed", "Reload the current source details", 409)
                return self.detail_result(dream, row, account, None)
            if dream["version"] != payload.expected_version or dream["lifecycle"] != "saved":
                raise APIError("version_conflict", "Reload current dream details", 409)
            prior = (
                (
                    await c.execute(
                        select(OVERRIDES).where(
                            OVERRIDES.c.user_id == identity.user_id,
                            OVERRIDES.c.revision_id == dream["current_revision_id"],
                            OVERRIDES.c.detail_key == payload.detail_key,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if prior:
                original = prior["corrected_value"]["original"]
                original_motif = prior["corrected_value"].get("original_motif_id")
            else:
                analysis = await self.journal._current_analysis(c, dream)
                observation = (
                    next(
                        (
                            item
                            for item in analysis["extraction"].get("observations", [])
                            if item.get("detail_key") == payload.detail_key
                        ),
                        None,
                    )
                    if analysis
                    else None
                )
                if observation is None:
                    raise APIError(
                        "detail_not_current", "Choose a detail from current analysis", 409
                    )
                citation = (
                    (
                        await c.execute(
                            select(E).where(
                                E.c.user_id == identity.user_id,
                                E.c.id.in_(observation["evidence_ids"]),
                                E.c.dream_id == dream_id,
                                E.c.revision_id == dream["current_revision_id"],
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if citation is None:
                    raise APIError("detail_not_current", "Detail evidence is unavailable", 409)
                original = {
                    "key": observation["key"],
                    "type": observation["type"],
                    "label": observation["label"],
                    "citation": {
                        "dream_id": str(dream_id),
                        "revision_id": str(dream["current_revision_id"]),
                        "start": citation["start"],
                        "end": citation["end"],
                        "quote": citation["exact_quote"],
                        "source_hash": citation["content_hash"],
                    },
                }
                original_motif = observation.get("motif_id")
            validated = Observation.model_validate_json(json.dumps(original))
            source = await current_source(c, dream)
            verify_citation(
                validated.citation,
                dream_id=dream_id,
                revision_id=source["id"],
                source_hash=source["content_hash"],
                text=source["text"],
            )
            value = {"original": original, "original_motif_id": original_motif}
            if not payload.removed:
                value["label"] = (
                    payload.label
                    or (prior["corrected_value"].get("label") if prior else None)
                    or original["label"]
                )
                value["type"] = (
                    payload.type
                    or (prior["corrected_value"].get("type") if prior else None)
                    or original["type"]
                )
                if payload.motif_id:
                    motif = await owned_motif(c, identity.user_id, payload.motif_id)
                    if motif["type"] != value["type"]:
                        raise APIError(
                            "motif_type_mismatch", "Choose a motif of the corrected type", 409
                        )
                    motif_id = payload.motif_id
                else:
                    motif_id = await new_motif(c, identity.user_id, value["type"], value["label"])
                value["motif_id"] = str(motif_id)
                await confirm_source(c, identity.user_id, motif_id, dream)
            # Explicit corrections can replace a former identity, but never silently merge labels.
            old_ids = [
                item
                for item in (
                    original_motif,
                    prior["corrected_value"].get("motif_id") if prior else None,
                )
                if item and item != value.get("motif_id")
            ]
            if old_ids:
                await c.execute(
                    update(DM)
                    .where(
                        DM.c.user_id == identity.user_id,
                        DM.c.revision_id == source["id"],
                        DM.c.motif_id.in_(old_ids),
                    )
                    .values(confirmation_state="rejected")
                )
            row = (
                (
                    await c.execute(
                        insert(OVERRIDES)
                        .values(
                            id=admission.resource_id,
                            user_id=identity.user_id,
                            dream_id=dream_id,
                            revision_id=source["id"],
                            detail_key=payload.detail_key,
                            corrected_value=value,
                            removed=payload.removed,
                        )
                        .on_conflict_do_update(
                            index_elements=[
                                OVERRIDES.c.user_id,
                                OVERRIDES.c.revision_id,
                                OVERRIDES.c.detail_key,
                            ],
                            set_={
                                "corrected_value": value,
                                "removed": payload.removed,
                                "version": OVERRIDES.c.version + 1,
                            },
                        )
                        .returning(OVERRIDES)
                    )
                )
                .mappings()
                .one()
            )
            await self.bind_key(c, identity.user_id, "dream.details", key, row["id"])
            dream = (
                (
                    await c.execute(
                        update(D)
                        .where(D.c.id == dream_id)
                        .values(version=D.c.version + 1)
                        .returning(D)
                    )
                )
                .mappings()
                .one()
            )
            account, jobs = await self.memory.changed(c, identity)
            return self.detail_result(dream, row, account, jobs.get(dream_id))

    def detail_result(self, dream, override, account, job_id):
        return DetailResult(
            dream_id=dream["id"],
            revision_id=dream["current_revision_id"],
            dream_version=dream["version"],
            detail_key=override["detail_key"],
            removed=override["removed"],
            corrected_value=override["corrected_value"],
            memory_version=account["memory_version"],
            analysis_job_id=job_id,
        )

    async def bind_key(self, c, user_id, route, key, resource_id):
        keys = tables["app.idempotency_keys"]
        await c.execute(
            update(keys)
            .where(keys.c.user_id == user_id, keys.c.route == route, keys.c.key == key)
            .values(result_resource_id=resource_id)
        )

    async def followup_view(self, c, dream):
        analysis = await self.journal._current_analysis(c, dream)
        question = analysis["followup"] if analysis else None
        latest = (
            (
                await c.execute(
                    select(F)
                    .where(
                        F.c.user_id == dream["user_id"],
                        F.c.dream_id == dream["id"],
                        F.c.revision_id == dream["current_revision_id"],
                    )
                    .order_by(F.c.updated_at.desc(), F.c.id.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        version = question_version(analysis) if question else None
        status = "available" if question else "not_available"
        if latest and (version is None or latest["question_version"] == version):
            status = "skipped" if latest["skipped"] else "answered"
        return FollowupView(
            dream_id=dream["id"],
            revision_id=dream["current_revision_id"],
            dream_version=dream["version"],
            question_version=version,
            question=question,
            status=status,
            latest_answer=FollowupAnswer.model_validate(
                {key: latest[key] for key in FollowupAnswer.model_fields}
            )
            if latest
            else None,
        )

    async def get_followup(self, identity, dream_id):
        async with self.db.tenant_transaction(identity.user_id) as c:
            await active_account(c, identity.user_id)
            return await self.followup_view(c, await owned_dream(c, identity.user_id, dream_id))

    async def put_followup(self, identity, dream_id, payload, key):
        async with self.db.tenant_transaction(identity.user_id) as c:
            await active_account(c, identity.user_id)
            dream = await owned_dream(c, identity.user_id, dream_id)
            admission = await reserve_resource(
                c,
                identity,
                self.settings,
                route="followup.answer",
                key=key,
                body={"dream_id": str(dream_id), **payload.model_dump(mode="json")},
            )
            if admission.replayed:
                return await self.followup_view(c, dream)
            if dream["version"] != payload.expected_version or dream["lifecycle"] != "saved":
                raise APIError("version_conflict", "Reload the current follow-up", 409)
            prior = (
                (
                    await c.execute(
                        select(F).where(
                            F.c.user_id == identity.user_id,
                            F.c.revision_id == dream["current_revision_id"],
                            F.c.question_version == payload.question_version,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (prior["version"] if prior else 0) != payload.expected_answer_version:
                raise APIError("version_conflict", "Reload the saved answer", 409)
            analysis = await self.journal._current_analysis(c, dream)
            if not prior and (
                not analysis
                or not analysis["followup"]
                or question_version(analysis) != payload.question_version
            ):
                raise APIError("question_changed", "Reload the current follow-up question", 409)
            question = prior["question_text"] if prior else analysis["followup"]
            if payload.motif_id:
                await owned_motif(c, identity.user_id, payload.motif_id)
            if prior and prior["association_id"]:
                old = (
                    (
                        await c.execute(
                            select(A).where(
                                A.c.id == prior["association_id"], A.c.deleted_at.is_(None)
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if old:
                    await forget(c, old)
            association_id = None
            if payload.save_as_association:
                await confirm_source(c, identity.user_id, payload.motif_id, dream)
                association_id = await c.scalar(
                    insert(A)
                    .values(
                        user_id=identity.user_id,
                        motif_id=payload.motif_id,
                        meaning=payload.answer,
                        status="confirmed",
                        enabled=True,
                        source_dream_id=dream_id,
                        origin="user",
                        proposal_hash=meaning_hash(payload.answer),
                    )
                    .returning(A.c.id)
                )
            values = dict(
                answer=payload.answer,
                skipped=payload.skipped,
                save_as_association=payload.save_as_association,
                association_id=association_id,
            )
            row = (
                (
                    await c.execute(
                        insert(F)
                        .values(
                            id=admission.resource_id,
                            user_id=identity.user_id,
                            dream_id=dream_id,
                            revision_id=dream["current_revision_id"],
                            question_version=payload.question_version,
                            question_text=question,
                            **values,
                        )
                        .on_conflict_do_update(
                            index_elements=[F.c.user_id, F.c.revision_id, F.c.question_version],
                            set_={**values, "version": F.c.version + 1},
                        )
                        .returning(F)
                    )
                )
                .mappings()
                .one()
            )
            await self.bind_key(c, identity.user_id, "followup.answer", key, row["id"])
            dream = (
                (
                    await c.execute(
                        update(D)
                        .where(D.c.id == dream_id)
                        .values(version=D.c.version + 1)
                        .returning(D)
                    )
                )
                .mappings()
                .one()
            )
            await self.memory.changed(c, identity)
            return await self.followup_view(c, dream)
