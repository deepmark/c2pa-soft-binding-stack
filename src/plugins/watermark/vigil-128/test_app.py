"""Unit tests for the vigil-128 plugin (FastAPI app + core algorithm)."""
from __future__ import annotations

import base64
from pathlib import Path

from fastapi.testclient import TestClient

from app import (
    ALG,
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


def test_embed_writes_output_and_returns_binding_value(tmp_path: Path):
    src = tmp_path / "input.wav"
    dst = tmp_path / "watermarked.wav"
    src.write_bytes(b"some audio bytes")

    with TestClient(app) as client:
        r = client.post(
            "/embed",
            json={"input_path": str(src), "output_path": str(dst)},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["bindingValue"] == compute_binding_value(b"some audio bytes")
    assert body["outputPath"] == str(dst)
    assert dst.is_file()
    # Dummy embedder is passthrough.
    assert dst.read_bytes() == src.read_bytes()


def test_embed_404_when_input_missing(tmp_path: Path):
    with TestClient(app) as client:
        r = client.post(
            "/embed",
            json={"input_path": str(tmp_path / "nope.wav"), "output_path": str(tmp_path / "out.wav")},
        )
    assert r.status_code == 400
    assert "not found" in r.json()["detail"]


def test_detect_endpoint(tmp_path: Path):
    src = tmp_path / "input.wav"
    src.write_bytes(b"hello audio")
    with TestClient(app) as client:
        r = client.post("/detect", json={"input_path": str(src)})
    assert r.status_code == 200
    assert r.json()["bindingValue"] == compute_binding_value(b"hello audio")
