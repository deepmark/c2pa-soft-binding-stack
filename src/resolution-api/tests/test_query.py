"""Tests for query.py — GET/POST /matches/byBinding, POST /matches/byContent, POST /matches/byReference."""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock, patch

import pytest

from resolution_api.services.algorithms_catalog import AlgorithmEntry


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        if length is not None:
            return self._docs[:length]
        return list(self._docs)


VALID_B64 = base64.b64encode(b"\x00\x01\x02").decode()
ALG = "me.deepmark.audio.aware.20"


# ---------------------------------------------------------------------------
# GET /matches/byBinding
# ---------------------------------------------------------------------------

class TestGetByBinding:
    async def test_success_with_matches(self, client, mock_bindings_col):
        mock_bindings_col.find = lambda *a, **kw: _FakeCursor([
            {"manifestId": "urn:c2pa:m1", "endpoint": None},
        ])
        resp = await client.get(
            "/matches/byBinding",
            params={"value": VALID_B64, "alg": ALG},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["matches"]) == 1
        assert body["matches"][0]["manifestId"] == "urn:c2pa:m1"

    async def test_no_matches(self, client, mock_bindings_col):
        resp = await client.get(
            "/matches/byBinding",
            params={"value": VALID_B64, "alg": ALG},
        )
        assert resp.status_code == 200
        assert resp.json()["matches"] == []

    async def test_empty_value(self, client):
        resp = await client.get(
            "/matches/byBinding",
            params={"value": "", "alg": ALG},
        )
        assert resp.status_code == 400

    async def test_empty_alg(self, client):
        resp = await client.get(
            "/matches/byBinding",
            params={"value": VALID_B64, "alg": ""},
        )
        assert resp.status_code == 400

    async def test_invalid_base64(self, client):
        resp = await client.get(
            "/matches/byBinding",
            params={"value": "not!!valid!!b64", "alg": ALG},
        )
        assert resp.status_code == 400

    async def test_max_results_respected(self, client, mock_bindings_col):
        mock_bindings_col.find = lambda *a, **kw: _FakeCursor([
            {"manifestId": f"urn:c2pa:m{i}", "endpoint": None}
            for i in range(3)
        ])
        resp = await client.get(
            "/matches/byBinding",
            params={"value": VALID_B64, "alg": ALG, "maxResults": 3},
        )
        assert resp.status_code == 200
        assert len(resp.json()["matches"]) == 3


# ---------------------------------------------------------------------------
# POST /matches/byBinding
# ---------------------------------------------------------------------------

class TestPostByBinding:
    async def test_success(self, client, mock_bindings_col):
        mock_bindings_col.find = lambda *a, **kw: _FakeCursor([
            {"manifestId": "urn:c2pa:m1", "endpoint": None},
        ])
        resp = await client.post(
            "/matches/byBinding",
            json={"alg": ALG, "value": VALID_B64},
        )
        assert resp.status_code == 200
        assert len(resp.json()["matches"]) == 1

    async def test_empty_value(self, client):
        resp = await client.post(
            "/matches/byBinding",
            json={"alg": ALG, "value": ""},
        )
        assert resp.status_code == 400

    async def test_empty_alg(self, client):
        resp = await client.post(
            "/matches/byBinding",
            json={"alg": " ", "value": VALID_B64},
        )
        assert resp.status_code == 400

    async def test_invalid_base64(self, client):
        resp = await client.post(
            "/matches/byBinding",
            json={"alg": ALG, "value": "not-b64!"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /matches/byContent
# ---------------------------------------------------------------------------

class TestPostByContent:
    async def test_unsupported_content_type(self, client):
        resp = await client.post(
            "/matches/byContent",
            files={"file": ("image.png", b"\x89PNG", "image/png")},
        )
        assert resp.status_code == 415

    async def test_empty_file(self, client):
        resp = await client.post(
            "/matches/byContent",
            files={"file": ("empty.wav", b"", "audio/wav")},
        )
        assert resp.status_code == 400

    async def test_success(self, client, mock_bindings_col):
        mock_bindings_col.find = lambda *a, **kw: _FakeCursor([
            {"manifestId": "urn:c2pa:m1", "endpoint": None},
        ])
        entry = AlgorithmEntry(
            alg=ALG,
            type="watermark",
            url="http://fake:9004",
        )
        with (
            patch(
                "resolution_api.routers.query.load_catalog",
                return_value=[entry],
            ),
            patch(
                "resolution_api.routers.query.AsyncPluginClient",
            ) as MockClient,
        ):
            instance = AsyncMock()
            instance.detect = AsyncMock(return_value=VALID_B64)
            MockClient.return_value = instance

            resp = await client.post(
                "/matches/byContent",
                files={"file": ("test.wav", b"\x00\x01\x02", "audio/wav")},
            )
        assert resp.status_code == 200
        assert len(resp.json()["matches"]) == 1

    async def test_no_watermark_detected(self, client, mock_bindings_col):
        entry = AlgorithmEntry(alg=ALG, type="watermark", url="http://fake:9004")
        with (
            patch("resolution_api.routers.query.load_catalog", return_value=[entry]),
            patch("resolution_api.routers.query.AsyncPluginClient") as MockClient,
        ):
            instance = AsyncMock()
            instance.detect = AsyncMock(return_value=None)
            MockClient.return_value = instance

            resp = await client.post(
                "/matches/byContent",
                files={"file": ("test.wav", b"\x00\x01", "audio/wav")},
            )
        assert resp.status_code == 200
        assert resp.json()["matches"] == []


# ---------------------------------------------------------------------------
# POST /matches/byReference
# ---------------------------------------------------------------------------

class TestPostByReference:
    async def test_non_https_url(self, client):
        resp = await client.post(
            "/matches/byReference",
            json={
                "referenceUrl": "http://example.com/audio.wav",
                "assetLength": 1000,
            },
        )
        assert resp.status_code == 400
        assert "HTTPS" in resp.json()["detail"]

    async def test_zero_asset_length(self, client):
        resp = await client.post(
            "/matches/byReference",
            json={
                "referenceUrl": "https://example.com/audio.wav",
                "assetLength": 0,
            },
        )
        assert resp.status_code == 400

    async def test_asset_too_large(self, client):
        resp = await client.post(
            "/matches/byReference",
            json={
                "referenceUrl": "https://example.com/audio.wav",
                "assetLength": 200 * 1024 * 1024,
            },
        )
        assert resp.status_code == 400

    async def test_blocked_host_localhost(self, client):
        resp = await client.post(
            "/matches/byReference",
            json={
                "referenceUrl": "https://localhost/audio.wav",
                "assetLength": 1000,
            },
        )
        assert resp.status_code == 400
        assert "blocked" in resp.json()["detail"]

    async def test_blocked_host_127(self, client):
        resp = await client.post(
            "/matches/byReference",
            json={
                "referenceUrl": "https://127.0.0.1/audio.wav",
                "assetLength": 1000,
            },
        )
        assert resp.status_code == 400
