"""Unit tests for the vigil-128 plugin (FastAPI app + core algorithm)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import (
    _DUMMY_EMBED_MAP,
    ALG,
    BINDING_BITS,
    BINDING_VALUE_HEADER,
    MEDIA_TYPE_HEADER,
    TYPE,
    _detect_bytes,
    _embed_bytes,
    app,
)


@pytest.fixture(autouse=True)
def _reset_dummy_map():
    """Side-channel map is process-global; wipe between tests."""
    _DUMMY_EMBED_MAP.clear()
    yield
    _DUMMY_EMBED_MAP.clear()


def test_binding_bits_is_128():
    assert BINDING_BITS == 128


def test_embed_is_passthrough_for_dummy():
    src = b"\x00\x01\x02\x03"
    out = _embed_bytes(src, "ZmFrZQ==")
    assert out == src


def test_detect_round_trips_through_embed():
    src = b"test audio payload"
    value = "Y2FsbGVyT3ZlcnJpZGU="
    embedded = _embed_bytes(src, value)
    assert _detect_bytes(embedded) == value


def test_detect_returns_none_for_unembedded_bytes():
    assert _detect_bytes(b"never seen these bytes") is None


def test_alg_id_is_canonical():
    assert ALG == "me.deepmark.audio.vigil.128"
    assert TYPE == "watermark"


def test_info_endpoint():
    with TestClient(app) as client:
        r = client.get("/info")
    assert r.status_code == 200
    body = r.json()
    assert body["alg"] == ALG
    assert body["type"] == "watermark"
    assert body["bindingBits"] == 128
    assert "audio/wav" in body["mediaTypes"]


def test_health_endpoint():
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_embed_requires_binding_value_header():
    with TestClient(app) as client:
        r = client.post(
            "/embed",
            content=b"audio",
            headers={
                "Content-Type": "application/octet-stream",
                MEDIA_TYPE_HEADER: "audio/wav",
            },
        )
    assert r.status_code == 400
    assert "X-Binding-Value" in r.json()["detail"]


def test_embed_echoes_caller_provided_binding_value():
    src = b"audio"
    value = "Y2FsbGVyT3ZlcnJpZGU="
    with TestClient(app) as client:
        r = client.post(
            "/embed",
            content=src,
            headers={
                "Content-Type": "application/octet-stream",
                MEDIA_TYPE_HEADER: "audio/wav",
                BINDING_VALUE_HEADER: value,
            },
        )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert r.headers[BINDING_VALUE_HEADER] == value
    # Dummy embedder is passthrough.
    assert r.content == src


def test_embed_400_on_empty_body():
    with TestClient(app) as client:
        r = client.post(
            "/embed",
            content=b"",
            headers={
                "Content-Type": "application/octet-stream",
                BINDING_VALUE_HEADER: "x",
            },
        )
    assert r.status_code == 400


def test_detect_endpoint_round_trips_after_embed():
    src = b"hello audio"
    value = "Y2FsbGVyT3ZlcnJpZGU="
    with TestClient(app) as client:
        embed = client.post(
            "/embed",
            content=src,
            headers={
                "Content-Type": "application/octet-stream",
                BINDING_VALUE_HEADER: value,
            },
        )
        assert embed.status_code == 200

        detect = client.post(
            "/detect",
            content=embed.content,
            headers={"Content-Type": "application/octet-stream"},
        )
    assert detect.status_code == 200
    assert detect.json()["bindingValue"] == value


def test_detect_returns_null_for_unknown_bytes():
    with TestClient(app) as client:
        r = client.post(
            "/detect",
            content=b"never embedded",
            headers={"Content-Type": "application/octet-stream"},
        )
    assert r.status_code == 200
    assert r.json()["bindingValue"] is None


def test_detect_400_on_empty_body():
    with TestClient(app) as client:
        r = client.post(
            "/detect",
            content=b"",
            headers={"Content-Type": "application/octet-stream"},
        )
    assert r.status_code == 400
