"""Tests for service.py — GET /services/supportedAlgorithms."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import _FakeCursor


class TestGetSupportedAlgorithms:
    async def test_returns_watermarks_and_fingerprints(self, client, mock_algorithms_col):
        docs = [
            {"alg": "me.deepmark.audio.aware.20", "type": "watermark", "bindingBits": 20, "mediaTypes": ["audio/wav"]},
            {"alg": "org.example.audiofp.v1", "type": "fingerprint", "bindingBits": 64, "mediaTypes": ["audio/mpeg"]},
        ]
        mock_algorithms_col.find = lambda *a, **kw: _FakeCursor(docs)
        resp = await client.get("/services/supportedAlgorithms")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["watermarks"]) == 1
        assert body["watermarks"][0]["alg"] == "me.deepmark.audio.aware.20"
        assert len(body["fingerprints"]) == 1
        assert body["fingerprints"][0]["alg"] == "org.example.audiofp.v1"

    async def test_empty_catalog(self, client, mock_algorithms_col):
        mock_algorithms_col.find = lambda *a, **kw: _FakeCursor([])
        resp = await client.get("/services/supportedAlgorithms")
        assert resp.status_code == 200
        body = resp.json()
        assert body["watermarks"] == []
        assert body["fingerprints"] == []

    async def test_watermarks_only(self, client, mock_algorithms_col):
        docs = [
            {"alg": "wm1", "type": "watermark", "bindingBits": 20, "mediaTypes": ["audio/wav"]},
            {"alg": "wm2", "type": "watermark", "bindingBits": 32, "mediaTypes": ["audio/wav"]},
        ]
        mock_algorithms_col.find = lambda *a, **kw: _FakeCursor(docs)
        resp = await client.get("/services/supportedAlgorithms")
        body = resp.json()
        assert len(body["watermarks"]) == 2
        assert body["fingerprints"] == []

    async def test_catalog_load_error(self, client, mock_algorithms_col):
        class _ErrorCursor:
            async def to_list(self, length=None):
                raise Exception("DB error")

        mock_algorithms_col.find = lambda *a, **kw: _ErrorCursor()
        resp = await client.get("/services/supportedAlgorithms")
        assert resp.status_code == 500
