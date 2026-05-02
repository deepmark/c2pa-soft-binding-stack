"""Health/readiness endpoints for ingestion-api."""
from __future__ import annotations

import httpx
from fastapi import APIRouter

from ingestion_api.core.config import settings
from ingestion_api.core.database import MongoDB
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
    - MongoDB ping
    - cert + key files present
    - algorithm catalog loadable
    - every configured ``audio_algs`` plugin /health responding
    - resolution-api /health responding (if RESOLUTION_API_URL set)
    """
    mongo_ok = False
    mongo_err: str | None = None
    if MongoDB.client is not None:
        try:
            await MongoDB.client.admin.command("ping")
            mongo_ok = True
        except Exception as exc:  # noqa: BLE001
            mongo_err = str(exc)
    else:
        mongo_err = "MongoDB not connected"

    cert_path = settings.resolved_cert_chain_path()
    key_path = settings.resolved_private_key_path()
    creds_ok = cert_path.is_file() and key_path.is_file()

    catalog = load_catalog()
    by_alg = {e.alg: e for e in catalog}

    plugins_report: list[dict] = []
    plugins_ok = True
    with httpx.Client(timeout=2.0) as c:
        for alg in settings.audio_algs:
            entry = by_alg.get(alg)
            if entry is None or not entry.url:
                plugins_ok = False
                plugins_report.append({
                    "alg": alg,
                    "ok": False,
                    "url": entry.url if entry else None,
                    "error": (
                        f"alg {alg!r} missing or has no URL in algorithms.yaml"
                    ),
                })
                continue
            try:
                r = c.get(entry.url.rstrip("/") + "/health")
                r.raise_for_status()
                plugins_report.append({
                    "alg": alg, "ok": True, "url": entry.url, "error": None,
                })
            except httpx.HTTPError as exc:
                plugins_ok = False
                plugins_report.append({
                    "alg": alg, "ok": False, "url": entry.url, "error": str(exc),
                })

    resolution_ok: bool | None = None
    resolution_err: str | None = None
    if settings.resolution_push_enabled and settings.resolution_api_url:
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
        if mongo_ok and creds_ok and plugins_ok and (resolution_ok is None or resolution_ok)
        else "degraded"
    )

    return {
        "status": overall,
        "mongodb": {
            "ok": mongo_ok,
            "url": settings.mongodb_url,
            "database": settings.database_name,
            "error": mongo_err,
        },
        "credentials": {
            "ok": creds_ok,
            "cert_chain_path": str(cert_path),
            "private_key_path": str(key_path),
        },
        "ingest": {
            "audio_algs": list(settings.audio_algs),
            "signing_alg": settings.signing_alg,
            "ta_url": settings.ta_url,
        },
        "catalog": {
            "path": str(settings.algorithms_catalog_path),
            "entries": len(catalog),
        },
        "plugins": plugins_report,
        "resolution_api": {
            "push_enabled": settings.resolution_push_enabled,
            "url": settings.resolution_api_url or None,
            "ok": resolution_ok,
            "error": resolution_err,
        },
    }
