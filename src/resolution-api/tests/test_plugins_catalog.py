"""Tests for the plugins.yaml loader."""
from pathlib import Path
from textwrap import dedent

from resolution_api.services.plugins_catalog import (
    PluginNotFoundError,
    load_plugin_catalog,
    resolve,
)
import pytest


def test_load_plugin_catalog_returns_empty_for_missing_file(tmp_path: Path):
    assert load_plugin_catalog(tmp_path / "missing.yaml") == []


def test_load_plugin_catalog_parses_well_formed_entries(tmp_path: Path):
    p = tmp_path / "plugins.yaml"
    p.write_text(dedent("""
        plugins:
          - alg: me.deepmark.audio.aware.20
            type: watermark
            bindingBits: 20
            mediaTypes: ["audio/wav"]
            url: http://watermark-aware-20:9004
          - alg: org.example.fp.v1
            type: fingerprint
            bindingBits: 64
            mediaTypes: ["audio/mpeg"]
    """))
    entries = load_plugin_catalog(p)
    assert len(entries) == 2
    wm = next(e for e in entries if e.type == "watermark")
    assert wm.alg == "me.deepmark.audio.aware.20"
    assert wm.binding_bits == 20
    assert wm.url == "http://watermark-aware-20:9004"
    assert "audio/wav" in wm.media_types
    fp = next(e for e in entries if e.type == "fingerprint")
    assert fp.alg == "org.example.fp.v1"
    assert fp.url is None


def test_load_plugin_catalog_accepts_non_byte_aligned_binding_bits(tmp_path: Path):
    """bindingBits doesn't have to be a multiple of 8."""
    p = tmp_path / "plugins.yaml"
    p.write_text(dedent("""
        plugins:
          - alg: weird.width
            type: watermark
            bindingBits: 100
            mediaTypes: ["audio/wav"]
    """))
    entries = load_plugin_catalog(p)
    assert len(entries) == 1
    assert entries[0].binding_bits == 100


def test_load_plugin_catalog_skips_malformed_entries(tmp_path: Path):
    p = tmp_path / "plugins.yaml"
    p.write_text(dedent("""
        plugins:
          - alg: ok.alg
            type: watermark
            bindingBits: 20
          - {not_alg: nope}
          - alg: zero.bits
            type: watermark
            bindingBits: 0
          - alg: missing.bits
            type: watermark
    """))
    entries = load_plugin_catalog(p)
    assert len(entries) == 1
    assert entries[0].alg == "ok.alg"
    assert entries[0].binding_bits == 20


def test_resolve_returns_matching_entry(tmp_path: Path):
    p = tmp_path / "plugins.yaml"
    p.write_text(dedent("""
        plugins:
          - alg: me.deepmark.audio.aware.20
            type: watermark
            bindingBits: 20
            url: http://watermark-aware-20:9004
    """))
    catalog = load_plugin_catalog(p)
    entry = resolve("me.deepmark.audio.aware.20", catalog=catalog)
    assert entry.alg == "me.deepmark.audio.aware.20"


def test_resolve_raises_for_unknown_alg(tmp_path: Path):
    p = tmp_path / "plugins.yaml"
    p.write_text(dedent("""
        plugins:
          - alg: me.deepmark.audio.aware.20
            type: watermark
            bindingBits: 20
    """))
    catalog = load_plugin_catalog(p)
    with pytest.raises(PluginNotFoundError):
        resolve("no.such.alg", catalog=catalog)
