"""Health/readiness endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from resolution_api.core.config import settings
from resolution_api.services.algorithms_catalog import load_catalog

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
    """Pings Mongo and verifies the algorithm catalog is loadable."""
    from resolution_api.core.database import MongoDB

    mongo_ok = True
    mongo_err: str | None = None
    try:
        if MongoDB.client is None:
            mongo_ok = False
            mongo_err = "not connected"
        else:
            await MongoDB.client.admin.command("ping")
    except Exception as exc:
        mongo_ok = False
        mongo_err = str(exc)

    catalog = load_catalog()

    return {
        "status": "ok" if (mongo_ok and catalog) else "degraded",
        "mongo": {"ok": mongo_ok, "error": mongo_err},
        "catalog": {
            "path": str(settings.algorithms_catalog_path),
            "entries": len(catalog),
        },
    }
