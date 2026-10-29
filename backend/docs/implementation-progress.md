# Implementation progress

## Phase 1 - October 29, 2026

Implemented:

- Python 3.12 package and uv.lock with reproducible production/dev dependencies.
- Typed settings, secret redaction, safe startup errors and production configuration gates.
- Explicit CORS, request correlation, sanitized HTTP/validation/unexpected errors.
- Process liveness and bounded database/schema readiness checks.
- Provider configuration metadata without inference requests or credentials.
- Non-root Docker image and pinned pgvector/PostgreSQL Compose development service.
- Contract tests, opt-in PostgreSQL integration test, real Uvicorn smoke script.
- CI workflow for PostgreSQL integration, Docker build and container HTTP checks.

Local verification:

- Ruff: passed.
- pytest: 9 passed, 1 skipped (real PostgreSQL integration unavailable).
- Real local Uvicorn HTTP smoke: passed liveness, OpenAPI, request IDs and safe 503 readiness.
- Production settings tested for HTTPS, verified DB TLS mode, required providers and schema revision.

Outstanding environment acceptance:

- Docker and PostgreSQL are not installed on this machine. Container build/start and real
  database/schema queries have not been verified locally. Run the README's integration and
  Compose commands, or the supplied CI workflow, to close this acceptance gate.
- Phase 0 live Nebius/Supabase capability checks remain unperformed. No model identifiers
  or embedding dimensions were invented. Local startup reports missing providers honestly.
- The current Starlette version warns that its HTTPX TestClient transport is deprecated;
  contract tests pass. This does not affect the production Uvicorn service.

Phase 1 code is implemented; the full phase acceptance gate stays pending until the real
database and container checks pass. Phase 2 schema/RLS, auth, workers and inference are not implemented.

## Phase 2 - October 29, 2026

Implemented:

- Alembic baseline and follow-up migrations; 32 application/configuration tables plus version table.
- Full planned relational schema, typed vector(D), tenant-composite parent references, stable
  unique constraints, search/queue indexes and database timestamps.
- Same-dream current-revision constraints, immutable revisions, verified Unicode evidence spans.
- Private app schema, explicit owner/runtime/queue groups, forced RLS on all tenant tables,
  revoked Data API role grants, and default-deny no-context queries.
- Async tenant transactions with parameterized transaction-local UUID settings and pool reset.
- Runtime readiness rejects owner/superuser/queue credentials and schema/dimension mismatches.
- Explicit administrator provisioning and separate migration/runtime login setup.
- Windows selector-loop support for psycopg; README, Docker and CI updated for migrations.

Verified locally:

- Ruff passed. Full pytest suite: **22 passed, zero skipped**.
- Real PostgreSQL with pgvector: fresh schema, preserved-data upgrade, downgrade/rebuild,
  missing-dimension rejection, repeated upgrade-to-head and actual vector-size rejection.
- All-table default-deny reads, user A/B isolation, reused pool connection reset after commit
  and rollback, cross-tenant FK denial, same-user wrong-revision denial and safe delete cascade.
- Immutable original revisions, exact supplementary-Unicode evidence, fabricated evidence rejection.
- Queue limited to lease updates; no journal reads, ownership changes, deletion or DDL.
- Data API authenticated-role login denied schema access; runtime cannot assume owner.
- Actual Uvicorn HTTP readiness under a restricted login and safe unavailable-DB smoke passed.
- Administrator provisioning CLI, dedicated migration CLI and runtime check_database.py passed.

Test environment and remaining deployment checks:

- PostgreSQL was found installed outside PATH. Testing used a disposable workspace copy and
  isolated loopback cluster on port 55439; the installed database/services were not modified.
- Windows test pgvector: community precompiled v0.8.6 for PG18, downloaded only into ignored
  tmp/phase2-pg. Source: https://github.com/andreiramani/pgvector_pgsql_windows/releases/tag/0.8.6_18
- Download archive SHA256: BDA17EB97D9E687E3DA701ADBF4B65A342943B3E0CDC81935CCF0B9833A1ED62.
  This records artifact provenance, not an independent source-build verification.
- Synthetic vectors used D=3 only for tests. Production model IDs/D remain Phase 0 inputs.
- Hosted Supabase exposed-schema configuration and privileges need verification on the actual
  project. Tests validate equivalent PostgreSQL role/grant denial, not a live Supabase REST API.
- Docker/CI execution remains unverified locally because Docker is unavailable. CI is configured
  to provision/migrate the pinned pgvector/PostgreSQL container and test the API container.
- The isolated test server is stopped after verification; rerun tests against a disposable
  pgvector cluster using the README instructions. No credentials/data from production were used.

Phase 2 local schema/security acceptance checks are complete. Hosted deployment, authentication,
inference services and worker execution are subsequent phases, not claims of this implementation.

## Phase 3 - October 29, 2026

Implemented:

- Supabase GET /auth/v1/user verification with project publishable key and bounded HTTP timeout.
  Missing/invalid/expired/wrong-project credentials fail closed. No unverified JWT/session fallback.
- Idempotent, transactionally provisioned account and preferences after verified identity; deletion
  markers are respected. Identity and anonymous status never come from user_metadata or request IDs.
- GET/PATCH /api/v1/me and /api/v1/me/preferences, optimistic version conflicts, synchronized
  validated IANA timezones, bounded display names, strict settings fields and UTC responses.
- Schema migration 0003_identity_limits with version fields and forced-RLS AI admission counters.
- Atomic per-user daily quota + idempotency service, shared across processes and pools. Request
  fingerprints/resource IDs persist without raw bodies; quota failures occur before model work.
- Request byte limits covering chunked bodies, uniform typed error envelopes, Retry-After CORS
  exposure, bearer OpenAPI contracts, emotion vocabulary and bounded pagination helpers.
- HTTPX and timezone data are runtime dependencies. API docs and setup notes updated.

Verification:

- Ruff passed. Full pytest suite: **46 passed, zero skipped**.
- Actual PostgreSQL 18.4/pgvector integration passed upgrades, rollback/rebuild and tenant checks.
- Eight concurrent first requests produced one profile/preferences pair; A/B settings stay private.
- Two independent database pools admitted only the configured allowance under concurrent load.
- Rollback did not consume a reservation; identical-key replay did not invoke the test provider
  again; altered-body reuse returned 409; exhaustion returned 429 with Retry-After.
- Rejected identity responses provisioned no rows. Auth network/malformed responses return 503.
- Auth tests exercise HTTPX MockTransport through the real adapter; invalid/expired/other-project
  token rejection is simulated at the remote-response boundary, not proven against hosted Supabase.
- Anonymous gating/isolation, account-deletion markers, stale versions, invalid preferences,
  unknown identity fields, body/streaming limits and safe errors passed.
- Real Uvicorn liveness/OpenAPI/database readiness smoke checks remain passing.

Remaining deployment inputs:

- No Supabase credentials were provided. Verify real login, expiration, revocation and a token
  from another project on the chosen hosted project before deployment.
- Phase 0 model IDs/dimension and Docker/hosted CI verification remain outstanding as previously
  recorded. No live NVIDIA model calls are part of this phase.
- AI quotas are an implemented admission primitive. Phase 4/5/11 must call it transactionally
  when they create real resources/jobs; the test-only AI route is not included in production.
- The disposable loopback PostgreSQL cluster is stopped after verification.

## Phase 4 - October 29, 2026

Implemented:

- All eight planned journal routes: create/list/detail/patch/revision/analyze/analysis/delete.
- Exact Unicode/whitespace source preservation, SHA256 hashes and immutable emotion/text revisions.
- Drafts, promotion, optimistic edit conflicts, no-op handling, metadata-only revision preservation.
- Atomic idempotent source/revision/job writes; no provider call or model availability dependency.
- Source idempotency separated from AI allowance. Exhaustion defers automatic jobs rather than
  rejecting a saved dream; explicit failed-job retry remains quota-limited.
- Per-revision/pipeline/memory job deduplication, retry generations, persisted admission handoff.
- Owner-only filters, date/UUID cursors, literal keyword and reported-emotion/date filtering.
- Conservative stale-content invalidation, cleared cached AI text, preserved user decisions,
  cancelled superseded work, physical source deletion and incremented publication fence epochs.
- Current-analysis/evidence checks used by list, detail and analysis views.
- Reuses the Phase 2/3 schema; no database migration is needed.

Verification:

- Ruff passed. Full suite: **59 passed, zero skipped** against the isolated PostgreSQL/pgvector cluster.
- Concurrent create replay produced one dream, revision and job with one AI reservation.
- Source text/newlines/emoji/Urdu and SHA256 round-trip exactly; original revisions retain old tags.
- Date/emotion/keyword filtering and same-date UUID cursor pagination passed.
- All source routes reject another user's IDs with 404.
- Conflicting edits produce one success and one 409; no-op edits create no extra revision/job.
- Simulated queue persistence failure rolls back source/revision/idempotency/quota/job together.
- Allowance exhaustion still saves a source and persists a future waiting_quota job.
- Completed-job metadata edits requeue the existing job with a new generation; pipeline changes
  hide old cached output. Reanalysis preserves confirmed user decisions.
- Source deletion removes history, cancels jobs, fences publication and clears stale cached text.

Boundaries:

- Worker claiming/execution/recovery and publication fences are Phase 5 work. Phase 4 persists
  the required state but cannot demonstrate worker crash recovery or late-response rejection yet.
- Real model extraction/interpretation is Phase 6; no fake output is substituted for queued work.
- Supabase verification is still mocked at the HTTP adapter boundary in local API tests. Hosted
  credentials and Docker/CI checks remain deployment inputs, as recorded in earlier phases.
- The disposable PostgreSQL server is stopped after verification; installed DB services were untouched.

## Phase 5 - October 29, 2026

Implemented:

- Separate worker CLI/process and narrowly privileged queue connection with startup role checks.
- SKIP LOCKED claims, unique lease tokens, independent heartbeats, absolute/stage deadlines,
  bounded attempts, jittered backoff, provider Retry-After classification and safe terminal failures.
- Tenant-private durable checkpoints with source hashes/version validation and restart reuse.
- Atomic publication and success acknowledgement; token/expiry/source/pipeline/memory/deletion
  epoch checks under the same account/source/job locks as source mutation paths.
- Deferred quota admission before handler work, without charging existing reservations again.
- GET /jobs/{id}, source-valid failed-job retry with expected counters and request idempotency.
- Reaper for exhausted/expired jobs, seven-day artifact cleanup and encountered-owner key/quota cleanup.
- Graceful drain/cancellation with reclaimable leases, Docker worker service and CI CLI smoke step.
- Migration 0004_worker_checkpoints: private artifacts and narrow deadline-column queue grants.

Verification:

- Ruff passed. Full suite: **84 passed, zero skipped**, with real PostgreSQL 18.4/pgvector.
- Two workers published once; heartbeat prevented takeover; handler work held no journal DB locks.
- Killed an actual child process after its persisted checkpoint during a simulated external call;
  replacement reclaimed its lease and published once. No physical installed database was modified.
- Old tokens could not heartbeat/publish after takeover. Deletion, source-version, archive-bound,
  memory, pipeline and data-epoch changes prevented publication.
- Checkpoints survived a retry and skipped completed synthetic extraction; queue role was denied
  checkpoint access. Stage timeout and exhausted leases produced bounded recoverable/terminal states.
- Deferred allowance released the lease without consuming an attempt; later admission was charged once.
- Reaper purged old private artifacts/expired keys. Owned retry was idempotent and cross-user access denied.
- Shutdown left a recoverable lease. Standalone worker --once startup passed with separate restricted logins.

Boundaries:

- Production analysis handler intentionally reports inference_not_implemented after preparation.
  Successful publication tests use clearly synthetic, test-only handlers. Phase 6 owns actual Nemotron
  calls, quote/meaning validation and atomic evidence persistence.
- No real provider requests were made. Retry/status behavior was tested with controlled failures;
  real account/provider capability and hosted Supabase verification remain Phase 0/deployment checks.
- Docker/hosted CI execution remains unverified locally; configuration is included for later execution.
- Retention maintenance is job-scoped; complete inactive-account privacy retention is Phase 14 work.
- The disposable loopback test server is stopped after verification.

## Phase 6 - October 29, 2026

Implemented:

- OpenAI-compatible Nebius SDK adapter targeting a configured NVIDIA Nemotron model; SDK
  retries disabled so the durable worker owns retries, timeouts and Retry-After handling.
- Explicit JSON object/JSON schema modes, output caps and conservative context size guard.
- Separate versioned extraction/reflection prompts; reserved continuity/Detective templates.
- Strict structured output, exact owned source/revision/hash/quote checks, deterministic unique
  offset correction, and one persisted repair allowance shared across stages per submission.
- Separate observations, unchanged user mood tags, 1-3 tentative reflections and optional question.
- Application-assigned evidence IDs with evidence/analysis links and success in one transaction.
- Private provider/model/prompt/latency/nullable usage/status telemetry, without raw responses
  or hidden reasoning. Validated checkpoints support retry without repeating completed calls.
- Analysis API returns linked evidence; configuration examples, Compose settings and opt-in
  metadata-only live probe are included. No schema migration is required (head remains 0004).

Verification:

- Ruff passed. Full suite: **106 passed, zero skipped**, including real PostgreSQL/pgvector
  and real SDK requests through controlled HTTPX transport responses.
- Valid extraction/reflection publishes evidence and job success atomically; simulated link
  persistence failure rolls back publication, and retry reuses validated stage checkpoints.
- Fabricated/foreign IDs, incorrect hashes, invented/ambiguous quotes, invalid structure,
  negations, truncation and unsupported model configuration are rejected in fixtures.
- Injection fixtures cannot change the requested model, tool availability or output contract.
- Unicode offset repair, positive emotion after negation, shared repair exhaustion, context
  rejection and provider rate limits are covered. Original mood tags remain unchanged.
- Live Uvicorn/OpenAPI/request-ID/database-unavailable smoke check passed. Probe --help passed.

Remaining gate and limits:

- **Live Nemotron verification is pending credentials.** The user explicitly requested completing
  development before configuring keys. No real provider requests were made. Run the documented
  --live probe, then verify a saved dream through the API/worker when credentials are available.
- Quote checks verify source fidelity; wording/negation guards are English-focused heuristics,
  not a guarantee of psychological interpretation quality or universal injection resistance.
- Docker/hosted CI and hosted Supabase account checks remain deployment verification tasks.

## Phase 7 - October 29, 2026

Implemented:

- Nebius embedding SDK adapter, explicit verified document/query input prefixes, conservative
  per-input token and batch byte/count guards, finite nonzero float32 vector validation and
  pre-insertion dimension checks. Central worker owns retries; SDK retries remain disabled.
- Exact current-source indexing: one vector for short entries, deterministic contiguous Unicode
  chunks for longer entries, source/input hashes, offsets and separate query vectors where needed.
- Migration 0005_embedding_spaces: tenant-owned spaces and chunk vectors, composite ownership/
  dimension foreign keys, forced RLS and restricted grants. Legacy fixed-size vectors are retained
  but not searched. Current schema has 37 tables including Alembic's version table.
- Independent durable embedding jobs, automatically queued after admitted successful analysis
  when configured; explicit quota/idempotent POST /dreams/{id}/index for backfill/retry.
- Index failure/cancellation/reaping preserves single-entry analysis state. Vector publication,
  complete-archive space activation and job success commit atomically with source/lease fences.
- GET /dreams/{id}/candidates: exact tenant/space/current-revision cosine top 8, text/motif and
  confirmed enabled association matches, final cap of 12 unique current saved dreams. Responses
  include snippets, source versions/hashes, ranking metadata, coverage and explicit degradation.
- Provider outages retain bounded retry jobs while lexical/motif fallback remains usable.
- Model/dimension/prefix/chunk changes require a new version. Complete re-embedding precedes
  atomic activation; consistent old-space queries remain available during rebuilding. Different
  dimensions can coexist without cross-space comparisons.
- Reindex CLI, opt-in synthetic live embedding probe, Compose/config examples, API contract and
  benchmark fixture/result documentation. No real model ID or prefixes are silently selected.

Verification:

- Ruff passed. Full suite: **132 passed, zero skipped**, on the disposable PostgreSQL 18.4/
  pgvector cluster; SDK embedding requests used controlled HTTPX transports.
- Migration upgrade/downgrade/rebuild, forced tenant isolation and runtime readiness passed.
- Exact cosine nominations exclude other owners, current dream, stale revisions, drafts and
  deleted entries. Two house candidates never create a connection or merge motif identities.
- Confirmed associations/specific motifs outrank generic text; long Urdu/emoji chunks cover
  every source code point and repeated chunk matches deduplicate to one dream.
- Provider outage returns disclosed text fallback, retains retry work and later recovers.
- Dimension mismatch prevents publication; model changes without a new version fail; partial
  rebuild retains v1 and complete rebuild atomically activates v2, including changed dimensions.
- Simulated activation failure rolls back chunk vectors and success. Source edits during an
  embedding request cancel late publication. Automatic indexing shares analysis admission.
- Labeled synthetic fixture: **recall@5 = 1.0**, three queries over 13 documents. See
  retrieval-benchmark.json; this measures pipeline behavior, not real model semantic quality.
- Live Uvicorn/OpenAPI/request-ID/database-unavailable smoke passed. Both new CLI --help
  commands passed without provider requests.

Remaining verification and boundaries:

- Live Nemotron and embedding probes remain pending the user's credentials, as requested.
  Verify catalog ID, model-specific prefixes, dimension and provider limits before live indexing.
- Retrieval nominates candidates only. Evidence-based comparison/identity separation remains
  Phase 9; user-facing memory correction endpoints remain Phase 8. No identity is inferred here.
- Exact search and lexical scoring scan a small owner archive. Large-archive performance,
  inactive-account/retired-space retention, Docker/hosted CI and hosted auth remain later checks.
- Source mutation still conservatively cancels owner jobs; the documented reindex command can
  refill outstanding indexing. Run migrations and update EXPECTED_SCHEMA_REVISION before use.
- The isolated test server is stopped after verification; installed database services are untouched.

## Phase 8 - October 29, 2026

Implemented:

- Owned paginated motif/memory APIs; explicit user identities and personal meanings; versioned
  edits, confirmation/rejection, disable and forget operations. Model proposals retain provenance,
  remain suggested until confirmed, and respect stored normalized rejection/forgetting signatures.
- Source-local automatic mention IDs, without label-based identity merging. Explicit user links
  can share identity between entries. Publication persists mention/evidence links atomically and
  preserves existing confirmation/rejection decisions.
- Revision-bound detail overrides/removals anchored to original verified spans, applied before
  reflection and marked as user corrections. Original source/quotes and reported moods stay intact.
- Versioned GET/PUT follow-up flow, answer/skip, entry-only clarification and separate explicit
  consent/motif selection for reusable memory. Forgotten follow-up meaning cannot return through
  its saved-answer path; original user answers remain private journal records.
- Personal context loading through leased tenant transactions: confirmed enabled non-deleted
  associations on confirmed current-source identities only, bounded to eight with omission metadata.
- Memory/archive version advancement, derived-text/checkpoint invalidation, old-job cancellation
  and conservative saved-archive rebuild queues in the same transaction. Detail/answer edits advance
  dream.version; automatic rebuilding defers at quota rather than blocking saved corrections.
- Migration 0006_personal_memory adds provenance/forgetting fields and follow-up versions without
  changing the 37-table count. New v2 extraction/reflection prompts preserve released v1 files.
  Pipeline fingerprint, Compose/default configuration and API/setup documentation are updated.

Verification:

- Ruff passed. Full suite: **151 passed, zero skipped**, including real PostgreSQL 18.4/pgvector
  migration upgrade/downgrade/rebuild, tenant checks and real SDK requests through fixture transport.
- A saved grandmother's-home association enters the next linked analysis and affects its fixture
  reflection. Suggested, disabled, deleted and unrelated equal-label identities are absent from inputs.
- Two automatic houses remain distinct until an explicit user identity link; only confirmed links
  carry their personal meanings. Confirming a model proposal activates its existing source mention.
- User label corrections survive transient worker retry, cached raw extraction and reanalysis.
  Removal survives model key/quote-width changes. Original text/revision/mood tags remain unchanged.
- Foreign motif/memory/source/detail/follow-up IDs are rejected; concurrent memory edits yield one
  success and one conflict. Pagination is owner scoped. Replayed writes avoid duplicate mutations.
- Rejected/deleted model meanings cannot be reproposed identically. Forgetting clears stored meaning.
- Follow-up consent, private entry-only clarification, skip, answer editing/version conflicts and
  repeated-question suppression passed. Forgotten memory is absent from subsequent model data,
  including saved follow-up text previously promoted to memory.
- Memory deletion clears dependent assistant answers/checkpoints and prevents an in-flight model
  result from publishing; fresh queued work excludes removed meaning. Simulated queue failure rolls
  back memory/identity/version/invalidation together; quota exhaustion preserves saved memory.
- Live Uvicorn/OpenAPI/request-ID/database-unavailable smoke passed; probe --help made no API call.

Remaining checks and boundaries:

- Live Nemotron/embedding verification remains pending keys under the user's development-first
  instruction. Fixture generation demonstrates data flow, not real model interpretation quality.
- The memory proposal writer is an internal service for later agent work, not a model-controlled
  identity selector or public write tool. Comparison/continuity remain Phase 9.
- Corrections are revision-bound. Forgetting memory does not rewrite independently authored
  original dream text, user detail labels or original follow-up journal answers.
- Rebuilding currently invalidates the small owner's archive conservatively; precise dependencies,
  large-archive scheduling and broader retention remain later work. Hosted auth, Docker and hosted
  CI execution remain deployment verification tasks.
- Update EXPECTED_SCHEMA_REVISION=0006_personal_memory and PROMPT_VERSION=v2, migrate, then restart.
- The isolated test server is stopped after verification; installed DB services are untouched.
