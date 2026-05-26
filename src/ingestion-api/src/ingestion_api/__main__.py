"""
FastAPI application entry point for ingestion-api.

App startup wires the long-lived collaborators onto ``app.state``:
- ``app.state.signing_service``    — keeps cert metadata (signing_alg,
                                     ta_url, cert_sha1) for the
                                     IngestionRecord; the underlying
                                     Signer is released to
                                     ``manifest_builder`` (Context
                                     ownership transfer per the c2pa
                                     SDK rules)
- ``app.state.manifest_builder``   — singleton ManifestBuilderService;
                                     its Context owns the consumed
                                     Signer for the process lifetime
- ``app.state.artifacts``          — filesystem-backed binary artifact store
- ``app.state.records``            — MongoDB-backed IngestionRecord repository
- ``app.state.resolution_client``  — auto-push HTTP client (no-op when
                                     RESOLUTION_PUSH_ENABLED=false)
- ``app.state.ingestion_service``  — orchestrator tying them all together

Middleware stack (outermost first; ASGI middleware runs in reverse-add order):
- ``RequestIDMiddleware``      — generates/echoes X-Request-ID, binds contextvar
- ``ProxyHeadersMiddleware``   — honors X-Forwarded-Proto/Host so absolute URLs
                                  in IngestionResponse are correct behind a proxy.

Signing material is validated at startup (cert + key parsed into a ``Signer``). 
A missing or malformed cert fails the lifespan startup hard rather than throwing 503s on the first ingest.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from ingestion_api.adapters.publisher import ResolutionPushClient
from ingestion_api.core.plugin import load_plugin_catalog_from_db  # used in startup check
from ingestion_api.core.config import settings
from ingestion_api.repositories.database import (
    MongoDB,
    get_failed_ingestions_collection,
    get_ingestions_collection,
)
from ingestion_api.core.logging import configure_logging, get_logger
from ingestion_api.middleware.request_id import RequestIDMiddleware
from ingestion_api.core.credentials import MissingSigningMaterialError
from ingestion_api.repositories.artifacts import ArtifactStore
from ingestion_api.repositories.ingestions import (
    MongoFailedIngestionRepository,
    MongoIngestionRecordRepository,
)
from ingestion_api.routers import health, ingestion
from ingestion_api.services.ingestion import IngestionService
from ingestion_api.services.manifest import ManifestBuilderService
from ingestion_api.services.signing import SigningService

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

    plugin_catalog = await load_plugin_catalog_from_db()
    if not plugin_catalog:
        logger.warning(
            "No algorithms found in %s.supported_algorithms at startup. "
            "Algorithms will be resolved from MongoDB on each request.",
            settings.algorithms_database_name,
        )
    else:
        logger.info(
            "Found %d plugin entries in %s.supported_algorithms: %s",
            len(plugin_catalog),
            settings.algorithms_database_name,
            [e.alg for e in plugin_catalog],
        )

    app.state.artifacts = ArtifactStore()
    app.state.records = MongoIngestionRecordRepository(get_ingestions_collection())
    app.state.failed_records = MongoFailedIngestionRepository(
        get_failed_ingestions_collection(),
    )

    signing_service = SigningService()
    try:
        # Eager parse of cert + key. 
        # Missing or malformed signing material is a fatal config error.
        signing_service.validate()
    except MissingSigningMaterialError:
        logger.exception("Signing material missing or unreadable; aborting startup")
        raise
    except Exception:
        logger.exception("Signing material malformed; aborting startup")
        raise
    app.state.signing_service = signing_service

    # Hand the Signer to a long-lived ManifestBuilderService. The
    # SDK's Context consumes the Signer on construction (see
    # services/manifest.py and services/signing.py for the ownership
    # rules); after this call, ``signing_service.signer`` raises
    # RuntimeError (ownership transferred) and the manifest builder
    # owns the FFI handle until shutdown.
    #
    # release_signer() and ManifestBuilderService(signer=...) aren't
    # atomic: if the constructor raises after release succeeded, the
    # Signer is orphaned (signing_service no longer owns it; the
    # half-built manifest builder doesn't either). Close it
    # explicitly so the FFI handle isn't dependent on GC timing.
    released_signer = signing_service.release_signer()
    try:
        app.state.manifest_builder = ManifestBuilderService(signer=released_signer)
    except Exception:
        try:
            released_signer.close()
        except Exception:
            logger.exception(
                "Failed to close orphaned Signer after ManifestBuilderService init failure",
            )
        raise

    app.state.resolution_client = ResolutionPushClient()
    app.state.ingestion_service = IngestionService(
        signing_service=app.state.signing_service,
        manifest_builder=app.state.manifest_builder,
        artifacts=app.state.artifacts,
        records=app.state.records,
        failed_records=app.state.failed_records,
        resolution_client=app.state.resolution_client,
    )

    try:
        yield
    finally:
        logger.info("Shutting down")
        # Each app.state.* may be unset if startup crashed before
        # wiring it. Guard with getattr so a partial-startup shutdown
        # doesn't emit AttributeError stack traces that mask the real
        # startup failure in the logs.
        #
        # Order: manifest_builder before signing_service, so the
        # Context releases the consumed Signer's FFI handle before
        # the (now-empty) signing service tears down. signing_service
        # itself is a no-op in normal production lifespan
        # (release_signer already nulled _signer); kept for tests and
        # any future code path that bypasses release_signer.
        for name in ("manifest_builder", "signing_service", "resolution_client"):
            obj = getattr(app.state, name, None)
            if obj is None:
                continue
            try:
                obj.close()
            except Exception:
                logger.exception("Error closing %s", name)
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

# Middleware order matters. 
# ASGI middleware runs in reverse of add order, so this stack is (outer -> inner):
#   RequestID -> ProxyHeaders -> app
# Rationale:
# - RequestID before ProxyHeaders so log lines emitted by Starlette /
#   FastAPI internals during URL/host rewriting are still stamped.
# - ProxyHeaders innermost so request.url / request.base_url see the
#   external scheme/host by the time route handlers run.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.forwarded_allow_ips)
app.add_middleware(RequestIDMiddleware)

app.include_router(health.router)
app.include_router(ingestion.router)


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
