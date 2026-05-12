"""Tests for fetch.py — GET /manifests/{id}, GET/POST /manifests/{id}/receipts."""
from __future__ import annotations

import io
from unittest.mock import AsyncMock

import pytest


MANIFEST_ID = "urn:c2pa:test-manifest"


# ---------------------------------------------------------------------------
# GET /manifests/{manifestId}
# ---------------------------------------------------------------------------

class TestGetManifest:
    async def test_success_full_store(self, client, mock_manifests_col, mock_fs):
        mock_manifests_col.find_one.return_value = {
            "_id": MANIFEST_ID,
            "manifestStoreFileId": "grid-store-1",
            "activeManifestFileId": "grid-active-1",
        }

        async def fake_download(file_id, buffer):
            buffer.write(b"C2PA-STORE-DATA")

        mock_fs.download_to_stream.side_effect = fake_download

        resp = await client.get(f"/manifests/{MANIFEST_ID}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/c2pa"
        assert resp.content == b"C2PA-STORE-DATA"

    async def test_success_active_manifest(self, client, mock_manifests_col, mock_fs):
        mock_manifests_col.find_one.return_value = {
            "_id": MANIFEST_ID,
            "manifestStoreFileId": "grid-store-1",
            "activeManifestFileId": "grid-active-1",
        }

        async def fake_download(file_id, buffer):
            buffer.write(b"ACTIVE_ONLY")

        mock_fs.download_to_stream.side_effect = fake_download

        resp = await client.get(
            f"/manifests/{MANIFEST_ID}",
            params={"returnActiveManifest": True},
        )
        assert resp.status_code == 200
        assert resp.content == b"ACTIVE_ONLY"

    async def test_not_found(self, client, mock_manifests_col):
        mock_manifests_col.find_one.return_value = None
        resp = await client.get(f"/manifests/{MANIFEST_ID}")
        assert resp.status_code == 404

    async def test_blob_file_id_missing(self, client, mock_manifests_col):
        mock_manifests_col.find_one.return_value = {
            "_id": MANIFEST_ID,
        }
        resp = await client.get(f"/manifests/{MANIFEST_ID}")
        assert resp.status_code == 404
        assert "data not found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /manifests/{manifestId}/receipts
# ---------------------------------------------------------------------------

class TestGetReceipt:
    async def test_success(self, client, mock_manifests_col):
        mock_manifests_col.find_one.return_value = {"_id": MANIFEST_ID}
        resp = await client.get(f"/manifests/{MANIFEST_ID}/receipts")
        assert resp.status_code == 200
        body = resp.json()
        assert body["verified"] is True
        assert body["@type"] == "org.c2pa.manifest-receipt"
        assert body["repository"]["manifestId"] == MANIFEST_ID

    async def test_not_found(self, client, mock_manifests_col):
        mock_manifests_col.find_one.return_value = None
        resp = await client.get(f"/manifests/{MANIFEST_ID}/receipts")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /manifests/{manifestId}/receipts
# ---------------------------------------------------------------------------

class TestPostReceipt:
    async def test_verify_matching_receipt(self, client, mock_manifests_col):
        mock_manifests_col.find_one.return_value = {"_id": MANIFEST_ID}
        resp = await client.post(
            f"/manifests/{MANIFEST_ID}/receipts",
            json={
                "@context": {"c2pa": "https://c2pa.org/ns/"},
                "@type": "org.c2pa.manifest-receipt",
                "repository": {"uri": "http://test/", "manifestId": MANIFEST_ID},
                "anchor": {"uri": "http://test/anchors/abc", "proof": {}},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["verified"] is True
        assert body["error"] is None

    async def test_verify_mismatched_manifest_id(self, client, mock_manifests_col):
        mock_manifests_col.find_one.return_value = {"_id": MANIFEST_ID}
        resp = await client.post(
            f"/manifests/{MANIFEST_ID}/receipts",
            json={
                "@context": {"c2pa": "https://c2pa.org/ns/"},
                "@type": "org.c2pa.manifest-receipt",
                "repository": {"uri": "http://test/", "manifestId": "urn:c2pa:wrong"},
                "anchor": {"uri": "http://test/anchors/abc", "proof": {}},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["verified"] is False
        assert body["error"] is not None

    async def test_manifest_not_found(self, client, mock_manifests_col):
        mock_manifests_col.find_one.return_value = None
        resp = await client.post(
            f"/manifests/{MANIFEST_ID}/receipts",
            json={
                "@context": {},
                "@type": "org.c2pa.manifest-receipt",
                "repository": {"manifestId": MANIFEST_ID},
                "anchor": {},
            },
        )
        assert resp.status_code == 404
