"""Source-local detail identities, durable overrides and opt-in personal context."""

import hashlib
import json
from uuid import UUID

from sqlalchemy import and_, delete, select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import tables
from app.schemas.analysis import Observation
from app.services.evidence import verify_citation

M, DM, A, OVERRIDES, F = (
    tables["app." + name]
    for name in (
        "motifs",
        "dream_motifs",
        "associations",
        "detail_overrides",
        "followup_answers",
    )
)


def detail_key(observation):
    citation = observation.citation
    data = [observation.type, citation.start, citation.end, citation.quote]
    return (
        "detail_" + hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()[:40]
    )


def mention_key(revision_id, key):
    return f"mention:{revision_id}:{key}"


def empty_context():
    return {
        "overrides": [],
        "mentions": {},
        "associations": [],
        "clarifications": [],
        "answered_questions": [],
        "associations_omitted": False,
    }


async def load_context(c, user_id, dream_id, revision_id):
    context = empty_context()
    context["overrides"] = [
        dict(row)
        for row in (
            await c.execute(
                select(OVERRIDES)
                .where(
                    OVERRIDES.c.user_id == user_id,
                    OVERRIDES.c.dream_id == dream_id,
                    OVERRIDES.c.revision_id == revision_id,
                )
                .order_by(OVERRIDES.c.detail_key)
            )
        ).mappings()
    ]
    links = (
        (
            await c.execute(
                select(DM.c.motif_id, DM.c.confirmation_state, M.c.normalized_key)
                .join(M, and_(M.c.user_id == DM.c.user_id, M.c.id == DM.c.motif_id))
                .where(
                    DM.c.user_id == user_id,
                    DM.c.dream_id == dream_id,
                    DM.c.revision_id == revision_id,
                )
            )
        )
        .mappings()
        .all()
    )
    context["mentions"] = {
        row["normalized_key"]: {
            "motif_id": str(row["motif_id"]),
            "confirmation_state": row["confirmation_state"],
        }
        for row in links
    }
    confirmed_ids = {row["motif_id"] for row in links if row["confirmation_state"] == "confirmed"}
    rows = (
        (
            await c.execute(
                select(
                    A.c.id, A.c.motif_id, A.c.meaning, A.c.origin, A.c.version, M.c.canonical_label
                )
                .join(M, and_(M.c.user_id == A.c.user_id, M.c.id == A.c.motif_id))
                .where(
                    A.c.user_id == user_id,
                    A.c.motif_id.in_(confirmed_ids),
                    A.c.status == "confirmed",
                    A.c.enabled.is_(True),
                    A.c.deleted_at.is_(None),
                )
                .order_by(A.c.id)
                .limit(9)
            )
        )
        .mappings()
        .all()
    )
    context["associations"] = [
        {
            "id": str(row["id"]),
            "motif_id": str(row["motif_id"]),
            "meaning": row["meaning"],
            "motif_label": row["canonical_label"],
            "origin": row["origin"],
            "version": row["version"],
            "user_confirmed": True,
        }
        for row in rows[:8]
    ]
    context["associations_omitted"] = len(rows) > 8
    answers = (
        (
            await c.execute(
                select(F)
                .where(
                    F.c.user_id == user_id, F.c.dream_id == dream_id, F.c.revision_id == revision_id
                )
                .order_by(F.c.updated_at.desc(), F.c.id.desc())
            )
        )
        .mappings()
        .all()
    )
    context["answered_questions"] = [row["question_text"] for row in answers]
    if answers and not answers[0]["skipped"] and not answers[0]["save_as_association"]:
        context["clarifications"] = [{"answer": answers[0]["answer"], "scope": "this_entry_only"}]
    return context


def apply_overrides(extracted, context, source):
    """User labels are corrections, not quotes. Preserve their original verified citation."""
    automatic = list(extracted.observations)
    corrected, metadata = [], {}
    for override in context["overrides"]:
        value = override["corrected_value"]
        original = Observation.model_validate_json(json.dumps(value["original"]))
        citation = verify_citation(original.citation, **source)
        # Model observation order and quote width may vary on retry. A correction is anchored
        # to the immutable source span/type, rather than the model's transient o1/o2 key.
        automatic = [
            item
            for item in automatic
            if not (
                detail_key(item) == override["detail_key"]
                or (
                    item.type == original.type
                    and item.citation.start < citation.end
                    and item.citation.end > citation.start
                )
            )
        ]
        if override["removed"]:
            continue
        observation = original.model_copy(
            update={
                "label": value.get("label", original.label),
                "type": value.get("type", original.type),
                "citation": citation,
            }
        )
        corrected.append(observation)
        metadata[id(observation)] = {
            "detail_key": override["detail_key"],
            "origin": "user",
            "motif_id": value.get("motif_id"),
            "confirmation_state": "confirmed",
        }
    # User overrides take priority over newly extracted details within the existing output cap.
    observations, annotations = [], {}
    for index, observation in enumerate((corrected + automatic)[:24]):
        key = f"o{index + 1}"
        annotations[key] = metadata.get(
            id(observation),
            {
                "detail_key": detail_key(observation),
                "origin": "automatic",
                **context["mentions"].get(
                    mention_key(source["revision_id"], detail_key(observation)), {}
                ),
            },
        )
        observations.append(observation.model_copy(update={"key": key}))
    return extracted.model_copy(update={"observations": observations}), annotations


async def publish_mentions(c, user_id, dream, extraction):
    """Persist source-specific automatic mentions; only explicit user links share identities."""
    evidence_by_link = {}
    for observation in extraction.get("observations", []):
        key = observation["detail_key"]
        motif_id = observation.get("motif_id")
        if motif_id:
            motif_id = UUID(str(motif_id))
            motif = (
                (await c.execute(select(M).where(M.c.user_id == user_id, M.c.id == motif_id)))
                .mappings()
                .one()
            )
        else:
            normalized = mention_key(dream["current_revision_id"], key)
            await c.execute(
                insert(M)
                .values(
                    user_id=user_id,
                    type=observation["type"],
                    canonical_label=observation["label"],
                    normalized_key=normalized,
                )
                .on_conflict_do_nothing(index_elements=[M.c.user_id, M.c.type, M.c.normalized_key])
            )
            motif = (
                (
                    await c.execute(
                        select(M).where(
                            M.c.user_id == user_id,
                            M.c.type == observation["type"],
                            M.c.normalized_key == normalized,
                        )
                    )
                )
                .mappings()
                .one()
            )
            motif_id = motif["id"]
        await c.execute(
            insert(DM)
            .values(
                user_id=user_id,
                motif_id=motif_id,
                dream_id=dream["id"],
                revision_id=dream["current_revision_id"],
                origin="user" if observation["origin"] == "user" else "extracted",
                confirmation_state="confirmed" if observation["origin"] == "user" else "suggested",
            )
            .on_conflict_do_nothing(index_elements=[DM.c.user_id, DM.c.revision_id, DM.c.motif_id])
        )
        link = (
            (
                await c.execute(
                    select(DM).where(
                        DM.c.user_id == user_id,
                        DM.c.revision_id == dream["current_revision_id"],
                        DM.c.motif_id == motif_id,
                    )
                )
            )
            .mappings()
            .one()
        )
        observation["motif_id"] = str(motif_id)
        observation["confirmation_state"] = link["confirmation_state"]
        evidence_by_link.setdefault(link["id"], set()).update(observation["evidence_ids"])
    links = tables["app.motif_evidence"]
    for link_id, ids in evidence_by_link.items():
        await c.execute(
            delete(links).where(links.c.user_id == user_id, links.c.dream_motif_id == link_id)
        )
        for evidence_id in ids:
            await c.execute(
                insert(links).values(
                    user_id=user_id, dream_motif_id=link_id, evidence_id=UUID(evidence_id)
                )
            )
