# Phase 4 API contract

All application routes use /api/v1 (configurable API_PREFIX). Health routes remain unversioned.
The running service publishes /openapi.json and /docs with Bearer authentication and error models.

## Identity

The frontend signs up/signs in/refreshes with Supabase Auth and sends its user access token:

```http
Authorization: Bearer <access_token>
```

The server calls the configured project's GET /auth/v1/user with that token and the project
publishable key. No unverified JWT decoding, metadata-based authorization, fallback user,
or token cache is used. Invalid/expired/wrong-project credentials return 401. Auth service
timeouts, redirects, non-auth errors or invalid responses return 503. No account is created
on verification failure. Missing server Auth configuration also returns 503 for a supplied token.

Identity comes only from the verified Auth user UUID. user_metadata is ignored. Anonymous
Auth users require DEMO_ENABLED=true and receive their own isolated journal and smaller budget.
This does not create anonymous accounts; that remains a frontend/Supabase Auth operation.

## Account routes

| Method/path | Input | Output |
|---|---|---|
| GET /me | Bearer token | id, display_name, timezone, version, is_anonymous, timestamps |
| PATCH /me | expected_version plus display_name and/or timezone | Updated profile |
| GET /me/preferences | Bearer token | auto_connections, require_memory_confirmation, timezone, theme, reduced_motion, version, updated_at |
| PATCH /me/preferences | expected_version plus changed preference fields | Updated preferences |

Example profile update:

```json
{"expected_version":1,"display_name":"Sarah","timezone":"Asia/Karachi"}
```

Example preference update:

```json
{"expected_version":1,"theme":"dark","reduced_motion":true,"auto_connections":true}
```

GET provisions account/preferences idempotently after verification, using UTC until a timezone
is chosen. Concurrent first requests create one account and one preferences row. An account
marked deleting/deleted returns 403 and is never reset to active by provisioning.

PATCH requires the current integer version and at least one non-null change. Unknown fields
(including user_id/id/permissions/model IDs), null updates, invalid timezones and overlong names
return 422. Booleans must be JSON true/false. Display names are <=120 characters. Themes are
dark/light/system. Timezones are IANA names such as Asia/Karachi or UTC. Timezone is synchronized
between profile and preferences; changing it increments the other resource's version too.
Stale versions return 409; reload before retrying. There is no API to select another user.

## Shared contracts

- IDs are UUID strings. Timestamps from tenant queries are UTC ISO 8601; dates are calendar dates.
- Every response from request handling has X-Request-ID. Error envelopes include the same ID.
- Errors: {"error":{"code":"...","message":"...","request_id":"...","details":{}}}.
- 401 invalid identity; 403 disabled/unavailable account; 404 absent/other-owned resources;
  409 version or idempotency conflict; 413 unsupported/oversized body; 422 validation;
  429 AI allowance exhausted; 503 dependency unavailable.
- JSON request bodies are limited to 131072 bytes by default, including chunked streams.
  Compressed request bodies are rejected. No tokens/raw request inputs appear in errors.
- Cursor helpers encode (dream_date, id), to be used with dream_date DESC, id DESC in Phase 4.
  PageParams defaults to 20 rows and caps at 100. No dream-list endpoint is implemented yet.
- Shared emotion vocabulary: calm, curious, afraid, joyful, unsure, sad, anxious, excited,
  confused, peaceful, nostalgic, awe. DreamText accepts 1-20000 characters for later capture.

## AI quota and idempotency integration for subsequent phases

Phase 3 supplies an internal reserve_ai service; it exposes no artificial AI/model endpoint.
Production currently has no model calls. Subsequent workers must have a successful admission
before model work. Phase 4 separates durable capture idempotency from AI job admission so
quota failure never discards user writing; its deferred-job contract is described below.

```python
async with db.tenant_transaction(identity.user_id) as connection:
    admission = await reserve_ai(
        connection, identity, settings,
        route="dream.create", key=idempotency_key, body=validated_body,
    )
    if admission.replayed:
        # Return the original owned resource; do not insert another job or invoke a model.
        ...
    else:
        # Insert the resource with admission.resource_id and its job before commit.
        ...
```

Convert validated bodies to JSON-compatible values before passing them (Pydantic model_dump
with mode="json"). Include all behavior-changing IDs/options in the fingerprint. Operation
names are server-owned constants: dream.create, dream.analyze, analysis.work, detective.message.
Keys are 1-128 ASCII letters/digits/underscore/hyphen. Same user/operation/key/body within
24 hours returns the same resource ID without charging again. Changed body returns 409.
Expired keys may start a new operation; the same key under another user is unrelated.

Reservations count admitted operations/units, not provider tokens. Defaults: 100 units/user/UTC
day, 5 for anonymous users (never more than the regular allowance). All API replicas must use
the same quota settings. Account-row locking plus a conditional PostgreSQL counter update
serializes concurrent reservations across pools/processes. Rollback of resource creation also
rolls back the quota/key. Once committed, failed provider work still consumes admission units;
retry the same logical operation without recharging it. Retried worker model calls remain
bounded by later worker/provider budgets, not by this counter alone.

429 includes Retry-After seconds until the next UTC day. Replays work at exhausted quota.
No model call may happen before successful admission. Counter rows contain no journal content;
future privacy cleanup must account for idempotency mappings and deliberate anti-abuse retention.

## Verification limits

Auth tests use HTTPX MockTransport at the actual Supabase adapter boundary; database tests use
real PostgreSQL/pgvector and restricted roles. No live Supabase project credentials were
provided, so a real sign-in/expired-token/cross-project smoke test is still needed on deployment.
Primary references: [Supabase user verification](https://supabase.com/docs/reference/python/auth-getuser)
and [Supabase user types](https://supabase.com/docs/guides/auth/users).

## Journal endpoints (Phase 4)

All endpoints require the same verified bearer identity. Unknown/other-owned/deleted IDs return
404. POST creation and POST analysis require a 1-128 character Idempotency-Key. No client
user_id, model ID, image URL, audio path or arbitrary analysis settings are accepted.

### Create

POST /api/v1/dreams returns 201:

```json
{
  "title": "The house in the forest",
  "dream_date": "2026-10-29",
  "text": "I returned to the house.\nThe door was open and I felt calm.",
  "reported_emotions": ["calm"],
  "lifecycle": "saved"
}
```

title defaults to Untitled dream and is 1-160 characters; text is <=20000 characters and
must contain something besides whitespace when saved. Drafts may have empty text. Text is
not trimmed, spell-corrected, normalized or rewritten. NUL/invalid Unicode are rejected because
PostgreSQL UTF-8 text cannot store them. Emotions come from the shared vocabulary and cannot
repeat. The server assigns input_mode=text; voice is a later capability.

Response includes id/title/date/version, current revision_id/revision, lifecycle, input_mode,
reported_emotions, original text, SHA256 content_hash, timestamps, analysis_state and optional
analysis_job_id. A retry with the same normalized request body/key returns the same resource
and creates no new revision, quota charge or job. A changed body under that key returns 409.
Replaying a deleted resource returns 404 and never recreates it within the key retention window.

Draft creation is unmetered. Saving inserts the source, revision, idempotency mapping, analysis
job and any admitted quota reservation in one database transaction. Provider configuration/
availability is not consulted. If automatic AI admission returns 429, a savepoint rolls back
that reservation and the source/job still commits with deferred admission; source persistence
failure rolls everything back. No partially saved dream or detached job is returned.

### Browse and source inspection

GET /dreams accepts limit (default 20, max 100), cursor, from_date/to_date (inclusive), emotion,
keyword (<=200 characters) and lifecycle=draft/saved. It returns items and next_cursor.
Default includes both lifecycles, ordered dream_date DESC then UUID DESC. Date ranges must
be valid. Keywords perform literal case-insensitive title/current-text matching; % and _
are literal characters, not user-controlled SQL wildcards. Unknown query fields return 422.
Pagination is stable for an unchanged archive; restart browsing after date edits if needed.

GET /dreams/{id} returns current original text and status. GET /dreams/{id}/revisions/{revision_id}
allows explicit owned source history while the parent dream is active, with is_current=false
for older revisions. Historical source access is not permission to cite old text as a current
observation. Once the parent is deleted, all source/history routes return 404.

### Edit and delete

PATCH /dreams/{id} accepts expected_version plus title/date/text/reported_emotions/lifecycle.
Nulls, missing changes and unsupported fields return 422. A stale version returns 409.
An exact no-op does not bump the version or create a revision/job. A real content/emotion
change creates a new immutable revision; title/date-only changes preserve revision identity
but bump the dream version and invalidate/requeue the derived analysis. Draft -> saved queues
analysis. Saved -> draft is not supported; deletion is a separate action.

Edits clear/mark stale owner-derived analyses, assistant answers, summaries and connections,
preserve connection_decisions, invalidate storyline caches, and cancel superseded jobs.
This is intentionally conservative until later phases can traverse precise dependencies.
Unchanged original dream entries and user-authored data remain intact.

DELETE /dreams/{id} returns 204, physically removes the source and immutable revisions via
tenant-safe cascades, clears stale derived text and cancels pending/running work. It increments
archive_version, data_epoch and memory_version. Unrelated original entries remain accessible.
Repeated deletion returns 404. Storage cleanup/full journal export are later phases.

### Analysis jobs and worker handoff

POST /dreams/{id}/analyze takes {"expected_version":1} and returns 202 with dream_id,
revision_id, job_id and status. A current queued/running job is reused; a completed job is
reused only if its cached analysis is still valid. Failed/cancelled work can be resubmitted
with a new request key and quota reservation, reusing its job ID with a new submission count.
Replaying the original request key reports its existing job without implicitly retrying it.

Jobs deduplicate by owner, revision, pipeline fingerprint and memory version. Pipeline fingerprint
includes prompt version, embedding version and configured extraction/reasoning/embedding IDs.
input_versions records source/dream/archive/memory versions, submission, admission state,
admission_key and admission_body; payload contains source IDs only. Captured data_epoch is on
the job row. No original text, tokens, provider credentials or reasoning trace enters the job.

- admission=reserved: allowance already committed; Phase 5 must not charge again.
- admission=deferred, stage=waiting_quota: run_after is after the next UTC budget reset.
  Before work, the worker must use the stored analysis.work admission key/body in a tenant
  transaction, verify current source/epoch, reserve allowance, map it to the existing job ID,
  and mark reserved atomically. If still over allowance, postpone rather than call a model.
- A worker must reject cancelled/superseded jobs and verify lease/source revision, memory
  version, epoch and relevant evidence before publication, as required in Phase 5.

GET /dreams/{id}/analysis returns not_requested for drafts, otherwise the processing state
without invented output. A ready result is exposed only for the current revision/pipeline/
memory and current linked evidence. Stale output is withheld consistently in list/detail/
analysis views. Until the Phase 5 worker and Phase 6 inference are implemented, real newly
submitted jobs remain queued/waiting_quota; this phase does not generate interpretations.
