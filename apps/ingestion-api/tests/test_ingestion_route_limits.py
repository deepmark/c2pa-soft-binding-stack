"""Route-level upload size limit tests."""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from ingestion_api.core.config import settings
from ingestion_api.routers import ingestion
from ingestion_api.routers.dependencies import get_artifact_store, get_ingestion_service


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ingestion.router)
    app.dependency_overrides[get_ingestion_service] = lambda: object()
    app.dependency_overrides[get_artifact_store] = lambda: object()
    return app


@pytest.mark.asyncio
async def test_ingest_file_exceeds_max_upload_size():
    app = _build_app()
    transport = httpx.ASGITransport(app=app)

    with patch.object(settings, "max_upload_size_bytes", 10):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/ingest",
                files={"file": ("big.wav", b"\x00" * 20, "audio/wav")},
                data={"algs": "me.deepmark.audio.aware.20"},
            )

    assert resp.status_code == 413
    assert "exceeds maximum allowed size" in resp.json()["detail"]
