"""
Algorithm catalog loader + async plugin client.

The catalog of supported soft-binding algorithms lives in a single YAML
file at the repo root (``algorithms.yaml``). Both ``resolution-api``
and ``ingestion-api`` mount this file from the same source of truth, so
the response of ``GET /services/supportedAlgorithms`` always matches what
ingestion-api can route to.

Schema::

    algorithms:
      - alg: me.deepmark.audio.aware.20
        type: watermark            # watermark | fingerprint
        valueBits: 20
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

AlgorithmType = Literal["watermark", "fingerprint"]

BINDING_VALUE_HEADER = "X-Binding-Value"
OCTET_STREAM = "application/octet-stream"


class AlgorithmNotFoundError(LookupError):
    """The catalog has no entry for the requested ``alg`` identifier."""


class PluginUnavailableError(RuntimeError):
    """The plugin container couldn't be reached, or returned a non-2xx."""


@dataclass(slots=True, frozen=True)
class AlgorithmEntry:
    alg: str
    type: AlgorithmType
    value_bits: int | None = None
    media_types: tuple[str, ...] = ()
    url: str | None = None


def load_catalog(path: Path | None = None) -> list[AlgorithmEntry]:
    """Read + validate the YAML catalog. Returns an empty list if the file
    is missing — that's a degraded state the operator can fix at runtime."""
    catalog_path = path or settings.algorithms_catalog_path
    if not catalog_path.is_file():
        logger.warning("Algorithm catalog not found at %s", catalog_path)
        return []

    raw = yaml.safe_load(catalog_path.read_text("utf-8")) or {}
    algorithms = raw.get("algorithms") or []
    out: list[AlgorithmEntry] = []
    for i, entry in enumerate(algorithms):
        try:
            out.append(
                AlgorithmEntry(
                    alg=str(entry["alg"]),
                    type=entry["type"],
                    value_bits=int(entry["valueBits"]) if entry.get("valueBits") else None,
                    media_types=tuple(entry.get("mediaTypes") or ()),
                    url=entry.get("url"),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "Skipping malformed algorithm entry #%d in %s: %s", i, catalog_path, exc,
            )
    return out


def resolve(alg: str, *, catalog: list[AlgorithmEntry] | None = None) -> AlgorithmEntry:
    """Find an algorithm by id; raises ``AlgorithmNotFoundError`` if missing."""
    entries = catalog if catalog is not None else load_catalog()
    for entry in entries:
        if entry.alg == alg:
            return entry
    raise AlgorithmNotFoundError(
        f"alg={alg!r} not found in catalog {settings.algorithms_catalog_path}. "
        f"Known algorithms: {[e.alg for e in entries]}"
    )


class AsyncPluginClient:
    """Async HTTP wrapper around a single plugin container for detection."""

    def __init__(
        self,
        entry: AlgorithmEntry,
        *,
        timeout_s: float = 60.0,
    ) -> None:
        if not entry.url:
            raise PluginUnavailableError(
                f"alg={entry.alg!r} has no URL configured in algorithms.yaml"
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
