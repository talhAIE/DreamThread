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
