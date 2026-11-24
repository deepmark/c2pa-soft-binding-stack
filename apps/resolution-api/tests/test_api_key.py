"""Store routes require X-API-Key when API_KEY is set; lookups and queries stay public."""
from __future__ import annotations

import pytest

from resolution_api.core.config import settings


@pytest.fixture()
def keyed(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "secret-key")


@pytest.mark.parametrize("method,path", [
    ("POST", "/manifests"),
    ("DELETE", "/manifests/urn:c2pa:x"),
    ("POST", "/bindings"),
    ("PUT", "/bindings"),
])
async def test_store_routes_reject_missing_or_wrong_key(client, keyed, method, path):
    assert (await client.request(method, path)).status_code == 401
    assert (await client.request(method, path, headers={"X-API-Key": "wrong"})).status_code == 401


async def test_correct_key_passes_the_guard(client, keyed):
    r = await client.delete("/manifests/urn:c2pa:x", headers={"X-API-Key": "secret-key"})
    assert r.status_code != 401


@pytest.mark.parametrize("path", ["/manifests/urn:c2pa:x", "/services/supportedAlgorithms", "/health"])
async def test_lookups_stay_public(client, keyed, path):
    assert (await client.get(path)).status_code != 401
