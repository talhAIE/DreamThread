"""Frozen Phase 2 schema snapshot. Never edit after release; add new migrations.

SQLAlchemy Core tables are shared by repositories and the baseline migration.
Embedding dimensions are supplied by the verified deployment configuration.
"""

from uuid import uuid4

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB


def build_metadata(dimensions: int | None) -> MetaData:
    m = MetaData(schema="app", naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })

    def col(name, kind=Text, *, nullable=False, default=None):
        return Column(name, kind, nullable=nullable, server_default=default)

    def json(name, default="{}"):
        return col(name, JSONB, default=text("'" + default + "'::jsonb"))

    def ref(parent, name, *, nullable=False, ondelete="CASCADE"):
        return (
            col(name, Uuid, nullable=nullable),
            ForeignKeyConstraint(["user_id", name], [f"app.{parent}.user_id", f"app.{parent}.id"],
                                 ondelete=ondelete, name=f"fk_{name}_{parent}"),
        )

    def choice(name, values):
        return CheckConstraint(f"{name} IN ({','.join(repr(v) for v in values.split())})",
                               name=f"ck_{name}_allowed")

    def table(name, *fields, uniques=(), checks=()):
        t = Table(
            name, m,
            Column("id", Uuid, primary_key=True, default=uuid4),
            col("user_id", Uuid),
            col("created_at", DateTime(timezone=True), default=func.now()),
            col("updated_at", DateTime(timezone=True), default=func.now()),
            UniqueConstraint("user_id", "id", name=f"uq_{name}_owner_id"),
            ForeignKeyConstraint(["user_id"], ["app.accounts.id"], ondelete="CASCADE"),
            *fields, *(UniqueConstraint(*u, name=f"uq_{name}_{i}")
                       for i, u in enumerate(uniques)), *checks,
        )
        Index(f"ix_{name}_owner", t.c.user_id)
        return t

    Table("accounts", m, Column("id", Uuid, primary_key=True),
          col("display_name", String(120), default=""), col("timezone", String(80), default="UTC"),
          col("data_epoch", Integer, default="0"), col("archive_version", Integer, default="0"),
          col("memory_version", Integer, default="0"),
          col("deletion_state", String(20), default="active"),
          col("created_at", DateTime(timezone=True), default=func.now()),
          col("updated_at", DateTime(timezone=True), default=func.now()),
          choice("deletion_state", "active deleting deleted"),
          CheckConstraint("data_epoch >= 0 AND archive_version >= 0 AND memory_version >= 0",
                          name="ck_account_versions"))
    table("preferences", col("auto_connections", Boolean, default="true"),
          col("require_memory_confirmation", Boolean, default="true"),
          col("timezone", String(80), default="UTC"), json("display_settings"),
          uniques=[("user_id",)])
    dreams = table("dreams", col("title", String(160)), col("dream_date", Date),
                   col("current_revision_id", Uuid, nullable=True),
                   col("version", Integer, default="1"),
                   col("lifecycle", String(20), default="draft"),
                   col("input_mode", String(10), default="text"),
                   col("analysis_state", String(20), default="queued"),
                   col("deleted_at", DateTime(timezone=True), nullable=True), checks=[
                       choice("lifecycle", "draft saved"), choice("input_mode", "text voice"),
                       choice("analysis_state", "queued extracting embedding retrieving comparing "
                              "verifying publishing ready failed stale"),
                       CheckConstraint("version >= 1", name="ck_dream_version"),
                   ])
    revisions = table("dream_revisions", *ref("dreams", "dream_id"),
                      col("revision", Integer), col("text", Text), col("content_hash", String(64)),
                      json("reported_emotions", "[]"),
                      col("recorded_at", DateTime(timezone=True), default=func.now()),
                      uniques=[("user_id", "dream_id", "revision"),
                               ("user_id", "dream_id", "id")], checks=[
                          CheckConstraint("revision >= 1", name="ck_revision_positive"),
                          CheckConstraint("jsonb_typeof(reported_emotions) = 'array'",
                                          name="ck_emotions_array"),
                      ])
    dreams.append_constraint(ForeignKeyConstraint(
        ["user_id", "id", "current_revision_id"],
        [revisions.c.user_id, revisions.c.dream_id, revisions.c.id],
        name="fk_dream_current_revision", use_alter=True,
        deferrable=True, initially="DEFERRED",
    ))

    def source():
        return (
            *ref("dreams", "dream_id"), col("revision_id", Uuid),
            ForeignKeyConstraint(["user_id", "dream_id", "revision_id"],
                                 [revisions.c.user_id, revisions.c.dream_id, revisions.c.id],
                                 ondelete="CASCADE"),
        )

    table("analyses", *source(), col("pipeline_version", String(50)),
          col("memory_version", Integer), json("extraction"), json("reflections", "[]"),
          col("followup", Text, nullable=True), col("state", String(20), default="pending"),
          uniques=[("user_id", "revision_id", "pipeline_version", "memory_version")],
          checks=[choice("state", "pending ready failed stale")])
    table("motifs", col("type", String(20)), col("canonical_label", String(160)),
          col("normalized_key", String(200)), uniques=[("user_id", "type", "normalized_key")],
          checks=[choice("type", "place person object event emotion")])
    table("dream_motifs", *source(), *ref("motifs", "motif_id"),
          col("origin", String(20)), col("confirmation_state", String(20), default="suggested"),
          uniques=[("user_id", "revision_id", "motif_id")], checks=[
              choice("origin", "extracted user"),
              choice("confirmation_state", "suggested confirmed rejected"),
          ])
    table("associations", *ref("motifs", "motif_id"), col("meaning", Text),
          col("status", String(20), default="suggested"), col("enabled", Boolean, default="true"),
          col("version", Integer, default="1"),
          *ref("dreams", "source_dream_id", nullable=True),
          checks=[choice("status", "suggested confirmed rejected")])
    table("detail_overrides", *source(), col("detail_key", String(200)),
          json("corrected_value"), col("removed", Boolean, default="false"),
          col("version", Integer, default="1"),
          uniques=[("user_id", "revision_id", "detail_key")])
    table("followup_answers", *source(), col("question_version", String(50)),
          col("answer", Text, nullable=True), col("skipped", Boolean, default="false"),
          *ref("associations", "association_id", nullable=True),
          uniques=[("user_id", "revision_id", "question_version")])
    table("dream_embeddings", *source(), col("model_id", String(200)),
          col("embedding_version", String(50)), col("embedding", VECTOR(dimensions)),
          col("input_hash", String(64)),
          uniques=[("user_id", "revision_id", "model_id", "embedding_version")])
    table("jobs", col("kind", String(50)), col("dedupe_key", String(200)), json("payload"),
          col("status", String(20), default="queued"), col("stage", String(50), default="queued"),
          col("attempts", Integer, default="0"),
          col("run_after", DateTime(timezone=True), default=func.now()),
          col("lease_token", Uuid, nullable=True),
          col("leased_until", DateTime(timezone=True), nullable=True),
          col("heartbeat_at", DateTime(timezone=True), nullable=True),
          col("error_code", String(80), nullable=True), col("data_epoch", Integer),
          json("input_versions"), uniques=[("user_id", "kind", "dedupe_key")],
          checks=[choice("status", "queued running succeeded failed cancelled"),
                  CheckConstraint("attempts >= 0", name="ck_attempts_nonnegative")])
    table("model_runs", *ref("jobs", "job_id", nullable=True),
          col("provider", String(50)), col("model_id", String(200)), col("task", String(50)),
          col("prompt_version", String(50)), col("latency_ms", Float, nullable=True),
          col("input_tokens", Integer, nullable=True), col("output_tokens", Integer, nullable=True),
          col("status", String(20)), col("trace_stage", String(50)))

    def pair():
        return (*ref("dreams", "dream_a_id"), *ref("dreams", "dream_b_id"),
                col("relation_key", String(200)),
                CheckConstraint("dream_a_id < dream_b_id", name="ck_pair_order"))

    table("connections", *pair(), col("status", String(20), default="suggested"),
          col("observation", Text), json("basis_versions"),
          *ref("model_runs", "model_run_id", nullable=True),
          uniques=[("user_id", "dream_a_id", "dream_b_id", "relation_key")],
          checks=[choice("status", "suggested confirmed rejected stale")])
    table("connection_decisions", *pair(), col("decision", String(20)),
          col("version", Integer, default="1"),
          uniques=[("user_id", "dream_a_id", "dream_b_id", "relation_key")],
          checks=[choice("decision", "confirmed rejected")])
    table("evidence", *source(), col("start", Integer), col("end", Integer),
          col("exact_quote", Text), col("content_hash", String(64)), checks=[
              CheckConstraint('"start" >= 0 AND "end" > "start"', name="ck_evidence_span"),
          ])
    table("storylines", *ref("motifs", "motif_id"), col("title", String(160)),
          col("status", String(20), default="active"), col("source_signature", String(64)),
          uniques=[("user_id", "motif_id")], checks=[choice("status", "active stale")])
    table("storyline_entries", *ref("storylines", "storyline_id"), *source(),
          uniques=[("user_id", "storyline_id", "dream_id")])
    table("conversations", col("title", String(160)))
    table("messages", *ref("conversations", "conversation_id"), col("role", String(20)),
          col("body", Text), col("status", String(20), default="pending"),
          col("request_key", String(200)), json("context_versions"),
          uniques=[("user_id", "conversation_id", "request_key", "role")], checks=[
              choice("role", "user assistant"),
              choice("status", "pending completed failed stale"),
          ])
    table("idempotency_keys", col("route", String(200)), col("key", String(200)),
          col("request_hash", String(64)), col("result_resource_id", Uuid, nullable=True),
          col("expires_at", DateTime(timezone=True)), uniques=[("user_id", "route", "key")])
    table("monthly_summaries", col("month", Date), col("timezone", String(80)),
          json("summary"), col("source_signature", String(64)),
          col("state", String(20), default="ready"),
          uniques=[("user_id", "month", "timezone")])
    table("audio_assets", col("storage_key", String(500)), json("verified_metadata"),
          col("lifecycle", String(30), default="pending"),
          col("expires_at", DateTime(timezone=True)), uniques=[("storage_key",)])
    table("cleanup_tasks", col("operation_key", String(200)), json("storage_keys", "[]"),
          col("state", String(20), default="queued"), col("attempts", Integer, default="0"),
          uniques=[("user_id", "operation_key")])
    table("exports", col("format", String(10)), *ref("jobs", "job_id"),
          col("archive_version", Integer), col("data_epoch", Integer),
          col("status", String(20), default="pending"),
          col("storage_key", String(500), nullable=True),
          col("expires_at", DateTime(timezone=True)),
          checks=[choice("format", "json text")])

    for name, parent, column in [
        ("connection_evidence", "connections", "connection_id"),
        ("message_evidence", "messages", "message_id"),
        ("analysis_evidence", "analyses", "analysis_id"),
        ("summary_evidence", "monthly_summaries", "summary_id"),
        ("storyline_evidence", "storylines", "storyline_id"),
        ("motif_evidence", "dream_motifs", "dream_motif_id"),
    ]:
        table(name, *ref(parent, column), *ref("evidence", "evidence_id"),
              uniques=[("user_id", column, "evidence_id")])
    Index("ix_dreams_date", dreams.c.user_id, dreams.c.dream_date.desc(), dreams.c.id.desc())
    jobs = m.tables["app.jobs"]
    Index("ix_jobs_claim", jobs.c.run_after, jobs.c.created_at,
          postgresql_where=text("status IN ('queued', 'running')"))
    Table("schema_configuration", m, Column("id", Integer, primary_key=True),
          col("embedding_dimensions", Integer),
          CheckConstraint("id = 1 AND embedding_dimensions > 0", name="ck_schema_configuration"))
    return m
