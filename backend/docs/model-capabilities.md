# Phase 6 provider capability status

Development uses the OpenAI-compatible SDK against Nebius Token Factory's configured base URL.
Reference: [API introduction](https://docs.tokenfactory.nebius.com/api-reference/introduction)
and [structured JSON output](https://docs.tokenfactory.nebius.com/ai-models-inference/json).

Live account verification is pending: the user requested development first and will configure
keys later. No real model ID, model availability, latency, token usage or structured-output
capability is claimed verified. Test-only model IDs are fixtures, not deployment recommendations.

Configure the exact available NVIDIA Nemotron catalog ID as NEBIUS_EXTRACTION_MODEL, with
NEBIUS_API_KEY in backend/.env. Both Phase 6 stages use it. Keep json_object initially; switch
to json_schema only after a separate successful probe of that mode. Set the context budget and
output cap within the chosen model's actual limits. Unsupported modes fail visibly.

Run `uv run python scripts/probe_inference.py --live` from backend after configuration. It uses
a synthetic dream, confirms the configured ID is listed, and runs the production validators.
Retain its metadata-only output for model/mode/usage/latency evidence. Then verify an actual
saved dream through worker polling and the analysis endpoint. This script does not itself
verify database publication, hosted authentication or Docker deployment.

Local coverage uses the real SDK with HTTPX MockTransport and real PostgreSQL/pgvector for
worker publication. It verifies request shape, errors, bounded repair, citation checks,
checkpoint reuse and atomic rollback. It cannot establish real provider output quality.
