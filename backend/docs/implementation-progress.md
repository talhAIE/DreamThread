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
