"""
Watermark embedding (publisher side).

This is the operation that lives outside the resolution API: the publisher
embeds a soft binding value into an asset before shipping it. The result
of this function would later be uploaded to a distribution service, while
``(alg, value, manifestId)`` is registered with the resolution API via
``POST /bindings``.

Current implementation is a deterministic **dummy** for
``me.deepmark.audio.vigil.128``:

- ``compute_binding_value`` derives a 128-bit value from the source bytes
  (SHA-256 truncated to 16 bytes, base64-encoded).
- ``embed`` is a passthrough — bytes go in, the same bytes come out. Real
  Vigil-128 DSP code would modulate the audio signal here.

The signature already matches what a real embedder will need
(``(asset_bytes, value_b64) -> bytes``), so swapping in production code
is a one-line change in ``__init__.py``.
"""
from __future__ import annotations

from soft_binding_api.utils.hashing import sha256_truncated_b64

BINDING_ALG = "me.deepmark.audio.vigil.128"
"""Canonical soft-binding algorithm identifier for audio."""

BINDING_BITS = 128
"""Width of the extracted/embedded value in bits."""


def compute_binding_value(asset_bytes: bytes) -> str:
    """
    Deterministic dummy binding value.

    Returns the base64 encoding of SHA-256(asset_bytes)[:16]. Same input ->
    same value, every time, on every host. That makes the ingest pipeline
    reproducible and makes test fixtures easy to assert against.
    """
    return sha256_truncated_b64(asset_bytes, n_bits=BINDING_BITS)


def embed(asset_bytes: bytes, value_b64: str) -> bytes:  # noqa: ARG001
    """
    Reference signature for a watermark embedder.

    Args:
        asset_bytes: Raw asset bytes (audio/video/image, depending on alg).
        value_b64: Base64-encoded binding value to embed.

    Returns:
        Watermarked asset bytes. The dummy implementation returns the
        input unchanged; replace with real DSP for production.
    """
    return asset_bytes
