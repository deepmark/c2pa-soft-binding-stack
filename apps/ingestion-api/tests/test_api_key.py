"""Ingestion routes need X-API-Key when API_KEY is set; health stays public; push sends the key."""
from __future__ import annotations

import httpx
import pytest

from ingestion_api.__main__ import app
from ingestion_api.adapters.publisher import ResolutionPushClient
from ingestion_api.contracts.publisher import ResolutionPushBinding, ResolutionPushRequest
from ingestion_api.core.config import settings


@pytest.fixture()
def keyed(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "secret-key")


async def _get(path: str, headers: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get(path, headers=headers or {})


@pytest.mark.parametrize("path", ["/ingestions", "/ingest/ing_x", "/ingest/ing_x/asset"])
async def test_ingestion_routes_reject_missing_or_wrong_key(keyed, path):
    assert (await _get(path)).status_code == 401
    assert (await _get(path, {"X-API-Key": "wrong"})).status_code == 401


async def test_correct_key_passes_the_guard(keyed):
    # No app state in this test, so the handler answers 503; the point is it isn't 401.
    assert (await _get("/ingestions", {"X-API-Key": "secret-key"})).status_code != 401


async def test_health_stays_public(keyed):
    assert (await _get("/health")).status_code != 401


async def test_no_key_configured_leaves_routes_open(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "")
    assert (await _get("/ingestions")).status_code != 401


def test_push_sends_resolution_api_key(monkeypatch):
    monkeypatch.setattr(settings, "resolution_api_key", "push-key")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-api-key"))
        return httpx.Response(201, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        ResolutionPushClient(base_url="http://resolution:8000", client=http).push(
            ResolutionPushRequest(manifest_bytes=b"m", manifest_id="urn:c2pa:test",
                                  bindings=[ResolutionPushBinding(alg="a", binding_value="v")]),
        )
    assert seen and all(k == "push-key" for k in seen)
