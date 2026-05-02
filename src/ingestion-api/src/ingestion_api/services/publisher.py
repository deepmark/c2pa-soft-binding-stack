"""
Auto-push to the soft-binding resolution API.

After signing, ingestion-api can post the manifest store + every
soft-binding to resolution-api so the just-ingested asset is
immediately resolvable via ``GET /matches/byBinding``. Configurable
via ``RESOLUTION_API_URL``; an empty value disables.

Wire shape:
1. ``POST {RESOLUTION_API_URL}/manifests`` with the raw manifest bytes
   as ``application/c2pa``. Response carries the assigned manifestId.
2. ``POST {RESOLUTION_API_URL}/bindings`` with
   ``{alg, bindingValue, manifestId}`` — one call per binding.
   Resolution-api's ``/bindings`` is intentionally singular (one
   binding per request); we loop over the list here.

Failure mode: caller logs + records a ``ResolutionPushResult`` with
``status=FAILED`` and an error message; the ingest request still
succeeds, returning the artifacts so a retry/sync can reconcile later.
A single ``/bindings`` failure aborts the rest of the loop — partial
state is signalled via ``status=FAILED`` so the operator can re-push
with the persisted manifest bytes + sidecar.
"""
from __future__ import annotations

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
    bindings: Sequence[BindingPair]
    fallback_manifest_id: str | None = None


class ResolutionPushClient:
    """HTTP client for the resolution-api auto-push."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout_s: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = (base_url if base_url is not None else settings.resolution_api_url).rstrip("/")
        self._timeout = (
            timeout_s if timeout_s is not None else settings.resolution_request_timeout_s
        )
        self._owned_client = client is None
        self._client = client or httpx.Client(timeout=self._timeout)

    @property
    def enabled(self) -> bool:
        return bool(self._base_url)

    def close(self) -> None:
        if self._owned_client:
            self._client.close()

    def push(self, req: ResolutionPushRequest) -> tuple[ResolutionPushResult, str | None]:
        """
        Push manifest + every binding. Returns (result, manifestId-on-success).

        Never raises — failure modes are surfaced through
        ``ResolutionPushResult.status=FAILED``.
        """
        if not self.enabled:
            return ResolutionPushResult(status=ResolutionPushStatus.SKIPPED), req.fallback_manifest_id

        if not req.bindings:
            return (
                ResolutionPushResult(
                    status=ResolutionPushStatus.FAILED,
                    error="ResolutionPushRequest.bindings must be non-empty",
                ),
                None,
            )

        try:
            manifest_id = self._post_manifest(req.manifest_bytes) or req.fallback_manifest_id
            if not manifest_id:
                return (
                    ResolutionPushResult(
                        status=ResolutionPushStatus.FAILED,
                        error="resolution-api returned no manifestId and no fallback was provided",
                    ),
                    None,
                )
            for pair in req.bindings:
                self._post_binding(pair.alg, pair.binding_value, manifest_id)
            return ResolutionPushResult(status=ResolutionPushStatus.OK), manifest_id
        except httpx.HTTPError as exc:
            logger.warning("Resolution-api push failed: %s", exc)
            return ResolutionPushResult(status=ResolutionPushStatus.FAILED, error=str(exc)), None
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected resolution-api push failure")
            return ResolutionPushResult(status=ResolutionPushStatus.FAILED, error=str(exc)), None

    def _post_manifest(self, manifest_bytes: bytes) -> str | None:
        url = f"{self._base_url}/manifests"
        r = self._client.post(
            url,
            content=manifest_bytes,
            headers={"Content-Type": "application/c2pa"},
        )
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            return None
        return data.get("manifestId")

    def _post_binding(self, alg: str, binding_value: str, manifest_id: str) -> None:
        url = f"{self._base_url}/bindings"
        r = self._client.post(
            url,
            json={"alg": alg, "bindingValue": binding_value, "manifestId": manifest_id},
        )
        r.raise_for_status()
