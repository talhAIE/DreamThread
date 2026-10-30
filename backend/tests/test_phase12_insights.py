from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from app.errors import APIError
from app.schemas.insights import CapsuleCitation, EmotionCount, InsightsResult, MotifCount
from app.services.insights import (
    InsightsService,
    month_bounds,
    parse_month,
    source_signature,
)


class Result:
    def __init__(self, value=None, rows=()):
        self.value = value
        self.rows = list(rows)

    def scalar(self):
        return self.value

    def mappings(self):
        return self

    def all(self):
        return self.rows


class ScriptedConnection:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(str(statement))
        return self.results.pop(0)

    async def scalar(self, statement):
        return (await self.execute(statement)).scalar()


@pytest.mark.anyio
async def test_month_aggregates_distinct_dreams_and_preserves_missing_emotions():
    motif_id = uuid4()
    connection = ScriptedConnection(
        [
            Result(4),
            Result(1),
            Result(rows=[
                {"emotion": "afraid", "dream_count": 2},
                {"emotion": "joyful", "dream_count": 2},
            ]),
            Result(rows=[
                {
                    "motif_id": motif_id,
                    "type": "place",
                    "label": "House",
                    "dream_count": 2,
                }
            ]),
            Result(3),
        ]
    )
    result = await InsightsService(None).aggregate(
        connection, uuid4(), date(2026, 10, 1), "America/New_York"
    )

    assert result == InsightsResult(
        month="2026-10",
        timezone="America/New_York",
        dream_date_count=4,
        recorded_at_count=3,
        missing_emotion_dream_count=1,
        motif_counts=[MotifCount(motif_id=motif_id, type="place", label="House", dream_count=2)],
        emotion_counts=[
            EmotionCount(emotion="afraid", dream_count=2),
            EmotionCount(emotion="joyful", dream_count=2),
        ],
    )
    assert "count(distinct" in connection.statements[0].lower()
    assert "jsonb_array_length" in connection.statements[1]
    assert "jsonb_array_elements_text" in connection.statements[2]
    assert "count(distinct" in connection.statements[3].lower()
    assert "confirmation_state" in connection.statements[3]


def test_month_boundaries_use_the_requested_timezone_across_dst():
    start, end = month_bounds(date(2026, 3, 1), "America/New_York")

    assert start == datetime(2026, 3, 1, 5, tzinfo=UTC)
    assert end == datetime(2026, 4, 1, 4, tzinfo=UTC)


def test_capsule_cache_signature_includes_period_timezone_and_both_versions():
    account = {"archive_version": 8, "memory_version": 3}

    signature = source_signature(account, date(2026, 10, 1), "UTC")

    assert signature == source_signature(account, date(2026, 10, 1), "UTC")
    assert signature != source_signature(account, date(2026, 10, 1), "Pacific/Auckland")
    assert signature != source_signature(
        {**account, "archive_version": 9}, date(2026, 10, 1), "UTC"
    )
    assert signature != source_signature(
        {**account, "memory_version": 4}, date(2026, 10, 1), "UTC"
    )


def test_month_parser_rejects_non_calendar_months():
    with pytest.raises(APIError):
        parse_month("2026-13")
    with pytest.raises(APIError):
        parse_month("2026-2")


def test_capsule_retrospective_is_deterministic_and_uses_verified_citation():
    evidence_id, dream_id, revision_id = uuid4(), uuid4(), uuid4()
    insights = InsightsResult(
        month="2026-10",
        timezone="UTC",
        dream_date_count=2,
        recorded_at_count=3,
        missing_emotion_dream_count=1,
        motif_counts=[
            MotifCount(motif_id=uuid4(), type="place", label="House", dream_count=2)
        ],
        emotion_counts=[
            EmotionCount(emotion="joyful", dream_count=1),
            EmotionCount(emotion="afraid", dream_count=1),
        ],
    )
    citation = CapsuleCitation(
        evidence_id=evidence_id,
        dream_id=dream_id,
        revision_id=revision_id,
        dream_date=date(2026, 10, 4),
        title="The House",
        quote="A blue door stood open.",
        start=0,
        end=24,
    )

    text = InsightsService._retrospective(insights, [citation])

    assert text == InsightsService._retrospective(insights, [citation])
    assert "2 saved dreams are dated 2026-10" in text
    assert "missing tags are not interpreted as neutral" in text
    assert f"[evidence:{evidence_id}]" in text
    cached = {
        "insights": insights.model_dump(mode="json"),
        "retrospective": text,
        "evidence_ids": [str(evidence_id)],
    }
    assert InsightsService._cached_content(cached, [citation]).retrospective == text
    assert InsightsService._cached_content(
        {**cached, "evidence_ids": []}, [citation]
    ) is None
    assert InsightsService._cached_content(
        {"insights": cached["insights"], "evidence_ids": [str(evidence_id)]}, [citation]
    ) is None


def test_empty_capsule_retrospective_does_not_infer_neutrality_or_history():
    insights = InsightsResult(
        month="2026-10",
        timezone="UTC",
        dream_date_count=0,
        recorded_at_count=0,
        missing_emotion_dream_count=0,
        motif_counts=[],
        emotion_counts=[],
    )

    text = InsightsService._retrospective(insights, [])

    assert "0 saved dreams" in text
    assert "No current-revision evidence excerpts" in text
