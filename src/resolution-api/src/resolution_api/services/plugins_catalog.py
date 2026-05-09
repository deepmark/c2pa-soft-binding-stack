"""
Plugin catalog loader.

The catalog of supported soft-binding plugins lives in a single YAML
file at the repo root (``plugins.yaml``). Both ``resolution-api``
and ``ingestion-api`` mount this file from the same source of truth, so
the response of ``GET /services/supportedAlgorithms`` always matches what
ingestion-api can route to.

Schema::

    plugins:
      - alg: me.deepmark.audio.vigil.128
        type: watermark            # watermark | fingerprint
        bindingBits: 128
        mediaTypes: ["audio/wav", "audio/mpeg"]
        url: http://watermark-vigil-128:8000   # ingestion-api uses this; resolution
                                                # api ignores it but keeps it for
                                                # /ready dumps if exposed.

Hot-reloads on every read. Cheap (small file, parsed lazily) and avoids
having to bounce the service when a new plugin is added to the catalog.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from resolution_api.core.config import settings
from resolution_api.core.logging import get_logger

logger = get_logger(__name__)

PluginType = Literal["watermark", "fingerprint"]


@dataclass(slots=True, frozen=True)
class PluginEntry:
    alg: str
    type: PluginType
    binding_bits: int
    media_types: tuple[str, ...] = ()
    url: str | None = None


def load_plugin_catalog(path: Path | None = None) -> list[PluginEntry]:
    """Read + validate the YAML catalog. Returns an empty list if the file
    is missing — that's a degraded state the operator can fix at runtime.

    ``bindingBits`` is required and must be positive. Non-byte-aligned
    widths are allowed (ingestion-api masks the high bits when minting).
    Entries with missing or non-positive ``bindingBits`` are skipped so
    this service never advertises an alg that ingestion-api would reject.
    """
    catalog_path = path or settings.plugins_catalog_path
    if not catalog_path.is_file():
        logger.warning("Plugin catalog not found at %s", catalog_path)
        return []

    raw = yaml.safe_load(catalog_path.read_text("utf-8")) or {}
    plugins = raw.get("plugins") or []
    out: list[PluginEntry] = []
    for i, entry in enumerate(plugins):
        try:
            binding_bits = int(entry["bindingBits"])
            if binding_bits <= 0:
                raise ValueError(
                    f"bindingBits must be positive, got {binding_bits}",
                )
            out.append(
                PluginEntry(
                    alg=str(entry["alg"]),
                    type=entry["type"],
                    binding_bits=binding_bits,
                    media_types=tuple(entry.get("mediaTypes") or ()),
                    url=entry.get("url"),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "Skipping malformed plugin entry #%d in %s: %s", i, catalog_path, exc,
            )
    return out
