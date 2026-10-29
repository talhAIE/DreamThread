# DreamThread backend implementation plan

Status: Phases 1-8 code implemented; 151 regression tests passed before the Neon transition.
Database decision updated October 29, 2026: hosted Neon PostgreSQL/pgvector replaces the local
application database and the earlier planned Supabase database. Supabase remains Auth only
(plus optional future voice storage). Live Neon and model verification remain pending credentials.
Prepared: October 28, 2026.
Track: Best Apps and Agents, Nebius x NVIDIA Global AI Hackathon.

## 1. Goal and how to follow this plan

Build a private dream journal that saves original accounts, connects recurring details over time, and answers questions using inspectable evidence from the user's own entries.

Follow phases in order. Check off a phase only after its acceptance checks pass. Each phase should produce runnable code, migrations where needed, and the specified tests. Commit each completed phase with a short verification note. Later phases must preserve the earlier checks.

This plan defines a testable route to a working system; it does not claim that writing code from it guarantees correctness. External credentials, account-specific model capabilities, hosted deployment, and live inference must be verified during implementation.

### Product boundaries

- Interpretations are tentative reflection prompts, not diagnoses, prophecy, or established psychological facts.
- Original text, user-reported emotions, model-extracted observations, user-confirmed associations, and speculative reflections are separate data types.
- Every factual AI claim about an entry needs verified source evidence.
- The system must be useful with one entry and must never invent a history for an empty journal.
- Read/write access is scoped to the authenticated user.
- A shared word such as "house" does not prove two dreams describe the same place.
- A public demo uses clearly labeled fictional examples, never private journal entries.
- UI images are visual references. Their placeholder counts, dates, quotes, and generated text are not specifications or real data.

### Release levels

| Release | Required phases | Result |
|---|---|---|
| Core hackathon release | 0-11, 14-16 | Text capture, reflection, memory, real continuity, constellation, Detective, export/delete, hosted demo |
| Complete planned backend | All phases | Adds insights/time capsules and verified voice workflow |
| Later improvements | After release | Scheduled notifications, advanced reranking, large-archive ANN tuning, mobile-specific services |

Phases 12 and 13 are planned features, not prerequisites for proving the core loop. Hide any frontend feature whose backend is not available. Do not show a nonfunctional microphone in a text-only release.

No calendar estimates are fixed because the submission deadline, team capacity, and hosting budget have not been supplied.

## 2. Hackathon alignment and evidence

The supplied rules require a working system using at least one NVIDIA open-source model on Nebius Token Factory or Nebius AI Cloud. Use Nemotron through Token Factory for core application reasoning. Neon handles persistence and Supabase handles authentication; neither replaces the required inference integration.

| Requirement or judging criterion | Backend implementation | Proof to retain |
|---|---|---|
| NVIDIA model on Nebius | Nemotron extraction and continuity/Detective calls | Exact model IDs, account capability probe, redacted live run metadata |
| Technological implementation | Persisted retrieval, bounded model tool use, evidence validation | Integration tests and a short stage trace |
| Coherent product design | Stable frontend contracts and meaningful processing states | Working end-to-end UI journey |
| Credible impact | Recurring-dream retrieval for regular dream journalers | Clearly scoped problem statement and user feedback if collected |
| Originality | User-specific associations plus historical comparisons | Correction and changing-house demonstration |
| Working demo URL | Public HTTPS app with authenticated API | Clean-browser acceptance run |
| Public source + license | Repository with root LICENSE and README | Repository URL and clean setup verification |
| Public video <= 3 minutes | Capture a live retrieval and source inspection | Public YouTube URL |
| Tool feedback | Record actual benefits, limitations, failures, latency | docs/nebius-feedback.md |
| Pre-existing work disclosure | Proposal/UI existed before implementation | Honest dated work log; compare with official submission period |

Nebius Serverless Endpoints and Jobs are encouraged in the supplied track text, not mandatory. Token Factory inference is the baseline. Add those services only after confirming they suit the account and workload; never describe a generic Docker worker as a Nebius Serverless Job.

## 3. Chosen architecture

```text
Next.js client
  | sign-in / refresh
  +--------------------------> Supabase Auth
  |
  | HTTPS /api/v1 + bearer access token
  v
FastAPI API
  | validate identity, permissions, limits
  | short database transactions
  +--------------------------> Neon PostgreSQL + pgvector [direct verified-TLS connection]
  |                             - private application schema
  |                             - durable job queue
  |                             - user-scoped journal and graph
  |
  +--------------------------> private Supabase Storage [voice only]

Python worker
  | lease persisted jobs
  | retrieve only the job owner's data
  +--------------------------> Nebius Token Factory
  |                             - selected NVIDIA Nemotron
  |                             - verified embedding model
  |
  +--------------------------> verify and atomically publish results

Client polls jobs and reloads completed resources.
```

### Technology choices

| Area | Choice |
|---|---|
| Runtime | Python 3.12 baseline, confirm compatibility and pin dependencies |
| API | FastAPI, Uvicorn, Pydantic v2, pydantic-settings |
| Database | Hosted Neon PostgreSQL with pgvector; no local application database |
| ORM/migrations | SQLAlchemy 2 async engine + psycopg 3; Alembic sync migrations |
| Auth verification | Supabase Auth user endpoint through HTTPX; optional JWKS optimization later |
| Model integration | OpenAI Python SDK configured with Nebius base URL |
| Background work | Separate Python worker and PostgreSQL jobs table |
| Retrieval | Exact user-filtered vector search plus text/motif candidates |
| Tests | pytest, pytest-asyncio, HTTPX ASGI client; real PostgreSQL integration database |
| Quality | Ruff and a locked dependency file |
| Packaging | Docker image; separate API and worker processes |
| Optional speech | Locally hosted faster-whisper adapter, only after model/hardware probe |
| Observability | Structured logs + metadata-only model_runs/job stage records |

Use direct Neon endpoints (connection pooling OFF) for administrator provisioning, migrations,
runtime and queue connections in this release. Neon -pooler URLs are rejected until explicit
transaction-pool tenant-context and prepared-statement checks are added. Use verified TLS with
the runtime's CA bundle. SQLAlchemy pools remain small (runtime 3 per process, queue 1), use
pre-ping/recycling and configurable cold-start timeouts. One API plus one worker normally allows
about seven connections; replicas/CLI sessions add more. A polling worker may keep compute active.

Create restricted logins through SQL, not Neon's Roles UI/API, and keep four separate connection
URLs. ADMIN_DATABASE_URL is setup-only; MIGRATION_DATABASE_URL belongs to release/migration;
DATABASE_URL is tenant runtime; QUEUE_DATABASE_URL is cross-user leasing only. Runtime/queue
must reject neon_superuser and broad read/write-role membership as well as privileged attributes.
The provisioning script installs vector in extensions without requiring full Postgres superuser.

The current implementation and complete run sequence are documented in backend/docs/neon-setup.md.
No existing local journal is automatically copied, deleted or migrated to another provider.

No separate Neo4j, Redis, Celery, or agent framework is required for the first release. The relational graph and durable queue are sufficient at this scale.

## 4. Decisions and non-negotiable invariants

1. Never accept user_id from the client as authority. Derive it from a validated token.
2. Never run API/worker queries under a database owner, superuser, or BYPASSRLS role.
3. Every child reference must belong to the same user as its parent; enforce composite foreign keys.
4. Saving a dream must not depend on a successful model request.
5. AI calls run outside database transactions and row locks.
6. Store a brief execution audit, not hidden chain-of-thought or provider reasoning text.
7. Link statuses are suggested, confirmed, or rejected. Rejected links stay rejected across retries.
8. A model cannot confirm a personal association, execute SQL, fetch URLs, or delete data.
9. Every derived object carries the source revisions used to produce it.
10. Revision changes, memory corrections, and deletion invalidate affected derived content before it is served.
11. Quote validation proves that text exists in a source, not that an interpretation is true.
12. No unverified partial model answer is streamed as a final sourced answer.
13. Deletion must prevent in-flight work from restoring removed data.
14. Model availability and tool/schema support are verified with the actual account, not assumed from a name.

## 5. Planned project layout

```text
backend/
  pyproject.toml
  uv.lock
  .env.example
  Dockerfile
  compose.yaml
  alembic.ini
  migrations/
  app/
    main.py
    config.py
    api/
      dependencies.py
      errors.py
      routes/
        health.py
        me.py
        dreams.py
        jobs.py
        memory.py
        connections.py
        constellation.py
        storylines.py
        detective.py
        insights.py
        privacy.py
        audio.py
        demo.py
    auth/
      identity.py
    db/
      session.py
      models/
      repositories/
    schemas/
    services/
      journal.py
      revisions.py
      extraction.py
      retrieval.py
      evidence.py
      continuity.py
      memory.py
      storylines.py
      detective.py
      insights.py
      privacy.py
      audio.py
    providers/
      nebius.py
      embeddings.py
      transcription.py
    agents/
      tools.py
      runner.py
    prompts/
      extraction_v1.txt
      continuity_v1.txt
      detective_v1.txt
    workers/
      main.py
      queue.py
      handlers.py
    telemetry/
  scripts/
    probe_inference.py
    probe_embeddings.py
    configure_neon.py
    check_neon.py
    seed_demo.py
    run_live_smoke.py       # future Phase 16 authenticated app journey
  tests/
    unit/
    integration/
    evaluation/
    fixtures/
  docs/
    model-capabilities.md
    api-contract.md
    deployment.md
    nebius-feedback.md
    implementation-progress.md
```

Phases 1-8 files now exist. Later-phase files remain deliverables. Current runtime layout and
verification records are in backend/README.md and backend/docs/implementation-progress.md.

## Phase 0 - Verify access, models, and deployment assumptions

Dependencies: none.

### Tasks

- [ ] Confirm hackathon submission dates and record what existed before the period.
- [ ] Create/select Neon (database), Supabase (Auth) and Nebius (AI) projects; choose regions and check credits.
- [ ] Privately configure the direct Neon administrator URL, generate distinct SQL-login URLs,
  provision roles/pgvector, migrate, and pass check_neon.py --live on the intended branch.
- [ ] Get the Token Factory API key privately and keep it out of chat, browser code, source control, and logs.
- [ ] Query the account model catalog through GET /v1/models.
- [ ] Choose an available NVIDIA Nemotron for extraction and one for reasoning. They may be the same model.
- [ ] Prefer a smaller model for routine work and a stronger one for comparisons if measured benefits justify it.
- [ ] Probe each chosen model: ordinary completion, structured schema output, tool calls, context/output limits, latency, token usage, and error behavior.
- [ ] Select an available embedding model and verify query/document formatting requirements.
- [ ] Measure actual returned embedding dimension; record it as D. Test one query against known related/unrelated dreams.
- [ ] Confirm model licensing/attribution and account permission to use the services.
- [ ] Choose a host supporting persistent API/worker processes with outbound access to Neon, Supabase Auth and Nebius.

Use the documented base URL:
`https://api.tokenfactory.nebius.com/v1/`

Proposed Nano/Super/Ultra names are routing preferences, not verified API identifiers. Fill environment variables using exact catalog IDs. Do not copy an unrelated model from a provider quickstart simply because its example works.

Example provider initialization to implement:

```python
from openai import AsyncOpenAI

client = AsyncOpenAI(
    api_key=settings.nebius_api_key,
    base_url=settings.nebius_base_url,
    timeout=60.0,
    max_retries=0,  # central application retry policy owns retries
)
```

If schema mode is unsupported, use JSON output with strict Pydantic validation and at most one repair attempt. If native tools are unsupported, try another available NVIDIA model first. A validated JSON action envelope is a documented fallback, not a claim of native tool support.

### Deliverables and gate

- [ ] docs/model-capabilities.md records tested IDs, D, formats, limits, and date.
- [ ] scripts/probe_inference.py and scripts/probe_embeddings.py --live pass with actual credentials;
  native tool capability checks for later agent phases remain a separate gate.
- [ ] One real NVIDIA completion and one real embedding succeed from the intended environment.
- [ ] A hosting decision and secrets list are recorded.
- [ ] Do not proceed with invented model IDs or an assumed embedding dimension.

## Phase 1 - Bootstrap the service and configuration

Dependencies: Phase 0.

### Tasks

- [ ] Create backend package, lock dependencies, add Ruff, pytest configuration, .gitignore.
- [ ] Add typed settings with required values validated on startup.
- [ ] Configure explicit CORS origins and request IDs; never combine wildcard origins with credentials.
- [ ] Add /health/live and /health/ready outside /api/v1.
- [ ] Liveness checks process health. Readiness checks DB connectivity and schema revision.
- [ ] Expose provider configuration status without secrets; do not make a billable inference call per readiness probe.
- [ ] Define uniform error responses and scrub stack traces in production.
- [x] Add Docker image and Compose API/worker services that connect to hosted Neon; no local DB service.

### Configuration contract

```dotenv
APP_ENV=development
API_PREFIX=/api/v1
CORS_ORIGINS=["http://localhost:3000"]
ADMIN_DATABASE_URL=postgresql+psycopg://NEON_ADMIN:REDACTED@DIRECT_NEON_HOST/neondb?sslmode=verify-full
DATABASE_URL=postgresql+psycopg://dreamthread_app:REDACTED@DIRECT_NEON_HOST/neondb?sslmode=verify-full
MIGRATION_DATABASE_URL=postgresql+psycopg://dreamthread_migrator:REDACTED@DIRECT_NEON_HOST/neondb?sslmode=verify-full
QUEUE_DATABASE_URL=postgresql+psycopg://dreamthread_claimer:REDACTED@DIRECT_NEON_HOST/neondb?sslmode=verify-full
EXPECTED_SCHEMA_REVISION=0006_personal_memory
DB_POOL_SIZE=3
QUEUE_POOL_SIZE=1
DB_TIMEOUT_SECONDS=20
DB_CONNECT_TIMEOUT_SECONDS=15
DB_POOL_RECYCLE_SECONDS=300
SUPABASE_URL=https://PROJECT.supabase.co
SUPABASE_PUBLISHABLE_KEY=SET_LOCALLY
SUPABASE_SECRET_KEY=SET_ONLY_IF_SERVER_STORAGE_OR_ADMIN_FEATURES_NEED_IT
NEBIUS_BASE_URL=https://api.tokenfactory.nebius.com/v1/
NEBIUS_API_KEY=SET_LOCALLY
NEBIUS_EXTRACTION_MODEL=EXACT_VERIFIED_NVIDIA_ID
NEBIUS_REASONING_MODEL=EXACT_VERIFIED_NVIDIA_ID
NEBIUS_EMBEDDING_MODEL=EXACT_VERIFIED_EMBEDDING_ID
EMBEDDING_DIMENSIONS=SET_TO_MEASURED_INTEGER
EMBEDDING_VERSION=v1
PROMPT_VERSION=v2
EMBEDDING_DOCUMENT_PREFIX=SET_FROM_MODEL_CARD
EMBEDDING_QUERY_PREFIX=SET_FROM_MODEL_CARD
EMBEDDING_PREFIXES_VERIFIED=false
VOICE_ENABLED=false
DEMO_ENABLED=false
MODEL_TIMEOUT_SECONDS=60
JOB_MAX_ATTEMPTS=3
WORKER_CONCURRENCY=2
```

The above is a template, not a ready-to-run .env. backend/.env.example contains the complete
defaults; configure_neon.py writes three distinct SQL-login URLs from one private administrator
URL without network calls. Optional secret keys remain unset when unused. The baseline migration
needs a measured D; Phase 7 spaces enforce per-version dimensions and require complete reindexing
before switching a model/prefix/dimension. Environment changes never mix vector spaces.

### Gate

- [ ] API starts with valid settings and fails clearly with missing required configuration.
- [ ] Liveness/readiness and OpenAPI work.
- [ ] A test request returns a request_id; errors reveal no secrets.
- [ ] Hosted Neon role/TLS/schema/tenant-context checks and API/worker startup pass from a clean environment.

## Phase 2 - Database, migrations, and tenant isolation

Dependencies: Phase 1.

### Database conventions

Use schema app in Neon for application data. Browsers access FastAPI, never a Neon administrator
connection. Supabase is Auth only; its PostgreSQL/Data API is not used for journal persistence.
Enable and force RLS, remove public grants, and revoke direct Data API-role grants if those roles
exist. Use a dedicated SQL-created runtime login with no DDL, ownership, BYPASSRLS, managed-service
administrator membership or broad data-reader/writer memberships. A different restricted login
leases jobs. Keep owner credentials out of running API/worker containers.

All tenant tables use UUID IDs, user_id, timestamps, UNIQUE(user_id, id), and appropriate indexes. Parent references use FOREIGN KEY(user_id, parent_id) REFERENCES parent(user_id, id). Use ON DELETE CASCADE for subordinate records and explicit lifecycle logic for derived results.

The account row's id equals the validated Supabase user UUID. Keep app.accounts self-contained for portable PostgreSQL tests; provisioning occurs only after Auth verification. Auth admin deletion is separate from deleting journal data.

### Logical schema to implement

| Table | Important fields and constraints |
|---|---|
| accounts | id/user UUID, display_name, timezone, data_epoch integer, archive_version integer, memory_version integer, deletion_state |
| preferences | user_id unique, auto_connections, require_memory_confirmation, timezone/display settings |
| dreams | id, user_id, title, dream_date DATE, current_revision_id, version, lifecycle draft/saved, input_mode text/voice, analysis_state |
| dream_revisions | dream_id, revision integer, exact text, content_hash, reported_emotions JSONB, recorded_at; unique dream/revision; immutable |
| analyses | dream_id, revision_id, pipeline_version, memory_version, structured extraction, reflections JSONB, optional followup, state; unique analysis input version |
| motifs | user_id, type place/person/object/event/emotion, canonical_label, normalized_key; unique user/type/key |
| dream_motifs | dream_id, revision_id, motif_id, origin extracted/user, confirmation_state, evidence references |
| associations | motif_id, user-authored meaning, status suggested/confirmed/rejected, enabled, version, source_dream_id nullable |
| detail_overrides | dream_id, revision_id, stable extracted-detail key, corrected value or removed flag, version; precedence over model output |
| followup_answers | dream_id, revision_id, question version, answer or skipped state, optional confirmed association ID |
| dream_embeddings | dream_id, revision_id, model_id, embedding_version, vector(D), input_hash; unique revision/model/version |
| connections | ordered dream pair, relation_key, status, evidence-backed observation, basis_versions, model_run_id; unique user/pair/relation |
| connection_decisions | stable ordered pair + relation key, decision confirmed/rejected, version; survives reanalysis |
| evidence | id, source dream/revision, start/end, exact_quote, content_hash; reusable verified citation record |
| connection_evidence | connection_id, evidence_id; composite tenant FKs |
| storylines | user_id, motif_id, title, status, source_signature; initially one storyline per confirmed motif |
| storyline_entries | storyline_id, dream_id, revision_id; unique membership |
| conversations | id, user_id, title, created_at |
| messages | conversation_id, role user/assistant, body, status pending/completed/failed/stale, request_key, context_versions |
| message_evidence | message_id, evidence_id; source claims are linked here |
| jobs | kind, dedupe_key, minimal payload IDs, status, stage, attempts, run_after, lease fields, error_code, epoch/version inputs |
| model_runs | user_id, job_id nullable, provider/model ID, task, prompt_version, latency, token usage nullable, status, trace stage; no raw prompts |
| idempotency_keys | user_id, route, key, request_hash, result_resource_id, expires_at; unique user/route/key |
| monthly_summaries | month, timezone, structured summary, source_signature, evidence links; optional Phase 12 |
| audio_assets | storage key, verified metadata, lifecycle, expires_at; optional Phase 13 |
| cleanup_tasks | user_id, exact storage keys/operation IDs, retry state; no journal content |
| exports | user_id, format, job_id, archive_version, data_epoch, status, private storage key nullable, expires_at |

Use typed columns for fields filtered by queries; JSONB for validated variable AI structures, not a substitute for ownership constraints. Source dependencies for analyses/storyline summaries/monthly summaries must be enumerable; add analysis_evidence and summary_evidence link tables as applicable.

Create circular dream/current-revision references in two migration steps: create dreams with a nullable current_revision_id, create dream_revisions referencing dreams, then add the composite current-revision constraint. In the create transaction insert the dream, insert revision 1, and update the current pointer before commit. Enforce that the selected revision belongs to that exact dream, not merely the same user. Use restrictive constraints or explicit deletion ordering for current pointers so cascade cycles do not block deletion.

### RLS strategy for SQLAlchemy connections

The runtime DB role uses a transaction-local application identity, not an assumed Supabase REST JWT context.

```sql
-- Example policy shape for a tenant table:
USING (
  user_id = nullif(current_setting('app.user_id', true), '')::uuid
)
WITH CHECK (
  user_id = nullif(current_setting('app.user_id', true), '')::uuid
);
```

Inside EVERY tenant transaction, API and worker set identity with a bound parameter:

```sql
SELECT set_config('app.user_id', :verified_user_id, true);
```

Use ENABLE ROW LEVEL SECURITY and FORCE ROW LEVEL SECURITY on tenant tables; account policy uses id rather than user_id. Transaction-local settings reset on commit/rollback; never use session-wide tenant state. Background work obtains user identity only from a trusted persisted job.

Queue claiming needs a narrow separate role that can lease jobs across users but cannot read journal tables. Process each claimed job through the tenant-scoped runtime role. Worker credentials must not turn normal API access into an owner connection.

### Gate

- [ ] Alembic builds a clean test DB and upgrades an existing one.
- [ ] App role without tenant context sees no rows.
- [ ] Users A and B cannot access each other's data through any tested query.
- [ ] A pooled connection reused for B does not retain A's context.
- [ ] Cross-user child insertion fails at the database layer.
- [ ] Direct Data API access cannot read app tables.
- [ ] The production runtime role is not the migration role.

## Phase 3 - Authentication, preferences, and request contracts

Dependencies: Phase 2.

### Tasks

- [ ] Frontend handles Supabase sign-up/sign-in/refresh. Backend accepts Authorization: Bearer <access_token>.
- [ ] Initially verify through Supabase Auth GET /auth/v1/user, with project publishable API key and user's bearer token.
- [ ] Treat Auth outage as 503, invalid/expired identity as 401. Never fall back to unverified JWT decoding.
- [ ] Provision app.accounts idempotently on the first verified request.
- [ ] Add GET/PATCH /me and GET/PATCH /me/preferences.
- [ ] Validate allowed emotions and IANA timezone; default stored timezone to UTC until chosen.
- [ ] Add per-user AI limits and input-size limits before expensive work.
- [ ] Anonymous sample users, if enabled, get isolated authenticated identities and limited quotas.

Optional later optimization: verify JWTs using trusted project JWKS, algorithm allowlist, issuer, audience, expiry and key rotation handling. Test legacy symmetric-key behavior separately. Do not implement your own cryptography.

### Common API rules

- Prefix: /api/v1; UUIDs as strings; timestamps UTC ISO 8601; dream_date is a local calendar date.
- Pagination: cursor + limit, default 20, maximum 100; stable order dream_date DESC, id DESC.
- Errors: { "error": { "code": "...", "message": "...", "request_id": "...", "details": {} } }.
- 401 unauthenticated, 404 absent OR another user's resource, 409 version/idempotency conflict, 422 invalid input, 429 quota with Retry-After, 503 unavailable dependency.
- Expensive create/retry operations support Idempotency-Key. Same key + same body returns the original resource; different body returns 409.
- No client-supplied SQL, arbitrary sort expressions, storage paths, model IDs, or system prompts.
- Rate-limit enforcement must work across processes; use a DB-backed quota reservation/counter, not only in-memory middleware.

### Gate

- [ ] Invalid, expired, absent and other-project tokens fail.
- [ ] Both users can retrieve/update only their own settings.
- [ ] Concurrent first requests create one account.
- [ ] Quota failures occur before a provider call.

## Phase 4 - Dream journal and immutable revisions

Dependencies: Phase 3.

### Endpoints

| Method/path | Behavior |
|---|---|
| POST /dreams | Save draft or saved entry; return 201 and analysis_job_id when submitted |
| GET /dreams | Filter by date, reported emotion, keyword; paginated summaries |
| GET /dreams/{id} | Current original text, revision/version, reported tags, analysis status |
| GET /dreams/{id}/revisions/{revision_id} | Owned immutable revision for explicit history/source inspection; reject inactive/deleted sources |
| PATCH /dreams/{id} | User edit using expected_version; create immutable revision for content changes |
| POST /dreams/{id}/analyze | Submit/retry current saved revision; return 202 job reference |
| GET /dreams/{id}/analysis | Current valid reflection/extraction or processing state |
| DELETE /dreams/{id} | Immediately hide and invalidate; asynchronous cleanup if necessary |

POST body example:

```json
{
  "title": "The house in the forest",
  "dream_date": "2026-10-28",
  "text": "I returned to the house. The door was open and I felt calm.",
  "reported_emotions": ["calm"],
  "lifecycle": "saved"
}
```

### Tasks

- [ ] Validate title <= 160 characters; saved text 1-20,000 characters; drafts may be empty.
- [ ] Store exact submitted text. Do not silently rewrite punctuation, whitespace, or spelling.
- [ ] Normalize only separate search representations.
- [ ] Save current reported emotions in the source revision so corrections are versioned.
- [ ] Insert dream/revision and analysis job in ONE transaction when saved.
- [ ] Draft saves never trigger inference; promotion to saved queues work.
- [ ] Edits require expected_version, returning 409 for concurrent modification.
- [ ] A changed source marks dependent analyses, connections and answers stale immediately.
- [ ] Reanalysis supersedes derived data without erasing user decisions.
- [ ] No imagery generation on each save; UI thumbnails use labeled demo assets or neutral motif artwork.

### Gate

- [ ] Text survives round-trip exactly, including Unicode/newlines.
- [ ] Date filters and cursor pagination work.
- [ ] Provider outage does not prevent saving.
- [ ] Retried create request produces one dream and one analysis job.
- [ ] Stale AI output is not presented as current after an edit.

## Phase 5 - Durable jobs and worker recovery

Dependencies: Phase 4.

### State model

Jobs: queued -> running -> succeeded; retryable failure -> queued with run_after; exhausted failure -> failed; superseded/deleted work -> cancelled.

Analysis stages: queued, extracting, embedding, retrieving, comparing, verifying, publishing, ready, failed, stale.

The UI must distinguish "saved" from "analysis ready". Extraction may be visible before continuity finishes only as a separately versioned validated result.

### Tasks

- [ ] Implement POSTGRES claim transaction using FOR UPDATE SKIP LOCKED; commit lease quickly.
- [ ] Record lease_token, leased_until, heartbeat_at, attempt count, and configurable stage deadline.
- [ ] Initial defaults: 120-second lease, heartbeat every 20 seconds, provider timeout 60 seconds. Tune with measured calls.
- [ ] Heartbeat and publish require matching lease_token; an expired worker cannot overwrite its successor.
- [ ] Acknowledge success only after result publication commits.
- [ ] Reclaim expired leases; use retry backoff with jitter, maximum 3 attempts.
- [ ] Respect provider Retry-After; retry timeouts/429/transient 5xx, not invalid credentials or bad schemas indefinitely.
- [ ] Persist minimal stage checkpoints so restart can skip valid completed steps.
- [ ] Dedupe by owner + kind + source revision + pipeline version + memory version.
- [ ] Add GET /jobs/{id}; POST /jobs/{id}/retry only for eligible failed owned jobs.
- [ ] Add internal cleanup/reaper loop for expired leases and retention tasks.
- [ ] Do not rely on FastAPI BackgroundTasks for durable AI processing.

Before final write, recheck dream existence, current revision, account data_epoch, memory_version, deletion_state and lease. Cancel outdated work. Do not hold a database transaction open during inference.

For publication and deletion, acquire the same short-lived account-row lock first, then source rows in deterministic ID order. Validate the epoch/versions and write while holding those locks in the same transaction. A check performed before the transaction is insufficient: deletion could otherwise commit between the check and publication. Recheck every retrieved source revision, not only the newly analyzed dream. Define one consistent lock order for edits, memory changes, publication, and deletion to avoid deadlocks.

### Gate

- [ ] Kill worker mid-call; replacement eventually completes or visibly fails.
- [ ] Two workers do not publish duplicate links/analyses.
- [ ] Late response from a superseded worker is discarded.
- [ ] Deleting a dream while a job runs does not recreate it.
- [ ] Safe user-visible error and retry controls appear when provider work fails.

## Phase 6 - Nemotron extraction, reflection, and evidence verification

Dependencies: Phase 5.

### Tasks

- [ ] Implement provider adapter with typed requests/results, timeouts, central retries, token caps.
- [ ] Separate versioned system prompts for extraction, comparison, and Detective.
- [ ] In prompts, label dream text, memory text, and retrieved entries as untrusted data.
- [ ] Extract settings, people, objects, events, and explicitly reported emotions.
- [ ] Require exact source spans for extracted details; model offsets are suggestions until checked.
- [ ] Generate 1-3 brief possible reflections and at most one contextual follow-up.
- [ ] Never convert inferred feelings into reported emotion tags.
- [ ] Validate Pydantic schema; allow one bounded repair with validation errors, then fail honestly.
- [ ] Record provider/model, usage if returned, latency, prompt version, result status.

### Evidence contract

```json
{
  "dream_id": "uuid",
  "revision_id": "uuid",
  "start": 0,
  "end": 24,
  "quote": "I returned to the house.",
  "source_hash": "sha256"
}
```

Offsets are zero-based Python Unicode code-point positions, end exclusive. They are not JavaScript UTF-16 indices. Return the exact quote for display; frontend must convert positions when highlighting.

Verification order:

1. Load the source through an owner-filtered repository.
2. Confirm permitted revision, hash, and active source.
3. Require 0 <= start < end <= len(text).
4. Require text[start:end] == quote exactly.
5. If offsets are incorrect but a quote occurs once, deterministically repair offsets.
6. If repeated/ambiguous, request clarification or discard; never pick a convenient occurrence.
7. Reject unknown IDs, paraphrases presented as quotes, and invented spans.
8. Attach evidence IDs to claims only after verification.

Example extraction shape:

```json
{
  "observations": [
    {
      "type": "place",
      "label": "House",
      "quote": "the house",
      "start": 18,
      "end": 27
    }
  ],
  "reported_emotions": [],
  "possible_reflections": [
    {
      "text": "You could explore what felt familiar about the house.",
      "kind": "tentative_reflection",
      "evidence_ids": ["verified-evidence-uuid"]
    }
  ],
  "followup_question": "Did the house remind you of a place you know?"
}
```

The shape illustrates field names; evidence IDs are assigned by the application after validation, not trusted from model inventions. Build test fixtures with actual matching offsets rather than copying illustrative numbers.

### Gate

- [ ] One-entry analysis works without pretending to find historical patterns.
- [ ] Fabricated quotes/IDs and wrong-user sources fail validation.
- [ ] Negations such as "I was not afraid" do not become a reported fear tag.
- [ ] Injected instructions in dream text cannot change tools, identity, or output policy.
- [ ] A live Nemotron call passes the same schema/evidence validators as fixture responses.

## Phase 7 - Embeddings and retrieval

Dependencies: Phase 6.

### Tasks

- [ ] Embed the exact current dream content or a documented bounded search representation.
- [ ] For MVP, one embedding per dream; respect provider token limits via a tested conservative size guard.
- [ ] If accepted text exceeds embedding limits, use deterministic chunks with source offsets and deduplicate retrieved dream IDs; never truncate silently.
- [ ] Apply the embedding model's verified query/document prefixes where required.
- [ ] Store model/version/dimension and input hash.
- [ ] Start with exact cosine search scoped to user before ranking; no ANN index needed for small archives.
- [ ] Merge semantic top 8, explicit motif matches, and text matches; cap at 12 unique candidate dreams.
- [ ] Exclude the current dream and inactive/stale revisions.
- [ ] Use semantic rank to nominate candidates, not establish equivalence or causation.
- [ ] Search confirmed associations when relevant without overwriting original text.
- [ ] If embedding provider fails, mark retrieval degraded and use text/motif retrieval; disclose degraded processing, retain an embedding retry.
- [ ] Changing embedding model requires a new version, complete re-embedding, and atomic switch. Never compare mixed vector spaces.

Retrieval tools return IDs, dates, snippets, source revisions, and ranking metadata. Fetch original text separately for evidence. Generic matches should lose to specific descriptions, personal associations, and explicit user confirmations.

### Gate

- [ ] Known recurring-house fixture ranks within top 5 among distracting entries.
- [ ] Two unrelated houses may remain separate after comparison.
- [ ] Other users' embeddings never enter candidates.
- [ ] Dimension mismatch fails clearly before insertion.
- [ ] Unicode and long-input behavior are tested.
- [ ] Retrieval benchmark records recall@5 for a small labeled fixture set.

## Phase 8 - Personal memory and corrections

Dependencies: Phase 7.

### Endpoints

- GET /motifs; GET /memory.
- POST /memory: user-authored association, linked motif, optional source.
- PATCH /memory/{id}: edit meaning, enabled state, or confirmation decision.
- DELETE /memory/{id}: remove association and invalidate its use.
- PATCH /dreams/{id}/details: user override/removal of extracted detail with expected_version.
- GET /dreams/{id}/followup; PUT /dreams/{id}/followup: answer or skip, with explicit save_as_association.

### Tasks

- [ ] Distinguish automatic symbol mention from user-confirmed identity and meaning.
- [ ] Never merge two people or places just because their labels match.
- [ ] Persist extracted-detail overrides separately so reanalysis cannot undo corrections.
- [ ] Model-suggested meanings remain suggestions until the user confirms.
- [ ] Increment memory_version/archive_version on relevant corrections.
- [ ] Make old memory-conditioned reflections stale and queue appropriate rebuilds.
- [ ] Removing memory prevents future prompt inclusion; invalidate dependent stored answers too.
- [ ] Preserve rejected proposals so identical suggestions do not repeatedly return.

### Gate

- [ ] Saved "grandmother's home" association affects the next relevant analysis.
- [ ] An unconfirmed association is not stated as the user's belief.
- [ ] Disabled/deleted association is absent from future model inputs.
- [ ] User corrections survive retries and reanalysis.
- [ ] Cross-user motif/memory IDs are rejected.

## Phase 9 - Dream Continuity Engine

Configuration handoff (2026-10-29): see `backend/docs/developer-handoff.md` for all-phase
settings, verified live services and remaining external inputs. The private `backend/.env`
now covers all current Settings fields, including a reserved reasoning model matching the
live-tested extraction model. Commented future agent/graph/voice/export settings are proposed
contracts only; add validated consumers and tests during the corresponding phase. Voice stays
disabled until storage and ASR verification pass. Production domain/hosting and any privileged
storage key remain external setup requirements, not completed configuration.

Dependencies: Phase 8.

### Tool boundary

Expose only typed application functions to the model:

| Tool | Allowed action |
|---|---|
| search_dreams(query, filters, limit) | Search current user's archive |
| get_dreams(ids) | Read permitted current source entries |
| get_personal_associations(motif_ids) | Read relevant confirmed enabled memory |
| get_confirmed_connections(dream_id) | Read accepted historical relationships |
| propose_comparison(structured_result) | Return a proposal for application validation |

Identity is injected by server context and is not a model argument. No generic SQL, HTTP fetch, filesystem, shell, auth administration, or deletion tools.

### Execution sequence

1. Load current source and valid extraction.
2. Run deterministic initial retrieval.
3. Give Nemotron bounded candidates and read-only tools.
4. Let it request additional source entries/associations if needed.
5. Require structured observations, similarities, differences, uncertainty, and citations.
6. Verify quotes, ownership, revisions, and relevance.
7. Screen claim entailment: quotes must actually support the comparison. Use rules/manual evaluation and, if helpful, a separate bounded review call; quote existence alone is insufficient.
8. Save suggested links, preserving existing confirmed/rejected user decisions.
9. Publish the valid analysis and connection proposals atomically against input versions.
10. Rebuild affected storyline membership from confirmed relationships.

Limits: max 4 model/tool rounds, 8 tool invocations, 12 candidate dreams, per-tool result size cap, configurable overall deadline and token budget. On limit exhaustion, produce a retryable/insufficient-evidence state rather than an endless loop.

For links between A and B, order UUIDs into dream_a_id < dream_b_id. Use a stable relation_key independent of generated prose. Reject self-links and cross-user pairs. Persist user decisions separately from replaceable analysis results.

### Endpoints

- GET /dreams/{id}/connections.
- PATCH /connections/{id}: status confirmed or rejected with expected_version.
- GET /connections/{id}/evidence.

### Gate

- [ ] Seed older house dream, add a related dream, obtain a supported comparison.
- [ ] Show at least one real tool-backed Nemotron investigation in execution metadata.
- [ ] Confirm/reject works and rejection survives duplicate delivery and reanalysis.
- [ ] Insufficient evidence produces no forced link.
- [ ] A changed/deleted source cannot remain the basis of an active connection.

## Phase 10 - Constellation and storylines

Dependencies: Phase 9.

### Endpoints

- GET /constellation?from=&to=&types=&limit=.
- GET /motifs/{id}: active mentions and personal associations.
- GET /storylines.
- GET /storylines/{id}: ordered entries, factual changes, citations.
- POST /storylines/{id}/refresh: versioned rebuild job, 202.

### Graph contract

```json
{
  "nodes": [
    {"id": "dream:uuid", "kind": "dream", "entity_id": "uuid", "label": "An open door"},
    {"id": "motif:uuid", "kind": "motif", "entity_id": "uuid", "label": "House"}
  ],
  "edges": [
    {
      "id": "edge:uuid",
      "source": "dream:uuid",
      "target": "motif:uuid",
      "kind": "mentions",
      "status": "observed",
      "evidence_ids": ["uuid"]
    }
  ],
  "truncated": false,
  "archive_version": 7
}
```

### Tasks

- [ ] Return deterministic stable IDs; graph coordinates/animation belong to frontend.
- [ ] Distinguish observed mentions, suggested continuity, confirmed continuity, and personal associations.
- [ ] Exclude rejected continuity edges by default; suggested edges must be visibly labeled.
- [ ] Cap graph size (initially 100 nodes/200 edges), return truncated and filters for exploration.
- [ ] Use confirmed links and confirmed identity mappings for storyline membership.
- [ ] Sort chapters by dream_date then created_at/id, not insertion order alone.
- [ ] Build factual comparisons from source details; do not assert psychological causation.
- [ ] Recompute when a source, confirmation, or association changes.
- [ ] Return a meaningful empty graph for zero/one-entry history.

### Gate

- [ ] Every graph edge can open its evidence.
- [ ] No orphan edges point to missing nodes.
- [ ] Rejecting a link changes the graph and dependent storyline.
- [ ] Three confirmed fictional house entries display in correct order.
- [ ] Deleted dreams disappear from graph and storylines immediately.

## Phase 11 - Dream Detective

Dependencies: Phase 10.

### Endpoints

- POST /conversations; GET /conversations; DELETE /conversations/{id}.
- GET /conversations/{id}/messages.
- POST /conversations/{id}/messages: save question and queue answer; return 202 with job_id/message_id.
- GET /messages/{id}: answer status, observations, reflections, citations.

### Tasks

- [ ] Use the same bounded owner-scoped tools and evidence validator.
- [ ] Retrieve from original entries, not just prior AI summaries.
- [ ] Scope explicit source IDs/dates/motifs before semantic ranking.
- [ ] Answer targeted questions with supported observations and separate tentative reflection.
- [ ] For "all", "ever", "first", counts, and date ranges, use exhaustive SQL/filter aggregation; top-k retrieval cannot establish completeness.
- [ ] For a historical comparison, fetch every relevant confirmed storyline member within the supported limit; disclose partial coverage if capped.
- [ ] No relevant entries -> say there is not enough saved evidence.
- [ ] Bound conversation context; prior assistant text is not primary evidence.
- [ ] Verify final structured response before exposing it; show job progress rather than unverified token streaming.
- [ ] Citation objects include source title/date, revision, exact quote, offsets, and claim mapping.
- [ ] Source links resolve through the authenticated dream/revision route; the browser cannot access another user's citation by guessing its UUID.
- [ ] Store question once under idempotency key and publish one answer per answer job.
- [ ] Mark answers stale when evidence or used memory changes. Do not silently regenerate or continue using stale messages as evidence.

### Gate

- [ ] "How has the house changed?" answers from multiple cited entries.
- [ ] "When did I first mention the forest?" finds the true earliest entry, including one outside semantic top-k.
- [ ] Empty archive and unsupported questions get honest responses.
- [ ] Hostile text in a retrieved entry cannot trigger unauthorized actions.
- [ ] Every factual claim in the fixture response maps to validated relevant citations.
- [ ] Previous conversation IDs from another user return 404.

## Phase 12 - Insights and monthly time capsules

Dependencies: Phase 11. Complete after the core path is stable.

### Endpoints and tasks

- [ ] GET /insights?month=YYYY-MM&timezone=IANA.
- [ ] GET /time-capsules; GET /time-capsules/{month}.
- [ ] POST /time-capsules/{month}/refresh -> 202 job.
- [ ] Compute dream totals, recurring motif counts, and reported-emotion counts in SQL.
- [ ] Define each metric: count distinct dreams per motif; multi-emotion entries can contribute to multiple tags.
- [ ] Use dream_date for calendar grouping; recorded_at is separately labeled.
- [ ] Report missing emotion tags; do not interpret missing tags as neutral.
- [ ] Model may summarize validated aggregates and cited excerpts, not invent numbers.
- [ ] Do not produce clinical scores or claim the charts establish improved mental health.
- [ ] Version cache by archive_version/memory_version/timezone/period; invalidate on edit/deletion.
- [ ] Generate on demand first; monthly scheduler is optional.

### Gate

- [ ] SQL totals match fixture calculations, including multi-tag and untagged dreams.
- [ ] Month boundaries and timezone selection behave consistently.
- [ ] Cached summaries disappear or become stale after source changes.
- [ ] Generated retrospective contains verified citations.

## Phase 13 - Voice capture and private audio

Dependencies: Phase 11. A separate capability probe must pass before enabling voice.

### Decision

Baseline adapter: faster-whisper on a separate CPU/GPU-capable worker. Confirm model download/license, memory, FFmpeg/runtime requirements, processing latency, and target deployment capacity. Do not assume the Token Factory chat endpoint transcribes audio. If a verified Nebius speech service is selected instead, document that actual API and replace the adapter.

### Flow

1. POST /audio/upload-intents validates intended type/size and returns a short-lived signed private upload URL plus asset_id.
2. Client uploads only to the server-chosen key.
3. POST /audio/{id}/complete verifies the stored object, actual size/container/duration, and ownership.
4. Queue transcription; GET /audio/{id} returns job status and transcript draft.
5. User reviews/edits transcript, then POST /dreams with input_mode voice and owned audio_asset_id.
6. Save reviewed text as the original journal revision; raw ASR output is not automatically treated as the user's final wording.
7. Continue through the same extraction/continuity workflow.

### Tasks and limits

- [ ] Private bucket, no permanent public URLs, server-generated keys under user UUID.
- [ ] Initial maximum: 10 MB and 3 minutes; verify MIME and decoded media, not file extension alone.
- [ ] Bound media decoding CPU/time and reject invalid files; no arbitrary remote-URL transcription.
- [ ] Validate upload completion against intent; delete oversized/unexpected objects.
- [ ] Default to delete raw audio after transcript confirmation, with a documented cleanup deadline.
- [ ] Expire abandoned uploads/drafts and protect temporary transcripts like journal text.
- [ ] If retention preference is offered, explain it and support playback through short-lived signed URLs.
- [ ] Signed URL expiry is not immediate revocation; keep TTL short and delete object for removal.
- [ ] Disable voice clearly when speech infrastructure is unavailable.

### Gate

- [ ] Real recording -> private upload -> transcript review -> saved dream -> analysis works.
- [ ] Other user cannot complete, read, attach, or delete asset.
- [ ] Invalid/oversized audio and worker timeout fail safely.
- [ ] Retention cleanup and delete-during-transcription do not restore removed data.

## Phase 14 - Privacy, deletion, exports, and abuse controls

Dependencies: Phase 11; include Phase 12/13 data if implemented.

### Endpoints

- POST /exports with format json/text -> export job.
- GET /exports/{id} -> status and authenticated download route/short-lived private URL.
- DELETE /me/journal with explicit confirmation payload -> 202 cleanup job.
- DELETE /me/account -> optional separate account-removal workflow, not conflated with journal deletion.

### Tasks

- [ ] Document exactly which data is sent to Nebius; do not promise end-to-end encryption for server-processed text.
- [ ] Export dreams/revisions, reported emotions, associations, decisions, storylines and conversations, with schema_version and timestamps.
- [ ] Never export keys, hidden reasoning, other users' data, or internal raw provider payloads.
- [ ] Scope exports to a consistent archive version; purge temporary files after a documented short TTL.
- [ ] Export jobs capture data_epoch and archive_version, build from one consistent DB snapshot, and validate versions again before publishing the download. If data changed, discard and retry with a new snapshot; deletion cancels the export. Never publish a file built from an older epoch.
- [ ] Journal deletion increments data_epoch and marks deletion_state immediately, blocking reads/jobs.
- [ ] Purge content, embeddings, evidence, analyses, messages, associations, decisions, summaries, pending exports and audio.
- [ ] Cancel jobs; workers recheck epoch after external calls and before commit.
- [ ] Persist minimal cleanup tasks for external object deletion; retry idempotently.
- [ ] Return completion only after mandatory content deletion finishes. Surface pending storage cleanup.
- [ ] Keep no sensitive content in tombstones or model metadata.
- [ ] State backup/provider retention accurately based on actual service policies; app deletion is not proof of instant third-party backup erasure.
- [ ] If deleting Auth account, clean owned storage first, remove application data, then perform Auth admin deletion and deny further app provisioning.
- [ ] Configure body limits, per-user AI quotas, global model-call concurrency, upload limits, and rate limits.
- [ ] Keep logs free of dream text, tokens, signed URLs, and full prompts; log metadata/errors only.

For single-dream deletion, mark the source inactive immediately; invalidate any object whose evidence references it. Other dreams stay available, but their affected derived content must be rebuilt or withheld. Rebuild tasks use the surviving sources only.

### Gate

- [ ] Export contains only the requesting user's expected data.
- [ ] Delete while comparison/Detective/export/transcription is running cannot repopulate content.
- [ ] Conversation responses and graph endpoints do not leak removed source text.
- [ ] Storage failures remain visible and retryable without exposing data.
- [ ] Journal deletion preserves login; account deletion is separately tested if offered.
- [ ] Production logs pass a sensitive-data inspection.

## Phase 15 - Integration, evaluation, and frontend acceptance

Dependencies: all features selected for release, including Phase 14.

### UI-to-API mapping

| UI screen | Backend requirements |
|---|---|
| Welcome | Supabase sign-in + verified /me |
| Home | /dreams summaries + /constellation preview + connection summaries |
| Journal | Filtered /dreams and pagination |
| Capture | /dreams, /jobs, optional /audio |
| Dream detail | Original revision, /analysis, follow-up, corrected details, connections |
| Constellation | /constellation + motif/evidence inspector |
| Storyline | /storylines/{id} |
| Dream Detective | Conversations, message jobs, source access |
| Insights/time capsule | /insights and /time-capsules, when enabled |
| Personal memory | /memory + /motifs |
| Settings/privacy | /me/preferences, /exports, journal deletion |
| First-use state | Empty real lists/graphs; optional labeled sample import |

Do not hard-code the counts and quotes visible in mockups. Define pending/failed/stale/empty states in the frontend contract. Export OpenAPI and generate frontend types when stable.

### Tests to implement

| Category | Required scenarios |
|---|---|
| Ownership | Every resource route, graph/search/export, cross-user FK, pooled tenant reset |
| Journal | Exact text preservation, drafts, concurrent edits, idempotent create |
| Jobs | Duplicate delivery, lease expiry, restarts, superseded source, exhausted retry |
| Evidence | Fabricated quote/ID, ambiguous occurrence, wrong offsets, deleted source |
| Memory | Confirmation, removal, correction survives reanalysis |
| Retrieval | Paraphrases, unrelated houses, true first occurrence, zero history |
| Agent | Tool limits, prompt injection, unavailable tool/model, insufficient evidence |
| Lifecycle | Edit/delete during inference, invalidated old chat, purge/export race |
| Metrics | SQL counts, multi-tag emotions, month boundaries, cache invalidation |
| Voice if enabled | Private upload, bad media, review, expiry, cleanup |
| Deployment | HTTPS/CORS/auth, migration, API restart, worker restart, real inference |

Use real PostgreSQL/pgvector and restricted roles for CI integration tests. SQLite cannot verify
these security/query semantics. CI may use an isolated disposable PostgreSQL service, but no
local database is required for developers or the application. Do not point destructive
TEST_ADMIN_DATABASE_URL fixtures at the application Neon database. Hosted acceptance uses a
separate Neon branch and the read-only check_neon.py --live followed by an authenticated app
journey. Ordinary CI uses deterministic provider fakes; live model probes stay opt-in/cost-bounded.

Create 20-30 synthetic entries with labeled related and unrelated pairs, negation, Unicode, contradictions, and adversarial instructions. Fictional Sarah fixture:
- Oct 4: outside a dark house, reported fear.
- Oct 16: enters a similar house, hears her name, reports curiosity.
- Oct 28: open door and warm light, reports calm.
- Several unrelated houses/forests as distractors.

Add dates and quotes consistently across the seed data. Manually review generated reflections; a plausible tone is not evidence of correctness.

### Release gates

- [ ] All ownership, evidence, and deletion tests pass; no known critical failure.
- [ ] All predefined multi-dream factual claims have verified relevant citations.
- [ ] Retrieval recall@5 >= 80% on the labeled small fixture; inspect misses.
- [ ] No unsupported historical count/first/causal claim in the evaluation set.
- [ ] Real live capture-to-connection and Detective run succeeds on chosen models.
- [ ] Target DB-only endpoints p95 < 500 ms under a small documented test load; measure, do not claim without evidence.
- [ ] Target save response < 1 second excluding auth/network variance; inference runs asynchronously.
- [ ] Measure extraction/continuity/Detective median and p95; set UI deadlines based on actual service performance.
- [ ] Provider quota/exhaustion is visible; no fabricated fallback answers.
- [ ] Fresh-browser frontend journey works, including failure/retry and source opening.

## Phase 16 - Deployment and submission readiness

Dependencies: Phase 15.

### Deployment

- [ ] Build a pinned Docker image running as a non-root user.
- [ ] Deploy API and persistent worker as separate services.
- [ ] Set production secrets through host secret configuration, never image build args or NEXT_PUBLIC variables.
- [ ] Run migrations as a controlled release step, not independently from every API replica.
- [ ] Verify TLS, CORS, request limits, DB SSL verification, DNS/network reachability, and connection pool budget.
- [ ] Configure readiness, restart policy, resource limits, graceful shutdown, lease recovery.
- [ ] Use direct staging/live Neon endpoints and restricted SQL credentials; Compose contains no local DB.
- [ ] Monitor job age/failures, provider failures/429s, token usage, latency, DB pool saturation, cleanup backlog.
- [ ] Do not calculate costs from invented prices; record measured usage and current account pricing.
- [ ] Keep migration rollback/backup restore procedure and prior deployable image.
- [ ] Add/run scripts/run_live_smoke.py against the hosted app with an isolated synthetic account;
  check_neon.py --live is the existing read-only database check, not the full app journey.
- [ ] Test from a clean browser without developer session state.

### Current reproducible setup commands

From backend/, after creating a Neon project/database:

```powershell
uv sync --locked
Copy-Item .env.example .env
# Set ADMIN_DATABASE_URL to the direct Neon administrator URL in .env (pooling OFF).
uv run python scripts/configure_neon.py
uv run python scripts/provision_database.py --neon-logins
# Fill Supabase Auth and Nebius model/key/prefix settings privately.
uv run python scripts/probe_embeddings.py --live
# Set EMBEDDING_DIMENSIONS to the returned measured_dimensions integer.
uv run python scripts/probe_inference.py --live
uv run alembic upgrade head
uv run python scripts/check_database.py
uv run python scripts/check_neon.py --live
uv run uvicorn app.main:create_app --factory --loop app.loop:create_loop --reload --port 8000 --no-access-log
```

In a separate terminal from backend/:

```powershell
uv run python -m app.workers.main
```

Verification:

```powershell
uv run ruff check .
uv run pytest -m 'not integration'
uv run python scripts/check_neon.py --live
# Run an authenticated save -> job -> evidence -> candidates -> memory correction journey.
# Full destructive DB integration tests belong to isolated CI, never the application Neon DB.
```

These commands target the current implementation. Credentials, provider account capabilities
and hosted Neon acceptance are still required; writing configuration is not proof of a live run.
See backend/docs/neon-setup.md for full steps, role/TLS details and troubleshooting. Docker is
optional: docker compose up --build -d starts API/worker only after host-side setup/migration.

### Demo and sample data

- [ ] Seed/import a fictional sample journal per authenticated demo user.
- [ ] POST /demo/import only if DEMO_ENABLED; inserts once per owner and fixture version.
- [ ] Never let a shared public demo account reveal writing from previous visitors.
- [ ] Rate-limit demo inference and use disposable/sandboxed accounts.
- [ ] Test real new entry processing with preexisting synthetic history.
- [ ] Retain metadata proof of actual Nebius/NVIDIA use without exposing secrets or private content.
- [ ] Do not substitute precomputed responses while describing them as live inference.

Suggested video sequence:
- 0:00-0:20: audience and fragmented dream-memory problem.
- 0:20-0:50: save a new dream and show processing.
- 0:50-1:30: inspect discovered connection and actual quotes.
- 1:30-1:55: constellation and changing-house storyline.
- 1:55-2:30: Detective question and source inspection.
- 2:30-2:55: Nemotron/Token Factory architecture and impact.

### Submission checklist

- [ ] Working public demo URL and clear access instructions.
- [ ] Public repo with an open-source license at root, recognized by repository UI.
- [ ] README: problem/audience, architecture, setup, environment, migrations, worker, tests, demo flow.
- [ ] README explicitly identifies exact NVIDIA models and Nebius inference path.
- [ ] Honest notes on any other Nebius tools/services actually used.
- [ ] Project description explaining what/why/how.
- [ ] Public YouTube video <= 3 minutes.
- [ ] Actual Nebius/NVIDIA feedback document.
- [ ] Written pre-existing-work disclosure where required.
- [ ] City award selection only if the team actually attended the relevant event.

## 6. Phase completion record

Code and hosted verification are tracked separately. Prior 151-test evidence is recorded in
backend/docs/implementation-progress.md; the Neon transition still needs live account checks.

| Phase | Complete | Evidence to record |
|---|---|---|
| 0 Access/model probe | [ ] | Verified models, D, live probe |
| 1 Bootstrap | Code implemented | Startup/health/config checks passed; Neon configuration added |
| 2 Database/security | Code implemented; Neon gate pending | Prior migrations/RLS passed; live Neon roles/TLS/schema smoke required |
| 3 Auth/contracts | Code implemented; hosted Auth pending | Auth/limits fixtures + API schema passed |
| 4 Journal/revisions | Code implemented | CRUD/idempotency/edit tests passed; hosted app journey pending |
| 5 Worker | Code implemented | Prior crash/retry/lease tests passed; hosted Neon execution pending |
| 6 Extraction/evidence | Code implemented; live gate pending | Fixture citation checks passed; live Nemotron probe required |
| 7 Retrieval | Code implemented; live gate pending | Ranked synthetic fixtures passed; real embedding quality pending |
| 8 Memory | Code implemented | Correction/reanalysis/forgetting checks passed; live app journey pending |
| 9 Continuity | [ ] | Real tool-backed connection |
| 10 Graph/storylines | [ ] | Evidence navigation + ordering |
| 11 Detective | [ ] | Historical answers + sources |
| 12 Insights | [ ] | Verified counts + summary |
| 13 Voice | [ ] | Real reviewed recording flow |
| 14 Privacy/lifecycle | [ ] | Delete/export/race tests |
| 15 Integration | [ ] | Evaluation + frontend journey |
| 16 Deployment/submission | [ ] | Hosted smoke run + deliverables |

## 7. Documentation sources and verification limits

The original plan was written before implementation. Phases 1-8 now have code and recorded
regression evidence. The Neon update uses current primary provider documentation; no Neon
credentials, live hosted migration or live model requests were used for this transition.
Passing offline tests does not verify account-specific Neon privileges or deployment behavior.

- [Nebius Token Factory API introduction](https://docs.tokenfactory.nebius.com/api-reference/introduction): OpenAI-compatible integration, documented base URL, server-side keys.
- [Nebius API reference](https://api.tokenfactory.nebius.com/docs): model listing, chat completion, and embeddings endpoints.
- [Nebius documentation index](https://docs.tokenfactory.nebius.com/llms.txt): links to tools, structured output, and inference guides. Some individual guide pages were inaccessible during review; schema/tool support remains a required live probe.
- [Supabase JWT documentation](https://supabase.com/docs/guides/auth/jwts): identity verification principles.
- [Supabase JWT signing keys](https://supabase.com/docs/guides/auth/signing-keys): JWKS and key rotation for the optional optimization.
- [Supabase user management](https://supabase.com/docs/guides/auth/managing-user-data): account-data lifecycle and storage considerations.
- [Supabase RLS documentation](https://supabase.com/docs/guides/database/postgres/row-level-security): role/grant/policy boundaries.
- [Neon roles](https://neon.com/docs/manage/roles): SQL-created restricted roles versus managed administrator roles.
- [Neon connection pooling](https://neon.com/docs/connect/connection-pooling): direct migration/admin connections and pool limitations.
- [Neon pgvector](https://neon.com/docs/extensions/pgvector): hosted vector support.
- [Neon secure connections](https://neon.com/docs/connect/connect-securely): verified TLS and CA configuration.
- [pgvector official repository](https://github.com/pgvector/pgvector): vector storage and exact/approximate search.
- [PostgreSQL SELECT documentation](https://www.postgresql.org/docs/current/sql-select.html): locking and SKIP LOCKED queue primitives.
- [FastAPI background tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/): in-process task mechanism; the durable worker here is an application architecture choice.
- Hackathon requirements: user-supplied text in this conversation; verify final dates/rules on the organizer's submission page before release.
- Product requirements: DreamThread_Product_Proposal.pdf in this workspace, plus the approved refined UI direction.

