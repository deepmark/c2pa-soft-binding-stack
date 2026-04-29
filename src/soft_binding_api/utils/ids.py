"""ID generators for the ingest pipeline."""
from __future__ import annotations

import uuid


def new_ingestion_id() -> str:
    """Internal ingestion identifier (used as the storage key)."""
    return f"ing_{uuid.uuid4().hex}"


def new_manifest_urn() -> str:
    """C2PA manifest URN. Used as a placeholder when the SDK doesn't surface
    the actual manifest label until the asset is read back."""
    return f"urn:c2pa:{uuid.uuid4()}"
