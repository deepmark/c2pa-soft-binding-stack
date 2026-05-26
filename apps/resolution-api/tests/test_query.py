"""Tests for query.py — GET/POST /matches/byBinding, POST /matches/byContent, POST /matches/byReference."""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock, patch

import pytest

from resolution_api.core.config import settings
from resolution_api.services.plugins_catalog import PluginEntry


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

    async def test_file_exceeds_max_upload_size(self, client):
        with patch.object(settings, "max_upload_size_bytes", 10):
            resp = await client.post(
                "/matches/byContent",
                files={"file": ("big.wav", b"\x00" * 20, "audio/wav")},
            )
        assert resp.status_code == 413
        assert "exceeds maximum allowed size" in resp.json()["detail"]

    async def test_success(self, client, mock_bindings_col):
        mock_bindings_col.find = lambda *a, **kw: _FakeCursor([
            {"manifestId": "urn:c2pa:m1", "endpoint": None},
        ])
        entry = PluginEntry(
            alg=ALG,
            type="watermark",
            url="http://fake:9004",
        )
        with (
            patch(
                "resolution_api.routers.query.load_all_plugins",
                new_callable=AsyncMock,
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
        entry = PluginEntry(alg=ALG, type="watermark", url="http://fake:9004")
        with (
            patch(
                "resolution_api.routers.query.load_all_plugins",
                new_callable=AsyncMock,
                return_value=[entry],
            ),
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

    async def test_no_content_type_header(self, client):
        resp = await client.post(
            "/matches/byContent",
            files={"file": ("test.wav", b"\x00\x01", "")},
        )
        assert resp.status_code == 415

    async def test_plugin_unavailable_with_explicit_alg(self, client):
        from resolution_api.services.plugins_catalog import PluginUnavailableError

        entry = PluginEntry(alg=ALG, type="watermark", url="http://fake:9004")
        with (
            patch(
                "resolution_api.routers.query.resolve",
                new_callable=AsyncMock,
                return_value=entry,
            ),
            patch("resolution_api.routers.query.AsyncPluginClient") as MockClient,
        ):
            instance = AsyncMock()
            instance.detect = AsyncMock(side_effect=PluginUnavailableError("down"))
            MockClient.return_value = instance

            resp = await client.post(
                "/matches/byContent",
                files={"file": ("test.wav", b"\x00\x01\x02", "audio/wav")},
                params={"alg": ALG},
            )
        assert resp.status_code == 500
        assert "detection failed" in resp.json()["detail"].lower()


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

    async def test_negative_asset_length(self, client):
        resp = await client.post(
            "/matches/byReference",
            json={
                "referenceUrl": "https://example.com/audio.wav",
                "assetLength": -1,
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
        assert "exceeds maximum" in resp.json()["detail"]

    async def test_blocked_metadata_host(self, client):
        resp = await client.post(
            "/matches/byReference",
            json={
                "referenceUrl": "https://metadata.google.internal/computeMetadata",
                "assetLength": 1000,
            },
        )
        assert resp.status_code == 400
        assert "blocked" in resp.json()["detail"]

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

    async def test_blocked_link_local_169_254(self, client):
        resp = await client.post(
            "/matches/byReference",
            json={
                "referenceUrl": "https://169.254.169.254/latest/meta-data",
                "assetLength": 1000,
            },
        )
        assert resp.status_code == 400
        assert "blocked" in resp.json()["detail"]

    async def test_unresolvable_hostname(self, client):
        with patch(
            "resolution_api.routers.query._validate_and_resolve_url",
            side_effect=__import__("fastapi").HTTPException(
                status_code=400,
                detail="Invalid request body: referenceUrl hostname could not be resolved",
            ),
        ):
            resp = await client.post(
                "/matches/byReference",
                json={
                    "referenceUrl": "https://this-does-not-exist-xyz.invalid/f.wav",
                    "assetLength": 1000,
                },
            )
        assert resp.status_code == 400
        assert "could not be resolved" in resp.json()["detail"]

    async def test_redirect_rejected(self, client, mock_bindings_col):
        import httpx

        async def fake_validate(url_str):
            return "93.184.216.34", 443, "example.com"

        def redirect_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(301, headers={"Location": "https://evil.com/"})

        transport = httpx.MockTransport(redirect_handler)

        with (
            patch("resolution_api.routers.query._validate_and_resolve_url", side_effect=fake_validate),
            patch("resolution_api.routers.query._make_pinned_transport", return_value=transport),
        ):
            resp = await client.post(
                "/matches/byReference",
                json={
                    "referenceUrl": "https://example.com/audio.wav",
                    "assetLength": 1000,
                },
            )
        assert resp.status_code == 400
        assert "redirect" in resp.json()["detail"]
