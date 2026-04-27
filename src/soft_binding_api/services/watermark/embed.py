"""
Watermark embedding (publisher side).

This is the operation that lives **outside** the resolution API: the
publisher embeds a soft binding value into an asset before shipping it.
The result of this function would later be uploaded to a distribution
service, while ``(alg, value, manifestId)`` is registered here via
``POST /bindings``.

Plug your real embedder in via ``register_watermark`` from
``soft_binding_api.services.registry``.

Example wiring (in some startup module the publisher controls)::

    from soft_binding_api.services.registry import register_watermark
    from myorg.audio_watermark import embed as audio_embed

    register_watermark("myorg.audiomark.v1", embed=audio_embed)
"""
from __future__ import annotations


def embed(asset_bytes: bytes, value_b64: str) -> bytes:
    """
    Reference signature for a watermark embedder.

    Args:
        asset_bytes: Raw asset bytes (audio/video/image, depending on alg).
        value_b64:   Base64-encoded binding value to embed.

    Returns:
        Watermarked asset bytes.
    """
    raise NotImplementedError(
        "No watermark embedder is registered. "
        "Register one with `register_watermark(alg, embed=...)`."
    )
