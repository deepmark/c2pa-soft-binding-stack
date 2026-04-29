"""
End-to-end orchestrator test (no Mongo, no live plugin, no live resolution API).

Stubs:
- ``PluginClient.embed`` -> writes ``output_path`` (passthrough copy) and
  returns the deterministic 128-bit binding value, matching what the
  real vigil-128 plugin would produce.
- ``ResolutionPushClient.push`` -> records the request and returns a
  ``ResolutionPushResult.OK``.

Requires the ES256 test credentials at the repo root (`credentials/`).
"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from ingestion_api.models.ingestion import (
    IngestionStatus,
    ResolutionPushResult,
    ResolutionPushStatus,
)
from ingestion_api.services import algorithms as algorithms_module
from ingestion_api.services import resolution as resolution_module
from ingestion_api.services.algorithms import AlgorithmEntry
from ingestion_api.services.orchestrator import (
    IngestionInput,
    IngestionService,
    UnsupportedAudioFormatError,
)
from ingestion_api.services.resolution import ResolutionPushClient
from ingestion_api.services.signing import SigningService
from ingestion_api.services.storage import LocalAssetStore
from ingestion_api.utils.hashing import sha256_truncated_b64

BINDING_ALG = "me.deepmark.audio.vigil.128"


def _binding_value(b: bytes) -> str:
    return sha256_truncated_b64(b, n_bits=128)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubPluginClient:
    """Drop-in replacement for the real PluginClient used by the orchestrator."""
    def __init__(self, entry: AlgorithmEntry, **_kwargs):
        self.entry = entry

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def embed(self, *, input_path, output_path, value=None):
        src = Path(input_path)
        dst = Path(output_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return value or _binding_value(src.read_bytes())


class _StubResolutionClient:
    """Always-OK auto-push stub. Records calls for assertions."""
    def __init__(self):
        self.calls = []
        self.enabled = True

    def push(self, req):
        self.calls.append(req)
        return ResolutionPushResult(status=ResolutionPushStatus.OK), "urn:c2pa:stubbed-resolution-id"

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def local_store(tmp_path: Path) -> LocalAssetStore:
    return LocalAssetStore(root=tmp_path / "storage")


@pytest.fixture
def signing_service(credentials_present: bool) -> SigningService:
    if not credentials_present:
        pytest.skip("credentials/es256_certs.pem + es256_private.key not present")
    return SigningService()


@pytest.fixture
def stub_resolution() -> _StubResolutionClient:
    return _StubResolutionClient()


@pytest.fixture
def patched_plugin(monkeypatch):
    """Force ``algorithms.resolve`` to return our local entry, and replace
    ``PluginClient`` (the symbol the orchestrator imports) with the stub."""
    entry = AlgorithmEntry(
        alg=BINDING_ALG,
        type="watermark",
        value_bits=128,
        media_types=("audio/wav",),
        url="http://stubbed:8000",
    )
    monkeypatch.setattr(algorithms_module, "resolve", lambda alg, **_: entry)
    # Patch the symbol the orchestrator pulled into its module namespace
    # (`from .algorithms import PluginClient` makes the orchestrator's
    # PluginClient binding the one to override).
    from ingestion_api.services import orchestrator as orchestrator_module
    monkeypatch.setattr(orchestrator_module, "PluginClient", _StubPluginClient)
    monkeypatch.setattr(
        orchestrator_module,
        "resolve_algorithm",
        lambda alg, **_: entry,
    )
    return entry


@pytest.fixture
def ingestion_service(
    signing_service: SigningService,
    local_store: LocalAssetStore,
    stub_resolution: _StubResolutionClient,
    patched_plugin: AlgorithmEntry,
    tmp_path: Path,
) -> IngestionService:
    return IngestionService(
        signing_service=signing_service,
        local_store=local_store,
        resolution_client=stub_resolution,
        soft_binding_alg=BINDING_ALG,
        shared_volume_path=tmp_path / "shared",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ingest_produces_signed_asset_and_metadata(
    ingestion_service: IngestionService,
    sample_wav_bytes: bytes,
    stub_resolution: _StubResolutionClient,
    tmp_path: Path,
):
    payload = IngestionInput(
        filename="sample.wav",
        content_type="audio/wav",
        data=sample_wav_bytes,
        title="sample test",
    )
    result = asyncio.run(ingestion_service.ingest(payload))

    assert result.signed_asset_path.is_file()
    assert result.signed_asset_path.stat().st_size > 0
    assert result.manifest_bytes_path is not None
    assert result.manifest_bytes_path.is_file()

    record = result.record
    assert record.status is IngestionStatus.OK
    assert record.alg == BINDING_ALG
    assert record.bindingValue == _binding_value(sample_wav_bytes)
    assert record.originalFilename == "sample.wav"
    assert record.originalMimeType == "audio/wav"
    assert record.resolutionPushStatus is ResolutionPushStatus.OK

    # Only signed.<ext>, manifest.c2pa, metadata.json — no original / watermarked.
    base = result.signed_asset_path.parent
    written = sorted(p.name for p in base.iterdir())
    assert written == ["manifest.c2pa", "metadata.json", "signed.wav"], written

    # Metadata sidecar matches the returned record.
    sidecar = json.loads((base / "metadata.json").read_text("utf-8"))
    assert sidecar["alg"] == BINDING_ALG
    assert sidecar["bindingValue"] == record.bindingValue
    assert sidecar["ingestionId"] == record.ingestionId
    assert sidecar["resolutionPushStatus"] == "ok"
    assert "originalAssetPath" not in sidecar

    # Resolution-api stub received the manifest.
    assert len(stub_resolution.calls) == 1
    push = stub_resolution.calls[0]
    assert push.alg == BINDING_ALG
    assert push.binding_value == record.bindingValue
    assert push.manifest_bytes  # non-empty


def test_ingest_uses_resolution_returned_manifest_id(
    ingestion_service: IngestionService,
    sample_wav_bytes: bytes,
):
    """When the resolution API mints its own manifestId, that one wins."""
    result = asyncio.run(
        ingestion_service.ingest(
            IngestionInput(
                filename="sample.wav",
                content_type="audio/wav",
                data=sample_wav_bytes,
            )
        )
    )
    assert result.record.manifestId == "urn:c2pa:stubbed-resolution-id"


def test_ingest_records_failed_push(
    signing_service: SigningService,
    local_store: LocalAssetStore,
    patched_plugin: AlgorithmEntry,
    sample_wav_bytes: bytes,
    tmp_path: Path,
):
    """Resolution-api 5xx -> record FAILED, ingest still succeeds."""

    class _FailingResolution:
        enabled = True

        def push(self, req):
            return (
                ResolutionPushResult(status=ResolutionPushStatus.FAILED, error="boom"),
                None,
            )

        def close(self):
            pass

    svc = IngestionService(
        signing_service=signing_service,
        local_store=local_store,
        resolution_client=_FailingResolution(),
        soft_binding_alg=BINDING_ALG,
        shared_volume_path=tmp_path / "shared",
    )

    result = asyncio.run(
        svc.ingest(
            IngestionInput(
                filename="sample.wav",
                content_type="audio/wav",
                data=sample_wav_bytes,
            )
        )
    )
    assert result.record.status is IngestionStatus.OK
    assert result.record.resolutionPushStatus is ResolutionPushStatus.FAILED
    assert result.record.resolutionPushError == "boom"


def test_ingest_rejects_unsupported_format(
    ingestion_service: IngestionService,
):
    with pytest.raises(UnsupportedAudioFormatError):
        asyncio.run(
            ingestion_service.ingest(
                IngestionInput(
                    filename="not_audio.txt",
                    content_type="text/plain",
                    data=b"hello",
                )
            )
        )


def test_signed_asset_round_trips_through_reader(
    ingestion_service: IngestionService,
    sample_wav_bytes: bytes,
):
    result = asyncio.run(
        ingestion_service.ingest(
            IngestionInput(
                filename="sample.wav",
                content_type="audio/wav",
                data=sample_wav_bytes,
            )
        )
    )

    from c2pa import Reader
    with Reader(str(result.signed_asset_path)) as r:
        full = json.loads(r.json())

    assert full["active_manifest"].startswith("urn:c2pa:")
    active = full["manifests"][full["active_manifest"]]

    actions = next(
        a for a in active["assertions"]
        if a["label"].startswith("c2pa.actions")
    )["data"]["actions"]
    action_names = [a["action"] for a in actions]
    assert action_names[0] == "c2pa.opened"
    assert "c2pa.watermarked.bound" in action_names

    sb = next(
        a for a in active["assertions"] if a["label"] == "c2pa.soft-binding"
    )
    assert sb["data"]["alg"] == BINDING_ALG
    assert sb["data"]["blocks"][0]["value"] == result.record.bindingValue
