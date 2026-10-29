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
