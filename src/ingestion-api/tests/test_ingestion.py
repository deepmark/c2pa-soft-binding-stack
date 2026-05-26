"""
End-to-end IngestionService test (no Mongo, no live plugin, no live resolution API).

Stubs:
- ``PluginDispatcher.embed`` -> passthrough watermarked bytes + a stable
  test-derived binding value. In production the *real* PluginDispatcher
  mints the value via ``secrets`` and sends it to the plugin; the stub
  bypasses both sides for deterministic assertions.
- ``ResolutionPushClient.push`` -> records the request and returns a
  ``ResolutionPushOutput.OK``.

Requires the ES256 test credentials at the repo root (`credentials/`).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ingestion_api.adapters.dispatcher import (
    WatermarkOutput,
    PluginUnavailableError,
    reset_plugin_info_cache,
)
from ingestion_api.contracts.plugin import PluginEntry
from ingestion_api.contracts.ingestion import IngestionRequest
from ingestion_api.core.errors import (
    IngestionError,
    InvalidAlgRequestError,
    UnsupportedMediaError,
)
from ingestion_api.models.enums import (
    FailureStage,
    MediaType,
    ResolutionPushStatus,
)
from ingestion_api.models.responses import ResolutionPushOutput
from ingestion_api.repositories.artifacts import ArtifactStore
from ingestion_api.repositories.ingestions import (
    InMemoryFailedIngestionRepository,
    InMemoryIngestionRecordRepository,
)
from ingestion_api.services.ingestion import IngestionService
from ingestion_api.services.manifest import ManifestBuilderService
from ingestion_api.services.signing import SigningService
from ingestion_api.utils.hashing import sha256_hex
from _helpers import stub_binding_value as _binding_value

from unittest.mock import patch, AsyncMock
from ingestion_api.core.errors import PluginNotFoundError as _PluginNotFoundError


def _mock_resolve_from_catalog(catalog: list[PluginEntry]):
    """Return an async mock of resolve_plugin that looks up from a list."""
    async def _resolve(alg: str):
        for entry in catalog:
            if entry.alg == alg:
                return entry
        raise _PluginNotFoundError(f"alg={alg!r} not found")
    return _resolve

BINDING_ALG = "me.deepmark.audio.aware.20"


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubPluginDispatcher:
    """Drop-in replacement for the real PluginDispatcher used by the pipeline.

    Returns a deterministic value derived from the input bytes so tests
    can assert on it. The real dispatcher mints a random value; we stub
    past that so assertions stay stable across runs.
    """
    def __init__(self, entry: PluginEntry, **_kwargs):
        self.entry = entry
        self.last_mime_type: str | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def embed(self, *, media_bytes: bytes, mime_type: str) -> WatermarkOutput:
        self.last_mime_type = mime_type
        return WatermarkOutput(
            binding_value=_binding_value(media_bytes),
            watermarked_bytes=media_bytes,
        )

    def compute(self, *, media_bytes: bytes, mime_type: str) -> str:
        self.last_mime_type = mime_type
        return "fp:" + _binding_value(media_bytes)

    def info_cached(self) -> dict:
        return {
            "alg": self.entry.alg,
            "type": self.entry.type,
            "bindingBits": self.entry.binding_bits,
            "version": "stub-0.0.1",
        }


class _StubResolutionClient:
    """Always-OK auto-push stub. Records calls for assertions."""
    def __init__(self):
        self.calls = []
        self.request_ids = []
        self.enabled = True

    def push(self, req, *, request_id=None):
        self.calls.append(req)
        self.request_ids.append(request_id)
        return ResolutionPushOutput(status=ResolutionPushStatus.OK)

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
def failed_records() -> InMemoryFailedIngestionRepository:
    return InMemoryFailedIngestionRepository()


@pytest.fixture(autouse=True)
def _reset_plugin_info_cache():
    """The plugin info cache is process-global; wipe between tests."""
    reset_plugin_info_cache()
    yield
    reset_plugin_info_cache()


@pytest.fixture
def signing_service(credentials_present: bool) -> SigningService:
    if not credentials_present:
        pytest.skip("credentials/es256_certs.pem + es256_private.key not present")
    return SigningService()


@pytest.fixture
def manifest_builder(signing_service: SigningService) -> ManifestBuilderService:
    """Singleton ManifestBuilderService for the test.

    Mirrors production wiring (``__main__.py``): consume the
    signing_service's Signer into the builder's Context exactly once.
    Each test gets a fresh ``signing_service`` (function-scoped), so a
    fresh Signer is built and consumed per test."""
    return ManifestBuilderService(signer=signing_service.release_signer())


@pytest.fixture
def stub_resolution() -> _StubResolutionClient:
    return _StubResolutionClient()


@pytest.fixture
def patched_plugin(monkeypatch) -> PluginEntry:
    """Build a deterministic test ``PluginEntry`` and replace
    ``PluginDispatcher`` (the symbol the plugin runner imported) with
    the stub. The catalog itself is now threaded through DI — see
    the ``ingestion_service`` fixture for how it's wired."""
    entry = PluginEntry(
        alg=BINDING_ALG,
        type="watermark",
        binding_bits=128,
        media_types=("audio/wav",),
        url="http://stubbed:8000",
    )
    from ingestion_api.pipeline import plugin_runner as pipeline_module
    monkeypatch.setattr(pipeline_module, "PluginDispatcher", _StubPluginDispatcher)
    return entry


@pytest.fixture
def ingestion_service(
    signing_service: SigningService,
    manifest_builder: ManifestBuilderService,
    artifacts: ArtifactStore,
    records: InMemoryIngestionRecordRepository,
    failed_records: InMemoryFailedIngestionRepository,
    stub_resolution: _StubResolutionClient,
    patched_plugin: PluginEntry,
    monkeypatch,
) -> IngestionService:
    monkeypatch.setattr(
        "ingestion_api.services.ingestion.resolve_plugin",
        _mock_resolve_from_catalog([patched_plugin]),
    )
    return IngestionService(
        signing_service=signing_service,
        manifest_builder=manifest_builder,
        artifacts=artifacts,
        records=records,
        failed_records=failed_records,
        resolution_client=stub_resolution,
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
    payload = IngestionRequest(
        filename="sample.wav",
        content_type="audio/wav",
        data=sample_wav_bytes,
        algs=[BINDING_ALG],
        title="sample test",
    )
    result = asyncio.run(ingestion_service.ingest(payload))

    assert result.signed_asset_path.is_file()
    assert result.signed_asset_path.stat().st_size > 0
    assert result.manifest_bytes_path is not None
    assert result.manifest_bytes_path.is_file()

    record = result.record
    assert len(record.softBindings) == 1
    only = record.softBindings[0]
    assert only.alg == BINDING_ALG
    assert only.kind == "watermark"
    assert only.bindingValue == _binding_value(sample_wav_bytes)
    assert record.mimeType == "audio/wav"
    assert record.mediaType is MediaType.AUDIO
    assert record.resolutionPushStatus is ResolutionPushStatus.OK
    # New: content identity + cert + plugin snapshot.
    assert record.uploadSha256 == sha256_hex(sample_wav_bytes)
    assert len(record.assetSha256) == 64  # SHA-256 hex
    assert record.assetSizeBytes > 0
    assert record.signingCertSha1 is None or len(record.signingCertSha1) == 40
    assert record.pluginVersions == {
        BINDING_ALG: {
            "alg": BINDING_ALG,
            "type": "watermark",
            "bindingBits": 128,
            "version": "stub-0.0.1",
        },
    }
    # Push happened (1 attempt) and timestamps line up.
    assert record.resolutionPushAttempts == 1
    assert record.lastPushAttemptAt == record.createdAt

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
            IngestionRequest(
                filename="sample.wav",
                content_type="audio/wav",
                data=sample_wav_bytes,
                algs=[BINDING_ALG],
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
    manifest_builder: ManifestBuilderService,
    artifacts: ArtifactStore,
    records: InMemoryIngestionRecordRepository,
    failed_records: InMemoryFailedIngestionRepository,
    patched_plugin: PluginEntry,
    sample_wav_bytes: bytes,
):
    """Resolution-api 5xx -> ingestion record persisted with push status FAILED.
    No FailedIngestion is written (this is a downstream push failure, not a
    pipeline failure)."""

    class _FailingResolution:
        enabled = True

        def push(self, req, *, request_id=None):
            return ResolutionPushOutput(status=ResolutionPushStatus.FAILED, error="boom")

        def close(self):
            pass

    with patch(
        "ingestion_api.services.ingestion.resolve_plugin",
        side_effect=_mock_resolve_from_catalog([patched_plugin]),
    ):
        svc = IngestionService(
            signing_service=signing_service,
            manifest_builder=manifest_builder,
            artifacts=artifacts,
            records=records,
            failed_records=failed_records,
            resolution_client=_FailingResolution(),
        )

        result = asyncio.run(
            svc.ingest(
                IngestionRequest(
                    filename="sample.wav",
                    content_type="audio/wav",
                    data=sample_wav_bytes,
                    algs=[BINDING_ALG],
                )
            )
        )
    assert result.record.resolutionPushStatus is ResolutionPushStatus.FAILED
    assert result.record.resolutionPushError == "boom"
    assert result.record.resolutionPushAttempts == 1
    # Pipeline succeeded; FailedIngestion collection stays empty.
    assert asyncio.run(failed_records.get(result.record.ingestionId)) is None


def test_pipeline_failure_persists_failed_ingestion(
    signing_service: SigningService,
    manifest_builder: ManifestBuilderService,
    artifacts: ArtifactStore,
    records: InMemoryIngestionRecordRepository,
    failed_records: InMemoryFailedIngestionRepository,
    monkeypatch,
    stub_resolution: _StubResolutionClient,
    sample_wav_bytes: bytes,
):
    """Plugin /embed unreachable -> IngestionError + FailedIngestion persisted."""
    entry = PluginEntry(
        alg=BINDING_ALG,
        type="watermark",
        binding_bits=128,
        media_types=("audio/wav",),
        url="http://stubbed:8000",
    )
    from ingestion_api.pipeline import plugin_runner as pipeline_module

    class _BrokenPluginDispatcher:
        def __init__(self, e, **_kw):
            self.entry = e

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def embed(self, **_kw):
            raise PluginUnavailableError("connection refused")

        def info_cached(self):
            return {"version": "broken"}

    monkeypatch.setattr(pipeline_module, "PluginDispatcher", _BrokenPluginDispatcher)
    monkeypatch.setattr(
        "ingestion_api.services.ingestion.resolve_plugin",
        _mock_resolve_from_catalog([entry]),
    )

    svc = IngestionService(
        signing_service=signing_service,
        manifest_builder=manifest_builder,
        artifacts=artifacts,
        records=records,
        failed_records=failed_records,
        resolution_client=stub_resolution,
    )

    with pytest.raises(IngestionError) as exc_info:
        asyncio.run(
            svc.ingest(
                IngestionRequest(
                    filename="sample.wav",
                    content_type="audio/wav",
                    data=sample_wav_bytes,
                    algs=[BINDING_ALG],
                )
            )
        )

    assert exc_info.value.stage is FailureStage.PLUGIN_PASS

    # Failed record persisted with stage + upload identity preserved.
    failures = list(failed_records._records.values())  # noqa: SLF001
    assert len(failures) == 1
    failed = failures[0]
    assert failed.failureStage is FailureStage.PLUGIN_PASS
    assert failed.uploadSha256 == sha256_hex(sample_wav_bytes)
    assert failed.attemptedAlgs == [BINDING_ALG]
    assert "connection refused" in failed.error
    assert failed.mimeType == "audio/wav"

    # No success record written.
    assert asyncio.run(records.get(failed.ingestionId)) is None


def test_pipeline_rejects_plugin_that_changes_audio_format(
    signing_service: SigningService,
    manifest_builder: ManifestBuilderService,
    artifacts: ArtifactStore,
    records: InMemoryIngestionRecordRepository,
    failed_records: InMemoryFailedIngestionRepository,
    monkeypatch,
    stub_resolution: _StubResolutionClient,
    sample_wav_bytes: bytes,
):
    """Watermark plugin returns bytes with a different sample rate -> 5xx + persisted failure."""
    import io
    import wave

    entry = PluginEntry(
        alg=BINDING_ALG,
        type="watermark",
        binding_bits=128,
        media_types=("audio/wav",),
        url="http://stubbed:8000",
    )

    def _resampled_wav() -> bytes:
        """Same content shape but different sample rate -> format mismatch."""
        buf = io.BytesIO()
        with wave.open(buf, "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)  # input is 22050
            w.writeframes(b"\x00\x00" * 100)
        return buf.getvalue()

    class _ResamplingPlugin:
        def __init__(self, e, **_kw):
            self.entry = e

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def embed(self, *, media_bytes, mime_type):
            return WatermarkOutput(
                binding_value=_binding_value(media_bytes),
                watermarked_bytes=_resampled_wav(),
            )

        def info_cached(self):
            return {"version": "resampler"}

    from ingestion_api.pipeline import plugin_runner as pipeline_module
    monkeypatch.setattr(pipeline_module, "PluginDispatcher", _ResamplingPlugin)
    monkeypatch.setattr(
        "ingestion_api.services.ingestion.resolve_plugin",
        _mock_resolve_from_catalog([entry]),
    )

    svc = IngestionService(
        signing_service=signing_service,
        manifest_builder=manifest_builder,
        artifacts=artifacts,
        records=records,
        failed_records=failed_records,
        resolution_client=stub_resolution,
    )

    with pytest.raises(IngestionError, match="changed audio format") as exc_info:
        asyncio.run(
            svc.ingest(
                IngestionRequest(
                    filename="sample.wav",
                    content_type="audio/wav",
                    data=sample_wav_bytes,
                    algs=[BINDING_ALG],
                )
            )
        )
    assert exc_info.value.stage is FailureStage.PLUGIN_PASS

    failures = list(failed_records._records.values())  # noqa: SLF001
    assert len(failures) == 1
    assert failures[0].failureStage is FailureStage.PLUGIN_PASS
    # Pipeline didn't persist a success record.
    assert records._records == {}  # noqa: SLF001


def test_ingest_rejects_unsupported_media_format(
    ingestion_service: IngestionService,
):
    with pytest.raises(UnsupportedMediaError):
        asyncio.run(
            ingestion_service.ingest(
                IngestionRequest(
                    filename="not_audio.txt",
                    content_type="text/plain",
                    data=b"hello",
                    algs=[BINDING_ALG],
                )
            )
        )


def test_ingest_rejects_empty_algs(
    ingestion_service: IngestionService,
    sample_wav_bytes: bytes,
):
    with pytest.raises(InvalidAlgRequestError, match="at least one alg"):
        asyncio.run(
            ingestion_service.ingest(
                IngestionRequest(
                    filename="sample.wav",
                    content_type="audio/wav",
                    data=sample_wav_bytes,
                    algs=[],
                )
            )
        )


def test_ingest_rejects_unknown_alg(
    signing_service: SigningService,
    manifest_builder: ManifestBuilderService,
    artifacts: ArtifactStore,
    records: InMemoryIngestionRecordRepository,
    failed_records: InMemoryFailedIngestionRepository,
    stub_resolution: _StubResolutionClient,
    sample_wav_bytes: bytes,
    monkeypatch,
):
    """Caller asks for an alg not in the DB -> 400 (no record persisted)."""
    monkeypatch.setattr(
        "ingestion_api.services.ingestion.resolve_plugin",
        _mock_resolve_from_catalog([]),
    )
    svc = IngestionService(
        signing_service=signing_service,
        manifest_builder=manifest_builder,
        artifacts=artifacts,
        records=records,
        failed_records=failed_records,
        resolution_client=stub_resolution,
    )

    with pytest.raises(InvalidAlgRequestError, match="unknown algs"):
        asyncio.run(
            svc.ingest(
                IngestionRequest(
                    filename="sample.wav",
                    content_type="audio/wav",
                    data=sample_wav_bytes,
                    algs=["nope.does.not.exist"],
                )
            )
        )

    # Pre-allocate failure -> nothing persisted in either collection.
    assert failed_records._records == {}  # noqa: SLF001
    assert records._records == {}  # noqa: SLF001


def test_ingest_rejects_alg_incompatible_with_mime(
    signing_service: SigningService,
    manifest_builder: ManifestBuilderService,
    artifacts: ArtifactStore,
    records: InMemoryIngestionRecordRepository,
    failed_records: InMemoryFailedIngestionRepository,
    stub_resolution: _StubResolutionClient,
    sample_wav_bytes: bytes,
    monkeypatch,
):
    """Caller asks for an alg that exists but doesn't list audio/wav in mediaTypes."""
    video_only_entry = PluginEntry(
        alg="me.example.video.wm",
        type="watermark",
        binding_bits=128,
        media_types=("video/mp4",),  # NOT audio/wav
        url="http://stubbed:9000",
    )

    monkeypatch.setattr(
        "ingestion_api.services.ingestion.resolve_plugin",
        _mock_resolve_from_catalog([video_only_entry]),
    )
    svc = IngestionService(
        signing_service=signing_service,
        manifest_builder=manifest_builder,
        artifacts=artifacts,
        records=records,
        failed_records=failed_records,
        resolution_client=stub_resolution,
    )

    with pytest.raises(InvalidAlgRequestError, match="incompatible"):
        asyncio.run(
            svc.ingest(
                IngestionRequest(
                    filename="sample.wav",
                    content_type="audio/wav",
                    data=sample_wav_bytes,
                    algs=["me.example.video.wm"],
                )
            )
        )

    assert failed_records._records == {}  # noqa: SLF001
    assert records._records == {}  # noqa: SLF001


def test_signed_asset_round_trips_through_reader(
    ingestion_service: IngestionService,
    sample_wav_bytes: bytes,
):
    result = asyncio.run(
        ingestion_service.ingest(
            IngestionRequest(
                filename="sample.wav",
                content_type="audio/wav",
                data=sample_wav_bytes,
                algs=[BINDING_ALG],
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


def test_ingest_parent_ingredient_uses_upload_not_plugin_output(
    monkeypatch,
    signing_service: SigningService,
    artifacts: ArtifactStore,
    records: InMemoryIngestionRecordRepository,
    failed_records: InMemoryFailedIngestionRepository,
    stub_resolution: _StubResolutionClient,
    sample_wav_bytes: bytes,
):
    """Regression guard for the auto-parent foot-gun: when the watermark
    plugin returns DIFFERENT bytes than it received (the realistic case),
    the parent ingredient must be derived from the original upload bytes,
    NOT from the post-watermark output. Otherwise we'd emit a circular
    "we opened the file we just produced" claim and silently drop any
    prior provenance the user supplied.

    Strategy: stub a watermark plugin that mutates the bytes
    (format-preserving so the plugin runner's format-preservation
    check accepts the output), then snapshot the parent/source bytes the
    pipeline hands to the manifest builder by injecting a
    ``ManifestBuilderService`` subclass that captures call args.
    """
    import io
    import wave

    def _mutated_wav() -> bytes:
        """Same shape (mono / 16-bit / 22050 Hz) so the format-preservation
        guard passes; different sample stream so the bytes hash differently."""
        buf = io.BytesIO()
        with wave.open(buf, "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes(b"\xff\x7f" * 1000)
        return buf.getvalue()

    mutated = _mutated_wav()
    assert mutated != sample_wav_bytes, "fixture invariant"

    class _MutatingPlugin:
        def __init__(self, e, **_kw):
            self.entry = e
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return None
        def embed(self, *, media_bytes, mime_type):
            return WatermarkOutput(
                binding_value=_binding_value(mutated),
                watermarked_bytes=mutated,
            )
        def info_cached(self):
            return {"version": "mutating-stub"}

    entry = PluginEntry(
        alg=BINDING_ALG,
        type="watermark",
        binding_bits=128,
        media_types=("audio/wav",),
        url="http://stubbed:8000",
    )

    captured: dict[str, object] = {}

    class _CapturingBuilder(ManifestBuilderService):
        def build_and_sign(self, **kw):
            captured["parent_bytes"] = kw.get("parent_bytes")
            captured["source_bytes"] = kw.get("source_bytes")
            return super().build_and_sign(**kw)

    capturing_builder = _CapturingBuilder(signer=signing_service.release_signer())

    from ingestion_api.pipeline import plugin_runner as pipeline_module
    monkeypatch.setattr(pipeline_module, "PluginDispatcher", _MutatingPlugin)
    monkeypatch.setattr(
        "ingestion_api.services.ingestion.resolve_plugin",
        _mock_resolve_from_catalog([entry]),
    )

    svc = IngestionService(
        signing_service=signing_service,
        manifest_builder=capturing_builder,
        artifacts=artifacts,
        records=records,
        failed_records=failed_records,
        resolution_client=stub_resolution,
    )

    asyncio.run(
        svc.ingest(
            IngestionRequest(
                filename="sample.wav",
                content_type="audio/wav",
                data=sample_wav_bytes,
                algs=[BINDING_ALG],
            )
        )
    )

    assert captured["parent_bytes"] == sample_wav_bytes, (
        "parent ingredient must come from the upload bytes"
    )
    assert captured["source_bytes"] == mutated, (
        "sign source must be the post-watermark bytes"
    )


def test_ingest_with_watermark_plus_fingerprint(
    monkeypatch,
    signing_service: SigningService,
    manifest_builder: ManifestBuilderService,
    artifacts: ArtifactStore,
    records: InMemoryIngestionRecordRepository,
    failed_records: InMemoryFailedIngestionRepository,
    stub_resolution: _StubResolutionClient,
    sample_wav_bytes: bytes,
):
    """Multi-alg pipeline: one watermark + one fingerprint, both surfaced."""
    FP_ALG = "me.deepmark.audio.fp.stub"
    catalog = [
        PluginEntry(
            alg=BINDING_ALG,
            type="watermark",
            binding_bits=128,
            media_types=("audio/wav",),
            url="http://stubbed:8000",
        ),
        PluginEntry(
            alg=FP_ALG,
            type="fingerprint",
            binding_bits=128,
            media_types=("audio/wav",),
            url="http://stubbed:8001",
        ),
    ]
    from ingestion_api.pipeline import plugin_runner as pipeline_module
    monkeypatch.setattr(pipeline_module, "PluginDispatcher", _StubPluginDispatcher)
    monkeypatch.setattr(
        "ingestion_api.services.ingestion.resolve_plugin",
        _mock_resolve_from_catalog(catalog),
    )

    svc = IngestionService(
        signing_service=signing_service,
        manifest_builder=manifest_builder,
        artifacts=artifacts,
        records=records,
        failed_records=failed_records,
        resolution_client=stub_resolution,
    )

    result = asyncio.run(
        svc.ingest(
            IngestionRequest(
                filename="sample.wav",
                content_type="audio/wav",
                data=sample_wav_bytes,
                algs=[BINDING_ALG, FP_ALG],
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
