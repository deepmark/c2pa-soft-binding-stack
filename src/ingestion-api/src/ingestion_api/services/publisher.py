"""
Auto-push to the soft-binding resolution API.

After signing, ingestion-api can post the manifest store + every
soft-binding to resolution-api so the just-ingested asset is
immediately resolvable via ``GET /matches/byBinding``. Controlled by
``RESOLUTION_PUSH_ENABLED`` (default ``true``); when enabled,
``RESOLUTION_API_URL`` is required (config-time validator). Set
``RESOLUTION_PUSH_ENABLED=false`` for genuine standalone deployments
that don't have a resolution-api downstream.

Wire shape:
1. ``POST {RESOLUTION_API_URL}/manifests`` with the raw manifest bytes
   as ``application/c2pa``. Resolution-api derives the manifestId
   deterministically from the bytes (same as ingestion-api's
   extraction), so the call is idempotent and the response payload is
   informational — we use the locally-extracted ID for the binding
   posts.
2. ``POST {RESOLUTION_API_URL}/bindings`` with
   ``{alg, bindingValue, manifestId}`` — one call per binding.
   Resolution-api's ``/bindings`` is intentionally singular (one
   binding per request); we loop over the list here.

Failure mode: caller logs + records ``status=FAILED`` and an error
message; the ingest request still succeeds, returning the artifacts so
a retry/sync can reconcile later. A single ``/bindings`` failure
aborts the rest of the loop — partial state is signalled via
``status=FAILED`` so the operator can re-push with the persisted
manifest bytes (re-pushes are idempotent server-side).
"""
from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from ingestion_api.core.config import settings
from ingestion_api.core.logging import get_logger
from ingestion_api.models.ingestion import ResolutionPushResult, ResolutionPushStatus

logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class BindingPair:
    """One (alg, bindingValue) tuple to register against the manifest."""
    alg: str
    binding_value: str


@dataclass(slots=True)
class ResolutionPushRequest:
    manifest_bytes: bytes
    manifest_id: str
    bindings: Sequence[BindingPair]


class ResolutionPushClient:
    """HTTP client for the resolution-api auto-push."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        enabled: bool | None = None,
        timeout_s: float | None = None,
        max_retries: int | None = None,
        retry_backoff_s: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        # When ``base_url`` is passed explicitly we treat that as "the
        # caller knows what they want" and enable iff the URL is truthy
        # — settings.RESOLUTION_PUSH_ENABLED is only the operator-level
        # kill switch for the bare-call path used by lifespan startup.
        explicit_url = base_url is not None
        self._base_url = (base_url if explicit_url else settings.resolution_api_url).rstrip("/")
        if enabled is not None:
            self._enabled_flag = enabled
        elif explicit_url:
            self._enabled_flag = bool(self._base_url)
        else:
            self._enabled_flag = settings.resolution_push_enabled
        self._timeout = (
            timeout_s if timeout_s is not None else settings.resolution_request_timeout_s
        )
        self._max_retries = (
            max_retries if max_retries is not None else settings.resolution_max_retries
        )
        self._retry_backoff_s = (
            retry_backoff_s if retry_backoff_s is not None else settings.resolution_retry_backoff_s
        )
        self._owned_client = client is None
        self._client = client or httpx.Client(timeout=self._timeout)

    @property
    def enabled(self) -> bool:
        return self._enabled_flag and bool(self._base_url)

    def close(self) -> None:
        if self._owned_client:
            self._client.close()

    def push(self, req: ResolutionPushRequest) -> ResolutionPushResult:
        """
        Push manifest + every binding to resolution-api.

        Never raises — failure modes are surfaced through
        ``ResolutionPushResult.status=FAILED``. The caller-supplied
        ``req.manifest_id`` is used directly for the binding posts;
        resolution-api derives the same ID from ``req.manifest_bytes``,
        so the manifest POST is idempotent.
        """
        if not self.enabled:
            return ResolutionPushResult(status=ResolutionPushStatus.SKIPPED)

        if not req.bindings:
            return ResolutionPushResult(
                status=ResolutionPushStatus.FAILED,
                error="ResolutionPushRequest.bindings must be non-empty",
            )

        try:
            self._post_manifest(req.manifest_bytes)
            for pair in req.bindings:
                self._post_binding(pair.alg, pair.binding_value, req.manifest_id)
            return ResolutionPushResult(status=ResolutionPushStatus.OK)
        except httpx.HTTPError as exc:
            logger.warning("Resolution-api push failed: %s", exc)
            return ResolutionPushResult(status=ResolutionPushStatus.FAILED, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected resolution-api push failure")
            return ResolutionPushResult(status=ResolutionPushStatus.FAILED, error=str(exc))

    def _post_manifest(self, manifest_bytes: bytes) -> None:
        url = f"{self._base_url}/manifests"
        self._send_with_retry(
            "POST", url,
            content=manifest_bytes,
            headers={"Content-Type": "application/c2pa"},
        )

    def _post_binding(self, alg: str, binding_value: str, manifest_id: str) -> None:
        url = f"{self._base_url}/bindings"
        self._send_with_retry(
            "POST", url,
            json={"alg": alg, "bindingValue": binding_value, "manifestId": manifest_id},
        )

    def _send_with_retry(self, method: str, url: str, **httpx_kwargs) -> httpx.Response:
        """
        Issue a single HTTP request with bounded retries on transient
        failures. Transient = network/timeout errors and 5xx responses;
        4xx surfaces immediately (deterministic, won't recover by
        retrying). Sleeps with exponential backoff between attempts.
        """
        last_exc: Exception | None = None
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            try:
                r = self._client.request(method, url, **httpx_kwargs)
                r.raise_for_status()
                return r
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    raise  # 4xx is permanent — no point retrying
                last_exc = exc

            remaining = attempts - attempt - 1
            if remaining > 0:
                backoff = self._retry_backoff_s * (2 ** attempt)
                logger.warning(
                    "Resolution-api %s %s transient failure (attempt %d/%d): %s — retrying in %.2fs",
                    method, url, attempt + 1, attempts, last_exc, backoff,
                )
                time.sleep(backoff)

        assert last_exc is not None  # loop above either returns or sets last_exc
        raise last_exc
