"""
Ingest routes.

``POST /ingest`` accepts a single audio file upload, runs the full
watermark + manifest + sign pipeline, auto-pushes to the resolution API,
and returns JSON describing the resulting artifacts with download URLs.

Helpers:
- ``GET /ingest/{ingestionId}/asset``    download signed audio
- ``GET /ingest/{ingestionId}/manifest`` download raw signed manifest bytes
- ``GET /ingest/{ingestionId}``          ingestion metadata sidecar
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ingestion_api.core.logging import get_logger
from ingestion_api.models.ingestion import (
    IngestionRecord,
    IngestResponse,
    ResolutionPushResult,
    ResolutionPushStatus,
)
from ingestion_api.services.orchestrator import (
    IngestionError,
    IngestionInput,
    IngestionService,
    UnsupportedAudioFormatError,
)
from ingestion_api.services.storage import LocalAssetStore

logger = get_logger(__name__)
router = APIRouter(tags=["ingest"])


def get_ingestion_service(request: Request) -> IngestionService:
    svc: IngestionService | None = getattr(request.app.state, "ingestion_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="Ingestion service not initialised (check /ready)",
        )
    return svc


def get_local_store(request: Request) -> LocalAssetStore:
    store: LocalAssetStore | None = getattr(request.app.state, "local_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Local store not initialised")
    return store


def _build_response(record: IngestionRecord, request: Request) -> IngestResponse:
    base = str(request.base_url).rstrip("/")
    output_url = f"{base}/ingest/{record.ingestionId}/asset"
    manifest_url = (
        f"{base}/ingest/{record.ingestionId}/manifest"
        if record.manifestBytesPath
        else None
    )
    push_result = ResolutionPushResult(
        status=record.resolutionPushStatus,
        error=record.resolutionPushError,
    )
    return IngestResponse(
        ingestionId=record.ingestionId,
        manifestId=record.manifestId,
        alg=record.alg,
        bindingValue=record.bindingValue,
        originalFilename=record.originalFilename,
        originalMimeType=record.originalMimeType,
        outputAssetUrl=output_url,
        manifestUrl=manifest_url,
        signingAlg=record.signingAlg,
        taUrl=record.taUrl,
        signedAt=record.signedAt,
        createdAt=record.createdAt,
        resolutionPush=push_result,
    )


@router.post(
    "/ingest",
    response_model=IngestResponse,
    summary="Ingest an audio asset: watermark, build C2PA manifest, sign, store, push",
    responses={
        200: {"description": "Asset ingested successfully"},
        400: {"description": "Unsupported audio format / empty upload"},
        500: {"description": "Pipeline failure"},
        503: {"description": "Service not ready (certs missing or plugin unreachable)"},
    },
)
async def ingest_audio(
    request: Request,
    file: UploadFile = File(..., description="Audio file (WAV preferred)"),
    title: str | None = Form(None, description="Optional manifest title"),
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestResponse:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload")

    payload = IngestionInput(
        filename=file.filename or "upload.bin",
        content_type=file.content_type,
        data=data,
        title=title,
    )

    try:
        result = await service.ingest(payload)
    except UnsupportedAudioFormatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IngestionError as exc:
        logger.exception("Ingestion error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        # Missing certs surface as a clean 503.
        logger.exception("Missing signing material")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected ingest failure")
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {exc}") from exc

    return _build_response(result.record, request)


@router.get(
    "/ingest/{ingestionId}",
    response_model=IngestionRecord,
    summary="Get ingestion metadata",
    responses={404: {"description": "Ingestion not found"}},
)
async def get_ingestion(
    ingestionId: str,
    store: LocalAssetStore = Depends(get_local_store),
) -> IngestionRecord:
    record = store.load_metadata(ingestionId)
    if record is None:
        raise HTTPException(status_code=404, detail="Ingestion not found")
    return record


@router.get(
    "/ingest/{ingestionId}/asset",
    summary="Download the signed audio asset",
    responses={
        200: {"description": "Signed audio bytes"},
        404: {"description": "Ingestion or signed asset not found"},
    },
)
async def get_signed_asset(
    ingestionId: str,
    store: LocalAssetStore = Depends(get_local_store),
):
    path = store.load_signed_asset(ingestionId)
    if path is None:
        raise HTTPException(status_code=404, detail="Signed asset not found")
    record = store.load_metadata(ingestionId)
    media_type = record.originalMimeType if record else "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name)


@router.get(
    "/ingest/{ingestionId}/manifest",
    summary="Download the raw signed C2PA manifest bytes",
    responses={
        200: {"description": "Raw manifest bytes"},
        404: {"description": "Ingestion or manifest bytes not found"},
    },
)
async def get_manifest_bytes(
    ingestionId: str,
    store: LocalAssetStore = Depends(get_local_store),
):
    path = store.load_manifest_bytes_path(ingestionId)
    if path is None:
        raise HTTPException(status_code=404, detail="Manifest bytes not stored")
    return FileResponse(path, media_type="application/c2pa", filename=path.name)


@router.get(
    "/ingest",
    summary="Lightweight ingest endpoint info",
    include_in_schema=False,
)
async def ingest_root() -> JSONResponse:
    # Suppress unused import warning when ResolutionPushStatus isn't referenced
    # anywhere else in this module (keeps the model API surface explicit).
    _ = ResolutionPushStatus
    return JSONResponse(
        {"detail": "POST /ingest with a multipart form field `file=` to ingest audio."}
    )
