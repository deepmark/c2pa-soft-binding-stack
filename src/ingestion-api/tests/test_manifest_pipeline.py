"""
Integration test for the manifest builder + signing service.

These tests don't need Mongo (they exercise the SDK directly), but they
DO need the ES256 test cert chain + private key under ``credentials/``.
They're skipped automatically when the certs aren't present.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ingestion_api.services.manifest import ManifestBuilderService, SoftBindingSpec
from ingestion_api.services.signing import SigningService
from ingestion_api.utils.hashing import sha256_truncated_b64

BINDING_ALG = "me.deepmark.audio.vigil.128"


def compute_binding_value(b: bytes) -> str:
    """Same derivation as the vigil-128 plugin (sha256[:16] -> base64)."""
    return sha256_truncated_b64(b, n_bits=128)


def watermark_spec(value: str, alg: str = BINDING_ALG) -> SoftBindingSpec:
    return SoftBindingSpec(
        alg=alg, kind="watermark", value=value, related_to_watermark_action=True,
    )

requires_credentials = pytest.mark.skipif(
    True,
    reason="overridden by fixture below",
)


@pytest.fixture
def signing_service(credentials_present: bool) -> SigningService:
    if not credentials_present:
        pytest.skip("credentials/es256_certs.pem + es256_private.key not present")
    return SigningService()


def _read_active_manifest(path: Path) -> dict:
    """Read the signed asset and return the active manifest JSON."""
    from c2pa import Reader
    with Reader(str(path)) as r:
        full = json.loads(r.json())
    label = full["active_manifest"]
    return full["manifests"][label]


def test_signed_wav_is_produced(
    sample_wav_path: Path,
    sample_wav_bytes: bytes,
    tmp_path: Path,
    signing_service: SigningService,
):
    dest = tmp_path / "signed.wav"
    binding = compute_binding_value(sample_wav_bytes)

    builder = ManifestBuilderService(signer=signing_service.signer)
    result = builder.build_and_sign(
        source_bytes=sample_wav_bytes,
        dest_path=dest,
        mime_type="audio/wav",
        soft_bindings=[watermark_spec(binding)],
        title="sample.wav",
    )

    assert dest.is_file(), "signed asset must be written to disk"
    assert dest.stat().st_size > sample_wav_path.stat().st_size, (
        "signed file should be at least as large as the source"
    )
    assert result.manifest_bytes, "manifest bytes should be returned"


def test_signed_manifest_has_opened_watermarked_and_softbinding(
    sample_wav_bytes: bytes,
    tmp_path: Path,
    signing_service: SigningService,
):
    """End-to-end check of the C2PA contents of the signed asset."""
    dest = tmp_path / "signed.wav"
    binding = compute_binding_value(sample_wav_bytes)
    builder = ManifestBuilderService(signer=signing_service.signer)
    builder.build_and_sign(
        source_bytes=sample_wav_bytes,
        dest_path=dest,
        mime_type="audio/wav",
        soft_bindings=[watermark_spec(binding)],
        title="sample.wav",
    )

    active = _read_active_manifest(dest)

    # Parent ingredient (auto-created by the EDIT intent).
    relationships = [i.get("relationship") for i in active.get("ingredients", [])]
    assert "parentOf" in relationships, (
        f"expected a parentOf ingredient, got {relationships}"
    )

    # Actions: c2pa.opened MUST come first, then c2pa.watermarked.bound.
    actions_assertions = [
        a for a in active.get("assertions", [])
        if a.get("label", "").startswith("c2pa.actions")
    ]
    assert actions_assertions, "actions assertion missing"
    actions = actions_assertions[0]["data"]["actions"]
    action_names = [a.get("action") for a in actions]
    assert action_names[0] == "c2pa.opened", (
        f"first action must be c2pa.opened, got {action_names}"
    )
    assert "c2pa.watermarked.bound" in action_names, (
        f"watermarked action missing, got {action_names}"
    )

    # ingredientIds wired correctly on c2pa.opened.
    opened = next(a for a in actions if a.get("action") == "c2pa.opened")
    params = opened.get("parameters", {})
    ids = params.get("ingredientIds") or params.get("ingredients")
    assert ids, f"c2pa.opened must reference the parent ingredient, got {opened}"

    # Soft-binding assertion present, with our alg + 128-bit b64 value.
    sb = next(
        a for a in active.get("assertions", [])
        if a.get("label") == "c2pa.soft-binding"
    )
    assert sb["data"]["alg"] == BINDING_ALG
    blocks = sb["data"]["blocks"]
    assert blocks and blocks[0]["value"] == binding

    # c2pa.watermarked.bound -> related assertion JUMBF URI points at
    # the soft-binding we just emitted (per actions v2 spec).
    bound = next(a for a in actions if a.get("action") == "c2pa.watermarked.bound")
    related = bound.get("parameters", {}).get("relatedAssertions") or []
    assert any(
        "c2pa.soft-binding" in (r.get("url") if isinstance(r, dict) else r)
        for r in related
    ), f"expected relatedAssertions to reference c2pa.soft-binding, got {related}"


def test_manifest_id_can_be_read_back(
    sample_wav_bytes: bytes,
    tmp_path: Path,
    signing_service: SigningService,
):
    """The Reader-derived active manifest URN should be a urn:c2pa:..."""
    dest = tmp_path / "signed.wav"
    binding = compute_binding_value(sample_wav_bytes)
    builder = ManifestBuilderService(signer=signing_service.signer)
    builder.build_and_sign(
        source_bytes=sample_wav_bytes,
        dest_path=dest,
        mime_type="audio/wav",
        soft_bindings=[watermark_spec(binding)],
    )

    from c2pa import Reader
    with Reader(str(dest)) as r:
        full = json.loads(r.json())
    label = full["active_manifest"]
    assert label.startswith("urn:c2pa:"), label
