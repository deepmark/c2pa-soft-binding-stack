"""
FastAPI application entry point for the soft-binding resolution API.

Lookup-side only. Ingestion (watermarking, manifest signing, asset
storage) lives in the sibling ``ingestion-api`` service.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from resolution_api.core.config import settings
from resolution_api.core.database import MongoDB
from resolution_api.core.logging import configure_logging, get_logger
from resolution_api.routers import fetch, health, query, service, store

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info("Starting %s v%s", settings.api_title, settings.api_version)
    await MongoDB.connect()
    logger.info("Connected to MongoDB at %s", settings.mongodb_url)
    try:
        yield
    finally:
        logger.info("Shutting down")
        await MongoDB.close()


app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    description=settings.api_description,
    lifespan=lifespan,
)

app.include_router(health.router)
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
            "query_by_binding": "/matches/byBinding",
            "get_manifest": "/manifests/{manifestId}",
            "supported_algorithms": "/services/supportedAlgorithms",
            "health": "/health",
            "ready": "/ready",
        },
    }


def run() -> None:
    """Console-script entry point for ``resolution-api``."""
    uvicorn.run(
        "resolution_api.__main__:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        reload=os.environ.get("RELOAD", "0") == "1",
    )


if __name__ == "__main__":
    run()
