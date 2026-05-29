"""
Media ingestion service.

End-to-end pipeline:

    upload bytes + caller-supplied alg list
        -> guess MediaType + MIME from upload
        -> resolve every requested alg from MongoDB and verify it
           declares the upload's MIME in its ``mediaTypes``
        -> for each alg in order, POST to its plugin container:
           - watermark plugins: /embed -> watermarked bytes + bindingValue
             (later passes see the mutated bytes)
           - fingerprint plugins: /compute -> bindingValue (no mutation)
        -> build C2PA manifest:
           - parent ingredient added explicitly from the ORIGINAL
             upload bytes (not the post-watermark output) so the
             c2pa.opened action / provenance chain points at what the
             user gave us. EDIT intent wires up c2pa.opened.
           - we add c2pa.watermarked.bound + one c2pa.soft-binding
             assertion per alg.
        -> sign (Builder.sign)
        -> extract canonical manifestId from signed asset (hard fail
           if SDK can't surface it — better than fabricating a UUID)
        -> persist signed asset + manifest bytes to disk (ArtifactStore)
        -> persist IngestionRecord to MongoDB (with content hashes,
           cert fingerprint, plugin /info snapshot, mediaType)
        -> auto-push manifest store + every binding to resolution-api
           (which derives the same manifestId from the same bytes)

Pipeline failures (after we've allocated an ingestion_id) are persisted
as ``status=failed`` records with whatever metadata was knowable at the
failure point. Pre-allocate failures (unsupported MIME, bad alg request)
raise as 4xx and are not persisted.

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
from functools import partial
from pathlib import Path

from ingestion_api.adapters.dispatcher import PluginUnavailableError
from ingestion_api.adapters.publisher import ResolutionPushClient
from ingestion_api.contracts.ingestion import IngestionOutput, IngestionRequest
from ingestion_api.contracts.manifest import SoftBindingSpec
from ingestion_api.contracts.plugin import PluginEntry, PluginPassOutput
from ingestion_api.contracts.publisher import ResolutionPushBinding, ResolutionPushRequest
from ingestion_api.core.errors import (
    IngestionError,
    InvalidAlgRequestError,
    PluginNotFoundError,
    UnsupportedMediaError,
)
from ingestion_api.core.logging import get_logger, get_request_id
from ingestion_api.core.plugin import resolve_plugin
from ingestion_api.models.enums import FailureStage, IngestionStatus, MediaType
from ingestion_api.models.ingestion import IngestionRecord
from ingestion_api.models.responses import ResolutionPushOutput
from ingestion_api.models.soft_binding import SoftBindingRecord, make_soft_binding
from ingestion_api.pipeline.plugin_runner import capture_plugin_versions, run_plugin_passes
from ingestion_api.repositories.artifacts import ArtifactStore, IngestionArtifacts
from ingestion_api.repositories.ingestions import IngestionRecordRepository
from ingestion_api.services.manifest import (
    BuiltManifest,
    ManifestBuilderService,
    read_active_manifest_label,
)
from ingestion_api.services.signing import SigningService
from ingestion_api.utils.hashing import sha256_hex
from ingestion_api.utils.ids import new_ingestion_id
from ingestion_api.utils.media import (
    SUPPORTED_MIME_TYPES,
    canonical_extension,
    container_matches_mime,
    guess_media_format,
)

logger = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class _PushOutcome:
    """Combined view of a resolution-api push attempt.

    ``attempted`` is decoupled from ``result.status`` because a SKIPPED
    status (push disabled in config) is *not* an attempt; everything
    else is. The persisted ``IngestionRecord`` needs both.
    """
    result: ResolutionPushOutput
    attempted: bool


class IngestionService:
    """Holds the long-lived collaborators. Construct once, reuse per request.
    """

    def __init__(
        self,
        *,
        signing_service: SigningService,
        manifest_builder: ManifestBuilderService,
        artifacts: ArtifactStore,
        records: IngestionRecordRepository,
        resolution_client: ResolutionPushClient | None = None,
    ) -> None:
        self._signing_service = signing_service
        self._manifest_builder = manifest_builder
        self._artifacts = artifacts
        self._records = records
        self._resolution_client = resolution_client or ResolutionPushClient()

    async def ingest(self, payload: IngestionRequest) -> IngestionOutput:

        guessed = guess_media_format(payload.filename, payload.content_type)
        if guessed is None:
            raise UnsupportedMediaError(
                f"Unsupported media format: filename={payload.filename!r} "
                f"content_type={payload.content_type!r}. "
                f"Supported MIME types: {sorted(SUPPORTED_MIME_TYPES)}",
            )
        media_type, mime_type = guessed

        if not payload.algs:
            raise InvalidAlgRequestError(
                "Request must include at least one alg in `algs`.",
            )

        # Resolve and MIME-check the requested algs up front so a bad
        # request doesn't allocate disk / a record / an ingestion id.
        try:
            entries = await self._resolve_plugins(payload.algs, mime_type)
        except InvalidAlgRequestError:
            raise
        except IngestionError as exc:
            # Catalog problem (alg not found) — also a caller-facing 400.
            raise InvalidAlgRequestError(str(exc)) from exc

        # Pipeline state from here on lives under an ingestion_id;
        # any failure gets persisted as status=failed.
        ext = canonical_extension(mime_type)
        upload_ext = Path(payload.filename).suffix.lower()
        if upload_ext and upload_ext == canonical_extension(mime_type):
            ext = upload_ext

        ingestion_id = new_ingestion_id()
        upload_sha256 = sha256_hex(payload.data)
        artifacts = self._artifacts.allocate(ingestion_id, ext=ext)

        # Capture the active request id NOW (in async context) so the
        # executor-bound plugin and resolution-push calls can stamp it
        # as ``X-Request-ID``. ContextVars don't auto-propagate into
        # ``loop.run_in_executor`` workers, hence the explicit pass.
        request_id = get_request_id()

        try:
            await self._persist_pending(
                ingestion_id=ingestion_id,
                mime_type=mime_type,
                media_type=media_type,
                upload_sha256=upload_sha256,
                attempted_algs=[entry.alg for entry in entries],
            )
            return await self._run_pipeline(
                payload, media_type, mime_type, entries,
                ingestion_id, upload_sha256, artifacts,
                request_id=request_id,
            )
        except Exception as exc:
            stage = getattr(exc, "stage", FailureStage.UNKNOWN)
            await self._persist_failure(
                ingestion_id=ingestion_id,
                mime_type=mime_type,
                media_type=media_type,
                upload_sha256=upload_sha256,
                attempted_algs=list(payload.algs),
                stage=stage,
                error=str(exc),
            )
            self._artifacts.cleanup(artifacts)
            raise

    async def _resolve_plugins(
        self, algs: Sequence[str], mime_type: str,
    ) -> list[PluginEntry]:
        """Resolve every requested alg from the database and validate
        it supports the upload's MIME.

        Queries MongoDB directly each time so newly-registered
        algorithms are available without a service restart.

        Raises ``InvalidAlgRequestError`` listing every offending alg
        in one message — better UX than raising on the first bad one.
        """
        resolved: list[PluginEntry] = []
        unknown: list[str] = []
        incompatible: list[tuple[str, tuple[str, ...]]] = []

        for alg in algs:
            try:
                entry = await resolve_plugin(alg)
            except PluginNotFoundError:
                unknown.append(alg)
                continue
            if mime_type not in entry.media_types:
                incompatible.append((alg, entry.media_types))
                continue
            resolved.append(entry)

        if unknown or incompatible:
            problems: list[str] = []
            if unknown:
                problems.append(f"unknown algs (not in catalog): {unknown}")
            if incompatible:
                problems.append(
                    "algs incompatible with upload MIME "
                    f"{mime_type!r}: " + ", ".join(
                        f"{alg!r} (supports {list(mts)})"
                        for alg, mts in incompatible
                    ),
                )
            raise InvalidAlgRequestError("; ".join(problems))

        return resolved

    async def _run_pipeline(
        self,
        payload: IngestionRequest,
        media_type: MediaType,
        mime_type: str,
        entries: list[PluginEntry],
        ingestion_id: str,
        upload_sha256: str,
        artifacts: IngestionArtifacts,
        *,
        request_id: str | None = None,
    ) -> IngestionOutput:
        """Top-level choreography. Each step is a private async method
        named after what it does"""
        passes = await self._run_plugins(
            entries, payload.data, media_type, mime_type, request_id=request_id,
        )
        signed_at = datetime.now(timezone.utc)
        built = await self._build_and_sign(
            passes, mime_type, payload, artifacts.signed_path,
            parent_bytes=payload.data,
        )
        manifest_bytes_path = self._persist_manifest_bytes(artifacts, built)
        manifest_id = await self._extract_manifest_id(built.manifest_bytes)
        push = await self._push_to_resolution(
            built.manifest_bytes, manifest_id, passes, request_id=request_id,
        )

        record = self._build_success_record(
            ingestion_id=ingestion_id,
            mime_type=mime_type,
            media_type=media_type,
            passes=passes,
            entries=entries,
            manifest_id=manifest_id,
            signed_path=artifacts.signed_path,
            upload_sha256=upload_sha256,
            signed_at=signed_at,
            push=push,
        )
        await self._records.write(record)

        logger.info(
            "Ingestion %s OK media=%s algs=%s manifestId=%s asset_sha=%s push=%s",
            ingestion_id,
            media_type.value,
            [r.alg for r in record.softBindings],
            manifest_id,
            record.assetSha256[:12],
            push.result.status.value,
        )
        return IngestionOutput(
            record=record,
            signed_asset_path=artifacts.signed_path,
            manifest_bytes_path=manifest_bytes_path,
        )

    # ------------------------------------------------------------------
    # Pipeline steps. Each one is a thin wrapper around a collaborator
    # plus the IngestionError translation layer.
    # ------------------------------------------------------------------

    async def _run_plugins(
        self,
        entries: list[PluginEntry],
        initial_bytes: bytes,
        media_type: MediaType,
        mime_type: str,
        *,
        request_id: str | None,
    ) -> list[PluginPassOutput]:
        try:
            passes = await asyncio.get_running_loop().run_in_executor(
                None,
                partial(
                    run_plugin_passes,
                    entries,
                    initial_bytes,
                    media_type,
                    mime_type,
                    request_id=request_id,
                ),
            )
        except PluginUnavailableError as exc:
            raise IngestionError(
                f"Plugin call failed: {exc}", stage=FailureStage.PLUGIN_PASS,
            ) from exc

        if not passes[-1].output_bytes:
            raise IngestionError(
                "Plugin chain produced an empty payload",
                stage=FailureStage.PLUGIN_PASS,
            )

        # Defensive container check: if a plugin transcodes (e.g. emits
        # WAV bytes for an audio/flac upload) the c2pa Builder fails at
        # sign time with an opaque encoding/unsupported error, because
        # we hand it ``mime_type`` and bytes that disagree. Catching the
        # mismatch here surfaces a PLUGIN_PASS failure so ops/forensics
        # see "plugin returned wrong container" rather than a c2pa-rs
        # internal at MANIFEST_SIGN.
        final_bytes = passes[-1].output_bytes
        if not container_matches_mime(final_bytes, mime_type):
            raise IngestionError(
                f"Plugin chain output does not match expected container "
                f"({mime_type!r}); leading bytes: {final_bytes[:8]!r}. "
                "The plugin likely transcoded the asset to a different "
                "format — only container-preserving plugins are supported.",
                stage=FailureStage.PLUGIN_PASS,
            )
        return passes

    async def _build_and_sign(
        self,
        passes: Sequence[PluginPassOutput],
        mime_type: str,
        payload: IngestionRequest,
        dest_path: Path,
        *,
        parent_bytes: bytes,
    ) -> BuiltManifest:
        """Build the manifest definition and sign.

        Runs the c2pa SDK call in a thread because the SDK is sync; we
        already captured ``request_id`` higher in the call chain, so
        the worker thread doesn't need to read the contextvar.

        ``parent_bytes`` is the original upload (NOT ``passes[-1].output_bytes``,
        which is the post-watermark output) — see the manifest builder
        docstring for why this matters.
        """
        soft_bindings = [
            SoftBindingSpec(
                alg=p.entry.alg,
                kind=p.entry.type,
                value=p.binding_value,
                related_to_watermark_action=(p.entry.type == "watermark"),
            )
            for p in passes
        ]
        try:
            return await asyncio.get_running_loop().run_in_executor(
                None,
                partial(
                    self._manifest_builder.build_and_sign,
                    source_bytes=passes[-1].output_bytes,
                    parent_bytes=parent_bytes,
                    dest_path=dest_path,
                    mime_type=mime_type,
                    soft_bindings=soft_bindings,
                    title=payload.title or payload.filename,
                ),
            )
        except IngestionError:
            raise
        except Exception as exc:
            raise IngestionError(
                f"Manifest sign failed: {exc}", stage=FailureStage.MANIFEST_SIGN,
            ) from exc

    def _persist_manifest_bytes(
        self, artifacts: IngestionArtifacts, built: BuiltManifest,
    ) -> Path | None:
        """Write the raw signed-manifest bytes alongside the asset."""
        if not built.manifest_bytes:
            return None
        self._artifacts.write_manifest_bytes(artifacts, built.manifest_bytes)
        return artifacts.manifest_bytes_path

    async def _extract_manifest_id(self, manifest_bytes: bytes) -> str:
        """Read back the active_manifest URN from the freshly-signed manifest.

        Hard-fails if the SDK Reader can't surface a label; fabricating
        a UUID would cause a foreign-key mismatch with resolution-api,
        which derives the same id from the same bytes.

        Reads from the in-memory manifest bytes (returned by
        ``Builder.sign``) rather than the on-disk signed asset.
        """
        manifest_id = await asyncio.get_running_loop().run_in_executor(
            None, read_active_manifest_label, manifest_bytes,
        )
        if not manifest_id:
            raise IngestionError(
                "Could not extract active manifestId from signed manifest — "
                "C2PA Reader returned no active_manifest label. This "
                "indicates a signing pipeline bug.",
                stage=FailureStage.MANIFEST_ID_EXTRACT,
            )
        return manifest_id

    async def _push_to_resolution(
        self,
        manifest_bytes: bytes,
        manifest_id: str,
        passes: Sequence[PluginPassOutput],
        *,
        request_id: str | None,
    ) -> _PushOutcome:
        """Push the manifest + each binding to resolution-api.

        Never raises. The publisher returns a status, and a SKIPPED
        status when push is disabled in config is *not* an attempt.
        """
        attempted = self._resolution_client.enabled
        push_request = ResolutionPushRequest(
            manifest_bytes=manifest_bytes or b"",
            manifest_id=manifest_id,
            bindings=[
                ResolutionPushBinding(alg=p.entry.alg, binding_value=p.binding_value)
                for p in passes
            ],
        )
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            partial(self._resolution_client.push, push_request, request_id=request_id),
        )
        return _PushOutcome(result=result, attempted=attempted)

    # ------------------------------------------------------------------
    # Record assembly + failure persistence.
    # ------------------------------------------------------------------

    async def _persist_pending(
        self,
        *,
        ingestion_id: str,
        mime_type: str,
        media_type: MediaType,
        upload_sha256: str,
        attempted_algs: list[str],
    ) -> None:
        """Write the initial lifecycle record before plugins/signing run."""
        pending = IngestionRecord(
            ingestionId=ingestion_id,
            status=IngestionStatus.PENDING,
            mimeType=mime_type,
            mediaType=media_type,
            uploadSha256=upload_sha256,
            attemptedAlgs=attempted_algs,
            signingAlg=self._signing_service.credentials.signing_alg,
            taUrl=self._signing_service.credentials.ta_url,
            createdAt=datetime.now(timezone.utc),
        )
        await self._records.write(pending)

    def _build_success_record(
        self,
        *,
        ingestion_id: str,
        mime_type: str,
        media_type: MediaType,
        passes: Sequence[PluginPassOutput],
        entries: Sequence[PluginEntry],
        manifest_id: str,
        signed_path: Path,
        upload_sha256: str,
        signed_at: datetime,
        push: _PushOutcome,
    ) -> IngestionRecord:
        """Assemble the persisted ``IngestionRecord`` from pipeline outputs.

        Reads the signed asset bytes once for the content hash + size
        (the asset is already on disk by this point, so a re-read is
        cheaper than threading the bytes through every stage just for
        hashing).
        """
        signed_bytes = signed_path.read_bytes()
        soft_bindings: list[SoftBindingRecord] = [
            make_soft_binding(
                alg=p.entry.alg, kind=p.entry.type, bindingValue=p.binding_value,
            )
            for p in passes
        ]
        return IngestionRecord(
            ingestionId=ingestion_id,
            mimeType=mime_type,
            mediaType=media_type,
            softBindings=soft_bindings,
            manifestId=manifest_id,
            assetSha256=sha256_hex(signed_bytes),
            assetSizeBytes=len(signed_bytes),
            uploadSha256=upload_sha256,
            signingCertSha1=self._signing_service.credentials.cert_sha1(),
            pluginVersions=capture_plugin_versions(entries),
            signingAlg=self._signing_service.credentials.signing_alg,
            taUrl=self._signing_service.credentials.ta_url,
            signedAt=signed_at,
            createdAt=signed_at,
            attemptedAlgs=[entry.alg for entry in entries],
            status=IngestionStatus.SUCCEEDED,
            resolutionPushStatus=push.result.status,
            resolutionPushError=push.result.error,
            resolutionPushAttempts=1 if push.attempted else 0,
            lastPushAttemptAt=signed_at if push.attempted else None,
        )

    async def _persist_failure(
        self,
        *,
        ingestion_id: str,
        mime_type: str | None,
        media_type: MediaType | None,
        upload_sha256: str,
        attempted_algs: list[str],
        stage: FailureStage,
        error: str,
    ) -> None:
        """Best-effort write of a failed IngestionRecord. Never raises."""
        try:
            cert_sha1 = self._signing_service.credentials.cert_sha1()
        except Exception:
            cert_sha1 = None
        record = IngestionRecord(
            ingestionId=ingestion_id,
            status=IngestionStatus.FAILED,
            failureStage=stage,
            error=error,
            mimeType=mime_type,
            mediaType=media_type,
            uploadSha256=upload_sha256,
            attemptedAlgs=attempted_algs,
            signingCertSha1=cert_sha1,
            signingAlg=self._signing_service.credentials.signing_alg,
            taUrl=self._signing_service.credentials.ta_url,
            createdAt=datetime.now(timezone.utc),
        )
        try:
            await self._records.write(record)
            logger.info(
                "Persisted failed ingestion %s stage=%s",
                ingestion_id,
                stage.value,
            )
        except Exception:
            logger.exception(
                "Failed to persist failed IngestionRecord for %s",
                ingestion_id,
            )
