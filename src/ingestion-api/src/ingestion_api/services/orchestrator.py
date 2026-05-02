"""
Audio ingestion orchestrator.

End-to-end pipeline:

    upload bytes
        -> for each configured alg, POST to its plugin container:
           - watermark plugins: /embed -> watermarked bytes + bindingValue
             (later passes see the mutated bytes)
           - fingerprint plugins: /compute -> bindingValue (no mutation)
        -> build C2PA manifest (EDIT intent: parent ingredient + c2pa.opened
           injected automatically; we add c2pa.watermarked.bound + one
           c2pa.soft-binding assertion per alg)
        -> sign (Builder.sign)
        -> extract canonical manifestId from signed asset (hard fail
           if SDK can't surface it — better than fabricating a UUID)
        -> persist signed asset + manifest bytes to disk (ArtifactStore)
        -> persist IngestionRecord to MongoDB (with content hashes,
           cert fingerprint, plugin /info snapshot)
        -> auto-push manifest store + every binding to resolution-api
           (which derives the same manifestId from the same bytes)

Pipeline failures (after we've allocated an ingestion_id) get
persisted to the ``failed_ingestions`` collection with whatever
metadata was knowable at the failure point. Pre-allocate failures
(unsupported MIME) still raise as 4xx and are not persisted.

Bytes never touch disk on the ingestion-api side until we write the
signed output. The plugin call is purely HTTP, so plugin containers can
live on a different host (no shared volume required).

Alg ordering is significant: watermark passes mutate the bytes, so any
fingerprints listed after a watermark are computed on the watermarked
asset (the same bytes a downstream consumer would recompute from). Put
watermarks first.
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ingestion_api.core.config import settings
from ingestion_api.core.logging import get_logger
from ingestion_api.models.ingestion import (
    FailedIngestion,
    FailureStage,
    IngestionRecord,
    ResolutionPushStatus,
    SoftBindingRecord,
    make_soft_binding,
)
from ingestion_api.services.algorithms import (
    AlgorithmEntry,
    AlgorithmNotFoundError,
    PluginClient,
    PluginUnavailableError,
    resolve as resolve_algorithm,
)
from ingestion_api.services.artifact_store import ArtifactStore, IngestionArtifacts
from ingestion_api.services.manifest import ManifestBuilderService, SoftBindingSpec
from ingestion_api.services.publisher import (
    BindingPair,
    ResolutionPushClient,
    ResolutionPushRequest,
)
from ingestion_api.services.record_repository import (
    MongoFailedIngestionRepository,
    MongoIngestionRecordRepository,
)
from ingestion_api.services.signing import SigningService
from ingestion_api.utils.audio import (
    SUPPORTED_AUDIO_EXTENSIONS,
    SUPPORTED_AUDIO_MIME_TYPES,
    guess_audio_format,
)
from ingestion_api.utils.hashing import sha256_hex
from ingestion_api.utils.ids import new_ingestion_id

logger = get_logger(__name__)


class IngestionError(RuntimeError):
    """Generic ingestion failure with a stable HTTP-friendly message.

    Carries a ``stage`` so the orchestrator's failure-persistence layer
    can record where in the pipeline things blew up.
    """

    def __init__(self, msg: str, *, stage: FailureStage = FailureStage.UNKNOWN) -> None:
        super().__init__(msg)
        self.stage = stage


class UnsupportedAudioFormatError(IngestionError):
    """4xx-class — raised before we've allocated an ingestion id, never persisted."""


@dataclass(slots=True)
class IngestionInput:
    filename: str
    content_type: str | None
    data: bytes
    title: str | None = None


@dataclass(slots=True)
class IngestionResult:
    record: IngestionRecord
    signed_asset_path: Path
    manifest_bytes_path: Path | None


@dataclass(slots=True, frozen=True)
class _BindingPass:
    """Output of a single plugin pass — one per configured alg."""
    entry: AlgorithmEntry
    binding_value: str
    output_bytes: bytes  # bytes after this pass (watermark mutates, fingerprint passes through)


class IngestionService:
    """Holds the long-lived collaborators. Construct once, reuse per request."""

    def __init__(
        self,
        *,
        signing_service: SigningService,
        artifacts: ArtifactStore,
        records: MongoIngestionRecordRepository,
        failed_records: MongoFailedIngestionRepository | None = None,
        resolution_client: ResolutionPushClient | None = None,
        soft_binding_algs: Sequence[str] | None = None,
    ) -> None:
        self._signing_service = signing_service
        self._artifacts = artifacts
        self._records = records
        self._failed_records = failed_records
        self._resolution_client = resolution_client or ResolutionPushClient()
        algs = list(soft_binding_algs) if soft_binding_algs else list(settings.audio_algs)
        if not algs:
            raise ValueError("IngestionService requires at least one soft-binding alg")
        self._algs: list[str] = algs

    async def ingest(self, payload: IngestionInput) -> IngestionResult:
        mime_type = guess_audio_format(payload.filename, payload.content_type)
        if mime_type is None:
            # Pre-allocate failure: caller bug, never persisted.
            raise UnsupportedAudioFormatError(
                f"Unsupported audio format: filename={payload.filename!r} "
                f"content_type={payload.content_type!r}. "
                f"Supported: {sorted(SUPPORTED_AUDIO_MIME_TYPES)}"
            )

        ext = "." + SUPPORTED_AUDIO_MIME_TYPES[mime_type]
        upload_ext = Path(payload.filename).suffix.lower()
        if upload_ext in SUPPORTED_AUDIO_EXTENSIONS:
            ext = upload_ext

        ingestion_id = new_ingestion_id()
        upload_sha256 = sha256_hex(payload.data)
        artifacts = self._artifacts.allocate(ingestion_id, ext=ext)

        try:
            return await self._run_pipeline(
                payload, mime_type, ingestion_id, upload_sha256, artifacts,
            )
        except Exception as exc:
            stage = getattr(exc, "stage", FailureStage.UNKNOWN)
            await self._persist_failure(
                ingestion_id=ingestion_id,
                mime_type=mime_type,
                upload_sha256=upload_sha256,
                stage=stage,
                error=str(exc),
            )
            self._artifacts.cleanup(artifacts)
            raise

    async def _run_pipeline(
        self,
        payload: IngestionInput,
        mime_type: str,
        ingestion_id: str,
        upload_sha256: str,
        artifacts: IngestionArtifacts,
    ) -> IngestionResult:
        loop = asyncio.get_running_loop()

        # 1. Resolve every configured alg up front so we fail fast on a
        # bad catalog before doing any plugin I/O.
        try:
            entries = [resolve_algorithm(alg) for alg in self._algs]
        except AlgorithmNotFoundError as exc:
            raise IngestionError(str(exc), stage=FailureStage.PLUGIN_PASS) from exc

        # 2. Run each alg's plugin in order. Watermark passes mutate the
        # bytes; fingerprint passes are pure reads on whatever bytes the
        # previous pass produced.
        try:
            passes = await loop.run_in_executor(
                None, lambda: self._run_plugin_passes(entries, payload.data),
            )
        except PluginUnavailableError as exc:
            raise IngestionError(
                f"Plugin call failed: {exc}", stage=FailureStage.PLUGIN_PASS,
            ) from exc

        final_bytes = passes[-1].output_bytes
        if not final_bytes:
            raise IngestionError(
                "Plugin chain produced an empty payload",
                stage=FailureStage.PLUGIN_PASS,
            )

        soft_binding_specs = [
            SoftBindingSpec(
                alg=p.entry.alg,
                kind=p.entry.type,
                value=p.binding_value,
                related_to_watermark_action=(p.entry.type == "watermark"),
            )
            for p in passes
        ]

        # 3. Build + sign manifest. Run in a thread because the SDK is sync.
        builder = self._make_manifest_builder()
        signed_at = datetime.now(timezone.utc)
        try:
            built = await loop.run_in_executor(
                None,
                lambda: builder.build_and_sign(
                    source_bytes=final_bytes,
                    dest_path=artifacts.signed_path,
                    mime_type=mime_type,
                    soft_bindings=soft_binding_specs,
                    title=payload.title or payload.filename,
                ),
            )
        except IngestionError:
            raise
        except Exception as exc:
            raise IngestionError(
                f"Manifest sign failed: {exc}", stage=FailureStage.MANIFEST_SIGN,
            ) from exc

        if built.manifest_bytes:
            self._artifacts.write_manifest_bytes(artifacts, built.manifest_bytes)
            manifest_bytes_path: Path | None = artifacts.manifest_bytes_path
        else:
            manifest_bytes_path = None

        # 4. Extract the canonical manifest URN from the signed asset.
        manifest_id = await loop.run_in_executor(
            None, self._read_active_manifest_label, artifacts.signed_path,
        )
        if not manifest_id:
            raise IngestionError(
                "Could not extract active manifestId from signed asset — "
                "C2PA Reader returned no active_manifest label. This "
                "indicates a signing pipeline bug.",
                stage=FailureStage.MANIFEST_ID_EXTRACT,
            )

        # Content + signing identity for the persisted record.
        signed_bytes = artifacts.signed_path.read_bytes()
        asset_sha256 = sha256_hex(signed_bytes)
        asset_size_bytes = len(signed_bytes)
        signing_cert_sha1 = self._signing_service.credentials.cert_sha1()
        plugin_versions = self._capture_plugin_versions(entries)

        # 5. Auto-push to resolution API: one /manifests + one /bindings
        # per pass. Resolution-api derives the same manifestId from the
        # bytes; we don't trust a returned override.
        bindings = [
            BindingPair(alg=p.entry.alg, binding_value=p.binding_value)
            for p in passes
        ]
        push_attempted = self._resolution_client.enabled
        push_result = await loop.run_in_executor(
            None,
            lambda: self._resolution_client.push(
                ResolutionPushRequest(
                    manifest_bytes=built.manifest_bytes or b"",
                    manifest_id=manifest_id,
                    bindings=bindings,
                ),
            ),
        )
        last_push_attempt_at = signed_at if push_attempted else None
        push_attempts = 1 if push_attempted else 0

        soft_binding_records: list[SoftBindingRecord] = [
            make_soft_binding(
                alg=p.entry.alg, kind=p.entry.type, bindingValue=p.binding_value,
            )
            for p in passes
        ]

        record = IngestionRecord(
            ingestionId=ingestion_id,
            mimeType=mime_type,
            softBindings=soft_binding_records,
            manifestId=manifest_id,
            assetSha256=asset_sha256,
            assetSizeBytes=asset_size_bytes,
            uploadSha256=upload_sha256,
            signingCertSha1=signing_cert_sha1,
            pluginVersions=plugin_versions,
            signingAlg=self._signing_service.credentials.signing_alg,
            taUrl=self._signing_service.credentials.ta_url,
            signedAt=signed_at,
            createdAt=signed_at,
            updatedAt=signed_at,
            resolutionPushStatus=push_result.status,
            resolutionPushError=push_result.error,
            resolutionPushAttempts=push_attempts,
            lastPushAttemptAt=last_push_attempt_at,
        )
        await self._records.write(record)

        logger.info(
            "Ingestion %s OK algs=%s manifestId=%s asset_sha=%s push=%s",
            ingestion_id,
            [r.alg for r in soft_binding_records],
            manifest_id,
            asset_sha256[:12],
            push_result.status.value,
        )
        return IngestionResult(
            record=record,
            signed_asset_path=artifacts.signed_path,
            manifest_bytes_path=manifest_bytes_path,
        )

    async def _persist_failure(
        self,
        *,
        ingestion_id: str,
        mime_type: str | None,
        upload_sha256: str,
        stage: FailureStage,
        error: str,
    ) -> None:
        """Best-effort write of a FailedIngestion. Never raises."""
        if self._failed_records is None:
            logger.warning(
                "Pipeline failure %s stage=%s not persisted (no failed_records repo)",
                ingestion_id, stage.value,
            )
            return
        now = datetime.now(timezone.utc)
        try:
            cert_sha1 = self._signing_service.credentials.cert_sha1()
        except Exception:
            cert_sha1 = None
        record = FailedIngestion(
            ingestionId=ingestion_id,
            failureStage=stage,
            error=error,
            mimeType=mime_type,
            uploadSha256=upload_sha256,
            attemptedAlgs=list(self._algs),
            signingCertSha1=cert_sha1,
            createdAt=now,
            updatedAt=now,
        )
        try:
            await self._failed_records.write(record)
            logger.info(
                "Persisted failed ingestion %s stage=%s", ingestion_id, stage.value,
            )
        except Exception:
            logger.exception(
                "Failed to persist FailedIngestion for %s", ingestion_id,
            )

    def _capture_plugin_versions(
        self, entries: Sequence[AlgorithmEntry],
    ) -> dict[str, dict] | None:
        """Snapshot ``/info`` per alg via the cached PluginClient.info_cached().

        Returns None if every plugin probe fails — keeps the record
        clean (no `{}`) when plugin metadata isn't available.
        """
        snapshots: dict[str, dict] = {}
        for entry in entries:
            try:
                with PluginClient(entry) as plugin:
                    snapshots[entry.alg] = plugin.info_cached()
            except Exception:
                logger.debug(
                    "Could not capture /info for alg=%s", entry.alg, exc_info=True,
                )
        return snapshots or None

    def _run_plugin_passes(
        self, entries: Sequence[AlgorithmEntry], initial_bytes: bytes,
    ) -> list[_BindingPass]:
        """Run every alg's plugin, threading mutated bytes through.

        Watermark plugins replace ``current_bytes`` with their /embed
        output; fingerprint plugins return only a binding value and
        leave the bytes untouched.
        """
        passes: list[_BindingPass] = []
        current_bytes = initial_bytes
        for entry in entries:
            with PluginClient(entry) as plugin:
                if entry.type == "watermark":
                    embed = plugin.embed(audio_bytes=current_bytes)
                    if not embed.watermarked_bytes:
                        raise IngestionError(
                            f"Plugin {entry.alg!r} returned an empty watermarked payload",
                            stage=FailureStage.PLUGIN_PASS,
                        )
                    current_bytes = embed.watermarked_bytes
                    binding_value = embed.binding_value
                elif entry.type == "fingerprint":
                    binding_value = plugin.compute(audio_bytes=current_bytes)
                else:
                    raise IngestionError(
                        f"Unsupported alg type {entry.type!r} for {entry.alg!r}",
                        stage=FailureStage.PLUGIN_PASS,
                    )
            passes.append(
                _BindingPass(
                    entry=entry,
                    binding_value=binding_value,
                    output_bytes=current_bytes,
                ),
            )
        return passes

    def _make_manifest_builder(self) -> ManifestBuilderService:
        return ManifestBuilderService(signer=self._signing_service.signer)

    @staticmethod
    def _read_active_manifest_label(signed_path: Path) -> str | None:
        try:
            import json
            from c2pa import Reader
            with Reader(str(signed_path)) as reader:
                manifest_json = reader.json()
                data = json.loads(manifest_json)
                return data.get("active_manifest")
        except Exception:
            logger.debug(
                "Could not read active manifest label from %s", signed_path, exc_info=True,
            )
            return None
