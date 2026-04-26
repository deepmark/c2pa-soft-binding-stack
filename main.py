"""
C2PA Soft Binding Resolution API (Decoupled spec §1.4.1).

Routes are split per spec route group into:
- routers.query   (§1.4.1.1)
- routers.store   (§1.4.1.2)
- routers.fetch   (§1.4.1.3)
- routers.service (§1.4.1.4)
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from config import settings
from database import MongoDB
from routers import fetch, query, service, store


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    await MongoDB.connect()
    yield
    await MongoDB.close()


app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    description=settings.api_description,
    lifespan=lifespan,
)

app.include_router(query.router)
app.include_router(store.router)
app.include_router(fetch.router)
app.include_router(service.router)


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "C2PA Soft Binding Resolution API",
        "version": settings.api_version,
        "endpoints": {
            "query_by_binding": "/matches/byBinding",
            "get_manifest": "/manifests/{manifestId}",
            "supported_algorithms": "/services/supportedAlgorithms",
        },
    }
