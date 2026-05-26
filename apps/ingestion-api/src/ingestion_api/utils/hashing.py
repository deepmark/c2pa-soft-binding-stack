"""
Hashing helpers used by the ingestion pipeline.
"""
from __future__ import annotations

import hashlib


def sha256_hex(data: bytes) -> str:
    """Hex SHA-256 digest. Used as a content fingerprint for filenames/IDs."""
    return hashlib.sha256(data).hexdigest()
