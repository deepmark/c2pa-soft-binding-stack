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
- ``POST /embed``  body = raw audio bytes (``application/octet-stream``);
  optional ``X-Binding-Value`` request header to override the value.
  Response body = watermarked bytes; ``X-Binding-Value`` response header
  carries the resulting binding value.
- ``POST /detect`` body = raw audio bytes -> JSON ``{bindingValue|null}``
- ``GET  /health``

Fingerprint plugin (``type: fingerprint``):
- ``GET  /info``
- ``POST /compute`` body = raw audio bytes -> JSON ``{bindingValue}``
- ``GET  /health``

Bytes are exchanged in raw HTTP bodies, so plugin containers can run on
hosts independent from ingestion-api (no shared filesystem required).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from ingestion_api.core.config import settings
from ingestion_api.core.logging import get_logger
from ingestion_api.models.ingestion import SoftBindingKind

logger = get_logger(__name__)

BINDING_VALUE_HEADER = "X-Binding-Value"
OCTET_STREAM = "application/octet-stream"

# Process-local cache for ``/info`` results, keyed on the plugin URL.
# No TTL: assumes plugin redeploys are paired with a rolling restart of
# ingestion-api. Per-worker (each uvicorn worker has its own dict).
_PLUGIN_INFO_CACHE: dict[str | None, dict] = {}


def reset_plugin_info_cache() -> None:
    """Test/admin helper. Production code shouldn't need this."""
    _PLUGIN_INFO_CACHE.clear()


class AlgorithmNotFoundError(LookupError):
    """The catalog has no entry for the requested ``alg`` identifier."""


class PluginUnavailableError(RuntimeError):
    """The plugin container couldn't be reached, or returned a non-2xx."""


@dataclass(slots=True, frozen=True)
class AlgorithmEntry:
    alg: str
    type: SoftBindingKind
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


@dataclass(slots=True, frozen=True)
class EmbedResult:
    """Outcome of a watermark ``/embed`` call."""
    binding_value: str
    watermarked_bytes: bytes


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
    def type(self) -> SoftBindingKind:
        return self._entry.type

    def close(self) -> None:
        if self._owned_client:
            self._client.close()

    def __enter__(self) -> "PluginClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def info(self) -> dict:
        return self._get_json("/info")

    def info_cached(self) -> dict:
        """``/info`` result, cached per-process keyed on plugin URL.

        Forensic data we capture per-ingest (``IngestionRecord.pluginVersions``)
        rarely changes — caching avoids one HTTP call per plugin per
        ingest. Cache is wiped on process restart, so deploying a new
        plugin version + a rolling restart of ingestion-api is the
        canonical way to refresh it.
        """
        cached = _PLUGIN_INFO_CACHE.get(self._entry.url)
        if cached is not None:
            return cached
        info = self.info()
        if self._entry.url:
            _PLUGIN_INFO_CACHE[self._entry.url] = info
        return info

    def health(self) -> dict:
        return self._get_json("/health")

    def embed(
        self,
        *,
        audio_bytes: bytes,
        value: str | None = None,
    ) -> EmbedResult:
        """Watermark plugin only. Returns the watermarked bytes + binding value."""
        if self._entry.type != "watermark":
            raise PluginUnavailableError(
                f"alg={self.alg!r} is not a watermark plugin (type={self._entry.type})"
            )
        headers = {"Content-Type": OCTET_STREAM}
        if value is not None:
            headers[BINDING_VALUE_HEADER] = value
        url = self._url("/embed")
        try:
            r = self._client.post(url, content=audio_bytes, headers=headers)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise PluginUnavailableError(f"POST {url}: {exc}") from exc

        binding_value = r.headers.get(BINDING_VALUE_HEADER)
        if not binding_value:
            raise PluginUnavailableError(
                f"Plugin {self.alg!r} did not return {BINDING_VALUE_HEADER} header"
            )
        return EmbedResult(binding_value=binding_value, watermarked_bytes=r.content)

    def detect(self, *, audio_bytes: bytes) -> str | None:
        """Watermark plugin only."""
        if self._entry.type != "watermark":
            raise PluginUnavailableError(
                f"alg={self.alg!r} is not a watermark plugin (type={self._entry.type})"
            )
        data = self._post_json_with_body("/detect", audio_bytes)
        v = data.get("bindingValue")
        return v if isinstance(v, str) else None

    def compute(self, *, audio_bytes: bytes) -> str:
        """Fingerprint plugin only."""
        if self._entry.type != "fingerprint":
            raise PluginUnavailableError(
                f"alg={self.alg!r} is not a fingerprint plugin (type={self._entry.type})"
            )
        data = self._post_json_with_body("/compute", audio_bytes)
        return self._require_binding_value(data)

    def _url(self, path: str) -> str:
        return self._entry.url.rstrip("/") + path

    def _get_json(self, path: str) -> dict:
        url = self._url(path)
        try:
            r = self._client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise PluginUnavailableError(f"GET {url}: {exc}") from exc
        return r.json()

    def _post_json_with_body(self, path: str, body: bytes) -> dict:
        url = self._url(path)
        try:
            r = self._client.post(
                url, content=body, headers={"Content-Type": OCTET_STREAM},
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise PluginUnavailableError(f"POST {url}: {exc}") from exc
        return r.json()

    @staticmethod
    def _require_binding_value(data: dict) -> str:
        v = data.get("bindingValue")
        if not isinstance(v, str) or not v:
            raise PluginUnavailableError(
                f"Plugin returned no bindingValue: {data!r}"
            )
        return v
