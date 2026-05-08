"""Plugin pipeline output contracts."""
from __future__ import annotations

from dataclasses import dataclass

from ingestion_api.catalog.algorithms import AlgorithmEntry


@dataclass(slots=True, frozen=True)
class PluginPass:
    """Output of a single plugin pass — one per configured alg."""
    entry: AlgorithmEntry
    binding_value: str
    output_bytes: bytes  # bytes after this pass (watermark mutates, fingerprint passes through)
