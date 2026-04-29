"""
FastAPI application entry point.

This module is both the ASGI app (``soft_binding_api.__main__:app``) and a
``python -m soft_binding_api`` runner. Two reasons it lives here:

- Per the project layout, ``__main__.py`` is the canonical FastAPI app.
- Keeping the uvicorn runner in the same module lets ``softbinding-api``
  (a console script declared in ``pyproject.toml``) just call
  ``soft_binding_api.__main__:run``.

App startup wires the long-lived collaborators onto ``app.state``:
- ``app.state.signing_service``  — single ``Signer`` for the process
- ``app.state.local_store``      — filesystem-backed asset store
- ``app.state.ingestion_repo``   — Mongo-backed ingestion metadata
- ``app.state.ingestion_service``— orchestrator that ties the above together

Routers grab them via ``Depends(get_*)`` rather than module globals.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

# Importing the watermark package registers the dummy
# ``me.deepmark.audio.vigil.128`` embedder/detector as a side effect.
import soft_binding_api.services.watermark  # noqa: F401
from soft_binding_api.core.config import settings
from soft_binding_api.core.database import MongoDB
from soft_binding_api.core.logging import configure_logging, get_logger
from soft_binding_api.routers import fetch, health, ingest, query, service, store
from soft_binding_api.services.ingestion import IngestionService
from soft_binding_api.services.signing import SigningService
from soft_binding_api.services.storage import IngestionRepository, LocalAssetStore

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: configure logging, connect Mongo, wire services."""
    configure_logging()
    logger.info("Starting %s v%s", settings.api_title, settings.api_version)

    await MongoDB.connect()
    logger.info("Connected to MongoDB at %s", settings.mongodb_url)

    app.state.local_store = LocalAssetStore()
    app.state.ingestion_repo = IngestionRepository()
    app.state.signing_service = SigningService()
    app.state.ingestion_service = IngestionService(
        signing_service=app.state.signing_service,
        local_store=app.state.local_store,
        repo=app.state.ingestion_repo,
        soft_binding_alg=settings.default_audio_alg,
    )

    try:
        yield
    finally:
        logger.info("Shutting down")
        try:
            app.state.signing_service.close()
        except Exception:
            logger.exception("Error closing signing service")
        await MongoDB.close()


app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    description=settings.api_description,
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(ingest.router)
app.include_router(query.router)
app.include_router(store.router)
app.include_router(fetch.router)
app.include_router(service.router)


@app.get("/", include_in_schema=False)
async def root():
    return {
        "message": settings.api_title,
        "version": settings.api_version,
        "endpoints": {
            "ingest": "/ingest",
            "ingestion_metadata": "/ingest/{ingestionId}",
            "query_by_binding": "/matches/byBinding",
            "get_manifest": "/manifests/{manifestId}",
            "supported_algorithms": "/services/supportedAlgorithms",
            "health": "/health",
            "ready": "/ready",
        },
    }


def run() -> None:
    """Console-script entry point for ``softbinding-api``."""
    uvicorn.run(
        "soft_binding_api.__main__:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=os.environ.get("RELOAD", "0") == "1",
    )


if __name__ == "__main__":
    run()
