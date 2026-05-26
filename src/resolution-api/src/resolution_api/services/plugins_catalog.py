"""
Plugin catalog loader + async plugin client.

The catalog of supported soft-binding plugins is stored in the
``supported_algorithms`` MongoDB collection. Both ``resolution-api``
and ``ingestion-api`` read from this collection so the response of
``GET /services/supportedAlgorithms`` always matches what can be routed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

from resolution_api.core.database import get_supported_algorithms_collection
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


async def resolve(alg: str) -> PluginEntry:
    """Find a plugin by alg id; raises ``PluginNotFoundError`` if missing."""
    col = get_supported_algorithms_collection()
    doc = await col.find_one({"alg": alg})
    if not doc:
        raise PluginNotFoundError(f"alg={alg!r} not found in supported_algorithms collection")
    return PluginEntry(
        alg=doc["alg"],
        type=doc["type"],
        binding_bits=doc.get("bindingBits", 0),
        media_types=tuple(doc.get("mediaTypes") or ()),
        url=doc.get("url"),
    )


async def load_all_plugins() -> list[PluginEntry]:
    """Load all registered plugins from the database."""
    col = get_supported_algorithms_collection()
    docs = await col.find({}).to_list(length=100)
    return [
        PluginEntry(
            alg=doc["alg"],
            type=doc["type"],
            binding_bits=doc.get("bindingBits", 0),
            media_types=tuple(doc.get("mediaTypes") or ()),
            url=doc.get("url"),
        )
        for doc in docs
    ]


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
                f"alg={entry.alg!r} has no URL configured"
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
