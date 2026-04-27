"""Algorithm registry behaviour."""
from soft_binding_api.services.registry import (
    register_fingerprint,
    register_watermark,
    resolve_fingerprint,
    resolve_watermark_detector,
    resolve_watermark_embedder,
)


def test_register_and_resolve_watermark():
    def fake_embed(b: bytes, v: str) -> bytes: return b + v.encode()
    def fake_detect(b: bytes) -> str | None: return "deadbeef"

    register_watermark("test.wm.v1", embed=fake_embed, detect=fake_detect)

    assert resolve_watermark_embedder("test.wm.v1") is fake_embed
    assert resolve_watermark_detector("test.wm.v1") is fake_detect


def test_register_and_resolve_fingerprint():
    def fake_fp(b: bytes) -> str: return "abc"

    register_fingerprint("test.fp.v1", fake_fp)

    assert resolve_fingerprint("test.fp.v1") is fake_fp


def test_unknown_alg_returns_none():
    assert resolve_watermark_embedder("nope") is None
    assert resolve_watermark_detector("nope") is None
    assert resolve_fingerprint("nope") is None
