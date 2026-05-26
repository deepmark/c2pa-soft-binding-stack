"""Tests for store.py — POST/PUT /bindings, POST /manifests, DELETE /manifests/{id}."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

ALG = "me.deepmark.audio.aware.20"


# ---------------------------------------------------------------------------
# POST /bindings (upsert — idempotent)
# ---------------------------------------------------------------------------

class TestPostBindings:
    async def test_success(self, client, mock_manifests_col, mock_bindings_col):
        mock_manifests_col.find_one.return_value = {"_id": "urn:c2pa:m1"}
        resp = await client.post("/bindings", json={
            "alg": ALG,
            "bindingValue": "abc123",
            "manifestId": "urn:c2pa:m1",
        })
        assert resp.status_code == 204
        mock_bindings_col.update_one.assert_awaited_once()

    async def test_idempotent_rebind(self, client, mock_manifests_col, mock_bindings_col):
        mock_manifests_col.find_one.return_value = {"_id": "urn:c2pa:m1"}
        resp = await client.post("/bindings", json={
            "alg": ALG,
            "bindingValue": "abc123",
            "manifestId": "urn:c2pa:m1",
        })
        assert resp.status_code == 204

    async def test_manifest_not_found(self, client, mock_manifests_col):
        mock_manifests_col.find_one.return_value = None
        resp = await client.post("/bindings", json={
            "alg": ALG,
            "bindingValue": "abc123",
            "manifestId": "urn:c2pa:missing",
        })
        assert resp.status_code == 404

    async def test_empty_alg(self, client):
        resp = await client.post("/bindings", json={
            "alg": "  ",
            "bindingValue": "abc123",
            "manifestId": "urn:c2pa:m1",
        })
        assert resp.status_code == 400

    async def test_empty_binding_value(self, client):
        resp = await client.post("/bindings", json={
            "alg": ALG,
            "bindingValue": "",
            "manifestId": "urn:c2pa:m1",
        })
        assert resp.status_code == 400

    async def test_empty_manifest_id(self, client):
        resp = await client.post("/bindings", json={
            "alg": ALG,
            "bindingValue": "abc123",
            "manifestId": " ",
        })
        assert resp.status_code == 400

    async def test_alg_max_length_exceeded(self, client):
        resp = await client.post("/bindings", json={
            "alg": "a" * 257,
            "bindingValue": "abc123",
            "manifestId": "urn:c2pa:m1",
        })
        assert resp.status_code == 422

    async def test_manifest_id_max_length_exceeded(self, client):
        resp = await client.post("/bindings", json={
            "alg": ALG,
            "bindingValue": "abc123",
            "manifestId": "x" * 513,
        })
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# PUT /bindings
# ---------------------------------------------------------------------------

class TestPutBindings:
    async def test_success(self, client, mock_manifests_col, mock_bindings_col):
        mock_manifests_col.find_one.return_value = {"_id": "urn:c2pa:m2"}
        update_result = AsyncMock()
        update_result.matched_count = 1
        mock_bindings_col.update_one.return_value = update_result
        resp = await client.put("/bindings", json={
            "alg": ALG,
            "bindingValue": "abc123",
            "manifestId": "urn:c2pa:m2",
        })
        assert resp.status_code == 204

    async def test_manifest_not_found(self, client, mock_manifests_col):
        mock_manifests_col.find_one.return_value = None
        resp = await client.put("/bindings", json={
            "alg": ALG,
            "bindingValue": "abc123",
            "manifestId": "urn:c2pa:missing",
        })
        assert resp.status_code == 404

    async def test_binding_not_found(self, client, mock_manifests_col, mock_bindings_col):
        mock_manifests_col.find_one.return_value = {"_id": "urn:c2pa:m2"}
        update_result = AsyncMock()
        update_result.matched_count = 0
        mock_bindings_col.update_one.return_value = update_result
        resp = await client.put("/bindings", json={
            "alg": ALG,
            "bindingValue": "no-such-binding",
            "manifestId": "urn:c2pa:m2",
        })
        assert resp.status_code == 404
        assert "Soft binding value not found" in resp.json()["detail"]

    async def test_empty_alg(self, client):
        resp = await client.put("/bindings", json={
            "alg": "",
            "bindingValue": "abc123",
            "manifestId": "urn:c2pa:m1",
        })
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /manifests
# ---------------------------------------------------------------------------

class TestPostManifests:
    async def test_success_new_manifest(self, client, mock_manifests_col, mock_fs):
        mock_manifests_col.find_one.return_value = None
        with patch(
            "resolution_api.routers.store._extract_manifest_id",
            return_value="urn:c2pa:new-manifest",
        ):
            resp = await client.post(
                "/manifests",
                content=b"\x00\x01\x02",
                headers={"Content-Type": "application/c2pa"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["manifestId"] == "urn:c2pa:new-manifest"
        assert mock_fs.upload_from_stream.await_count == 2
        mock_manifests_col.insert_one.assert_awaited_once()

    async def test_idempotent_re_push(self, client, mock_manifests_col, mock_fs):
        mock_manifests_col.find_one.return_value = {"_id": "urn:c2pa:existing"}
        with patch(
            "resolution_api.routers.store._extract_manifest_id",
            return_value="urn:c2pa:existing",
        ):
            resp = await client.post(
                "/manifests",
                content=b"\x00\x01\x02",
                headers={"Content-Type": "application/c2pa"},
            )
        assert resp.status_code == 200
        mock_fs.upload_from_stream.assert_not_awaited()
        mock_manifests_col.insert_one.assert_not_awaited()

    async def test_empty_body(self, client):
        resp = await client.post(
            "/manifests",
            content=b"",
            headers={"Content-Type": "application/c2pa"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /manifests/{manifestId}
# ---------------------------------------------------------------------------

class TestDeleteManifest:
    async def test_success(self, client, mock_manifests_col, mock_bindings_col, mock_fs):
        mock_manifests_col.find_one.return_value = {
            "_id": "urn:c2pa:m1",
            "manifestStoreFileId": "grid-store-1",
            "activeManifestFileId": "grid-active-1",
        }
        resp = await client.delete("/manifests/urn:c2pa:m1")
        assert resp.status_code == 204
        mock_manifests_col.delete_one.assert_awaited_once()
        mock_bindings_col.delete_many.assert_awaited_once()
        assert mock_fs.delete.await_count == 2

    async def test_not_found(self, client, mock_manifests_col):
        mock_manifests_col.find_one.return_value = None
        resp = await client.delete("/manifests/urn:c2pa:nonexistent")
        assert resp.status_code == 404

    async def test_gridfs_blob_missing(self, client, mock_manifests_col, mock_fs):
        mock_manifests_col.find_one.return_value = {
            "_id": "urn:c2pa:m1",
            "manifestStoreFileId": "grid-store-1",
        }
        mock_fs.delete.side_effect = Exception("blob gone")
        resp = await client.delete("/manifests/urn:c2pa:m1")
        assert resp.status_code == 204
