"""Runtime Core metadata. Repositories use tenant_transaction for all access."""

from app.db.schema_v1 import build_metadata
from app.db.schema_v2 import extend_metadata
from app.db.schema_v3 import extend_metadata as extend_worker_metadata
from app.db.schema_v4 import extend_metadata as extend_embedding_metadata
from app.db.schema_v5 import extend_metadata as extend_memory_metadata
from app.db.schema_v6 import extend_metadata as extend_privacy_metadata
from app.db.schema_v7 import extend_metadata as extend_rate_limit_metadata
from app.db.schema_v8 import extend_metadata as extend_retrieval_metadata

# VECTOR without a compile-time size is safe for binds; migration pins the database to D.
metadata = build_metadata(None)
extend_metadata(metadata)
extend_worker_metadata(metadata)
extend_embedding_metadata(metadata)
extend_memory_metadata(metadata)
extend_privacy_metadata(metadata)
extend_rate_limit_metadata(metadata)
extend_retrieval_metadata(metadata)
tables = metadata.tables
