"""
Watermark detection (verifier side).

This is the operation invoked by ``/matches/byContent`` (and friends)
when the *server* is asked to extract a binding from an uploaded asset.
Clients that prefer to do extraction locally never hit this code path —
they call ``/matches/byBinding`` directly with the value they extracted.

Plug your real detector in via ``register_watermark`` from
``soft_binding_api.services.registry``::

    from soft_binding_api.services.registry import register_watermark
    from myorg.audio_watermark import detect as audio_detect

    register_watermark("myorg.audiomark.v1", detect=audio_detect)
"""
from __future__ import annotations
from typing import Optional


def detect(asset_bytes: bytes) -> Optional[str]:
    """
    Reference signature for a watermark detector.

    Args:
        asset_bytes: Raw asset bytes.

    Returns:
        The base64-encoded binding value, or ``None`` if no watermark
        was found.
    """
    raise NotImplementedError(
        "No watermark detector is registered. "
        "Register one with `register_watermark(alg, detect=...)`."
    )
