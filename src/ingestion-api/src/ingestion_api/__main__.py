"""
FastAPI application entry point for ingestion-api.

App startup wires the long-lived collaborators onto ``app.state``:
- ``app.state.signing_service``    — single C2PA Signer for the process
- ``app.state.local_store``        — filesystem-backed asset store
- ``app.state.resolution_client``  — auto-push HTTP client (no-op when
                                     RESOLUTION_API_URL is empty)
- ``app.state.ingestion_service``  — orchestrator tying them all together
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from ingestion_api.core.config import settings
from ingestion_api.core.logging import configure_logging, get_logger
from ingestion_api.routers import health, ingest
from ingestion_api.services.orchestrator import IngestionService
from ingestion_api.services.publisher import ResolutionPushClient
from ingestion_api.services.signing import SigningService
from ingestion_api.services.storage import LocalAssetStore

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info("Starting %s v%s", settings.api_title, settings.api_version)

    app.state.local_store = LocalAssetStore()
    app.state.signing_service = SigningService()
    app.state.resolution_client = ResolutionPushClient()
    app.state.ingestion_service = IngestionService(
        signing_service=app.state.signing_service,
        local_store=app.state.local_store,
        resolution_client=app.state.resolution_client,
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
        try:
            app.state.resolution_client.close()
        except Exception:
            logger.exception("Error closing resolution client")


app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    description=settings.api_description,
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(ingest.router)


@app.get("/", include_in_schema=False)
async def root():
    return {
        "message": settings.api_title,
        "version": settings.api_version,
        "endpoints": {
            "ingest": "/ingest",
            "ingestion_metadata": "/ingest/{ingestionId}",
            "health": "/health",
            "ready": "/ready",
        },
    }


def run() -> None:
    uvicorn.run(
        "ingestion_api.__main__:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8001")),
        reload=os.environ.get("RELOAD", "0") == "1",
    )


if __name__ == "__main__":
    run()
