"""
Algorithm catalog loader + plugin HTTP client.

Catalog source of truth is the shared ``algorithms.yaml`` mounted at
``settings.algorithms_catalog_path``. Each entry maps an algorithm
identifier to:

- its type (``watermark`` or ``fingerprint``),
- its value width in bits,
- supported media types,
- a base URL for the plugin container (``url``).

The plugin HTTP contract (matching ``plugins/watermark/<name>/app.py``):

Watermark plugin (``type: watermark``):
- ``GET  /info``
- ``POST /embed``  body ``{input_path, output_path, value?}`` -> ``{bindingValue}``
- ``POST /detect`` body ``{input_path}``                        -> ``{bindingValue|null}``
- ``GET  /health``

Fingerprint plugin (``type: fingerprint``):
- ``GET  /info``
- ``POST /compute`` body ``{input_path}`` -> ``{bindingValue}``
- ``GET  /health``

Bytes are exchanged via the shared Docker volume so we don't have to
base64-encode multi-MB audio over JSON. The caller (this service) picks
the input/output paths; the plugin reads/writes them.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
import yaml

from ingestion_api.core.config import settings
from ingestion_api.core.logging import get_logger

logger = get_logger(__name__)

AlgorithmType = Literal["watermark", "fingerprint"]


class AlgorithmNotFoundError(LookupError):
    """The catalog has no entry for the requested ``alg`` identifier."""


class PluginUnavailableError(RuntimeError):
    """The plugin container couldn't be reached, or returned a non-2xx."""


@dataclass(slots=True, frozen=True)
class AlgorithmEntry:
    alg: str
    type: AlgorithmType
    value_bits: int | None
    media_types: tuple[str, ...]
    url: str | None


def load_catalog(path: Path | None = None) -> list[AlgorithmEntry]:
    """Read + validate the YAML catalog. Returns an empty list if the file
    is missing or malformed (each malformed row is skipped with a warning)."""
    catalog_path = path or settings.algorithms_catalog_path
    if not catalog_path.is_file():
        logger.warning("Algorithm catalog not found at %s", catalog_path)
        return []

    raw = yaml.safe_load(catalog_path.read_text("utf-8")) or {}
    out: list[AlgorithmEntry] = []
    for i, entry in enumerate(raw.get("algorithms") or []):
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


class PluginClient:
    """Thin HTTP wrapper around a single plugin container."""

    def __init__(
        self,
        entry: AlgorithmEntry,
        *,
        timeout_s: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not entry.url:
            raise PluginUnavailableError(
                f"alg={entry.alg!r} has no URL configured in algorithms.yaml"
            )
        self._entry = entry
        self._timeout = timeout_s if timeout_s is not None else settings.plugin_request_timeout_s
        self._owned_client = client is None
        self._client = client or httpx.Client(timeout=self._timeout)

    @property
    def alg(self) -> str:
        return self._entry.alg

    @property
    def type(self) -> AlgorithmType:
        return self._entry.type

    def close(self) -> None:
        if self._owned_client:
            self._client.close()

    def __enter__(self) -> "PluginClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def info(self) -> dict:
        return self._get("/info")

    def health(self) -> dict:
        return self._get("/health")

    def embed(
        self,
        *,
        input_path: Path | str,
        output_path: Path | str,
        value: str | None = None,
    ) -> str:
        """Watermark plugin only. Returns the embedded binding value."""
        if self._entry.type != "watermark":
            raise PluginUnavailableError(
                f"alg={self.alg!r} is not a watermark plugin (type={self._entry.type})"
            )
        body = {
            "input_path": str(input_path),
            "output_path": str(output_path),
        }
        if value is not None:
            body["value"] = value
        data = self._post("/embed", body)
        return self._require_binding_value(data)

    def detect(self, *, input_path: Path | str) -> str | None:
        """Watermark plugin only."""
        if self._entry.type != "watermark":
            raise PluginUnavailableError(
                f"alg={self.alg!r} is not a watermark plugin (type={self._entry.type})"
            )
        data = self._post("/detect", {"input_path": str(input_path)})
        v = data.get("bindingValue")
        return v if isinstance(v, str) else None

    def compute(self, *, input_path: Path | str) -> str:
        """Fingerprint plugin only."""
        if self._entry.type != "fingerprint":
            raise PluginUnavailableError(
                f"alg={self.alg!r} is not a fingerprint plugin (type={self._entry.type})"
            )
        data = self._post("/compute", {"input_path": str(input_path)})
        return self._require_binding_value(data)

    def _get(self, path: str) -> dict:
        url = self._entry.url.rstrip("/") + path
        try:
            r = self._client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise PluginUnavailableError(f"GET {url}: {exc}") from exc
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        url = self._entry.url.rstrip("/") + path
        try:
            r = self._client.post(url, json=body)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise PluginUnavailableError(f"POST {url} {body}: {exc}") from exc
        return r.json()

    @staticmethod
    def _require_binding_value(data: dict) -> str:
        v = data.get("bindingValue")
        if not isinstance(v, str) or not v:
            raise PluginUnavailableError(
                f"Plugin returned no bindingValue: {data!r}"
            )
        return v
