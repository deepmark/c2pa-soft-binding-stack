"""
Audio ingestion orchestrator.

End-to-end pipeline:

    upload bytes
        -> persist as ``original.<ext>``
        -> derive deterministic 128-bit binding value (dummy SHA-256[:16])
        -> embed (dummy passthrough)
        -> build C2PA manifest (EDIT intent: parent ingredient + c2pa.opened
           injected automatically; we add c2pa.watermarked.bound + the
           c2pa.soft-binding assertion)
        -> sign (Builder.sign_file)
        -> persist signed asset + manifest bytes + metadata
        -> upsert metadata into Mongo

Failure modes are handled by writing a ``status="failed"`` record and
re-raising. The orchestrator is async only because it touches Mongo; the
SDK calls themselves are blocking and are run in a thread pool.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from soft_binding_api.core.config import settings
from soft_binding_api.core.logging import get_logger
from soft_binding_api.models.ingestion import IngestionRecord, IngestionStatus
from soft_binding_api.services.manifest import ManifestBuilderService
from soft_binding_api.services.registry import resolve_watermark_embedder
from soft_binding_api.services.signing import SigningService
from soft_binding_api.services.storage import (
    IngestionArtifacts,
    IngestionRepository,
    LocalAssetStore,
)
from soft_binding_api.services.watermark import compute_binding_value
from soft_binding_api.utils.audio import (
    SUPPORTED_AUDIO_EXTENSIONS,
    SUPPORTED_AUDIO_MIME_TYPES,
    guess_audio_format,
)
from soft_binding_api.utils.ids import new_ingestion_id, new_manifest_urn

logger = get_logger(__name__)


class IngestionError(RuntimeError):
    """Generic ingestion failure with a stable HTTP-friendly message."""


class UnsupportedAudioFormatError(IngestionError):
    pass


@dataclass(slots=True)
class IngestionInput:
    filename: str
    content_type: Optional[str]
    data: bytes
    title: Optional[str] = None


@dataclass(slots=True)
class IngestionResult:
    record: IngestionRecord
    signed_asset_path: Path
    manifest_bytes_path: Optional[Path]


class IngestionService:
    """
    Holds the long-lived collaborators (signer + builder + storage).
    Construct once, reuse per request.
    """

    def __init__(
        self,
        *,
        signing_service: SigningService,
        local_store: LocalAssetStore,
        repo: IngestionRepository,
        soft_binding_alg: Optional[str] = None,
    ) -> None:
        self._signing_service = signing_service
        self._local_store = local_store
        self._repo = repo
        self._alg = soft_binding_alg or settings.default_audio_alg

    async def ingest(self, payload: IngestionInput) -> IngestionResult:
        """Run the full pipeline and persist the result."""
        mime_type = guess_audio_format(payload.filename, payload.content_type)
        if mime_type is None:
            raise UnsupportedAudioFormatError(
                f"Unsupported audio format: filename={payload.filename!r} "
                f"content_type={payload.content_type!r}. "
                f"Supported: {sorted(SUPPORTED_AUDIO_MIME_TYPES)}"
            )

        ext = "." + SUPPORTED_AUDIO_MIME_TYPES[mime_type]
        # Prefer the uploaded filename's extension if it's already valid;
        # falls back to the canonical one for the MIME.
        upload_ext = Path(payload.filename).suffix.lower()
        if upload_ext in SUPPORTED_AUDIO_EXTENSIONS:
            ext = upload_ext

        ingestion_id = new_ingestion_id()
        artifacts = self._local_store.allocate(ingestion_id, ext=ext)

        try:
            return await self._run_pipeline(
                payload, mime_type, ingestion_id, artifacts,
            )
        except Exception as exc:
            await self._record_failure(ingestion_id, payload, mime_type, artifacts, exc)
            self._local_store.cleanup(artifacts)
            raise

    async def _run_pipeline(
        self,
        payload: IngestionInput,
        mime_type: str,
        ingestion_id: str,
        artifacts: IngestionArtifacts,
    ) -> IngestionResult:
        # 1. Derive binding value (deterministic dummy) from the upload bytes.
        binding_value = compute_binding_value(payload.data)

        # 2. Embed in memory (dummy passthrough; real embedders would
        #    modulate the audio signal in-place here).
        embedder = resolve_watermark_embedder(self._alg)
        if embedder is None:
            raise IngestionError(
                f"No watermark embedder registered for alg={self._alg!r}. "
                "Did you forget to import soft_binding_api.services.watermark?"
            )
        watermarked = embedder(payload.data, binding_value)

        # 3. Build + sign manifest. The watermarked bytes go straight into
        #    the SDK via an in-memory stream; only the signed output is
        #    persisted. Run in a thread because the SDK is sync.
        builder = self._make_manifest_builder()
        loop = asyncio.get_running_loop()
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

        # 4. Resolve manifest ID. Best-effort: try to read it back from the
        #    signed asset; fall back to a synthetic URN so the response is
        #    always populated.
        manifest_id = await loop.run_in_executor(
            None, self._read_active_manifest_label, artifacts.signed_path,
        )
        if not manifest_id:
            manifest_id = new_manifest_urn()

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
        )
        self._local_store.write_metadata(artifacts, record)
        await self._repo.insert(record)

        logger.info(
            "Ingestion %s OK alg=%s manifestId=%s",
            ingestion_id, self._alg, manifest_id,
        )
        return IngestionResult(
            record=record,
            signed_asset_path=artifacts.signed_path,
            manifest_bytes_path=manifest_bytes_path,
        )

    def _make_manifest_builder(self) -> ManifestBuilderService:
        return ManifestBuilderService(
            signer=self._signing_service.signer,
            soft_binding_alg=self._alg,
        )

    @staticmethod
    def _read_active_manifest_label(signed_path: Path) -> Optional[str]:
        """
        Try to read the active manifest label from the signed asset using
        the SDK's ``Reader``. Returns ``None`` if the asset can't be read
        (e.g. test fixture too small for the SDK to parse).
        """
        try:
            from c2pa import Reader  # local import to keep service module import-light
            with Reader(str(signed_path)) as reader:
                manifest_json = reader.json()
                # ``Reader.json()`` returns a JSON string; pull
                # active_manifest out without parsing more than necessary.
                import json
                data = json.loads(manifest_json)
                return data.get("active_manifest")
        except Exception:
            logger.debug("Could not read active manifest label from %s", signed_path,
                         exc_info=True)
            return None

    async def _record_failure(
        self,
        ingestion_id: str,
        payload: IngestionInput,
        mime_type: Optional[str],
        artifacts: IngestionArtifacts,
        exc: BaseException,
    ) -> None:
        try:
            now = datetime.now(timezone.utc)
            record = IngestionRecord(
                ingestionId=ingestion_id,
                originalFilename=payload.filename,
                originalMimeType=mime_type or "application/octet-stream",
                outputAssetPath=str(artifacts.signed_path),
                manifestBytesPath=None,
                alg=self._alg,
                bindingValue="",
                manifestId=None,
                signingAlg=self._signing_service.credentials.signing_alg,
                taUrl=self._signing_service.credentials.ta_url,
                signedAt=None,
                createdAt=now,
                status=IngestionStatus.FAILED,
                error=str(exc),
            )
            await self._repo.insert(record)
        except Exception:
            logger.exception("Failed to persist failure record for %s", ingestion_id)
