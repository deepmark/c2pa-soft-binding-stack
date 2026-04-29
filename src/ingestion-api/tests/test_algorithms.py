"""Tests for the YAML catalog loader + plugin client wrapper."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import httpx
import pytest

from ingestion_api.services.algorithms import (
    AlgorithmEntry,
    AlgorithmNotFoundError,
    PluginClient,
    PluginUnavailableError,
    load_catalog,
    resolve,
)


def _entry(**kw) -> AlgorithmEntry:
    return AlgorithmEntry(
        alg=kw.get("alg", "me.deepmark.audio.vigil.128"),
        type=kw.get("type", "watermark"),
        value_bits=kw.get("value_bits", 128),
        media_types=kw.get("media_types", ("audio/wav",)),
        url=kw.get("url", "http://plugin:8000"),
    )


# ---------------------------------------------------------------------------
# Catalog loader
# ---------------------------------------------------------------------------


def test_load_catalog_returns_empty_for_missing_file(tmp_path: Path):
    assert load_catalog(tmp_path / "missing.yaml") == []


def test_load_catalog_parses_well_formed_entries(tmp_path: Path):
    p = tmp_path / "algorithms.yaml"
    p.write_text(dedent("""
        algorithms:
          - alg: me.deepmark.audio.vigil.128
            type: watermark
            valueBits: 128
            mediaTypes: ["audio/wav"]
            url: http://watermark-vigil-128:8000
    """))
    entries = load_catalog(p)
    assert len(entries) == 1
    assert entries[0].alg == "me.deepmark.audio.vigil.128"
    assert entries[0].url == "http://watermark-vigil-128:8000"


def test_resolve_raises_for_unknown_alg(tmp_path: Path):
    with pytest.raises(AlgorithmNotFoundError):
        resolve("nope", catalog=[])


# ---------------------------------------------------------------------------
# Plugin client
# ---------------------------------------------------------------------------


def _mock_client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, base_url="http://plugin:8000")


def test_plugin_client_embed_round_trip():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"bindingValue": "ZmFrZQ==", "outputPath": "/shared/x"})

    with _mock_client(handler) as c:
        plugin = PluginClient(_entry(), client=c)
        v = plugin.embed(input_path="/shared/in.wav", output_path="/shared/out.wav")

    assert v == "ZmFrZQ=="
    assert captured["url"] == "http://plugin:8000/embed"
    assert "/shared/in.wav" in captured["body"]


def test_plugin_client_raises_on_5xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="kaboom")

    with _mock_client(handler) as c:
        plugin = PluginClient(_entry(), client=c)
        with pytest.raises(PluginUnavailableError):
            plugin.embed(input_path="/x", output_path="/y")


def test_plugin_client_rejects_wrong_type():
    fp_entry = _entry(type="fingerprint")
    with _mock_client(lambda r: httpx.Response(200, json={})) as c:
        plugin = PluginClient(fp_entry, client=c)
        with pytest.raises(PluginUnavailableError):
            plugin.embed(input_path="/x", output_path="/y")


def test_plugin_client_compute_for_fingerprint():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bindingValue": "abc"})

    with _mock_client(handler) as c:
        plugin = PluginClient(_entry(type="fingerprint"), client=c)
        assert plugin.compute(input_path="/x") == "abc"


def test_plugin_client_detect_returns_none_when_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bindingValue": None})

    with _mock_client(handler) as c:
        plugin = PluginClient(_entry(), client=c)
        assert plugin.detect(input_path="/x") is None
