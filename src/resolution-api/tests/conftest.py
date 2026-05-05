"""Shared pytest fixtures + env bootstrap for resolution-api tests."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

_REPO_ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("ALGORITHMS_CATALOG_PATH", str(_REPO_ROOT / "algorithms.yaml"))


@pytest.fixture()
def mock_manifests_col():
    col = AsyncMock()
    col.find_one = AsyncMock(return_value=None)
    col.insert_one = AsyncMock()
    col.delete_one = AsyncMock()
    col.delete_many = AsyncMock()
    return col


class _FakeCursor:
    """Lightweight cursor stub whose .to_list() is a coroutine."""

    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        if length is not None:
            return self._docs[:length]
        return list(self._docs)


@pytest.fixture()
def mock_bindings_col():
    col = AsyncMock()
    col.find_one = AsyncMock(return_value=None)
    col.insert_one = AsyncMock()
    col.update_one = AsyncMock()
    col.delete_many = AsyncMock()
    col.find = lambda *a, **kw: _FakeCursor([])
    return col


@pytest.fixture()
def mock_fs():
    fs = AsyncMock()
    fs.upload_from_stream = AsyncMock(return_value="gridfs-id")
    fs.download_to_stream = AsyncMock()
    fs.delete = AsyncMock()
    return fs


@pytest.fixture()
async def client(mock_manifests_col, mock_bindings_col, mock_fs):
    with (
        patch("resolution_api.core.database.MongoDB.connect", new_callable=AsyncMock),
        patch("resolution_api.core.database.MongoDB.close", new_callable=AsyncMock),
        patch(
            "resolution_api.routers.store.get_manifests_collection",
            return_value=mock_manifests_col,
        ),
        patch(
            "resolution_api.routers.store.get_soft_bindings_collection",
            return_value=mock_bindings_col,
        ),
        patch(
            "resolution_api.routers.store.get_manifest_blobs_bucket",
            return_value=mock_fs,
        ),
        patch(
            "resolution_api.routers.fetch.get_manifests_collection",
            return_value=mock_manifests_col,
        ),
        patch(
            "resolution_api.routers.fetch.get_manifest_blobs_bucket",
            return_value=mock_fs,
        ),
        patch(
            "resolution_api.routers.query.get_soft_bindings_collection",
            return_value=mock_bindings_col,
        ),
        patch(
            "resolution_api.core.database.MongoDB.client",
            new=None,
        ),
    ):
        from resolution_api.__main__ import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
