"""Tenant-scoped SQL insights and deterministic monthly capsules."""

import hashlib
import json
import re
from datetime import UTC, date, datetime, time
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import and_, delete, func, insert, select, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import tables
from app.errors import APIError
from app.schemas.identity import timezone_name
from app.schemas.insights import (
    CapsuleCitation,
    CapsuleContent,
    EmotionCount,
    InsightsResult,
    MotifCount,
    TimeCapsule,
    TimeCapsuleList,
    TimeCapsuleRefreshAccepted,
)
from app.services.accounts import active_account

A = tables["app.accounts"]
D = tables["app.dreams"]
R = tables["app.dream_revisions"]
DM = tables["app.dream_motifs"]
M = tables["app.motifs"]
SUMMARIES = tables["app.monthly_summaries"]
SUMMARY_EVIDENCE = tables["app.summary_evidence"]
E = tables["app.evidence"]
PREFS = tables["app.preferences"]
J = tables["app.jobs"]

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def parse_month(value: str) -> date:
    if not MONTH_PATTERN.fullmatch(value):
        raise APIError("invalid_month", "Month must use YYYY-MM format", 422)
    year, month = map(int, value.split("-"))
    try:
        parsed = date(year, month, 1)
        if year == 9999 and month == 12:
            raise ValueError
        return parsed
    except ValueError:
        raise APIError(
            "invalid_month",
            "Month is outside the supported calendar range",
            422,
        ) from None


def month_bounds(month: date, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    end_month = month.month + 1
    end_year = month.year
    if end_month == 13:
        end_month = 1
        end_year += 1
    start = datetime.combine(month, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(date(end_year, end_month, 1), time.min, tzinfo=zone).astimezone(UTC)
    return start, end


def source_signature(account, month: date, timezone: str) -> str:
    versions = {
        "archive_version": account["archive_version"],
        "memory_version": account["memory_version"],
        "month": month.isoformat(),
        "timezone": timezone,
    }
    return hashlib.sha256(
        json.dumps(versions, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class InsightsService:
    def __init__(self, db):
        self.db = db

    async def resolve_timezone(self, c, user_id: UUID, requested: str | None) -> str:
        if requested is not None:
            try:
                return timezone_name(requested)
            except ValueError:
                raise APIError("invalid_timezone", "Use a valid IANA timezone", 422) from None
        value = await c.scalar(select(PREFS.c.timezone).where(PREFS.c.user_id == user_id))
        return timezone_name(value or "UTC")

    async def aggregate(self, c, user_id: UUID, month: date, timezone: str) -> InsightsResult:
        next_month = date(
            month.year + (month.month == 12),
            month.month % 12 + 1,
            1,
        )
        base = and_(
            D.c.user_id == user_id,
            D.c.lifecycle == "saved",
            D.c.deleted_at.is_(None),
            D.c.dream_date >= month,
            D.c.dream_date < next_month,
            R.c.user_id == user_id,
            R.c.dream_id == D.c.id,
            R.c.id == D.c.current_revision_id,
        )

        dream_count = await c.scalar(
            select(func.count(func.distinct(D.c.id))).select_from(D.join(
                R,
                and_(
                    R.c.user_id == D.c.user_id,
                    R.c.dream_id == D.c.id,
                    R.c.id == D.c.current_revision_id,
                ),
            )).where(
                D.c.user_id == user_id,
                D.c.lifecycle == "saved",
                D.c.deleted_at.is_(None),
                D.c.dream_date >= month,
                D.c.dream_date < next_month,
            )
        )

        missing_emotions = await c.scalar(
            select(func.count(func.distinct(D.c.id)))
            .select_from(D.join(
                R,
                and_(
                    R.c.user_id == D.c.user_id,
                    R.c.dream_id == D.c.id,
                    R.c.id == D.c.current_revision_id,
                ),
            ))
            .where(base, func.jsonb_array_length(R.c.reported_emotions) == 0)
        )

        emotion_values = func.jsonb_array_elements_text(R.c.reported_emotions).table_valued(
            "emotion"
        ).lateral()
        emotions = (
            await c.execute(
                select(
                    emotion_values.c.emotion,
                    func.count(func.distinct(D.c.id)).label("dream_count"),
                )
                .select_from(
                    D.join(
                        R,
                        and_(
                            R.c.user_id == D.c.user_id,
                            R.c.dream_id == D.c.id,
                            R.c.id == D.c.current_revision_id,
                        ),
                    ).join(emotion_values, true())
                )
                .where(base)
                .group_by(emotion_values.c.emotion)
                .order_by(emotion_values.c.emotion)
            )
        ).mappings().all()

        motifs = (
            await c.execute(
                select(
                    M.c.id.label("motif_id"),
                    M.c.type,
                    M.c.canonical_label.label("label"),
                    func.count(func.distinct(D.c.id)).label("dream_count"),
                )
                .select_from(
                    D.join(
                        R,
                        and_(
                            R.c.user_id == D.c.user_id,
                            R.c.dream_id == D.c.id,
                            R.c.id == D.c.current_revision_id,
                        ),
                    )
                    .join(
                        DM,
                        and_(
                            DM.c.user_id == D.c.user_id,
                            DM.c.dream_id == D.c.id,
                            DM.c.revision_id == R.c.id,
                        ),
                    )
                    .join(M, and_(M.c.user_id == DM.c.user_id, M.c.id == DM.c.motif_id))
                )
                .where(base, DM.c.confirmation_state != "rejected")
                .group_by(M.c.id, M.c.type, M.c.canonical_label, M.c.normalized_key)
                .order_by(
                    func.count(func.distinct(D.c.id)).desc(),
                    M.c.type,
                    M.c.normalized_key,
                    M.c.id,
                )
            )
        ).mappings().all()

        start, end = month_bounds(month, timezone)
        recorded_at_count = await c.scalar(
            select(func.count(func.distinct(D.c.id)))
            .select_from(D.join(
                R,
                and_(
                    R.c.user_id == D.c.user_id,
                    R.c.dream_id == D.c.id,
                    R.c.id == D.c.current_revision_id,
                ),
            ))
            .where(
                D.c.user_id == user_id,
                D.c.lifecycle == "saved",
                D.c.deleted_at.is_(None),
                R.c.recorded_at >= start,
                R.c.recorded_at < end,
            )
        )
        return InsightsResult(
            month=month.strftime("%Y-%m"),
            timezone=timezone,
            dream_date_count=dream_count or 0,
            recorded_at_count=recorded_at_count or 0,
            missing_emotion_dream_count=missing_emotions or 0,
            motif_counts=[MotifCount(**row) for row in motifs],
            emotion_counts=[EmotionCount(**row) for row in emotions],
        )

    async def get_insights(self, user_id: UUID, month_text: str, timezone: str) -> InsightsResult:
        month = parse_month(month_text)
        async with self.db.tenant_transaction(user_id) as c:
            await active_account(c, user_id)
            try:
                selected_timezone = timezone_name(timezone)
            except ValueError:
                raise APIError("invalid_timezone", "Use a valid IANA timezone", 422) from None
            return await self.aggregate(c, user_id, month, selected_timezone)

    async def _verified_citations(self, c, user_id: UUID, summary_id: UUID):
        rows = (
            await c.execute(
                select(
                    E.c.id.label("evidence_id"),
                    E.c.dream_id,
                    E.c.revision_id,
                    E.c.start,
                    E.c.end,
                    E.c.exact_quote,
                    E.c.content_hash,
                    D.c.title,
                    D.c.dream_date,
                    D.c.current_revision_id,
                    R.c.text,
                    R.c.content_hash.label("revision_hash"),
                )
                .select_from(
                    SUMMARY_EVIDENCE.join(
                        E,
                        and_(
                            E.c.user_id == SUMMARY_EVIDENCE.c.user_id,
                            E.c.id == SUMMARY_EVIDENCE.c.evidence_id,
                        ),
                    )
                    .join(D, and_(D.c.user_id == E.c.user_id, D.c.id == E.c.dream_id))
                    .join(
                        R,
                        and_(
                            R.c.user_id == E.c.user_id,
                            R.c.dream_id == E.c.dream_id,
                            R.c.id == E.c.revision_id,
                        ),
                    )
                )
                .where(
                    SUMMARY_EVIDENCE.c.user_id == user_id,
                    SUMMARY_EVIDENCE.c.summary_id == summary_id,
                    D.c.lifecycle == "saved",
                    D.c.deleted_at.is_(None),
                    D.c.current_revision_id == E.c.revision_id,
                )
                .order_by(D.c.dream_date, E.c.dream_id, E.c.start, E.c.id)
            )
        ).mappings().all()
        citations = []
        seen_dreams = set()
        for row in rows:
            text = row["text"]
            if (
                row["dream_id"] in seen_dreams
                or row["content_hash"] != row["revision_hash"]
                or hashlib.sha256(text.encode("utf-8")).hexdigest() != row["revision_hash"]
                or text[row["start"] : row["end"]] != row["exact_quote"]
            ):
                continue
            seen_dreams.add(row["dream_id"])
            citations.append(
                CapsuleCitation(
                    evidence_id=row["evidence_id"],
                    dream_id=row["dream_id"],
                    revision_id=row["revision_id"],
                    dream_date=row["dream_date"],
                    title=row["title"],
                    quote=row["exact_quote"],
                    start=row["start"],
                    end=row["end"],
                )
            )
        return citations

    def _capsule_view(self, row, status, content=None):
        return TimeCapsule(
            id=row["id"],
            month=row["month"].strftime("%Y-%m"),
            timezone=row["timezone"],
            status=status,
            generated_at=row["updated_at"] if status == "ready" else None,
            capsule=content,
        )

    @staticmethod
    def _cached_content(summary, citations):
        summary = summary or {}
        expected_ids = summary.get("evidence_ids")
        retrospective = summary.get("retrospective")
        if (
            not isinstance(expected_ids, list)
            or expected_ids != [str(item.evidence_id) for item in citations]
            or not isinstance(retrospective, str)
        ):
            return None
        try:
            insights = InsightsResult.model_validate(summary.get("insights"))
        except ValidationError:
            return None
        return CapsuleContent(
            insights=insights,
            retrospective=retrospective,
            citations=citations,
        )

    @staticmethod
    def _retrospective(insights: InsightsResult, citations: list[CapsuleCitation]) -> str:
        dream_count = insights.dream_date_count
        dream_noun = "dream" if dream_count == 1 else "dreams"
        parts = [
            f"{dream_count} saved {dream_noun} are dated {insights.month}; "
            f"{insights.recorded_at_count} were recorded during that month in "
            f"{insights.timezone}."
        ]
        if insights.motif_counts:
            top = insights.motif_counts[0]
            noun = "dream" if top.dream_count == 1 else "dreams"
            parts.append(
                f"The most frequently linked motif was {top.label}, appearing in "
                f"{top.dream_count} {noun}."
            )
        if insights.emotion_counts:
            top = min(
                insights.emotion_counts,
                key=lambda item: (-item.dream_count, item.emotion),
            )
            noun = "dream" if top.dream_count == 1 else "dreams"
            parts.append(
                f"The most frequently reported emotion tag was {top.emotion}, "
                f"reported in {top.dream_count} {noun}."
            )
        if insights.missing_emotion_dream_count:
            missing = insights.missing_emotion_dream_count
            noun = "dream has" if missing == 1 else "dreams have"
            parts.append(
                f"{missing} {noun} no reported emotion tags; missing tags are not "
                "interpreted as neutral."
            )
        if citations:
            citation = citations[0]
            parts.append(
                f'A verified excerpt from "{citation.title}" is "{citation.quote}" '
                f"[evidence:{citation.evidence_id}]."
            )
        else:
            parts.append("No current-revision evidence excerpts were available to cite.")
        return " ".join(parts)

    async def list_capsules(self, user_id: UUID, timezone: str | None = None):
        async with self.db.tenant_transaction(user_id) as c:
            account = await active_account(c, user_id)
            selected_timezone = await self.resolve_timezone(c, user_id, timezone)
            rows = (
                await c.execute(
                    select(SUMMARIES)
                    .where(
                        SUMMARIES.c.user_id == user_id,
                        SUMMARIES.c.timezone == selected_timezone,
                    )
                    .order_by(SUMMARIES.c.month.desc(), SUMMARIES.c.id)
                )
            ).mappings().all()
            items = []
            for row in rows:
                valid = row["state"] == "ready" and row["source_signature"] == source_signature(
                    account, row["month"], selected_timezone
                )
                status = "ready" if valid else "pending" if row["state"] == "pending" else "stale"
                content = None
                if valid:
                    citations = await self._verified_citations(c, user_id, row["id"])
                    content = self._cached_content(row["summary"], citations)
                    if content is None:
                        status = "stale"
                        valid = False
                if not valid and row["state"] == "ready":
                    await c.execute(
                        update(SUMMARIES)
                        .where(SUMMARIES.c.user_id == user_id, SUMMARIES.c.id == row["id"])
                        .values(state="stale", summary={})
                    )
                    await c.execute(
                        delete(SUMMARY_EVIDENCE).where(
                            SUMMARY_EVIDENCE.c.user_id == user_id,
                            SUMMARY_EVIDENCE.c.summary_id == row["id"],
                        )
                    )
                items.append(self._capsule_view(row, status, content))
            return TimeCapsuleList(items=items)

    async def get_capsule(self, user_id: UUID, month_text: str, timezone: str | None = None):
        month = parse_month(month_text)
        async with self.db.tenant_transaction(user_id) as c:
            account = await active_account(c, user_id)
            selected_timezone = await self.resolve_timezone(c, user_id, timezone)
            row = (
                await c.execute(
                    select(SUMMARIES).where(
                        SUMMARIES.c.user_id == user_id,
                        SUMMARIES.c.month == month,
                        SUMMARIES.c.timezone == selected_timezone,
                    )
                )
            ).mappings().one_or_none()
            if row is None:
                raise APIError("not_found", "Time capsule not found", 404)
            valid = row["state"] == "ready" and row["source_signature"] == source_signature(
                account, month, selected_timezone
            )
            content = None
            status = "ready" if valid else "pending" if row["state"] == "pending" else "stale"
            if valid:
                citations = await self._verified_citations(c, user_id, row["id"])
                content = self._cached_content(row["summary"], citations)
                if content is None:
                    valid = False
                    status = "stale"
            if not valid and row["state"] == "ready":
                await c.execute(
                    update(SUMMARIES)
                    .where(SUMMARIES.c.user_id == user_id, SUMMARIES.c.id == row["id"])
                    .values(state="stale", summary={})
                )
                await c.execute(
                    delete(SUMMARY_EVIDENCE).where(
                        SUMMARY_EVIDENCE.c.user_id == user_id,
                        SUMMARY_EVIDENCE.c.summary_id == row["id"],
                    )
                )
            return self._capsule_view(row, status, content)

    async def request_refresh(self, user_id: UUID, month_text: str, timezone: str | None = None):
        month = parse_month(month_text)
        async with self.db.tenant_transaction(user_id) as c:
            account = await active_account(c, user_id)
            selected_timezone = await self.resolve_timezone(c, user_id, timezone)
            signature = source_signature(account, month, selected_timezone)
            dedupe = (
                f"{month:%Y-%m}:{selected_timezone}:"
                f"{account['archive_version']}:{account['memory_version']}"
            )
            prior = (
                await c.execute(
                    select(J)
                    .where(
                        J.c.user_id == user_id,
                        J.c.kind == "time_capsule_refresh",
                        J.c.dedupe_key == dedupe,
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if prior and prior["status"] in {"queued", "running"}:
                return TimeCapsuleRefreshAccepted(
                    job_id=prior["id"],
                    status=prior["status"],
                    month=month.strftime("%Y-%m"),
                    timezone=selected_timezone,
                )
            if prior and prior["status"] == "succeeded":
                cached = (
                    await c.execute(
                        select(SUMMARIES.c.state, SUMMARIES.c.source_signature).where(
                            SUMMARIES.c.user_id == user_id,
                            SUMMARIES.c.month == month,
                            SUMMARIES.c.timezone == selected_timezone,
                        )
                    )
                ).mappings().one_or_none()
                if (
                    cached
                    and cached["state"] == "ready"
                    and cached["source_signature"] == signature
                ):
                    return TimeCapsuleRefreshAccepted(
                        job_id=prior["id"],
                        status=prior["status"],
                        month=month.strftime("%Y-%m"),
                        timezone=selected_timezone,
                    )
            if prior:
                dedupe += f":retry:{uuid4()}"
            job_id = uuid4()
            values = {
                "id": job_id,
                "user_id": user_id,
                "kind": "time_capsule_refresh",
                "dedupe_key": dedupe,
                "payload": {"month": month.strftime("%Y-%m"), "timezone": selected_timezone},
                "data_epoch": account["data_epoch"],
                "input_versions": {
                    "archive_version": account["archive_version"],
                    "memory_version": account["memory_version"],
                    "source_signature": signature,
                },
            }
            await c.execute(insert(J).values(**values))
            await c.execute(
                pg_insert(SUMMARIES)
                .values(
                    user_id=user_id,
                    month=month,
                    timezone=selected_timezone,
                    summary={},
                    source_signature=signature,
                    state="pending",
                )
                .on_conflict_do_update(
                    index_elements=[SUMMARIES.c.user_id, SUMMARIES.c.month, SUMMARIES.c.timezone],
                    set_={"summary": {}, "source_signature": signature, "state": "pending"},
                )
            )
            row = (await c.execute(select(J).where(J.c.id == job_id))).mappings().one()
            return TimeCapsuleRefreshAccepted(
                job_id=row["id"],
                status=row["status"],
                month=month.strftime("%Y-%m"),
                timezone=selected_timezone,
            )

    async def run_refresh(self, job):
        month = parse_month(job["payload"]["month"])
        timezone = timezone_name(job["payload"]["timezone"])
        async with self.db.tenant_transaction(job["user_id"]) as c:
            account = (
                await c.execute(
                    select(A).where(A.c.id == job["user_id"]).with_for_update()
                )
            ).mappings().one_or_none()
            current = (
                await c.execute(
                    select(J).where(J.c.user_id == job["user_id"], J.c.id == job["id"])
                    .with_for_update()
                )
            ).mappings().one_or_none()
            now = await c.scalar(select(func.clock_timestamp()))
            if (
                account is None
                or account["deletion_state"] != "active"
                or current is None
                or current["status"] != "running"
                or current["lease_token"] != job["lease_token"]
                or current["leased_until"] is None
                or current["leased_until"] <= now
                or current["data_epoch"] != account["data_epoch"]
                or current["input_versions"].get("archive_version") != account["archive_version"]
                or current["input_versions"].get("memory_version") != account["memory_version"]
            ):
                if current is not None and current["status"] == "running":
                    await c.execute(
                        update(J)
                        .where(J.c.user_id == job["user_id"], J.c.id == job["id"])
                        .values(
                            status="cancelled",
                            stage="source_changed",
                            error_code="source_changed",
                            lease_token=None,
                            leased_until=None,
                            deadline_at=None,
                            stage_deadline_at=None,
                        )
                    )
                return

            insights = await self.aggregate(c, job["user_id"], month, timezone)
            citation_rows = (
                await c.execute(
                    select(
                        E.c.id,
                        E.c.dream_id,
                        E.c.revision_id,
                        E.c.start,
                        E.c.end,
                        E.c.exact_quote,
                        E.c.content_hash,
                        D.c.title,
                        D.c.dream_date,
                        R.c.text,
                        R.c.content_hash.label("revision_hash"),
                    )
                    .select_from(
                        E.join(
                            D,
                            and_(
                                D.c.user_id == E.c.user_id,
                                D.c.id == E.c.dream_id,
                                D.c.current_revision_id == E.c.revision_id,
                            ),
                        ).join(
                            R,
                            and_(
                                R.c.user_id == E.c.user_id,
                                R.c.dream_id == E.c.dream_id,
                                R.c.id == E.c.revision_id,
                            ),
                        )
                    )
                    .where(
                        E.c.user_id == job["user_id"],
                        D.c.lifecycle == "saved",
                        D.c.deleted_at.is_(None),
                        D.c.dream_date >= month,
                        D.c.dream_date < date(
                            month.year + (month.month == 12), month.month % 12 + 1, 1
                        ),
                    )
                    .order_by(D.c.dream_date, E.c.dream_id, E.c.start, E.c.id)
                )
            ).mappings().all()
            selected = []
            seen_dreams = set()
            for row in citation_rows:
                if (
                    row["dream_id"] not in seen_dreams
                    and row["content_hash"] == row["revision_hash"]
                    and hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
                    == row["revision_hash"]
                    and row["text"][row["start"] : row["end"]] == row["exact_quote"]
                ):
                    seen_dreams.add(row["dream_id"])
                    selected.append(row)

            summary_row = (
                await c.execute(
                    select(SUMMARIES).where(
                        SUMMARIES.c.user_id == job["user_id"],
                        SUMMARIES.c.month == month,
                        SUMMARIES.c.timezone == timezone,
                    ).with_for_update()
                )
            ).mappings().one_or_none()
            if summary_row is None:
                return
            await c.execute(
                delete(SUMMARY_EVIDENCE).where(
                    SUMMARY_EVIDENCE.c.user_id == job["user_id"],
                    SUMMARY_EVIDENCE.c.summary_id == summary_row["id"],
                )
            )
            evidence_ids = []
            for row in selected:
                evidence_ids.append(str(row["id"]))
                await c.execute(
                    insert(SUMMARY_EVIDENCE).values(
                        user_id=job["user_id"],
                        summary_id=summary_row["id"],
                        evidence_id=row["id"],
                    )
                )
            await c.execute(
                update(SUMMARIES)
                .where(SUMMARIES.c.user_id == job["user_id"], SUMMARIES.c.id == summary_row["id"])
                .values(
                    summary={
                        "insights": insights.model_dump(mode="json"),
                        "retrospective": self._retrospective(
                            insights,
                            [
                                CapsuleCitation(
                                    evidence_id=row["id"],
                                    dream_id=row["dream_id"],
                                    revision_id=row["revision_id"],
                                    dream_date=row["dream_date"],
                                    title=row["title"],
                                    quote=row["exact_quote"],
                                    start=row["start"],
                                    end=row["end"],
                                )
                                for row in selected
                            ],
                        ),
                        "evidence_ids": evidence_ids,
                    },
                    source_signature=current["input_versions"]["source_signature"],
                    state="ready",
                )
            )
            await c.execute(
                update(J)
                .where(J.c.user_id == job["user_id"], J.c.id == job["id"])
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
