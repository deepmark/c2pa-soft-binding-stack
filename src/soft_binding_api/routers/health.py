"""Health/readiness endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from soft_binding_api.core.config import settings

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict:
    """Always returns 200 if the process is up."""
    return {
        "status": "ok",
        "service": settings.api_title,
        "version": settings.api_version,
    }


@router.get("/ready", summary="Readiness probe")
async def ready() -> dict:
    """
    Reports whether collaborators (Mongo, signing material) are reachable.

    Kept intentionally lightweight: pings Mongo, checks cert/key files exist.
    Doesn't construct a Signer (that's done lazily on first ingest).
    """
    from soft_binding_api.core.database import MongoDB

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

    cert_path = settings.resolved_cert_chain_path()
    key_path = settings.resolved_private_key_path()
    creds_ok = cert_path.is_file() and key_path.is_file()

    return {
        "status": "ok" if (mongo_ok and creds_ok) else "degraded",
        "mongo": {"ok": mongo_ok, "error": mongo_err},
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
    }
