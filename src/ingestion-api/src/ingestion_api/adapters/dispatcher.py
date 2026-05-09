"""
Plugin HTTP dispatcher.

Thin wrapper around a single plugin container's HTTP contract
(matching ``plugins/watermark/<name>/app.py``):

Watermark plugin (``type: watermark``):
- ``GET  /info``
- ``POST /embed``  body = raw media bytes (``application/octet-stream``);
  required ``X-Binding-Value`` request header carries the API-generated
  value the plugin must embed; ``X-Media-Type`` carries the source MIME
  (e.g. ``audio/wav``). 
  Response body = watermarked bytes; ``X-Binding-Value`` response header 
  echoes the embedded value (must equal the request header).
- ``POST /detect`` body = raw media bytes -> JSON ``{bindingValue|null}``
- ``GET  /health``

Fingerprint plugin (``type: fingerprint``):
- ``GET  /info``
- ``POST /compute`` body = raw media bytes -> JSON ``{bindingValue}``
- ``GET  /health``

Header contract for binary endpoints (``/embed``, ``/detect``, ``/compute``):
- ``Content-Type: application/octet-stream`` — describes the *wire encoding*
  (opaque bytes). Tells frameworks/proxies/CDNs not to sniff or transcode.
- ``X-Media-Type: <mime>`` — describes the *semantic format* of those bytes
  (e.g. ``audio/wav``). The plugin uses this to pick its decoder. We split
  the two because a real plugin needs the MIME but we never want
  intermediate hops to interpret media types and "helpfully" re-encode.

Binding values are minted by ingestion-api (not by plugins) using
``secrets.token_bytes`` sized to the plugin's declared ``bindingBits``.
Bit widths that aren't byte-aligned are supported (the high bits of the
first byte are zeroed). 
The plugin must echo the value it embedded so we can detect a misbehaving 
plugins that ignore the header.

Bytes are exchanged in raw HTTP bodies, so plugin containers can run on
hosts independent from ingestion-api (no shared filesystem required).
"""
from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass

import httpx

from ingestion_api.contracts.plugin import PluginEntry
from ingestion_api.core.config import settings
from ingestion_api.core.errors import PluginUnavailableError
from ingestion_api.core.logging import REQUEST_ID_HEADER, get_logger, get_request_id
from ingestion_api.models.enums import SoftBindingKind

logger = get_logger(__name__)

BINDING_VALUE_HEADER = "X-Binding-Value"
MEDIA_TYPE_HEADER = "X-Media-Type"
OCTET_STREAM = "application/octet-stream"

# Process-local cache for ``/info`` results, keyed on the plugin URL.
# No TTL: assumes plugin redeploys are paired with a rolling restart of
# ingestion-api. Per-worker (each uvicorn worker has its own dict).
_PLUGIN_INFO_CACHE: dict[str | None, dict] = {}


def reset_plugin_info_cache() -> None:
    """Test/admin helper. Production code shouldn't need this."""
    _PLUGIN_INFO_CACHE.clear()


@dataclass(slots=True, frozen=True)
class EmbedResult:
    """Outcome of a watermark ``/embed`` call."""
    binding_value: str
    watermarked_bytes: bytes


class PluginDispatcher:
    """Thin HTTP wrapper around a single plugin container."""

    def __init__(
        self,
        entry: PluginEntry,
        *,
        timeout_s: float | None = None,
        client: httpx.Client | None = None,
        request_id: str | None = None,
    ) -> None:
        if not entry.url:
            raise PluginUnavailableError(
                f"alg={entry.alg!r} has no URL configured in plugins.yaml"
            )
        self._entry = entry
        self._timeout = timeout_s if timeout_s is not None else settings.plugin_request_timeout_s
        self._owned_client = client is None
        self._client = client or httpx.Client(timeout=self._timeout)
        # Capture explicitly when constructed in async context, fall
        # back to the contextvar otherwise. Safe across thread-pool
        # executor calls because the value is held on the instance.
        self._request_id = request_id if request_id is not None else get_request_id()

    @property
    def alg(self) -> str:
        return self._entry.alg

    @property
    def type(self) -> SoftBindingKind:
        return self._entry.type

    def close(self) -> None:
        if self._owned_client:
            self._client.close()

    def __enter__(self) -> PluginDispatcher:
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
        media_bytes: bytes,
        mime_type: str,
    ) -> EmbedResult:
        """Watermark plugin only. Returns the watermarked bytes + binding value.

        The binding value is minted here (not by the plugin). The plugin
        embeds whatever value we hand it via ``X-Binding-Value`` and must
        echo the same value back in its response header — we verify the
        echo to catch plugins that silently ignore the request header.
        """
        if self._entry.type != "watermark":
            raise PluginUnavailableError(
                f"alg={self.alg!r} is not a watermark plugin (type={self._entry.type})"
            )

        binding_value = _new_binding_value(self._entry.binding_bits)

        # Two-header pattern: Content-Type pins the wire encoding to opaque
        # bytes (so no proxy/CDN tries to transcode the media); X-Media-Type
        # tells the plugin what those bytes actually mean.
        headers = self._headers({
            "Content-Type": OCTET_STREAM,
            MEDIA_TYPE_HEADER: mime_type,
            BINDING_VALUE_HEADER: binding_value,
        })
        url = self._url("/embed")
        try:
            r = self._client.post(url, content=media_bytes, headers=headers)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise PluginUnavailableError(f"POST {url}: {exc}") from exc

        echoed = r.headers.get(BINDING_VALUE_HEADER)
        if not echoed:
            raise PluginUnavailableError(
                f"Plugin {self.alg!r} did not return {BINDING_VALUE_HEADER} header"
            )
        if echoed != binding_value:
            raise PluginUnavailableError(
                f"Plugin {self.alg!r} echoed a different binding value than "
                f"requested (sent={binding_value!r}, got={echoed!r})"
            )
        return EmbedResult(binding_value=binding_value, watermarked_bytes=r.content)

    def detect(self, *, media_bytes: bytes, mime_type: str) -> str | None:
        """Watermark plugin only."""
        if self._entry.type != "watermark":
            raise PluginUnavailableError(
                f"alg={self.alg!r} is not a watermark plugin (type={self._entry.type})"
            )
        data = self._post_json_with_body("/detect", media_bytes, mime_type=mime_type)
        v = data.get("bindingValue")
        return v if isinstance(v, str) else None

    def compute(self, *, media_bytes: bytes, mime_type: str) -> str:
        """Fingerprint plugin only."""
        if self._entry.type != "fingerprint":
            raise PluginUnavailableError(
                f"alg={self.alg!r} is not a fingerprint plugin (type={self._entry.type})"
            )
        data = self._post_json_with_body("/compute", media_bytes, mime_type=mime_type)
        return self._require_binding_value(data)

    def _url(self, path: str) -> str:
        return self._entry.url.rstrip("/") + path

    def _headers(self, base: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(base or {})
        if self._request_id:
            headers[REQUEST_ID_HEADER] = self._request_id
        return headers

    def _get_json(self, path: str) -> dict:
        url = self._url(path)
        try:
            r = self._client.get(url, headers=self._headers())
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise PluginUnavailableError(f"GET {url}: {exc}") from exc
        return r.json()

    def _post_json_with_body(
        self, path: str, body: bytes, *, mime_type: str | None = None,
    ) -> dict:
        url = self._url(path)
        base_headers = {"Content-Type": OCTET_STREAM}
        if mime_type:
            base_headers[MEDIA_TYPE_HEADER] = mime_type
        try:
            r = self._client.post(
                url,
                content=body,
                headers=self._headers(base_headers),
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


def _new_binding_value(binding_bits: int) -> str:
    """Cryptographically random binding value, ``binding_bits`` wide.

    URL-safe base64, no padding. We generate ``ceil(bits/8)`` random bytes
    and zero the unused high bits of the first byte, so the value contains
    exactly ``binding_bits`` bits of entropy.

    Wire convention: bytes are big-endian, MSB-first within each byte. For
    widths not divisible by 8, the unused high bits of byte 0 are zero.
    Any cross-language verifier (or future detector) must follow the same
    packing — otherwise a value generated here won't compare equal to one
    round-tripped through audio + a foreign decoder.
    """
    n_bytes = (binding_bits + 7) // 8
    raw = bytearray(secrets.token_bytes(n_bytes))
    extra = (8 * n_bytes) - binding_bits
    if extra:
        raw[0] &= 0xFF >> extra
    return base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode("ascii")
