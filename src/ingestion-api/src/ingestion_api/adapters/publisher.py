"""
Auto-push to the soft-binding resolution API.

After signing, ingestion-api can post the manifest store + every soft-binding to resolution-api 
so the just-ingested asset is immediately resolvable via ``GET /matches/byBinding``. 

Controlled by ``RESOLUTION_PUSH_ENABLED`` (default ``true``). 
When enabled ``RESOLUTION_API_URL`` is required (config-time validator). 
Set ``RESOLUTION_PUSH_ENABLED=false`` for standalone deployments that don't have a resolution-api downstream.

Wire shape:
1. ``POST {RESOLUTION_API_URL}/manifests`` with the raw manifest bytes as ``application/c2pa``. 
   Resolution-api derives the manifestId deterministically from the bytes 
   (same as ingestion-api's extraction), so the call is idempotent and the response payload is informational.

2. ``POST {RESOLUTION_API_URL}/bindings`` with ``{alg, bindingValue, manifestId}`` - one call per binding.

Failure mode: caller logs + records ``status=FAILED`` and an error message.
The ingest request still succeeds, returning the artifacts so a retry/sync can reconcile later. 
A single ``/bindings`` failure aborts the rest of the loop.
Partial state is signalled via ``status=FAILED`` so the operator can re-push with the persisted manifest bytes.
Re-pushes are idempotent server-side. 
See resolution-api's ``/manifests`` and ``/bindings`` endpoints for more details.
"""
from __future__ import annotations

import time

import httpx

from ingestion_api.contracts.publisher import ResolutionPushRequest
from ingestion_api.config import settings
from ingestion_api.logging import REQUEST_ID_HEADER, get_logger, get_request_id
from ingestion_api.models.enums import ResolutionPushStatus
from ingestion_api.models.responses import ResolutionPushResult

logger = get_logger(__name__)


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
        explicit_url = base_url is not None
        self._base_url = (base_url if explicit_url else settings.resolution_api_url).rstrip("/")
        if enabled is not None:
            self._enabled_flag = enabled
        elif explicit_url:
            self._enabled_flag = bool(self._base_url)
        else:
            self._enabled_flag = settings.resolution_push_enabled

        raw_timeout = (
            timeout_s if timeout_s is not None else settings.resolution_request_timeout_s
        )
        if raw_timeout <= 0:
            raise ValueError(
                f"timeout_s must be > 0 (got {raw_timeout!r})",
            )
        self._timeout = raw_timeout
        self._max_retries = max(
            0,
            max_retries if max_retries is not None else settings.resolution_max_retries,
        )
        self._retry_backoff_s = max(
            0.0,
            retry_backoff_s if retry_backoff_s is not None
            else settings.resolution_retry_backoff_s,
        )
        self._owned_client = client is None
        self._client = client or httpx.Client(timeout=self._timeout)

    @property
    def enabled(self) -> bool:
        return self._enabled_flag and bool(self._base_url)

    def close(self) -> None:
        if self._owned_client:
            self._client.close()

    def push(
        self,
        req: ResolutionPushRequest,
        *,
        request_id: str | None = None,
    ) -> ResolutionPushResult:
        """
        Push manifest + every binding to resolution-api.

        Never raises — failure modes are surfaced through
        ``ResolutionPushResult.status=FAILED``.

        ``request_id`` is forwarded as ``X-Request-ID`` on every call
        so resolution-api logs collate with ingestion-api's.
        """
        if not self.enabled:
            return ResolutionPushResult(status=ResolutionPushStatus.SKIPPED)

        if not req.bindings:
            return ResolutionPushResult(
                status=ResolutionPushStatus.FAILED,
                error="ResolutionPushRequest.bindings must be non-empty",
            )

        rid = request_id if request_id is not None else get_request_id()
        try:
            self._post_manifest(req.manifest_bytes, rid)
            for pair in req.bindings:
                self._post_binding(pair.alg, pair.binding_value, req.manifest_id, rid)
            return ResolutionPushResult(status=ResolutionPushStatus.OK)
        except httpx.HTTPError as exc:
            logger.warning("Resolution-api push failed: %s", exc)
            return ResolutionPushResult(status=ResolutionPushStatus.FAILED, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected resolution-api push failure")
            return ResolutionPushResult(status=ResolutionPushStatus.FAILED, error=str(exc))

    def _post_manifest(self, manifest_bytes: bytes, request_id: str | None) -> None:
        url = f"{self._base_url}/manifests"
        self._send_with_retry(
            "POST", url,
            content=manifest_bytes,
            headers=self._headers({"Content-Type": "application/c2pa"}, request_id),
        )

    def _post_binding(
        self, alg: str, binding_value: str, manifest_id: str, request_id: str | None,
    ) -> None:
        url = f"{self._base_url}/bindings"
        self._send_with_retry(
            "POST", url,
            json={"alg": alg, "bindingValue": binding_value, "manifestId": manifest_id},
            headers=self._headers(None, request_id),
        )

    @staticmethod
    def _headers(
        base: dict[str, str] | None, request_id: str | None,
    ) -> dict[str, str]:
        headers = dict(base or {})
        if request_id:
            headers[REQUEST_ID_HEADER] = request_id
        return headers

    def _send_with_retry(self, method: str, url: str, **httpx_kwargs) -> httpx.Response:
        """
        Issue a single HTTP request with bounded retries on transient failures. 
        Transient = network/timeout errors and 5xx responses;
        4xx surfaces immediately (won't recover by retrying). 
        Sleeps with exponential backoff between attempts.
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

        raise last_exc
