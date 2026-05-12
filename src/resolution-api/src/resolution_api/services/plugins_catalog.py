"""
Plugin catalog loader + async plugin client.

The catalog of supported soft-binding plugins lives in a single YAML
file at the repo root (``plugins.yaml``). Both ``resolution-api``
and ``ingestion-api`` mount this file from the same source of truth, so
the response of ``GET /services/supportedAlgorithms`` always matches what
ingestion-api can route to.

Schema::

    plugins:
      - alg: me.deepmark.audio.aware.20
        type: watermark            # watermark | fingerprint
        bindingBits: 20
        mediaTypes: ["audio/wav", "audio/mpeg"]
        url: http://watermark-aware-20:9004

Hot-reloads on every read. Cheap (small file, parsed lazily) and avoids
having to bounce the service when a new plugin is added to the catalog.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
import yaml

from resolution_api.core.config import settings
from resolution_api.core.logging import get_logger

logger = get_logger(__name__)

PluginType = Literal["watermark", "fingerprint"]

BINDING_VALUE_HEADER = "X-Binding-Value"
OCTET_STREAM = "application/octet-stream"


class PluginNotFoundError(LookupError):
    """The catalog has no entry for the requested ``alg`` identifier."""


class PluginUnavailableError(RuntimeError):
    """The plugin container couldn't be reached, or returned a non-2xx."""


@dataclass(slots=True, frozen=True)
class PluginEntry:
    alg: str
    type: PluginType
    binding_bits: int = 0
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


def resolve(alg: str, *, catalog: list[PluginEntry] | None = None) -> PluginEntry:
    """Find a plugin by alg id; raises ``PluginNotFoundError`` if missing."""
    entries = catalog if catalog is not None else load_plugin_catalog()
    for entry in entries:
        if entry.alg == alg:
            return entry
    raise PluginNotFoundError(
        f"alg={alg!r} not found in catalog {settings.plugins_catalog_path}. "
        f"Known plugins: {[e.alg for e in entries]}"
    )


class AsyncPluginClient:
    """Async HTTP wrapper around a single plugin container for detection."""

    def __init__(
        self,
        entry: PluginEntry,
        *,
        timeout_s: float = 60.0,
    ) -> None:
        if not entry.url:
            raise PluginUnavailableError(
                f"alg={entry.alg!r} has no URL configured in plugins.yaml"
            )
        self._entry = entry
        self._timeout = timeout_s

    @property
    def alg(self) -> str:
        return self._entry.alg

    async def detect(self, *, audio_bytes: bytes) -> str | None:
        url = self._entry.url.rstrip("/") + "/detect"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                r = await client.post(
                    url,
                    content=audio_bytes,
                    headers={"Content-Type": OCTET_STREAM},
                )
                r.raise_for_status()
            except httpx.HTTPError as exc:
                raise PluginUnavailableError(f"POST {url}: {exc}") from exc
        data = r.json()
        v = data.get("bindingValue")
        return v if isinstance(v, str) else None
