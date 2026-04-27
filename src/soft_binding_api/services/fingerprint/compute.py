"""
Fingerprint computation.

Fingerprints differ from watermarks: they are **derived from** the asset
content rather than embedded into it. Both publisher (when registering a
binding) and verifier (when querying ``/matches/byContent``) compute the
same fingerprint over the same bytes; the value should be deterministic
modulo the algorithm's perceptual robustness.

Plug your real computer in via ``register_fingerprint`` from
``soft_binding_api.services.registry``::

    from soft_binding_api.services.registry import register_fingerprint
    from myorg.audio_fingerprint import compute as audio_fp

    register_fingerprint("myorg.audiofp.v1", audio_fp)
"""
from __future__ import annotations


def compute(asset_bytes: bytes) -> str:
    """
    Reference signature for a fingerprint computer.

    Args:
        asset_bytes: Raw asset bytes.

    Returns:
        Base64-encoded fingerprint value, suitable for use as a soft
        binding ``value`` in queries and ``POST /bindings``.
    """
    raise NotImplementedError(
        "No fingerprint computer is registered. "
        "Register one with `register_fingerprint(alg, compute=...)`."
    )
