"""
Soft binding algorithm implementations.

The C2PA Decoupled Soft Binding spec only standardises the lookup API;
embedding watermarks into assets and extracting them out of assets is
delegated to algorithm-specific code that lives here.

Submodules:
- ``watermark.embed``    — write a binding value into an asset
- ``watermark.detect``   — recover a binding value from an asset
- ``fingerprint.compute``— derive a content fingerprint from an asset

See https://github.com/c2pa-org/softbinding-algorithm-list for registered
algorithm identifiers.
"""

from soft_binding_api.services.registry import (
    FingerprintComputer,
    WatermarkDetector,
    WatermarkEmbedder,
    register_fingerprint,
    register_watermark,
    resolve_fingerprint,
    resolve_watermark_detector,
    resolve_watermark_embedder,
)

__all__ = [
    "WatermarkEmbedder",
    "WatermarkDetector",
    "FingerprintComputer",
    "register_watermark",
    "register_fingerprint",
    "resolve_watermark_embedder",
    "resolve_watermark_detector",
    "resolve_fingerprint",
]
