"""Unit tests for the vigil-128 plugin (FastAPI app + core algorithm)."""
from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from app import (
    ALG,
    BINDING_VALUE_HEADER,
    TYPE,
    VALUE_BITS,
    _detect_bytes,
    _embed_bytes,
    app,
    compute_binding_value,
)


def test_binding_value_is_deterministic():
    a = b"hello world"
    assert compute_binding_value(a) == compute_binding_value(a)


def test_binding_value_changes_with_input():
    assert compute_binding_value(b"a") != compute_binding_value(b"b")


def test_binding_value_is_128_bits_b64():
    v = compute_binding_value(b"some audio bytes")
    raw = base64.b64decode(v)
    assert len(raw) * 8 == VALUE_BITS == 128
    assert len(v) == 24


def test_embed_is_passthrough_for_dummy():
    src = b"\x00\x01\x02\x03"
    out = _embed_bytes(src, "ZmFrZQ==")
    assert out == src


def test_detect_round_trips_through_embed():
    src = b"test audio payload"
    v = compute_binding_value(src)
    embedded = _embed_bytes(src, v)
    recovered = _detect_bytes(embedded)
    assert recovered == v


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
    assert body["valueBits"] == 128
    assert "audio/wav" in body["mediaTypes"]


def test_health_endpoint():
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_embed_returns_watermarked_bytes_and_binding_header():
    src = b"some audio bytes"
    with TestClient(app) as client:
        r = client.post(
            "/embed",
            content=src,
            headers={"Content-Type": "application/octet-stream"},
        )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert r.headers[BINDING_VALUE_HEADER] == compute_binding_value(src)
    # Dummy embedder is passthrough.
    assert r.content == src


def test_embed_honours_caller_provided_binding_value():
    src = b"audio"
    override = "Y2FsbGVyT3ZlcnJpZGU="
    with TestClient(app) as client:
        r = client.post(
            "/embed",
            content=src,
            headers={
                "Content-Type": "application/octet-stream",
                BINDING_VALUE_HEADER: override,
            },
        )
    assert r.status_code == 200
    assert r.headers[BINDING_VALUE_HEADER] == override


def test_embed_400_on_empty_body():
    with TestClient(app) as client:
        r = client.post(
            "/embed",
            content=b"",
            headers={"Content-Type": "application/octet-stream"},
        )
    assert r.status_code == 400


def test_detect_endpoint():
    src = b"hello audio"
    with TestClient(app) as client:
        r = client.post(
            "/detect",
            content=src,
            headers={"Content-Type": "application/octet-stream"},
        )
    assert r.status_code == 200
    assert r.json()["bindingValue"] == compute_binding_value(src)


def test_detect_400_on_empty_body():
    with TestClient(app) as client:
        r = client.post(
            "/detect",
            content=b"",
            headers={"Content-Type": "application/octet-stream"},
        )
    assert r.status_code == 400
