"""
FastAPI application entry point for ingestion-api.

App startup wires the long-lived collaborators onto ``app.state``:
- ``app.state.signing_service``    — single C2PA Signer for the process
- ``app.state.artifacts``          — filesystem-backed binary artifact store
- ``app.state.records``            — MongoDB-backed IngestionRecord repository
- ``app.state.resolution_client``  — auto-push HTTP client (no-op when
                                     RESOLUTION_PUSH_ENABLED=false)
- ``app.state.ingestion_service``  — orchestrator tying them all together

Middleware stack (outermost first; ASGI middleware runs in reverse-add order):
- ``RequestIDMiddleware``      — generates/echoes X-Request-ID, binds contextvar
- ``ProxyHeadersMiddleware``   — honors X-Forwarded-Proto/Host so absolute URLs
                                  in IngestResponse are correct behind a proxy.

Body-size enforcement is delegated to the reverse proxy (nginx
``client_max_body_size`` / k8s ingress ``proxy-body-size``); the
application doesn't try to cap uploads itself. See
``core/middleware.py`` for the rationale.

Signing material is validated at startup (cert + key parse-loaded into a
``Signer``); a missing or malformed cert fails the lifespan startup
hard rather than throwing 503s on the first ingest.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from ingestion_api.core.config import settings
from ingestion_api.core.database import (
    MongoDB,
    get_failed_ingestions_collection,
    get_ingestions_collection,
)
from ingestion_api.core.logging import configure_logging, get_logger
from ingestion_api.core.middleware import RequestIDMiddleware
from ingestion_api.routers import health, ingest
from ingestion_api.services.artifact_store import ArtifactStore
from ingestion_api.services.orchestrator import IngestionService
from ingestion_api.services.publisher import ResolutionPushClient
from ingestion_api.services.record_repository import (
    MongoFailedIngestionRepository,
    MongoIngestionRecordRepository,
)
from ingestion_api.services.signing import (
    MissingSigningMaterialError,
    SigningService,
)

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info("Starting %s v%s", settings.api_title, settings.api_version)

    await MongoDB.connect()
    logger.info("Connected to MongoDB at %s db=%s", settings.mongodb_url, settings.database_name)

    if settings.resolution_push_enabled:
        logger.info("Resolution-api auto-push ENABLED -> %s", settings.resolution_api_url)
    else:
        logger.warning(
            "Resolution-api auto-push DISABLED (RESOLUTION_PUSH_ENABLED=false) — "
            "ingested records will not be queryable via /matches/byBinding."
        )

    app.state.artifacts = ArtifactStore()
    app.state.records = MongoIngestionRecordRepository(get_ingestions_collection())
    app.state.failed_records = MongoFailedIngestionRepository(
        get_failed_ingestions_collection(),
    )

    signing_service = SigningService()
    try:
        # Eager parse of cert + key. Missing or malformed signing
        # material is a fatal config error — don't start the app at all
        # rather than 503ing on first ingest.
        signing_service.validate()
    except MissingSigningMaterialError:
        logger.exception("Signing material missing or unreadable; aborting startup")
        raise
    except Exception:
        logger.exception("Signing material malformed; aborting startup")
        raise
    app.state.signing_service = signing_service

    app.state.resolution_client = ResolutionPushClient()
    app.state.ingestion_service = IngestionService(
        signing_service=app.state.signing_service,
        artifacts=app.state.artifacts,
        records=app.state.records,
        failed_records=app.state.failed_records,
        resolution_client=app.state.resolution_client,
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
        try:
            await MongoDB.close()
        except Exception:
            logger.exception("Error closing MongoDB connection")


app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    description=settings.api_description,
    lifespan=lifespan,
)

# Middleware order matters. ASGI middleware runs in reverse of add
# order, so this stack is (outer -> inner):
#   RequestID -> ProxyHeaders -> app
# Rationale:
# - RequestID before ProxyHeaders so log lines emitted by Starlette /
#   FastAPI internals during URL/host rewriting are still stamped.
# - ProxyHeaders innermost so request.url / request.base_url see the
#   external scheme/host by the time route handlers run.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.forwarded_allow_ips)
app.add_middleware(RequestIDMiddleware)

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
            "ingestion_list": "/ingestions",
            "health": "/health",
            "ready": "/ready",
            "health_deep": "/health/deep",
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
