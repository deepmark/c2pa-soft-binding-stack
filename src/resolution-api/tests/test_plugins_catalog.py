"""Tests for the plugins.yaml loader."""
from pathlib import Path
from textwrap import dedent

from resolution_api.services.plugins_catalog import load_plugin_catalog


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
          - alg: org.example.fp.v1
            type: fingerprint
            bindingBits: 64
            mediaTypes: ["audio/mpeg"]
    """))
    entries = load_plugin_catalog(p)
    assert len(entries) == 2
    wm = next(e for e in entries if e.type == "watermark")
    assert wm.alg == "me.deepmark.audio.vigil.128"
    assert wm.binding_bits == 128
    assert wm.url == "http://watermark-vigil-128:8000"
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
            bindingBits: 128
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
    assert entries[0].binding_bits == 128
