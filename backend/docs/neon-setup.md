# DreamThread with Neon: complete setup

Neon hosts PostgreSQL/pgvector, journal data, personal memory, embeddings and jobs. Supabase
continues to provide sign-in/token verification. NVIDIA Nemotron and embeddings run on Nebius.
You do not need local PostgreSQL. Docker is optional for the API/worker only.

## 1. Create the hosted database

Create/select a Neon project and database. Prefer a region near the future API/worker host.
Open Connect, choose the intended branch/database and its administrator role, and turn
connection pooling OFF. Copy the direct PostgreSQL URL privately. The hostname must not
contain -pooler. Keep the exact database name and hostname from Neon; neondb is only an example.

Create application roles through our SQL provisioning script, not through the dashboard Roles
button: [Neon dashboard/API roles inherit neon_superuser](https://neon.com/docs/manage/roles).
Our runtime and queue reject that membership. The administrator role is allowed for setup only.

## 2. Create the private configuration

From PowerShell:

```powershell
cd C:\Users\talha\Desktop\DreamThread\backend
uv sync --locked
Copy-Item .env.example .env
notepad .env
```

If .env already exists, edit it instead of overwriting it. Change ADMIN_DATABASE_URL to your
direct Neon administrator URL. Both postgresql:// and postgresql+psycopg:// inputs are accepted;
our connection helper selects psycopg and upgrades Neon sslmode=require to verify-full.

```dotenv
ADMIN_DATABASE_URL="postgresql://neondb_owner:YOUR_PASSWORD@ep-YOUR_ENDPOINT.REGION.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
```

Keep passwords URL-encoded (the copied connection URL already handles this). Then run:

```powershell
uv run python scripts/configure_neon.py
```

This makes no database request. It writes three URLs into .env with distinct random passwords:

| Variable | SQL login | Purpose |
|---|---|---|
| DATABASE_URL | dreamthread_app | Tenant-scoped API/worker data access |
| MIGRATION_DATABASE_URL | dreamthread_migrator | Owner role for Alembic only |
| QUEUE_DATABASE_URL | dreamthread_claimer | Cross-user job leasing without journal access |

All URLs target the same direct Neon endpoint/database. Existing meaningful credentials are
preserved on rerun; changing to another Neon project requires reviewing/replacing the old three
URLs explicitly. Re-running setup does not rotate passwords on existing database roles.
Never paste URLs/keys into chat or commit .env. The generated file is Git-ignored.

Recommended application settings (already in .env.example):

```dotenv
APP_ENV=development
EXPECTED_SCHEMA_REVISION=0008_request_rate_limits
PROMPT_VERSION=v2
EMBEDDING_VERSION=v1
CORS_ORIGINS=["http://localhost:3000"]
DB_POOL_SIZE=3
QUEUE_POOL_SIZE=1
DB_TIMEOUT_SECONDS=20
DB_CONNECT_TIMEOUT_SECONDS=15
DB_POOL_RECYCLE_SECONDS=300
```

Each API process uses up to DB_POOL_SIZE connections. A worker process uses another runtime
pool plus QUEUE_POOL_SIZE, so the defaults allow about seven DB connections for one API and
one worker. Account for extra replicas/CLI sessions. An always-running polling worker can keep
Neon compute active; stop it when unused or tune WORKER_POLL_SECONDS (up to 30).

## 3. Provision the extension and restricted roles

```powershell
uv run python scripts/provision_database.py --neon-logins
```

This connects with the administrator URL, installs vector in extensions, creates NOLOGIN
privilege groups and the three SQL login roles, and grants each login its single group.
No user data is imported or erased. The administrator must own the selected database or have
the needed Neon administration privileges. Unsafe existing roles/extra memberships cause a
failure rather than privilege repair or escalation. Existing passwords are not reset.

If pgvector is already installed in public or another schema, setup fails with sanitized output.
Use a clean application database or deliberately resolve its extension placement before retrying;
the script does not relocate an existing extension. No full Postgres superuser is required.

## 4. Configure authentication and AI

Fill these existing fields in .env using your private accounts:

```dotenv
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_PUBLISHABLE_KEY=YOUR_PUBLISHABLE_KEY
NEBIUS_BASE_URL=https://api.tokenfactory.nebius.com/v1/
NEBIUS_API_KEY=YOUR_NEBIUS_KEY
NEBIUS_EXTRACTION_MODEL=EXACT_AVAILABLE_NVIDIA_NEMOTRON_ID
NEBIUS_EMBEDDING_MODEL=EXACT_AVAILABLE_EMBEDDING_ID
NEBIUS_OUTPUT_MODE=json_object
```

SUPABASE_SECRET_KEY is unused by current authentication. NEBIUS_REASONING_MODEL is reserved
for later reasoning stages; production Settings currently requires it, so select a verified
Nemotron ID before production. Current extraction/reflection both use the extraction model.

Set EMBEDDING_DOCUMENT_PREFIX and EMBEDDING_QUERY_PREFIX exactly according to your chosen
model card, with EMBEDDING_PREFIXES_VERIFIED=true only after checking that input contract.
Explicit empty strings are valid when the model requires no prefix. Keep the configured input
and batch budgets within the actual model/provider limits. See model-capabilities.md for examples.

Leave EMBEDDING_DIMENSIONS commented initially, then run this potentially billable synthetic probe:

```powershell
uv run python scripts/probe_embeddings.py --live
```

Set EMBEDDING_DIMENSIONS to its returned measured_dimensions integer. Then run:

```powershell
uv run python scripts/probe_inference.py --live
```

Both should report live_verified=true. These scripts need valid settings but do not require
the application tables yet. They use synthetic text and print metadata only. No live provider
or Neon check has been performed as part of the code migration without your credentials.

## 5. Migrate and verify Neon

```powershell
uv run alembic upgrade head
uv run python scripts/check_database.py
uv run python scripts/check_neon.py --live
```

Expected: Database readiness: ok, then neon_live_verified=true. The Neon check is read-only:
it checks current schema/runtime role, queue isolation, TLS, vector schema and transaction-local
tenant context. It does not insert sample users, lease jobs or call an AI model. A separate
authenticated end-to-end saved-dream check is still required before deployment.

Neon TLS is verified with a certifi CA bundle resolved within the running environment, including
Windows. An explicit sslrootcert in a URL takes precedence. Do not disable TLS to fix a connection
error. Connection pooling is deliberately OFF in this release. [Neon's pooling documentation](https://neon.com/docs/connect/connection-pooling)
explains why direct connections are appropriate for migrations/admin sessions. Transaction pooling
can be evaluated later with explicit tenant-context and prepared-statement acceptance tests.

## 6. Run the API and worker

Terminal 1, from backend/:

```powershell
uv run uvicorn app.main:create_app --factory --loop app.loop:create_loop --reload --port 8000 --no-access-log
```

Terminal 2, from backend/:

```powershell
uv run python -m app.workers.main
```

Open http://localhost:8000/docs and http://localhost:8000/health/ready. Sign in through Supabase
and supply the user's access token in Swagger Authorize to use protected endpoints. The API
does not accept the publishable key itself as user authentication. Save a dream, poll its job,
then inspect analysis/evidence, indexing and candidates. Stop each process with Ctrl+C.

Alternatively, after provisioning/migration and setting the measured dimension:

```powershell
docker compose up --build -d
docker compose logs --tail 50 api worker
```

Compose starts only API/worker, connects to Neon and does not inject administrator/migration
secrets. Use docker compose down to stop application containers. The container guide does not
create or delete any Neon database. The frontend is a separate service.

## Troubleshooting and verification limits

- Configuration failure: check direct hostname, encoded password, database name and required
  fields. Exported shell variables override .env; remove stale DATABASE_URL/QUEUE_DATABASE_URL/
  MIGRATION_DATABASE_URL/ADMIN_DATABASE_URL variables from that terminal when switching projects.
- Provisioning failure: check admin permissions, whether application roles were incorrectly
  created through the Roles UI, existing extra memberships and extension schema. Sanitized output
  intentionally omits raw server exception text and URLs. Existing passwords are never reset.
- unsafe_role: API is using an administrator/owner/broad-data role, or lacks the runtime group.
- schema_mismatch: run Alembic with the migration URL and check EXPECTED_SCHEMA_REVISION.
- unavailable: check Neon branch compute/network/TLS, URL credentials and connection allowance.
  Cold starts have a configurable connection timeout; retry the readiness check after wake-up.
- Failed embedding/reflection: use job error codes and the opt-in provider probes; database
  readiness alone does not prove provider availability or real model output quality.

Run unit/API tests with `uv run pytest -m 'not integration'`. Real migration/RLS tests require a
disposable PostgreSQL 18.4 cluster with pgvector available in the `extensions` schema. The test
fixture creates a uniquely named `dt_test_<suffix>` database and four uniquely named login roles,
then drops only those generated resources. It also creates the standard `anon`,
`authenticated`, and `service_role` NOLOGIN roles only if they are absent; those shared roles are
not removed. The cluster administrator URL must connect directly to a maintenance database and
have permission to create/drop databases and roles and install pgvector in the newly created test
database. Do not use any application, production, or shared user-data database for this setting.

Set `TEST_ADMIN_DATABASE_URL` only in the test process environment, using a private URL for that
disposable cluster, then run all database integration tests:

```powershell
$env:TEST_ADMIN_DATABASE_URL = "postgresql+psycopg://<test-admin>:<password>@<test-host>/<maintenance-db>"
uv run pytest -m integration -q
Remove-Item Env:TEST_ADMIN_DATABASE_URL
```

The integration fixture runs the complete Alembic chain and exercises tenant RLS, migrations,
worker leases/races, transaction behavior, deletion and idempotency. Never point
`TEST_ADMIN_DATABASE_URL` at the application Neon database. If a local PostgreSQL server or
Docker is unavailable, provision a separate disposable PostgreSQL 18.4/pgvector cluster first;
do not substitute SQLite or a production Neon branch. Hosted acceptance uses a separate Neon
branch, `check_neon.py --live`, and an authenticated app smoke journey.

Source references: [Neon roles](https://neon.com/docs/manage/roles),
[pgvector support](https://neon.com/docs/extensions/pgvector),
[TLS verification](https://neon.com/docs/connect/connect-securely).
