# Phase 3 API contract

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
Production currently has no model calls. Phase 4/5/11 handlers must call it before queuing
expensive work, within the SAME tenant transaction that inserts the resource and durable job.

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
names are server-owned allowlisted constants: dream.create, dream.analyze, detective.message.
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
