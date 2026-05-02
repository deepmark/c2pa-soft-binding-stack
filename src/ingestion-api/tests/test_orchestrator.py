"""
End-to-end orchestrator test (no Mongo, no live plugin, no live resolution API).

Stubs:
- ``PluginClient.embed`` -> returns an ``EmbedResult`` carrying the
  passthrough watermarked bytes + the deterministic 128-bit binding
  value, matching what the real vigil-128 plugin would produce.
- ``ResolutionPushClient.push`` -> records the request and returns a
  ``ResolutionPushResult.OK``.

Requires the ES256 test credentials at the repo root (`credentials/`).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ingestion_api.models.ingestion import (
    IngestionStatus,
    ResolutionPushResult,
    ResolutionPushStatus,
)
from ingestion_api.services import algorithms as algorithms_module
from ingestion_api.services.algorithms import AlgorithmEntry, EmbedResult
from ingestion_api.services.artifact_store import ArtifactStore
from ingestion_api.services.orchestrator import (
    IngestionInput,
    IngestionService,
    UnsupportedAudioFormatError,
)
from ingestion_api.services.record_repository import InMemoryIngestionRecordRepository
from ingestion_api.services.signing import SigningService
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

    def embed(self, *, audio_bytes: bytes, value: str | None = None) -> EmbedResult:
        return EmbedResult(
            binding_value=value or _binding_value(audio_bytes),
            watermarked_bytes=audio_bytes,
        )

    def compute(self, *, audio_bytes: bytes) -> str:
        # Stand-in for a fingerprint plugin: pure read, deterministic value.
        return "fp:" + _binding_value(audio_bytes)


class _StubResolutionClient:
    """Always-OK auto-push stub. Records calls for assertions."""
    def __init__(self):
        self.calls = []
        self.enabled = True

    def push(self, req):
        self.calls.append(req)
        return ResolutionPushResult(status=ResolutionPushStatus.OK)

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def artifacts(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(root=tmp_path / "storage")


@pytest.fixture
def records() -> InMemoryIngestionRecordRepository:
    return InMemoryIngestionRecordRepository()


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
    artifacts: ArtifactStore,
    records: InMemoryIngestionRecordRepository,
    stub_resolution: _StubResolutionClient,
    patched_plugin: AlgorithmEntry,
) -> IngestionService:
    return IngestionService(
        signing_service=signing_service,
        artifacts=artifacts,
        records=records,
        resolution_client=stub_resolution,
        soft_binding_algs=[BINDING_ALG],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ingest_produces_signed_asset_and_record(
    ingestion_service: IngestionService,
    records: InMemoryIngestionRecordRepository,
    sample_wav_bytes: bytes,
    stub_resolution: _StubResolutionClient,
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
    assert len(record.softBindings) == 1
    only = record.softBindings[0]
    assert only.alg == BINDING_ALG
    assert only.kind == "watermark"
    assert only.bindingValue == _binding_value(sample_wav_bytes)
    assert record.originalFilename == "sample.wav"
    assert record.originalMimeType == "audio/wav"
    assert record.resolutionPushStatus is ResolutionPushStatus.OK

    # Only binary artifacts on disk now — record metadata moved to Mongo.
    base = result.signed_asset_path.parent
    written = sorted(p.name for p in base.iterdir())
    assert written == ["manifest.c2pa", "signed.wav"], written

    # Record persisted into the repository and round-trips identically.
    persisted = asyncio.run(records.get(record.ingestionId))
    assert persisted is not None
    assert persisted.ingestionId == record.ingestionId
    assert persisted.softBindings == record.softBindings
    assert persisted.resolutionPushStatus is ResolutionPushStatus.OK

    # Resolution-api stub received the manifest + a binding-pair list.
    assert len(stub_resolution.calls) == 1
    push = stub_resolution.calls[0]
    assert [(b.alg, b.binding_value) for b in push.bindings] == [
        (BINDING_ALG, only.bindingValue),
    ]
    assert push.manifest_bytes  # non-empty


def test_ingest_manifest_id_matches_signed_asset(
    ingestion_service: IngestionService,
    sample_wav_bytes: bytes,
):
    """Stored manifestId is exactly the active_manifest URN inside the signed file."""
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
        active_manifest = json.loads(r.json())["active_manifest"]
    assert result.record.manifestId == active_manifest
    assert active_manifest.startswith("urn:c2pa:")


def test_ingest_records_failed_push(
    signing_service: SigningService,
    artifacts: ArtifactStore,
    records: InMemoryIngestionRecordRepository,
    patched_plugin: AlgorithmEntry,
    sample_wav_bytes: bytes,
):
    """Resolution-api 5xx -> record FAILED, ingest still succeeds."""

    class _FailingResolution:
        enabled = True

        def push(self, req):
            return ResolutionPushResult(status=ResolutionPushStatus.FAILED, error="boom")

        def close(self):
            pass

    svc = IngestionService(
        signing_service=signing_service,
        artifacts=artifacts,
        records=records,
        resolution_client=_FailingResolution(),
        soft_binding_algs=[BINDING_ALG],
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
    assert sb["data"]["blocks"][0]["value"] == result.record.softBindings[0].bindingValue


def test_ingest_with_watermark_plus_fingerprint(
    monkeypatch,
    signing_service: SigningService,
    artifacts: ArtifactStore,
    records: InMemoryIngestionRecordRepository,
    stub_resolution: _StubResolutionClient,
    sample_wav_bytes: bytes,
):
    """Multi-alg pipeline: one watermark + one fingerprint, both surfaced."""
    FP_ALG = "me.deepmark.audio.fp.stub"
    catalog = {
        BINDING_ALG: AlgorithmEntry(
            alg=BINDING_ALG,
            type="watermark",
            value_bits=128,
            media_types=("audio/wav",),
            url="http://stubbed:8000",
        ),
        FP_ALG: AlgorithmEntry(
            alg=FP_ALG,
            type="fingerprint",
            value_bits=128,
            media_types=("audio/wav",),
            url="http://stubbed:8001",
        ),
    }
    monkeypatch.setattr(algorithms_module, "resolve", lambda alg, **_: catalog[alg])
    from ingestion_api.services import orchestrator as orchestrator_module
    monkeypatch.setattr(orchestrator_module, "PluginClient", _StubPluginClient)
    monkeypatch.setattr(
        orchestrator_module, "resolve_algorithm", lambda alg, **_: catalog[alg],
    )

    svc = IngestionService(
        signing_service=signing_service,
        artifacts=artifacts,
        records=records,
        resolution_client=stub_resolution,
        soft_binding_algs=[BINDING_ALG, FP_ALG],
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

    # Record carries both bindings, in input order.
    kinds = [(b.alg, b.kind) for b in result.record.softBindings]
    assert kinds == [(BINDING_ALG, "watermark"), (FP_ALG, "fingerprint")]

    # Publisher saw both pairs, against the same /manifests call.
    assert len(stub_resolution.calls) == 1
    pushed = [(b.alg, b.binding_value) for b in stub_resolution.calls[0].bindings]
    assert pushed == [
        (BINDING_ALG, result.record.softBindings[0].bindingValue),
        (FP_ALG, result.record.softBindings[1].bindingValue),
    ]

    # Signed manifest has two soft-binding assertions, suffixed per
    # C2PA labelling rules. The Builder auto-suffixes the second; we
    # just verify both labels and both algs are present.
    from c2pa import Reader
    with Reader(str(result.signed_asset_path)) as r:
        full = json.loads(r.json())
    active = full["manifests"][full["active_manifest"]]

    sb_assertions = [
        a for a in active["assertions"] if a["label"].startswith("c2pa.soft-binding")
    ]
    sb_by_label = {a["label"]: a["data"]["alg"] for a in sb_assertions}
    assert sb_by_label == {
        "c2pa.soft-binding": BINDING_ALG,
        "c2pa.soft-binding__1": FP_ALG,
    }
    # Manifest validates cleanly (no hashedURI mismatch from the suffix
    # — regression guard for a real bug we hit during this refactor).
    assert full["validation_state"] == "Valid"
