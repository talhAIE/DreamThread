"""Runtime Core metadata. Repositories use tenant_transaction for all access."""

from app.db.schema_v1 import build_metadata
from app.db.schema_v2 import extend_metadata

# VECTOR without a compile-time size is safe for binds; migration pins the database to D.
metadata = build_metadata(None)
extend_metadata(metadata)
tables = metadata.tables
