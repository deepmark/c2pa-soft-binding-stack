"""Tests for service.py — GET /services/supportedAlgorithms."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from resolution_api.services.algorithms_catalog import AlgorithmEntry


class TestGetSupportedAlgorithms:
    async def test_returns_watermarks_and_fingerprints(self, client):
        catalog = [
            AlgorithmEntry(alg="me.deepmark.audio.aware.20", type="watermark"),
            AlgorithmEntry(alg="org.example.audiofp.v1", type="fingerprint"),
        ]
        with patch(
            "resolution_api.routers.service.load_catalog", return_value=catalog,
        ):
            resp = await client.get("/services/supportedAlgorithms")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["watermarks"]) == 1
        assert body["watermarks"][0]["alg"] == "me.deepmark.audio.aware.20"
        assert len(body["fingerprints"]) == 1
        assert body["fingerprints"][0]["alg"] == "org.example.audiofp.v1"

    async def test_empty_catalog(self, client):
        with patch(
            "resolution_api.routers.service.load_catalog", return_value=[],
        ):
            resp = await client.get("/services/supportedAlgorithms")
        assert resp.status_code == 200
        body = resp.json()
        assert body["watermarks"] == []
        assert body["fingerprints"] == []

    async def test_watermarks_only(self, client):
        catalog = [
            AlgorithmEntry(alg="wm1", type="watermark"),
            AlgorithmEntry(alg="wm2", type="watermark"),
        ]
        with patch(
            "resolution_api.routers.service.load_catalog", return_value=catalog,
        ):
            resp = await client.get("/services/supportedAlgorithms")
        body = resp.json()
        assert len(body["watermarks"]) == 2
        assert body["fingerprints"] == []

    async def test_catalog_load_error(self, client):
        with patch(
            "resolution_api.routers.service.load_catalog",
            side_effect=Exception("YAML parse error"),
        ):
            resp = await client.get("/services/supportedAlgorithms")
        assert resp.status_code == 500
