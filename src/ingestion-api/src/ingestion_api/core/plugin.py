"""
Plugin catalog loader.

Catalog source of truth is the shared ``plugins.yaml`` mounted at
``settings.plugins_catalog_path``. Each entry maps a soft-binding
algorithm identifier (``alg``) to the plugin that implements it:

- its type (``watermark`` or ``fingerprint``),
- its binding value width in bits,
- supported media types,
- a base URL for the plugin container (``url``).
"""
from __future__ import annotations

from pathlib import Path

import yaml

from ingestion_api.contracts.plugin import PluginEntry
from ingestion_api.core.config import settings
from ingestion_api.core.errors import PluginNotFoundError
from ingestion_api.core.logging import get_logger

logger = get_logger(__name__)


def load_plugin_catalog(path: Path | None = None) -> list[PluginEntry]:
    """Read + validate the YAML catalog. Returns an empty list if the file
    is missing or malformed (each malformed row is skipped with a warning).
    """
    catalog_path = path or settings.plugins_catalog_path
    if not catalog_path.is_file():
        logger.warning("Plugin catalog not found at %s", catalog_path)
        return []

    raw = yaml.safe_load(catalog_path.read_text("utf-8")) or {}
    out: list[PluginEntry] = []
    for i, entry in enumerate(raw.get("plugins") or []):
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


def resolve_plugin(
    alg: str, *, catalog: list[PluginEntry] | None = None,
) -> PluginEntry:
    """Find a plugin by alg id; raises ``PluginNotFoundError`` if missing.

    ``catalog`` should be supplied by the caller (loaded once at startup
    and threaded through DI). When omitted, a fresh on-disk load is
    performed.
    """
    entries = catalog if catalog is not None else load_plugin_catalog()
    for entry in entries:
        if entry.alg == alg:
            return entry
    raise PluginNotFoundError(
        f"alg={alg!r} not found in catalog {settings.plugins_catalog_path}. "
        f"Known algorithms: {[e.alg for e in entries]}"
    )
