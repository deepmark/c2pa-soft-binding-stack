"""
Watermark detection (verifier side).

This is the operation invoked by ``/matches/byContent`` (and friends)
when the *server* is asked to extract a binding from an uploaded asset.
Clients that prefer to do extraction locally never hit this code path —
they call ``/matches/byBinding`` directly with the value they extracted.

Current implementation is the **dummy** counterpart to ``embed``: since
the embedder is a passthrough, the detector simply re-runs the binding
derivation on the input bytes. This guarantees a clean round-trip
(``detect(embed(x, v)) == compute_binding_value(x)``) for tests and for
``/matches/byContent`` once that endpoint is wired up.
"""
from __future__ import annotations

from soft_binding_api.services.watermark.embed import compute_binding_value


def detect(asset_bytes: bytes) -> str | None:
    """
    Reference signature for a watermark detector.

    Args:
        asset_bytes: Raw asset bytes.

    Returns:
        The base64-encoded binding value, or ``None`` if no watermark
        was found. The dummy implementation always returns the value
        derived from the input bytes.
    """
    return compute_binding_value(asset_bytes)
