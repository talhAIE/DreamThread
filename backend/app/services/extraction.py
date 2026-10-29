"""Two bounded Nemotron stages with one shared, persisted repair allowance."""

import json
import re
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from app.providers.nebius import Nebius
from app.schemas.analysis import Citation, Extraction, ReflectionOutput, VerifiedEvidence
from app.services.evidence import OutputInvalid, verify_extraction, verify_reflections
from app.services.personal_context import apply_overrides
from app.workers.types import AnalysisPayload, Publication, WorkFailure

PROMPTS = Path(__file__).parents[1] / "prompts"


class ExtractionPipeline:
    def __init__(self, settings, provider=None):
        self.settings = settings
        self.provider = provider or Nebius(settings)

    async def stage(self, execution, name, schema, data, validate):
        cached = await execution.load_checkpoint(name)
        if cached is not None:
            try:
                return validate(schema.model_validate_json(json.dumps(cached)))
            except (ValidationError, OutputInvalid):
                pass  # A stale/invalid artifact is never trusted as a model result.
        version = self.settings.prompt_version
        if not re.fullmatch(r"v[0-9]+", version):
            raise WorkFailure("provider_configuration")
        prompt_path = PROMPTS / f"{name}_{version}.txt"
        if not prompt_path.is_file():
            raise WorkFailure("provider_configuration")
        prompt = prompt_path.read_text(encoding="utf-8")
        messages = [
            {
                "role": "system",
                "content": prompt + "\nJSON schema:\n" + json.dumps(schema.model_json_schema()),
            },
            {"role": "user", "content": json.dumps({"untrusted_data": data}, ensure_ascii=False)},
        ]
        budget = await execution.load_checkpoint("repair_budget") or {}
        used = (
            budget.get("used", 0)
            if budget.get("submission") == execution.job["input_versions"]["submission"]
            else 0
        )
        used = used if type(used) is int and used in {0, 1} else 1
        while True:
            completion = await self.provider.complete(
                messages, schema.model_json_schema(), task=name, record=execution.record_call
            )
            try:
                result = validate(schema.model_validate_json(completion.text))
            except (ValidationError, OutputInvalid) as error:
                await execution.mark_call(completion.run_id, "invalid_output")
                if used >= 1:
                    raise WorkFailure("invalid_output") from None
                used += 1
                await execution.checkpoint(
                    "repair_budget",
                    {
                        "used": used,
                        "submission": execution.job["input_versions"]["submission"],
                    },
                )
                messages.append(
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "repair_required": str(error)
                                if isinstance(error, OutputInvalid)
                                else "json_schema_invalid",
                                "previous_untrusted_output": completion.text,
                                "instruction": "Correct JSON using the source/schema.",
                            }
                        ),
                    }
                )
                continue
            await execution.mark_call(completion.run_id, "validated")
            await execution.checkpoint(name, result.model_dump(mode="json"))
            return result

    async def run(self, execution):
        if await execution.load_checkpoint("prepared") is None:
            await execution.checkpoint(
                "prepared",
                {
                    "revision_id": str(execution.primary.revision_id),
                    "content_hash": execution.primary.content_hash,
                },
            )
        self.provider.configured()
        source = dict(
            dream_id=execution.primary.dream_id,
            revision_id=execution.primary.revision_id,
            source_hash=execution.primary.content_hash,
            text=execution.original["text"],
        )
        await execution.set_stage("extracting")
        extracted = await self.stage(
            execution,
            "extraction",
            Extraction,
            {
                **source,
                "dream_id": str(source["dream_id"]),
                "revision_id": str(source["revision_id"]),
            },
            lambda output: verify_extraction(output, source),
        )
        context = await execution.personal_context()
        extracted, annotations = apply_overrides(extracted, context, source)
        # The reserved whole-entry reference allows a useful first-entry reflection with no motifs.
        citations = {o.key: o.citation for o in extracted.observations}
        citations["dream"] = Citation(
            **source_without_text(source), start=0, end=len(source["text"]), quote=source["text"]
        )
        await execution.set_stage("verifying")
        reflections = await self.stage(
            execution,
            "reflection",
            ReflectionOutput,
            {
                "original_dream": source["text"],
                "verified_observations": extracted.model_dump(mode="json"),
                "allowed_observation_keys": list(citations),
                "user_reported_emotions": execution.original["reported_emotions"],
                "detail_provenance": annotations,
                "personal_associations": context["associations"],
                "entry_clarifications": context["clarifications"],
            },
            lambda output: verify_reflections(output, set(citations)),
        )
        records, by_span, mapping = [], {}, {}
        for key, citation in citations.items():
            span = (citation.start, citation.end, citation.quote)
            if span not in by_span:
                record = VerifiedEvidence(**citation.model_dump(), id=uuid4())
                by_span[span] = record
                records.append(record)
            mapping[key] = by_span[span].id
        observations = [
            {
                "key": o.key,
                "type": o.type,
                "label": o.label,
                "evidence_ids": [str(mapping[o.key])],
                **annotations[o.key],
            }
            for o in extracted.observations
        ]
        possible = [
            {
                "text": item.text,
                "kind": item.kind,
                "evidence_ids": list(dict.fromkeys(str(mapping[k]) for k in item.observation_keys)),
            }
            for item in reflections.possible_reflections
        ]
        used_ids = {value for item in observations + possible for value in item["evidence_ids"]}
        records = [record for record in records if str(record.id) in used_ids]
        followup = reflections.followup_question
        if followup and followup.strip().casefold() in {
            question.strip().casefold() for question in context["answered_questions"]
        }:
            followup = None
        return Publication(
            AnalysisPayload(
                extraction={
                    "observations": observations,
                    "reported_emotions": execution.original["reported_emotions"],
                    "personal_context": {
                        "association_ids": [item["id"] for item in context["associations"]],
                        "associations_omitted": context["associations_omitted"],
                    },
                },
                reflections=possible,
                followup=followup,
            ),
            evidence=tuple(records),
            require_evidence=True,
        )


def source_without_text(source):
    return {k: v for k, v in source.items() if k != "text"}
