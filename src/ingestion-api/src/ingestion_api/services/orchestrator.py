"""
Audio ingestion orchestrator.

End-to-end pipeline:

    upload bytes
        -> POST /embed to plugin container (raw octet-stream),
           receive watermarked bytes + binding value
        -> build C2PA manifest (EDIT intent: parent ingredient + c2pa.opened
           injected automatically; we add c2pa.watermarked.bound + the
           c2pa.soft-binding assertion)
        -> sign (Builder.sign)
        -> persist signed asset + manifest bytes + metadata sidecar
        -> auto-push manifest store + binding to resolution-api

Bytes never touch disk on the ingestion-api side until we write the
signed output. The plugin call is purely HTTP, so plugin containers can
live on a different host (no shared volume required).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ingestion_api.core.config import settings
from ingestion_api.core.logging import get_logger
from ingestion_api.models.ingestion import (
    IngestionRecord,
    IngestionStatus,
    ResolutionPushResult,
    ResolutionPushStatus,
)
from ingestion_api.services.algorithms import (
    AlgorithmNotFoundError,
    EmbedResult,
    PluginClient,
    PluginUnavailableError,
    resolve as resolve_algorithm,
)
from ingestion_api.services.manifest import ManifestBuilderService
from ingestion_api.services.resolution import (
    ResolutionPushClient,
    ResolutionPushRequest,
)
from ingestion_api.services.signing import SigningService
from ingestion_api.services.storage import IngestionArtifacts, LocalAssetStore
from ingestion_api.utils.audio import (
    SUPPORTED_AUDIO_EXTENSIONS,
    SUPPORTED_AUDIO_MIME_TYPES,
    guess_audio_format,
)
from ingestion_api.utils.ids import new_ingestion_id, new_manifest_urn

logger = get_logger(__name__)


class IngestionError(RuntimeError):
    """Generic ingestion failure with a stable HTTP-friendly message."""


class UnsupportedAudioFormatError(IngestionError):
    pass


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


class IngestionService:
    """Holds the long-lived collaborators. Construct once, reuse per request."""

    def __init__(
        self,
        *,
        signing_service: SigningService,
        local_store: LocalAssetStore,
        resolution_client: ResolutionPushClient | None = None,
        soft_binding_alg: str | None = None,
    ) -> None:
        self._signing_service = signing_service
        self._local_store = local_store
        self._resolution_client = resolution_client or ResolutionPushClient()
        self._alg = soft_binding_alg or settings.default_audio_alg

    async def ingest(self, payload: IngestionInput) -> IngestionResult:
        mime_type = guess_audio_format(payload.filename, payload.content_type)
        if mime_type is None:
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
        artifacts = self._local_store.allocate(ingestion_id, ext=ext)

        try:
            return await self._run_pipeline(
                payload, mime_type, ext, ingestion_id, artifacts,
            )
        except Exception:
            self._local_store.cleanup(artifacts)
            raise

    async def _run_pipeline(
        self,
        payload: IngestionInput,
        mime_type: str,
        ext: str,
        ingestion_id: str,
        artifacts: IngestionArtifacts,
    ) -> IngestionResult:
        # 1. Resolve plugin and call /embed over HTTP.
        try:
            entry = resolve_algorithm(self._alg)
        except AlgorithmNotFoundError as exc:
            raise IngestionError(str(exc)) from exc

        loop = asyncio.get_running_loop()
        try:
            embed_result = await loop.run_in_executor(
                None,
                lambda: self._call_plugin_embed(entry, payload.data),
            )
        except PluginUnavailableError as exc:
            raise IngestionError(f"Plugin call failed: {exc}") from exc

        watermarked = embed_result.watermarked_bytes
        binding_value = embed_result.binding_value
        if not watermarked:
            raise IngestionError(
                f"Plugin {entry.alg!r} returned an empty watermarked payload",
            )

        # 2. Build + sign manifest. Run in a thread because the SDK is sync.
        builder = self._make_manifest_builder()
        signed_at = datetime.now(timezone.utc)
        built = await loop.run_in_executor(
            None,
            lambda: builder.build_and_sign(
                source_bytes=watermarked,
                dest_path=artifacts.signed_path,
                mime_type=mime_type,
                binding_value_b64=binding_value,
                title=payload.title or payload.filename,
            ),
        )

        if built.manifest_bytes:
            self._local_store.write_manifest_bytes(artifacts, built.manifest_bytes)
            manifest_bytes_path: Path | None = artifacts.manifest_bytes_path
        else:
            manifest_bytes_path = None

        # 3. Resolve manifest ID best-effort by reading back the signed asset.
        manifest_id = await loop.run_in_executor(
            None, self._read_active_manifest_label, artifacts.signed_path,
        )
        if not manifest_id:
            manifest_id = new_manifest_urn()

        # 4. Auto-push to resolution API.
        push_result, push_manifest_id = await loop.run_in_executor(
            None,
            lambda: self._resolution_client.push(
                ResolutionPushRequest(
                    manifest_bytes=built.manifest_bytes or b"",
                    alg=self._alg,
                    binding_value=binding_value,
                    fallback_manifest_id=manifest_id,
                ),
            ),
        )
        # If the resolution API minted its own manifestId, prefer that;
        # otherwise stick with whatever we read back from the signed asset.
        if push_result.status == ResolutionPushStatus.OK and push_manifest_id:
            manifest_id = push_manifest_id

        record = IngestionRecord(
            ingestionId=ingestion_id,
            originalFilename=payload.filename,
            originalMimeType=mime_type,
            outputAssetPath=str(artifacts.signed_path),
            manifestBytesPath=str(manifest_bytes_path) if manifest_bytes_path else None,
            alg=self._alg,
            bindingValue=binding_value,
            manifestId=manifest_id,
            signingAlg=self._signing_service.credentials.signing_alg,
            taUrl=self._signing_service.credentials.ta_url,
            signedAt=signed_at,
            createdAt=signed_at,
            status=IngestionStatus.OK,
            resolutionPushStatus=push_result.status,
            resolutionPushError=push_result.error,
        )
        self._local_store.write_metadata(artifacts, record)

        logger.info(
            "Ingestion %s OK alg=%s manifestId=%s push=%s",
            ingestion_id, self._alg, manifest_id, push_result.status.value,
        )
        return IngestionResult(
            record=record,
            signed_asset_path=artifacts.signed_path,
            manifest_bytes_path=manifest_bytes_path,
        )

    def _call_plugin_embed(self, entry, audio_bytes: bytes) -> EmbedResult:
        with PluginClient(entry) as plugin:
            return plugin.embed(audio_bytes=audio_bytes)

    def _make_manifest_builder(self) -> ManifestBuilderService:
        return ManifestBuilderService(
            signer=self._signing_service.signer,
            soft_binding_alg=self._alg,
        )

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
