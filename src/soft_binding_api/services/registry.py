"""
Algorithm registry.

`/matches/byContent` and the publisher-side ingest pipeline both need to
dispatch to a concrete algorithm by its `alg` identifier (e.g.
``"myorg.audiomark.v1"``). Real implementations register themselves here.
"""
from __future__ import annotations

from collections.abc import Callable

WatermarkEmbedder = Callable[[bytes, str], bytes]
"""(asset_bytes, value_b64) -> watermarked asset bytes"""

WatermarkDetector = Callable[[bytes], str | None]
"""asset_bytes -> base64 binding value (or None if not detected)"""

FingerprintComputer = Callable[[bytes], str]
"""asset_bytes -> base64 fingerprint value"""


_WATERMARK_EMBEDDERS: dict[str, WatermarkEmbedder] = {}
_WATERMARK_DETECTORS: dict[str, WatermarkDetector] = {}
_FINGERPRINTS: dict[str, FingerprintComputer] = {}


def register_watermark(
    alg: str,
    *,
    embed: WatermarkEmbedder | None = None,
    detect: WatermarkDetector | None = None,
) -> None:
    """Register a watermark algorithm. Either side (embed/detect) is optional."""
    if embed is not None:
        _WATERMARK_EMBEDDERS[alg] = embed
    if detect is not None:
        _WATERMARK_DETECTORS[alg] = detect


def register_fingerprint(alg: str, compute: FingerprintComputer) -> None:
    """Register a fingerprint algorithm."""
    _FINGERPRINTS[alg] = compute


def resolve_watermark_embedder(alg: str) -> WatermarkEmbedder | None:
    return _WATERMARK_EMBEDDERS.get(alg)


def resolve_watermark_detector(alg: str) -> WatermarkDetector | None:
    return _WATERMARK_DETECTORS.get(alg)


def resolve_fingerprint(alg: str) -> FingerprintComputer | None:
    return _FINGERPRINTS.get(alg)
