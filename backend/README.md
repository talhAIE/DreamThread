# DreamThread backend - Phase 7

Python 3.12, FastAPI, SQLAlchemy Core, Alembic, PostgreSQL and pgvector.
This release includes the database foundation, verified authentication, profile/preferences
endpoints, shared AI quota admission, and the dream journal API with immutable revisions,
durable analysis submissions, source invalidation and a recoverable background worker.
The worker now calls NVIDIA Nemotron through Nebius for extraction and tentative reflections,
verifies source quotes, and publishes linked evidence atomically. Live account verification is
pending the user's credentials; local tests use controlled provider responses. Phase 7 adds
durable embedding jobs and tenant-scoped hybrid candidate retrieval with visible fallback.

## Local setup

Install Python 3.12, uv and Docker with Compose. Run from backend/:

```powershell
uv sync --locked
Copy-Item .env.example .env
docker compose up -d db
$env:ADMIN_DATABASE_URL = 'postgresql+psycopg://dreamthread:local-development-only@localhost:5432/dreamthread'
uv run python scripts/provision_database.py --local-logins
```

Provisioning explicitly creates pgvector in schema extensions, NOLOGIN privilege groups and,
with --local-logins, development-only login roles. Existing passwords are not reset.
The administrator key is used only for provisioning. Do not pass it to the API or worker.
Local passwords committed in .env.example/Compose are public development fixtures only.

Set EMBEDDING_DIMENSIONS in your private .env to the actual dimension measured in Phase 0,
then migrate with the dedicated migration URL from .env:

```powershell
uv run alembic upgrade head
uv run python scripts/check_database.py
uv run uvicorn app.main:create_app --factory --loop app.loop:create_loop --reload --port 8000 --no-access-log
```

No migration can create vector columns without an explicit positive dimension. Tests use
three-dimensional synthetic vectors; **3 is not a recommended production embedding size**.
The API checks its dimension against the recorded database dimension. Changing models or D
requires a deliberate future embedding migration, not an environment-only change.

Open http://localhost:8000/docs. GET /health/live checks process health. GET /health/ready
checks the database role, schema revision and dimension without billable model requests.
Missing/unavailable DB, mismatched schema or an owner/superuser connection returns safe 503.
Providers are marked configured_unverified until real provider probes are performed.

After host-side provisioning/migration, run containers with docker compose up --build -d.
Compose API uses the restricted local runtime login and EMBEDDING_DIMENSIONS from .env;
without a dimension it deliberately fails configuration validation. The named DB volume
persists across restarts; do not delete it as a generic troubleshooting step.

## Database ownership and access

| Group (NOLOGIN) | Permissions |
|---|---|
| dreamthread_owner | DDL/migration ownership; trusted, never used by API |
| dreamthread_runtime | Tenant-scoped CRUD, no DDL/ownership/BYPASSRLS; revisions cannot update |
| dreamthread_queue | Jobs SELECT and lease-column UPDATE only; no journal access |

Production: run scripts/provision_database.py without --local-logins using an administrator
connection, then create independent login roles with strong private passwords and grant each
exactly one group above. Configure MIGRATION_DATABASE_URL only in the release/migration
environment. Keep DATABASE_URL runtime-only and future QUEUE_DATABASE_URL claimer-only.
Use sslmode=verify-full and the host's trusted certificate configuration remotely.

Supabase: ensure app is absent from exposed Data API schemas in project settings. The
migration additionally revokes schema/table access from anon, authenticated and service_role
when present, enables and forces RLS on every tenant table, and removes public grants/defaults.
Hosted Supabase settings require a separate hosted smoke check; SQL grants alone do not
change the dashboard's exposed-schema list. Existing pgvector must live in extensions.

All app/worker queries must use Database.tenant_transaction(verified_user_uuid). The helper
validates UUID syntax and sets app.user_id transaction-locally with a bound parameter.
It does not authenticate: Phase 3 must verify the token before supplying an identity.
Do not expose custom SQL or tenant selection to clients or models. A stolen runtime DB
credential can set a custom PostgreSQL setting; RLS here protects application query mistakes,
not against an attacker with those trusted backend credentials.

Every tenant child foreign key includes user_id. Current revision constraints also include
dream_id, so another revision owned by the same user cannot be selected accidentally.
Revisions are immutable, original source text is preserved, and evidence checks exact Unicode
code-point spans and source hash equality in PostgreSQL. Application claim/quote verification
and source invalidation still belong to later phases.

## Migrations

- 0001_core: 32 application/configuration tables, vector(D), indexes, composite foreign keys,
  private schema grants, forced RLS, restricted queue leasing and immutable revision trigger.
- 0002_revision_guard: database evidence source/span validation.
- 0003_identity_limits: profile/preference versions, verified anonymous status, tenant-isolated
  AI admission counters.
- 0004_worker_checkpoints: lease/stage deadlines and private checkpoint artifacts.
- 0005_embedding_spaces: tenant-private vector spaces and source-aligned embedding chunks.
  Set EXPECTED_SCHEMA_REVISION=0005_embedding_spaces in existing .env files before starting.
- app.alembic_version is readable by runtime for readiness; application DDL belongs to owner.
- schema_v1.py/security_v1.py are frozen snapshots. Future changes use new migrations;
  never edit released snapshots. Runtime tables currently re-export baseline metadata.
- Downgrades delete data; only exercise on disposable databases. Extension/group roles stay
  installed because other databases/services may use them. The initial app schema can remain.
- Health checks/startup never run migrations or provisioning automatically.

## Tests and CI

```powershell
uv run ruff check app migrations scripts tests
uv run pytest -m 'not integration'
uv run python scripts/smoke_foundation.py
# Administrator access to a disposable local/CI cluster only, NEVER production:
$env:TEST_ADMIN_DATABASE_URL = 'postgresql+psycopg://ADMIN:PASSWORD@localhost:5432/postgres'
uv run pytest
```

Integration tests create uniquely named disposable databases and login roles, then clean up
only those resources. They test real pgvector and PostgreSQL, not SQLite. They verify fresh
and existing upgrades, downgrade/rebuild, all-table no-context reads, same-connection tenant
reset, cross-user reads/writes/foreign keys, same-user wrong revisions, Unicode evidence,
vector dimensions, immutable revisions, queue access, Data API role denial, and live Uvicorn
readiness under restricted credentials. Without TEST_ADMIN_DATABASE_URL they are skipped.

The local Phase 5 verification passed all 84 tests, including isolated PostgreSQL/pgvector
integration. See docs/implementation-progress.md for details and deployment limits.

## Authentication and account API

Configure SUPABASE_URL as your project's HTTPS origin and SUPABASE_PUBLISHABLE_KEY in your
private .env. The frontend handles Supabase sign-up, login and refresh, then supplies the
access token in Authorization: Bearer. Backend verification always calls that project's
Auth /auth/v1/user endpoint; no local JWT-decoding shortcut or development bypass exists.

The protected endpoints are GET/PATCH /api/v1/me and GET/PATCH /api/v1/me/preferences.
PATCH requires expected_version and at least one changed field. Profiles/preferences are
provisioned atomically only after identity verification. UTC is the initial timezone.
Anonymous Auth users require DEMO_ENABLED=true. Never pass a service/admin key as a user token.

Defaults: a 5-second Auth timeout, 131072-byte request body limit, 100 AI admission units per
regular user per UTC day and 5 for an anonymous user. Environment names are in .env.example.
These limits are shared through PostgreSQL, not in-memory counters. reserve_ai is an internal
transactional service for future AI endpoints; no model call or artificial AI endpoint is
exposed in this phase. Subsequent resource/job creation must use its returned ID and commit
in the same transaction. Idempotent replays return the original resource without new work.

See [the API contract](docs/api-contract.md) for request examples, error codes, shared emotion/
pagination contracts, quota/idempotency integration, and hosted-auth verification limits.

## Dream journal API

- POST /api/v1/dreams: create draft/saved entry; requires Idempotency-Key. A saved entry returns
  201 with an analysis_job_id. Drafts do not reserve AI allowance or queue work.
- GET /api/v1/dreams: cursor pagination and date/emotion/keyword/lifecycle filters.
- GET/PATCH/DELETE /api/v1/dreams/{id}: source text, versioned changes, immediate source deletion.
- GET /api/v1/dreams/{id}/revisions/{revision_id}: owned historical source, with is_current flag.
- POST /api/v1/dreams/{id}/analyze: submit/retry current saved revision; Idempotency-Key and
  expected_version required; returns a durable job reference with 202.
- GET /api/v1/dreams/{id}/analysis: current processing state, or valid cached result when ready.

Source text and user-reported emotions are stored exactly in immutable revisions. Content/emotion
edits create a new revision; title/date changes update metadata and requeue stale analysis.
No AI request occurs on save. Saving still works when the AI allowance is exhausted: its job
is queued with waiting_quota and a future run_after. Explicit failed-job retry can return 429.
The worker reserves deferred allowance before running its handler.

Edits conservatively invalidate all cached AI results for the owner until precise evidence
dependency traversal exists. User decisions survive reanalysis. Delete removes the source and
its revisions immediately, clears owner-derived cached content, cancels in-flight jobs and
increments data_epoch to fence future publishing. The worker must enforce those fences in
the worker. There is no voice upload or generated dream imagery in this phase.

## Durable worker and job status

After administrator provisioning and migration, configure QUEUE_DATABASE_URL from .env.example
with the separate dreamthread_claimer login. DATABASE_URL remains the tenant-scoped runtime login.
The queue login must inherit only dreamthread_queue, never owner/runtime privileges. Startup checks
both connections and schema readiness; there is no administrator/runtime fallback for queue access.

```powershell
uv run python -m app.workers.main
# Process one eligible job (or exit idle), useful for setup checks:
uv run python -m app.workers.main --once
```

Compose now includes an independent worker service: docker compose up --build -d after host-side
provisioning/migration. It has no HTTP listener; monitor process health and oldest queued/running
job age. Docker/hosted CI execution remains a separate deployment check.

Defaults: two concurrent handlers, 120-second lease, 20-second heartbeat, 90-second stage deadline,
300-second attempt deadline, three automatic attempts, exponential jittered retry starting at
2 seconds (base capped at 60), and a 30-second reaper. Provider Retry-After can delay retries further.
Tune using real inference measurements in Phase 6. All timing/retention variables are in .env.example.

GET /api/v1/jobs/{id} returns safe status/stage/attempt/error/checkpoint-name metadata only.
POST /api/v1/jobs/{id}/retry requires an Idempotency-Key and expected_attempts/expected_submission
from GET. Only failed owned jobs with current source/pipeline/memory/epoch can restart; otherwise
use POST /dreams/{id}/analyze on the current source. A new manual retry is quota-limited and increments
submission; replaying its key does not create another attempt or reservation.

Workers claim with SKIP LOCKED and fresh lease tokens, release DB transactions before handler work,
renew leases independently, and fence every checkpoint/publication with the token plus current
source revisions, memory/pipeline and deletion epoch. Success commits with its published result.
SIGTERM/SIGINT stop new claims; active work drains for ten seconds, then cancellation leaves a
recoverable lease. Expired attempts can be reclaimed; exhausted ones become visible failures.

Checkpoint outputs live in tenant-private job_artifacts, not queue-readable job payloads. Stage
reuse verifies hashes and all referenced source versions. Terminal job artifacts are purged after
seven days; expired idempotency/old quota rows are cleaned for owners encountered in maintenance.
Full inactive-account retention remains Phase 14 work. Edits/deletion remove owner checkpoints.

The production analysis handler uses the Phase 6 provider pipeline described below. Without
credentials it fails safely with provider_not_configured and produces no interpretation.

The CI workflow provisions/migrates PostgreSQL, runs tests and builds/smoke-tests the API
container. Creating the workflow does not mean it has run. Windows Uvicorn must use the
supplied selector-loop factory for psycopg async compatibility; Linux can use it too.

## Nemotron extraction and reflections

Set NEBIUS_API_KEY and NEBIUS_EXTRACTION_MODEL in your private backend/.env. Use the exact
available NVIDIA Nemotron ID from your Nebius account catalog; no model ID is guessed or
automatically substituted. Both single-entry stages use this model. NEBIUS_REASONING_MODEL
is reserved for later cross-entry reasoning. Compose passes these server-side settings to
both API and worker; never put the key in frontend environment variables.

NEBIUS_OUTPUT_MODE=json_object is the default. json_schema is an explicit option to verify
against your chosen model; unsupported modes fail visibly without a silent fallback. Output
is capped by MODEL_MAX_OUTPUT_TOKENS. A conservative UTF-8 byte bound (including schema,
output allowance and overhead) rejects oversized context before sending it; text is never
silently truncated. Set MODEL_CONTEXT_TOKEN_BUDGET within the selected model's supported limit.

Extraction and reflection have separate versioned prompts in app/prompts. PROMPT_VERSION=v1
must correspond to existing templates. Continuity and Detective prompts are reserved for
later phases. Dream content is untrusted data, with no tools available to the model.

Schema/source/quote checks allow one shared repair across both stages per submission.
Validated checkpoints survive transient retries. Explicitly reported mood tags retain their
original values; model emotion observations and tentative reflections are separate fields.
Exact citations prove where text occurred, not the psychological validity of an interpretation.
Conservative negation/wording guards are heuristic and currently English-focused.

Analysis, verified evidence links and job success commit in one tenant-scoped transaction.
Provider metadata records requested model, prompt version, latency, status and nullable token
usage. Raw provider output and hidden reasoning are not persisted. Validated structured stage
outputs are private checkpoints, covered by worker retention and source invalidation.

After configuring credentials, explicitly run this potentially billable synthetic-source check:

```powershell
uv run python scripts/probe_inference.py --live
```

It confirms catalog membership, runs the production extraction/reflection validators and prints
metadata only. This live check has **not** been run; the user deferred it until keys are ready.
See [capability status](docs/model-capabilities.md). Then run the API/worker and save a dream,
poll its job, and retrieve /api/v1/dreams/{id}/analysis for the complete database-backed flow.
Phase 6 changes the pipeline fingerprint; analyze the current dream again if an older job is stale.
Phase 6 required no migration. Phase 7 advances the head to 0005_embedding_spaces.

## Embeddings and retrieval

Migrate with `uv run alembic upgrade head` using the separate migration credentials. The existing
worker now handles both analysis and embedding jobs. Configure NEBIUS_EMBEDDING_MODEL with an
available catalog ID, EMBEDDING_DIMENSIONS with its measured dimension, and both
EMBEDDING_DOCUMENT_PREFIX and EMBEDDING_QUERY_PREFIX with the exact model-card input format.
An explicitly empty prefix is allowed. Set EMBEDDING_PREFIXES_VERIFIED=true only after checking
that contract; absent prefixes or the false default prevent indexing. Neither model IDs nor
prefixes are guessed. The embedding model can be a different open model; Nemotron remains the
required NVIDIA model used for extraction/reflections.

Use `uv run python scripts/probe_embeddings.py --live` when credentials are ready. This opt-in
check confirms catalog membership, measures dimensions with synthetic text, validates vectors
and exercises the production adapter. It makes potentially billable calls. It has not been run.
Reference model/card links and verification limits are in [capability status](docs/model-capabilities.md).

Successful analysis automatically queues indexing when embedding configuration is valid. It
shares that analysis workflow's existing admission unit. Indexing failures never overwrite a
ready reflection. To index independently or retry an exhausted embedding job, call
POST /api/v1/dreams/{id}/index with Idempotency-Key and {"expected_version":1}. Explicit indexing
costs one admission unit when new work is queued; replay or reuse does not charge again. The
response supplies a job_id for the existing /jobs/{id} polling API. Transient provider errors
retain the durable job with bounded worker retries. After exhaustion, submit /index with a new
key. The generic /jobs/{id}/retry route currently supports analysis jobs only.

GET /api/v1/dreams/{id}/candidates makes no model calls. It returns at most 12 unique current
saved dreams, merging semantic top 8 with text and non-rejected motif matches. Confirmed
motifs/associations receive stronger weight; rare shared words outrank generic text matches.
Candidates include current IDs/revisions/hashes, dates, snippets and ranking reasons; fetch
original source separately before evidence-based comparison. Similarity never creates an
identity merge or connection. Comparison is Phase 9. Memory-editing endpoints arrive in Phase 8;
retrieval can already consume confirmed records through the shared schema.

Each response exposes degraded/reasons, active/target embedding versions, current archive
coverage and the primary entry's latest indexing job. When vectors are absent, rebuilding or
failed, text/motif retrieval remains available and degradation is disclosed. Complete but empty
results are valid. Stale revisions, drafts, deleted sources, the current dream and other users
are excluded before ranking. Exact cosine uses a materialized tenant/space/current-source filter
before distance computation, without an ANN index. This MVP scans the owner's archive and is
intended for small journals.

Short entries use one exact-source embedding. Longer entries use deterministic, contiguous
Python Unicode spans under EMBEDDING_INPUT_TOKEN_BUDGET (UTF-8 byte bound plus 32-token service
headroom and the longer prefix). Nothing is truncated. Requests batch at most 16 inputs and
respect EMBEDDING_BATCH_BYTE_BUDGET. Verify both limits against the actual provider/model.
Document and query embeddings are stored separately when prefixes differ. Chunk matches
deduplicate by dream and use maximum cosine similarity; these scores nominate candidates only.

Change EMBEDDING_VERSION whenever the model, dimension, prefixes or chunk contract changes.
Reusing a version with changed settings fails with embedding_version_conflict. Rebuild the
complete saved archive, for example from the trusted backend terminal:

```powershell
uv run python scripts/reindex_archive.py --user-id YOUR_VERIFIED_ACCOUNT_UUID
```

This queues potentially billable work through runtime tenant credentials and quota admission;
it does not impersonate a user through the HTTP API. Run the worker to process it. On quota
exhaustion, rerun later; admitted requests replay. New/edited sources can require another pass.
Failed jobs need a fresh /index request key. A replacement space becomes active atomically under
the account lock only when every current saved revision is indexed. Until then retrieval uses
the previous complete space where available and reports rebuilding. Stored query vectors allow
that old space to remain internally consistent even after server settings change. Different
dimensions coexist in isolated spaces and are never compared.

Phase 7 uses embedding_spaces/embedding_chunks; the Phase 2 fixed-dimension dream_embeddings
table and baseline schema_configuration value remain for migration compatibility and are not
searched. New-space dimension checks happen before insertion and in PostgreSQL. Old space
vectors are retained for rollback and cascade away with their source; broader retention is
Phase 14 work. Source mutation still conservatively cancels owner jobs, so use reindex_archive
to restore outstanding indexing after edits when needed.

[Benchmark results](docs/retrieval-benchmark.json) record recall@5=1.0 on three queries over
13 labeled fixture entries using real PostgreSQL and synthetic provider vectors. This is a
pipeline regression check, not a claim about real model retrieval quality.

## Configuration and observability

Settings load .env from the current directory. Invalid startup settings are sanitized.
CORS accepts explicit origins; credentialed cookies are disabled. Request IDs are validated
or generated. Errors omit submitted inputs, credentials and database exception strings.
Production requires HTTPS origins, verified DB TLS, configured provider settings and a
migrated expected revision. No secrets are returned by health endpoints. Uvicorn access logs
are disabled in documented commands to avoid logging query-string content.
