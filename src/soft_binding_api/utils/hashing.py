"""
Hashing helpers used by the dummy watermark embedder and the binding-value
generator. Kept in a tiny module so the algorithm is easy to swap out later
(e.g. when a real Vigil-128 embedder lands).
"""
from __future__ import annotations

import base64
import hashlib


def sha256_hex(data: bytes) -> str:
    """Hex SHA-256 digest. Used as a content fingerprint for filenames/IDs."""
    return hashlib.sha256(data).hexdigest()


def sha256_truncated_b64(data: bytes, n_bits: int = 128) -> str:
    """
    Deterministically derive an ``n_bits``-bit value from ``data`` and encode
    it as standard base64.

    For ``n_bits=128`` (the Vigil-128 default) this is the first 16 bytes of
    SHA-256(data), base64-encoded — exactly what the dummy watermark emits.
    """
    if n_bits <= 0 or n_bits % 8 != 0:
        raise ValueError("n_bits must be a positive multiple of 8")
    n_bytes = n_bits // 8
    digest = hashlib.sha256(data).digest()[:n_bytes]
    return base64.b64encode(digest).decode("ascii")
