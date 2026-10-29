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

## Phase 7 embedding inputs

The embedding model ID remains unset until verified against the user's account. Nebius's
[official cookbook](https://github.com/nebius/token-factory-cookbook/blob/main/integrations/pixeltable/README.md)
demonstrates its OpenAI-compatible embeddings integration. The configured model's own card
defines input prefixes/instructions; e.g. the [Qwen3 embedding card](https://huggingface.co/Qwen/Qwen3-Embedding-8B)
shows task instructions for retrieval queries. This is a format reference, not a guarantee of
current account availability or an automatic model selection.

Set both EMBEDDING_DOCUMENT_PREFIX and EMBEDDING_QUERY_PREFIX explicitly (including empty
strings if required), then EMBEDDING_PREFIXES_VERIFIED=true after checking the model card.
Run `uv run python scripts/probe_embeddings.py --live` only when ready. The script confirms
catalog membership, measures dimension using synthetic document/query inputs, rejects a
configured dimension mismatch, and exercises the production adapter. Set EMBEDDING_DIMENSIONS
to the measured value before indexing. Verify provider input/batch limits too. No live
embedding probe has been run; the user's development-first instruction still applies.

Changing these inputs/model/dimension requires a new EMBEDDING_VERSION. Each tenant space
stores the full input contract, model, dimension and fingerprint. The local benchmark result
in retrieval-benchmark.json uses synthetic transport vectors and does not measure the real model.
