# DreamThread backend - Phase 3

Python 3.12, FastAPI, SQLAlchemy Core, Alembic, PostgreSQL and pgvector.
This release includes the database foundation, verified authentication, profile/preferences
endpoints, shared AI quota admission and API contracts. Journal services, job execution and
model inference arrive in later phases.

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
  AI admission counters. Set EXPECTED_SCHEMA_REVISION=0003_identity_limits in existing .env files.
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

The local Phase 3 verification passed all 46 tests, including isolated PostgreSQL/pgvector
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

The CI workflow provisions/migrates PostgreSQL, runs tests and builds/smoke-tests the API
container. Creating the workflow does not mean it has run. Windows Uvicorn must use the
supplied selector-loop factory for psycopg async compatibility; Linux can use it too.

## Configuration and observability

Settings load .env from the current directory. Invalid startup settings are sanitized.
CORS accepts explicit origins; credentialed cookies are disabled. Request IDs are validated
or generated. Errors omit submitted inputs, credentials and database exception strings.
Production requires HTTPS origins, verified DB TLS, configured provider settings and a
migrated expected revision. No secrets are returned by health endpoints. Uvicorn access logs
are disabled in documented commands to avoid logging query-string content.
