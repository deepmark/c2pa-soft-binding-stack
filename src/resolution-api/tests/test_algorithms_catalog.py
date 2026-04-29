"""Tests for the algorithms.yaml loader."""
from pathlib import Path
from textwrap import dedent

from resolution_api.services.algorithms_catalog import load_catalog


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
          - alg: org.example.fp.v1
            type: fingerprint
            valueBits: 64
            mediaTypes: ["audio/mpeg"]
    """))
    entries = load_catalog(p)
    assert len(entries) == 2
    wm = next(e for e in entries if e.type == "watermark")
    assert wm.alg == "me.deepmark.audio.vigil.128"
    assert wm.value_bits == 128
    assert wm.url == "http://watermark-vigil-128:8000"
    assert "audio/wav" in wm.media_types
    fp = next(e for e in entries if e.type == "fingerprint")
    assert fp.alg == "org.example.fp.v1"
    assert fp.url is None


def test_load_catalog_skips_malformed_entries(tmp_path: Path):
    p = tmp_path / "algorithms.yaml"
    p.write_text(dedent("""
        algorithms:
          - alg: ok.alg
            type: watermark
          - {not_alg: nope}
    """))
    entries = load_catalog(p)
    assert len(entries) == 1
    assert entries[0].alg == "ok.alg"
