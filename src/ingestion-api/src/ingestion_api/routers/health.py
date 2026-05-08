"""
Health / readiness endpoints for ingestion-api.

Three endpoints, each scoped for a different consumer:

- ``GET /health`` — liveness. Cheap, never reaches outside the process.
  Always 200 unless the process is itself crashed.

- ``GET /ready`` — kubelet-style readiness. Process state + Mongo ping only. 
  Bounded sub-2s. Returns 503 when degraded so orchestrators can actually route traffic away.

- ``GET /health/deep`` — full fan-out: catalog plugins ``/health``, 
  the resolution-api ``/health`` (when push is enabled), 
  and the signing certificate's ``notAfter`` (informational, never gates readiness). 
  Designed for monitoring dashboards / human ops consumption, NOT for kubelets:
  every call probes every plugin in parallel.

Design notes:

- All probes use ``httpx.AsyncClient`` so a slow plugin doesn't park the worker; 
  plugin probes are run concurrently via ``asyncio.gather(return_exceptions=True)``.
- ``/ready`` wraps the MongoDB ``ping`` in ``asyncio.wait_for`` so a
  hung primary can't extend the probe past the timeout budget.
- Degraded ``/ready`` and ``/health/deep`` return HTTP 503
  with a body detailing which subsystem(s) are degraded.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ingestion_api.catalog.algorithms import AlgorithmEntry, load_catalog
from ingestion_api.config import settings
from ingestion_api.repositories.database import MongoDB
from ingestion_api.logging import get_logger
from ingestion_api.services.signing import SigningService

logger = get_logger(__name__)

router = APIRouter(tags=["health"])

# MongoDB ``ping`` is bounded by ``serverSelectionTimeoutMS=5_000`` from ``core/database.py``; 
_READY_MONGO_TIMEOUT_S = 1.5
# Per-probe HTTP timeout for the deep fan-out. Plugins respond to
# ``/health`` in tens of milliseconds locally; 2s is a generous cap.
_DEEP_HTTP_TIMEOUT_S = 2.0


def _build_info() -> dict[str, str | None]:
    """Build identity surfaced on health probes for ops/debugging."""
    return {
        "git_sha": settings.git_sha or None,
        "image_tag": settings.image_tag or None,
    }


@router.get("/health", summary="Liveness probe")
async def health() -> dict:
    return {
        "status": "ok",
        "service": settings.api_title,
        "version": settings.api_version,
        **_build_info(),
    }


@router.get(
    "/ready",
    summary="Readiness probe (process + MongoDB only)",
    responses={
        200: {"description": "Service is ready to accept traffic"},
        503: {"description": "Service is degraded — body details which subsystems are degraded"},
    },
)
async def ready(request: Request) -> JSONResponse:
    """
    Cheap readiness check, suitable for kubelet polling every ~10s.

    Checks:
    - process state: signing service has loaded a Signer at startup
    - cert + key files present on disk (existence only — no parse)
    - MongoDB ping (bounded)
    """
    mongo = await _check_mongo()
    creds_ok = (
        settings.cert_chain_path.is_file()
        and settings.private_key_path.is_file()
    )
    signing_loaded = _signing_loaded(request)

    overall_ok = mongo["ok"] and creds_ok and signing_loaded
    body: dict[str, Any] = {
        "status": "ok" if overall_ok else "degraded",
        "service": settings.api_title,
        "version": settings.api_version,
        **_build_info(),
        "mongodb": mongo,
        "credentials": {
            "ok": creds_ok,
            "cert_chain_path": str(settings.cert_chain_path),
            "private_key_path": str(settings.private_key_path),
        },
        "signing": {
            "loaded": signing_loaded,
            "alg": settings.signing_alg,
            "ta_url": settings.ta_url,
        },
    }
    return JSONResponse(body, status_code=200 if overall_ok else 503)


@router.get(
    "/health/deep",
    summary="Full fan-out diagnostics (plugins + resolution-api + cert expiry)",
    responses={
        200: {"description": "All probed subsystems healthy"},
        503: {
            "description": (
                "Some probed subsystem is degraded. Body details which one(s)."
            ),
        },
    },
)
async def health_deep(request: Request) -> JSONResponse:
    """
    Heavy diagnostic probe. NOT for orchestrator readiness checks.
    Each call concurrently probes every catalog plugin's ``/health``
    plus resolution-api's ``/health`` (when push is enabled). 
    
    Cert expiry (``notAfter``) is exposed on the response but will not fail the status. 
    Treat as informational and alert externally if nearing expiry.
    """
    mongo = await _check_mongo()
    creds_ok = (
        settings.cert_chain_path.is_file()
        and settings.private_key_path.is_file()
    )
    signing_loaded = _signing_loaded(request)
    cert_info = _cert_info(request)
    catalog = load_catalog()

    async with httpx.AsyncClient(timeout=_DEEP_HTTP_TIMEOUT_S) as client:
        plugin_task = asyncio.create_task(_probe_plugins(client, catalog))
        resolution_task = asyncio.create_task(_probe_resolution(client))
        plugins_report, plugins_ok = await plugin_task
        resolution = await resolution_task

    overall_ok = (
        mongo["ok"]
        and creds_ok
        and signing_loaded
        and plugins_ok
        and resolution["status"] in ("ok", "skipped")
    )
    body: dict[str, Any] = {
        "status": "ok" if overall_ok else "degraded",
        "service": settings.api_title,
        "version": settings.api_version,
        **_build_info(),
        "mongodb": mongo,
        "credentials": {
            "ok": creds_ok,
            "cert_chain_path": str(settings.cert_chain_path),
            "private_key_path": str(settings.private_key_path),
            **cert_info,
        },
        "signing": {
            "loaded": signing_loaded,
            "alg": settings.signing_alg,
            "ta_url": settings.ta_url,
        },
        "catalog": {
            "path": str(settings.algorithms_catalog_path),
            "entries": len(catalog),
        },
        "plugins": plugins_report,
        "resolution_api": resolution,
    }
    return JSONResponse(body, status_code=200 if overall_ok else 503)


# ---------------------------------------------------------------------------
# Probe helpers.
# ---------------------------------------------------------------------------


async def _check_mongo() -> dict[str, Any]:
    """Bounded MongoDB ``ping``. Never raises."""
    if MongoDB.client is None:
        return {
            "ok": False,
            "url": settings.mongodb_url,
            "database": settings.database_name,
            "error": "MongoDB not connected",
        }
    try:
        await asyncio.wait_for(
            MongoDB.client.admin.command("ping"),
            timeout=_READY_MONGO_TIMEOUT_S,
        )
        return {
            "ok": True,
            "url": settings.mongodb_url,
            "database": settings.database_name,
            "error": None,
        }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "url": settings.mongodb_url,
            "database": settings.database_name,
            "error": f"ping timed out after {_READY_MONGO_TIMEOUT_S}s",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "url": settings.mongodb_url,
            "database": settings.database_name,
            "error": str(exc),
        }


def _signing_loaded(request: Request) -> bool:
    svc: SigningService | None = getattr(request.app.state, "signing_service", None)
    return bool(svc and svc.is_loaded)


def _cert_info(request: Request) -> dict[str, Any]:
    """Best-effort leaf-cert metadata for /health/deep. Never raises."""
    svc: SigningService | None = getattr(request.app.state, "signing_service", None)
    if svc is None:
        return {
            "not_after": None,
            "expired": None,
            "expires_in_days": None,
        }
    not_after = svc.credentials.leaf_not_after()
    if not_after is None:
        return {
            "not_after": None,
            "expired": None,
            "expires_in_days": None,
        }
    now = datetime.now(timezone.utc)
    delta = not_after - now
    expires_in_seconds = int(delta.total_seconds())
    return {
        "not_after": not_after.isoformat(),
        "expired": expires_in_seconds < 0,
        "expires_in_days": max(0, expires_in_seconds // 86400),
    }


async def _probe_plugins(
    client: httpx.AsyncClient, catalog: list[AlgorithmEntry],
) -> tuple[list[dict[str, Any]], bool]:
    """Concurrently GET ``/health`` against every cataloged plugin."""

    async def _one(entry: AlgorithmEntry) -> dict[str, Any]:
        if not entry.url:
            return {
                "alg": entry.alg,
                "ok": False,
                "url": None,
                "error": f"alg {entry.alg!r} has no URL in algorithms.yaml",
            }
        url = entry.url.rstrip("/") + "/health"
        try:
            r = await client.get(url)
            r.raise_for_status()
            return {"alg": entry.alg, "ok": True, "url": entry.url, "error": None}
        except httpx.HTTPError as exc:
            return {
                "alg": entry.alg,
                "ok": False,
                "url": entry.url,
                "error": str(exc),
            }

    results = await asyncio.gather(
        *[_one(e) for e in catalog], return_exceptions=False,
    )
    plugins_ok = all(r["ok"] for r in results) if results else True
    return list(results), plugins_ok


async def _probe_resolution(client: httpx.AsyncClient) -> dict[str, Any]:
    """Probe resolution-api ``/health``. Returns a tri-state status."""
    if not settings.resolution_push_enabled or not settings.resolution_api_url:
        return {
            "status": "skipped",
            "push_enabled": settings.resolution_push_enabled,
            "url": settings.resolution_api_url or None,
            "error": None,
        }
    url = settings.resolution_api_url.rstrip("/") + "/health"
    try:
        r = await client.get(url)
        r.raise_for_status()
        return {
            "status": "ok",
            "push_enabled": True,
            "url": settings.resolution_api_url,
            "error": None,
        }
    except httpx.HTTPError as exc:
        return {
            "status": "failed",
            "push_enabled": True,
            "url": settings.resolution_api_url,
            "error": str(exc),
        }
