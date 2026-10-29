import json

import pytest
from pydantic import ValidationError
from test_phase6_inference import MemoryExecution, extraction, source

from app.schemas.analysis import Extraction
from app.schemas.memory import DetailPatch, FollowupPut, MemoryCreate, MemoryPatch
from app.services.personal_context import apply_overrides, detail_key, empty_context


@pytest.mark.parametrize(
    "payload",
    [
        {"motif": {"type": "place", "label": " "}, "meaning": "safe"},
        {"motif": {"type": "place", "label": "house"}, "meaning": " "},
        {"motif": {"type": "place", "label": "house"}, "meaning": "NUL\x00"},
    ],
)
def test_invalid_memory_strings_are_rejected(payload):
    with pytest.raises(ValidationError):
        MemoryCreate.model_validate(payload)


def test_nullable_empty_updates_and_implicit_followup_consent_are_rejected():
    for schema, payload in [
        (MemoryPatch, {"expected_version": 1}),
        (MemoryPatch, {"expected_version": 1, "enabled": None}),
        (DetailPatch, {"expected_version": 1, "detail_key": "detail_" + "0" * 40, "label": " "}),
        (
            FollowupPut,
            {
                "expected_version": 1,
                "question_version": "0" * 40,
                "skipped": True,
                "save_as_association": True,
            },
        ),
    ]:
        with pytest.raises(ValidationError):
            schema.model_validate(payload)


def test_override_survives_model_key_and_quote_width_changes():
    execution = MemoryExecution()
    original = Extraction.model_validate_json(json.dumps(extraction(execution))).observations[0]
    replacement = extraction(execution, quote="a house")
    replacement["observations"][0]["key"] = "o9"
    context = empty_context()
    context["overrides"] = [
        {
            "detail_key": detail_key(original),
            "removed": False,
            "corrected_value": {
                "original": original.model_dump(mode="json"),
                "label": "Grandmother's home",
            },
        }
    ]
    output, annotations = apply_overrides(
        Extraction.model_validate_json(json.dumps(replacement)), context, source(execution)
    )
    assert len(output.observations) == 1 and output.observations[0].label == "Grandmother's home"
    assert output.observations[0].citation.quote == "house"
    assert annotations["o1"]["detail_key"] == detail_key(original)
    assert annotations["o1"]["origin"] == "user"
    context["overrides"][0]["removed"] = True
    removed, _ = apply_overrides(
        Extraction.model_validate_json(json.dumps(replacement)), context, source(execution)
    )
    assert removed.observations == []
