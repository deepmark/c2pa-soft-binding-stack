"""Health/readiness endpoints for ingestion-api."""
from __future__ import annotations

import httpx
from fastapi import APIRouter

from ingestion_api.core.config import settings
from ingestion_api.services.algorithms import load_catalog

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict:
    return {
        "status": "ok",
        "service": settings.api_title,
        "version": settings.api_version,
    }


@router.get("/ready", summary="Readiness probe")
async def ready() -> dict:
    """
    Best-effort readiness:
    - cert + key files present
    - algorithm catalog loadable
    - default plugin /health responding
    - resolution-api /health responding (if RESOLUTION_API_URL set)
    """
    cert_path = settings.resolved_cert_chain_path()
    key_path = settings.resolved_private_key_path()
    creds_ok = cert_path.is_file() and key_path.is_file()

    catalog = load_catalog()
    plugin = next((e for e in catalog if e.alg == settings.default_audio_alg), None)

    plugin_ok = False
    plugin_err: str | None = None
    if plugin and plugin.url:
        try:
            with httpx.Client(timeout=2.0) as c:
                r = c.get(plugin.url.rstrip("/") + "/health")
                r.raise_for_status()
                plugin_ok = True
        except httpx.HTTPError as exc:
            plugin_err = str(exc)
    else:
        plugin_err = "default plugin missing or has no URL in algorithms.yaml"

    resolution_ok: bool | None = None
    resolution_err: str | None = None
    if settings.resolution_api_url:
        try:
            with httpx.Client(timeout=2.0) as c:
                r = c.get(settings.resolution_api_url.rstrip("/") + "/health")
                r.raise_for_status()
                resolution_ok = True
        except httpx.HTTPError as exc:
            resolution_ok = False
            resolution_err = str(exc)

    overall = (
        "ok"
        if creds_ok and plugin_ok and (resolution_ok is None or resolution_ok)
        else "degraded"
    )

    return {
        "status": overall,
        "credentials": {
            "ok": creds_ok,
            "cert_chain_path": str(cert_path),
            "private_key_path": str(key_path),
        },
        "ingest": {
            "default_alg": settings.default_audio_alg,
            "signing_alg": settings.signing_alg,
            "ta_url": settings.ta_url,
        },
        "catalog": {
            "path": str(settings.algorithms_catalog_path),
            "entries": len(catalog),
        },
        "plugin": {"ok": plugin_ok, "url": plugin.url if plugin else None, "error": plugin_err},
        "resolution_api": {
            "url": settings.resolution_api_url or None,
            "ok": resolution_ok,
            "error": resolution_err,
        },
    }
