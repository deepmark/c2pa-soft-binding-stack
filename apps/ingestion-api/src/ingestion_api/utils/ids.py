"""ID generators for the ingest pipeline."""
from __future__ import annotations

import uuid


def new_ingestion_id() -> str:
    """Internal ingestion identifier (used as the storage key)."""
    return f"ing_{uuid.uuid4().hex}"
