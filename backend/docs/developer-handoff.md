# DreamThread backend configuration handoff

Prepared 2026-10-29. The private configuration is `backend/.env`; the public template is
`backend/.env.example`. The implementation roadmap is `backendimplementation.md` at repository root.
No credentials are included in this document.

## What is ready

- Phases 1-12 and the database-backed Phase 14 export/deletion paths are implemented locally.
  Phase 13 remains disabled; Phase 14 external cleanup remains gated on private object storage.
- Neon hosts application data and the durable job queue. No local PostgreSQL is required.
- Existing Neon provisioning is at `0006_personal_memory`; apply migrations through
  `0009_continuity_search_index` before running the new API/worker implementation.
- Live Neon checks passed for restricted runtime/queue roles, TLS, pgvector and tenant context.
- Supabase project URL and publishable key are configured. Real user sign-in remains unverified.
- Nebius extraction model: `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`.
- Reserved reasoning setting uses the same tested model initially. It is not a separate reasoning
  implementation: phases 9-12 still need their own agent/tool/output capability verification.
- Embedding model: `Qwen/Qwen3-Embedding-8B`, measured dimension `4096`. Explicit document/query
  prefixes are configured, with a real newline before `Query:`. Preserve dotenv quoting when copying.
- Synthetic live extraction/reflection and embedding probes passed. Nano 3 uses direct-answer mode
  to avoid spending its entire output budget on reasoning.
- Local CORS allows `http://localhost:3000`; voice and demo mode remain disabled.

## Use the configuration

Receive `.env` through a private credential-sharing channel and save it at `backend/.env`.
It contains real database passwords and a Nebius billing credential. Keep it out of Git,
frontend bundles, screenshots, public repositories, and issue attachments.
The normal frontend needs only the Supabase project URL/publishable key and the backend API URL.
Never copy backend database/admin/Nebius secrets into frontend environment variables.

From the backend directory, install Python 3.12 and uv, then:

```powershell
uv sync --locked
uv run python scripts/check_database.py
uv run python scripts/check_neon.py --live
```

For a fresh database, first follow `docs/neon-setup.md` to generate/provision roles and apply
migrations. For this already provisioned database, do not regenerate credentials or reset roles.
Later migrations should run with the migration URL:

```powershell
uv run python -m alembic upgrade head
```

Start the API and worker in separate terminals, both from backend:

```powershell
uv run uvicorn app.main:create_app --factory --loop app.loop:create_loop --reload --port 8000 --no-access-log
```

```powershell
uv run python -m app.workers.main
```

Open `http://localhost:8000/docs` and `http://localhost:8000/health/ready`.
The API saves jobs; the separate worker processes analysis and embedding jobs from Neon.
No Redis/Celery credentials are needed. Stopping the worker leaves queued work unprocessed.

Optional live probes use synthetic data and incur model charges:

```powershell
uv run python scripts/probe_embeddings.py --live
uv run python scripts/probe_inference.py --live
```

Never point destructive `TEST_ADMIN_DATABASE_URL` fixtures at this application database.
Use disposable CI PostgreSQL or a separate isolated test database/Neon branch.

## Remaining phase configuration

Commented future entries at the end of .env are a proposed implementation contract. Current
Settings ignores them; uncommenting alone does not implement or enable any feature. The next
developer must add typed settings, validation, consumers, Compose mappings and tests together.

| Phase | Configuration and remaining action |
|---|---|
| 0-8 | Current .env is configured. Verify real Supabase signup/sign-in and authenticated API/worker journey. |
| 9 continuity | Reuse NEBIUS_REASONING_MODEL initially. Proposed caps: 4 rounds, 8 tools, 12 candidates, 90-second deadline and 2048 output tokens. Probe tool calling/structured outputs before release. |
| 10 constellation | Proposed graph limits: 100 nodes / 200 edges. No new external credential. |
| 11 Detective | Same reasoning model and bounded tools; implement conversation/context limits and validate evidence. No separate API key required. |
| 12 insights | SQL-backed, tenant-scoped summaries and deterministic time capsules are implemented; no scheduler or model credentials are needed. |
| 13 voice | No private bucket/provider endpoint, storage identity, media decoder, or verified faster-whisper worker is configured. Keep VOICE_ENABLED=false; configuration rejects attempts to enable it. Provision and verify the full private upload/transcription/deletion path before enabling. |
| 14 export/privacy | Exports are tenant-protected in PostgreSQL with an authenticated download route and 15-minute expiry. Journal deletion is durable and epoch-fenced for database records. Per-user requests are limited to 120/minute by default; model calls are globally capped via queue database advisory locking. External object keys can be recorded as pending cleanup tasks, but there is no adapter/worker or supported manual process to delete them. If any cleanup remains pending, the deletion job fails visibly and the account stays blocked in `deleting`; provision and verify the cleanup path before retrying. Nebius and Neon backup retention/erasure terms are operational release dependencies and have not been verified. |
| 15 acceptance | Separate synthetic test users/isolated fixtures and end-to-end validation. User access tokens are temporary runtime credentials, not static .env values. |
| 16 deployment | Select a deployment host and actual HTTPS frontend origin. Configure production secrets, HTTPS CORS, migration release step, resource limits, monitoring and restart policies. |

### Voice and external cleanup still needed

`SUPABASE_SECRET_KEY` is explicitly empty; current authentication needs only the publishable key.
No object-storage provider, bucket, endpoint, credential, or server adapter is configured; the
commented future storage settings are not consumed by the application. Voice and audio retention
remain disabled. See `privacy-data-flow.md` for the required provider configuration, media/ASR
acceptance gate, durable object-deletion tests, and unverified provider/backup-retention
dependencies. Exports do not use object storage: content stays in an RLS-protected database column
and is removed after 15 minutes.

Baseline ASR is faster-whisper with `small`, CPU, int8 as a starting configuration; this has not
been installed or performance-tested. Validate model availability/license, FFmpeg, worker capacity,
deployment capacity, processing deadlines, private bucket access, signed upload completion, and
delete-during-transcription before enabling voice. The documented media limits are 10 MiB and
180 seconds; actual upload/decode handling is not implemented.

### Production setup still needed

Keep development settings locally. On the selected host use `APP_ENV=production` and set
`CORS_ORIGINS` to the actual HTTPS frontend origin(s), without paths or wildcards. No domain
or hosting account was chosen during this setup. Configure Supabase authentication redirect
URLs for the frontend as part of deployment and verify the full sign-in flow.
Pass only runtime secrets to API/worker services. Administrator and migration URLs belong in
controlled setup/release environments. The provided Compose file already excludes them.
Use staging resources or agree explicitly before a second developer writes to this shared database.

## Handoff limits

An .env file cannot complete unimplemented phases, provision missing cloud resources or prove
production readiness. The remaining concrete external inputs are a storage/admin secret if needed,
private bucket provisioning, chosen deployment host and actual HTTPS frontend origin. Add them
when the corresponding feature is implemented. No dummy credentials were substituted for them.
