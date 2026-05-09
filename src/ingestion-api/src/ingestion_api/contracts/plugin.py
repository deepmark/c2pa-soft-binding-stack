"""Plugin catalog + pipeline contracts.

``PluginEntry`` is the YAML-row shape produced by ``core.plugins``
and consumed by the dispatcher / orchestrator / service. ``PluginPass``
is the per-alg output of a single plugin invocation.

Both live here because they cross layer boundaries — the catalog reader
loads them, the service holds them, the adapter calls plugins with them.
"""
from __future__ import annotations

from dataclasses import dataclass

from ingestion_api.models.enums import SoftBindingKind


@dataclass(slots=True, frozen=True)
class PluginEntry:
    alg: str
    type: SoftBindingKind
    binding_bits: int
    media_types: tuple[str, ...]
    url: str | None


@dataclass(slots=True, frozen=True)
class PluginPass:
    """Output of a single plugin pass — one per configured alg."""
    entry: PluginEntry
    binding_value: str
    output_bytes: bytes  # bytes after this pass (watermark mutates, fingerprint passes through)
