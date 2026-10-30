# DreamThread privacy and data-flow notes

## Application data

The browser sends journal content to the DreamThread API over HTTPS. The API stores journal
revisions, reported emotions, user-confirmed associations, evidence, conversations, and derived
records in the private `app` schema in Neon. Application queries set a transaction-local tenant
identity and tenant tables enforce row-level security. This is server-side encryption and access
control, not end-to-end encryption: the service can read journal plaintext to provide its features.

Supabase is used for authentication only. Authentication tokens and provider credentials are not
included in journal exports or model prompts.

## Nebius inference

When a user requests analysis, the backend sends the current dream text and the task-specific
prompt to the configured Nebius model. Continuity comparisons send the selected source entries and
validated evidence needed for that comparison. Detective requests send the user's question and
the bounded, selected dream excerpts needed to answer it. The application does not send an entire
archive by default; each feature constructs its own request context.

The application does not claim end-to-end encryption, immediate provider-side erasure, or any
specific Nebius request-retention period. The current provider contract and project retention
settings have not been independently verified for this deployment. Confirm them with the provider
before production release. Deleting a journal removes application records and prevents stale jobs
from publishing, but is not proof that provider logs or backups have been erased.

Worker logs contain job identifiers, error codes, and exception types rather than dream text,
tokens, signed URLs, full prompts, or raw provider payloads.

## Exports, deletion, and voice

Exports are generated from one repeatable-read database snapshot and published only after the
account's archive version and data epoch are rechecked. Export contents are held in a tenant-RLS
protected database column and removed after 15 minutes. An authenticated download route is used;
no public or persistent URL is generated.

Journal-wide deletion increments the data epoch and changes the account to `deleting` before
cleanup begins. Reads and job publication are blocked until database cleanup completes. Any
remaining external storage keys are represented only by minimal cleanup tasks and are reported as
pending; no private object-storage deletion adapter or cleanup worker is currently configured.
The database deletion workflow must not be interpreted as proof that an external object was
deleted. If any audio asset rows with storage keys exist, external cleanup is an unresolved
release blocker: the current deployment has no provider credentials with which to process those
tasks. In this case the deletion job fails with `storage_cleanup_pending` and the account remains
blocked in `deleting`; a retry does not declare success until every persisted cleanup task is
complete. There is currently no automated or documented manual object-cleanup path. Provision and
verify the storage adapter/worker before treating such a deletion as complete. Audio routes are
disabled, but this environment has no database connection with which to verify whether legacy
asset rows exist.

Voice capture is unavailable. `VOICE_ENABLED=true` is rejected until private storage, media
verification, a supported transcription worker, and deletion/retention behavior are implemented
and tested. No audio upload or playback endpoint is exposed.

### External storage and voice deployment gates

The configured architecture currently uses Neon for application/database storage, Supabase for
authentication, and Nebius for text/model inference. No object-storage SDK/adapter, bucket,
endpoint, storage credential, media decoder, or speech-transcription provider is configured.
The presence of an unused Supabase secret-key slot or commented future environment-variable names
does not establish storage access. Do not add a provider or enable uploads based on those alone.

Before implementing object cleanup or voice, the deployment owner must select and provision an
actual private object-storage provider and supply, through the private deployment-secret channel:

- Provider/project and region, private bucket name, and the HTTPS API endpoint/region settings.
- A least-privilege server identity usable by the API/worker for owner-scoped upload-intent
  verification, object metadata reads, short-lived signed upload/download URLs, and object
  deletion. No client-side long-lived storage credential or public bucket access.
- The maximum signed-URL lifetime and lifecycle/abandoned-upload retention policy. Expiration is
  not revocation; deletion must remove the object itself.
- A server-generated object-key contract scoped under the authenticated user UUID, plus a
  verified mapping between database asset rows and provider object keys.
- A separately provisioned transcription worker/provider. The current baseline is a dedicated
  faster-whisper worker; model/license, runtime dependencies (including media decoding), compute,
  memory, processing deadline, and measured capacity must be verified before use. Do not assume
  the configured Nebius text endpoint accepts audio.

The voice acceptance gate remains the specification's 10 MiB and three-minute maximum. Verify
the actual uploaded object against its upload intent, inspect its media/container and decoded
duration rather than trusting MIME or extension, bound decoder CPU/time, and reject malformed or
oversized media. Keep raw audio private; delete it after transcript confirmation by default, expire
abandoned intents/drafts, and document any separately opted-in retention period.

Required storage/voice acceptance tests before enabling the feature:

1. A user can upload only to a short-lived, server-generated key they own; another tenant cannot
   complete, read, attach, play, or delete that asset.
2. Expired/replayed intents, wrong keys, malformed media, spoofed MIME, actual size over 10 MiB,
   decoded duration over three minutes, and decoder timeouts fail closed; unexpected objects are
   removed.
3. Transcription runs only from the verified private object through a durable, retryable job.
   Retries are idempotent; deletion or a changed data epoch during processing cannot publish a
   transcript or recreate an asset.
4. Transcript output remains a reviewable draft; only user-reviewed text becomes a journal
   revision. Raw-audio retention and abandoned-draft expiry are tested.
5. Journal deletion durably queues every owned object key, retries provider failures idempotently,
   exposes pending cleanup, and does not report deletion complete while required object deletion
   is pending. Repeating deletion does not lose keys or remove unrelated objects.
6. Export expiry/deletion does not delete any object still required by a valid export. Current
   exports are database-backed and do not use object storage; preserve that property unless a
   future storage design explicitly accounts for export lifetime.

Provider-side inference retention and managed database backup retention/erasure are operational
dependencies, not properties established by application deletion. Before production, obtain and
record the actual Nebius request-retention and deletion terms and Neon backup/PITR retention and
restore-erasure behavior for the selected plans. Document the responsible operator, retention
periods, deletion/expiry process, and what cannot be selectively erased from backups. Do not claim
immediate provider-log or backup erasure unless the provider's policy and operational procedure
support that claim.
