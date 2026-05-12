"""Tests for the YAML catalog loader + plugin dispatcher wrapper."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import httpx
import pytest

from ingestion_api.adapters.dispatcher import (
    BINDING_VALUE_HEADER,
    MEDIA_TYPE_HEADER,
    PluginDispatcher,
    PluginUnavailableError,
)
from ingestion_api.contracts.plugin import PluginEntry
from ingestion_api.core.errors import PluginNotFoundError
from ingestion_api.core.plugin import load_plugin_catalog, resolve_plugin


def _entry(**kw) -> PluginEntry:
    return PluginEntry(
        alg=kw.get("alg", "me.deepmark.audio.vigil.128"),
        type=kw.get("type", "watermark"),
        binding_bits=kw.get("binding_bits", 128),
        media_types=kw.get("media_types", ("audio/wav",)),
        url=kw.get("url", "http://plugin:8000"),
    )


# ---------------------------------------------------------------------------
# Catalog loader
# ---------------------------------------------------------------------------


def test_load_plugin_catalog_returns_empty_for_missing_file(tmp_path: Path):
    assert load_plugin_catalog(tmp_path / "missing.yaml") == []


def test_load_plugin_catalog_parses_well_formed_entries(tmp_path: Path):
    p = tmp_path / "plugins.yaml"
    p.write_text(dedent("""
        plugins:
          - alg: me.deepmark.audio.vigil.128
            type: watermark
            bindingBits: 128
            mediaTypes: ["audio/wav"]
            url: http://watermark-vigil-128:8000
    """))
    entries = load_plugin_catalog(p)
    assert len(entries) == 1
    assert entries[0].alg == "me.deepmark.audio.vigil.128"
    assert entries[0].binding_bits == 128
    assert entries[0].url == "http://watermark-vigil-128:8000"


def test_load_plugin_catalog_accepts_non_byte_aligned_binding_bits(tmp_path: Path):
    """bindingBits doesn't have to be a multiple of 8."""
    p = tmp_path / "plugins.yaml"
    p.write_text(dedent("""
        plugins:
          - alg: weird.width
            type: watermark
            bindingBits: 100
            mediaTypes: ["audio/wav"]
            url: http://x:8000
    """))
    entries = load_plugin_catalog(p)
    assert len(entries) == 1
    assert entries[0].binding_bits == 100


def test_resolve_plugin_raises_for_unknown_alg(tmp_path: Path):
    with pytest.raises(PluginNotFoundError):
        resolve_plugin("nope", catalog=[])


def test_load_plugin_catalog_skips_entries_with_bad_binding_bits(tmp_path: Path):
    """bindingBits is required and must be positive."""
    p = tmp_path / "plugins.yaml"
    p.write_text(dedent("""
        plugins:
          - alg: ok.alg
            type: watermark
            bindingBits: 128
            mediaTypes: ["audio/wav"]
            url: http://ok:8000
          - alg: missing.bits
            type: watermark
            mediaTypes: ["audio/wav"]
            url: http://x:8000
          - alg: zero.bits
            type: watermark
            bindingBits: 0
            mediaTypes: ["audio/wav"]
            url: http://x:8000
          - alg: negative.bits
            type: watermark
            bindingBits: -8
            mediaTypes: ["audio/wav"]
            url: http://x:8000
    """))
    entries = load_plugin_catalog(p)
    assert [e.alg for e in entries] == ["ok.alg"]
    assert entries[0].binding_bits == 128


# ---------------------------------------------------------------------------
# Plugin client
# ---------------------------------------------------------------------------


def _mock_client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, base_url="http://plugin:8000")


def test_plugin_client_embed_generates_value_and_sends_headers():
    """API mints the value and sends it via X-Binding-Value; plugin echoes."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        captured["content_type"] = request.headers.get("content-type")
        captured["media_type"] = request.headers.get(MEDIA_TYPE_HEADER)
        captured["sent_value"] = request.headers.get(BINDING_VALUE_HEADER)
        return httpx.Response(
            200,
            content=b"watermarked-bytes",
            headers={
                BINDING_VALUE_HEADER: captured["sent_value"],  # echo
                "Content-Type": "application/octet-stream",
            },
        )

    with _mock_client(handler) as c:
        plugin = PluginDispatcher(_entry(), client=c)
        result = plugin.embed(media_bytes=b"raw-audio", mime_type="audio/wav")

    # Value is generated, non-empty, and matches what the plugin echoed.
    assert result.binding_value == captured["sent_value"]
    assert result.binding_value
    # 128 bits = 16 bytes -> standard b64 with padding len = 24.
    assert len(result.binding_value) == 24
    assert result.watermarked_bytes == b"watermarked-bytes"
    assert captured["url"] == "http://plugin:8000/embed"
    assert captured["body"] == b"raw-audio"
    assert captured["content_type"] == "application/octet-stream"
    assert captured["media_type"] == "audio/wav"


def test_plugin_client_embed_handles_non_byte_aligned_bits():
    """Width of 100 bits: low 4 bits of the last decoded byte must be zero."""
    import base64

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        v = request.headers.get(BINDING_VALUE_HEADER)
        captured["sent_value"] = v
        return httpx.Response(200, content=b"x", headers={BINDING_VALUE_HEADER: v})

    with _mock_client(handler) as c:
        plugin = PluginDispatcher(_entry(binding_bits=100), client=c)
        result = plugin.embed(media_bytes=b"a", mime_type="audio/wav")

    raw = base64.b64decode(result.binding_value)
    assert len(raw) == 13  # ceil(100 / 8)
    # 4 unused low bits of the last byte must be masked.
    assert raw[-1] & 0x0F == 0


def test_plugin_client_embed_values_are_unique_across_calls():
    """Cryptographic randomness — two calls must not produce the same value."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        v = request.headers.get(BINDING_VALUE_HEADER)
        seen.append(v)
        return httpx.Response(200, content=b"x", headers={BINDING_VALUE_HEADER: v})

    with _mock_client(handler) as c:
        plugin = PluginDispatcher(_entry(), client=c)
        for _ in range(5):
            plugin.embed(media_bytes=b"a", mime_type="audio/wav")

    assert len(set(seen)) == 5


def test_plugin_client_embed_raises_on_echo_mismatch():
    """Plugin that ignores the request header and returns a different value."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"x", headers={BINDING_VALUE_HEADER: "tampered"},
        )

    with _mock_client(handler) as c:
        plugin = PluginDispatcher(_entry(), client=c)
        with pytest.raises(PluginUnavailableError, match="echoed a different"):
            plugin.embed(media_bytes=b"a", mime_type="audio/wav")


def test_plugin_client_embed_raises_when_echo_header_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"bytes")

    with _mock_client(handler) as c:
        plugin = PluginDispatcher(_entry(), client=c)
        with pytest.raises(PluginUnavailableError):
            plugin.embed(media_bytes=b"a", mime_type="audio/wav")


def test_plugin_client_raises_on_5xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="kaboom")

    with _mock_client(handler) as c:
        plugin = PluginDispatcher(_entry(), client=c)
        with pytest.raises(PluginUnavailableError):
            plugin.embed(media_bytes=b"a", mime_type="audio/wav")


def test_plugin_client_rejects_wrong_type():
    fp_entry = _entry(type="fingerprint")
    with _mock_client(lambda r: httpx.Response(200, json={})) as c:
        plugin = PluginDispatcher(fp_entry, client=c)
        with pytest.raises(PluginUnavailableError):
            plugin.embed(media_bytes=b"a", mime_type="audio/wav")


def test_plugin_client_compute_for_fingerprint_sends_media_type():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["media_type"] = request.headers.get(MEDIA_TYPE_HEADER)
        return httpx.Response(200, json={"bindingValue": "abc"})

    with _mock_client(handler) as c:
        plugin = PluginDispatcher(_entry(type="fingerprint"), client=c)
        assert plugin.compute(media_bytes=b"raw", mime_type="audio/wav") == "abc"

    assert captured["body"] == b"raw"
    assert captured["media_type"] == "audio/wav"


def test_plugin_client_detect_returns_none_when_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bindingValue": None})

    with _mock_client(handler) as c:
        plugin = PluginDispatcher(_entry(), client=c)
        assert plugin.detect(media_bytes=b"raw", mime_type="audio/wav") is None
