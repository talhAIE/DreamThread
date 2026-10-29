# DreamThread backend - Phase 1

Python 3.12, FastAPI, Pydantic settings, SQLAlchemy/psycopg, and PostgreSQL.
This phase provides infrastructure only; authentication, migrations, models and jobs follow later.

## Local development

Install Python 3.12 and uv, then from this directory:

```powershell
uv sync --locked
Copy-Item .env.example .env
docker compose up -d db
uv run uvicorn app.main:create_app --factory --reload --port 8000 --no-access-log
```

Open http://localhost:8000/docs. GET /health/live confirms the process is alive.
GET /health/ready checks database access and the expected schema revision, without AI requests.
Without PostgreSQL, liveness works and readiness returns a sanitized 503.
Development credentials in Compose are for local use only; the DB port binds to loopback.
The development DB owner is temporary bootstrap access, not the Phase 2 runtime role.

For the complete containerized development service:

```powershell
docker compose up --build -d
docker compose logs api
docker compose down
```

The named database volume persists across restarts. Do not delete it to troubleshoot blindly.
The image runs as a non-root user and excludes .env and developer tooling.

## Configuration and schema readiness

DATABASE_URL is required and must use postgresql+psycopg. Settings load .env from the
working directory; run commands from backend/. Invalid settings fail startup without printing values.
Secrets are redacted in settings repr. CORS permits only explicit origins; bearer auth headers
are supported and credentialed cookies are disabled. X-Request-ID is echoed if it matches a
bounded safe format; otherwise a fresh ID is generated. Error bodies and logs never echo inputs.
Uvicorn access logs are disabled in documented commands to avoid logging query-string inputs.

EXPECTED_SCHEMA_REVISION=unversioned is a deliberate Phase 1 development bootstrap mode:
readiness accepts a reachable DB with no app.alembic_version table. It does not claim that
application tables exist. Phase 2 must configure Alembic's version table in schema app and
set an exact expected revision. A missing/mismatched version then returns 503. No automatic
DDL or migrations run from API startup or probes.

Production requires configured Supabase/Nebius settings, measured embedding dimension,
HTTPS CORS origins, sslmode=verify-full in DATABASE_URL, and a real expected schema revision.
Configured provider status is explicitly unverified: real access/model probes belong to Phase 0.
No API keys or model identifiers are returned from health endpoints.

## Checks

```powershell
uv run ruff check .
uv run pytest
uv run python scripts/smoke_foundation.py
# Only use a disposable database, not production:
$env:TEST_DATABASE_URL = 'postgresql+psycopg://dreamthread:local-development-only@localhost:5432/dreamthread'
uv run pytest -m integration
```

Unit/contract tests cover config errors, secrets, CORS, request IDs, 404/405/422/500,
readiness failure and provider status. The opt-in PostgreSQL test verifies real connectivity
and missing-revision detection. A skipped integration test is not a passing DB verification.

The GitHub Actions workflow also runs PostgreSQL integration and Docker build/HTTP checks.
It will execute only after this local commit is pushed; it has not been run by creating the file.
The local smoke script starts and terminates its own Uvicorn process, checks liveness/OpenAPI,
and confirms safe readiness failure against a deliberately unavailable database.
