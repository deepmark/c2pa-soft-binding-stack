"""
Ingest routes.

``POST /ingest`` accepts a single media asset upload + a caller-supplied
``algs`` list (repeated multipart form field), runs the full
soft-binding + manifest + sign pipeline, persists the record to MongoDB,
auto-pushes to the resolution API, and returns JSON describing the
resulting artifacts with download URLs.

Helpers:
- ``GET /ingest/{ingestionId}/asset``    download signed asset
- ``GET /ingest/{ingestionId}/manifest`` download raw signed manifest bytes
- ``GET /ingest/{ingestionId}``          ingestion record (from MongoDB)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ingestion_api.core.logging import get_logger
from ingestion_api.models.ingestion import (
    IngestionRecord,
    IngestResponse,
    ResolutionPushResult,
)
from ingestion_api.services.artifact_store import ArtifactStore
from ingestion_api.services.orchestrator import (
    IngestionError,
    IngestionInput,
    IngestionService,
    InvalidAlgRequestError,
    UnsupportedMediaError,
)
from ingestion_api.services.record_repository import MongoIngestionRecordRepository

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


def get_artifact_store(request: Request) -> ArtifactStore:
    store: ArtifactStore | None = getattr(request.app.state, "artifacts", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Artifact store not initialised")
    return store


def get_record_repository(request: Request) -> MongoIngestionRecordRepository:
    repo: MongoIngestionRecordRepository | None = getattr(request.app.state, "records", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="Record repository not initialised")
    return repo


def _build_response(
    record: IngestionRecord,
    request: Request,
    artifacts: ArtifactStore,
) -> IngestResponse:
    base = str(request.base_url).rstrip("/")
    output_url = f"{base}/ingest/{record.ingestionId}/asset"
    # Derive manifestUrl from the artifact store rather than persisting
    # path metadata on the record. Cheap (single stat() call).
    manifest_url = (
        f"{base}/ingest/{record.ingestionId}/manifest"
        if artifacts.load_manifest_bytes_path(record.ingestionId)
        else None
    )
    push_result = ResolutionPushResult(
        status=record.resolutionPushStatus,
        error=record.resolutionPushError,
    )
    return IngestResponse(
        ingestionId=record.ingestionId,
        manifestId=record.manifestId,
        softBindings=record.softBindings,
        mimeType=record.mimeType,
        mediaType=record.mediaType,
        assetSha256=record.assetSha256,
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
    summary="Ingest a media asset: apply soft-bindings, build C2PA manifest, sign, store, push",
    responses={
        200: {"description": "Asset ingested successfully"},
        400: {
            "description": (
                "Unsupported media format, empty upload, empty / unknown / "
                "MIME-incompatible algs"
            ),
        },
        500: {"description": "Pipeline failure"},
        503: {"description": "Service not ready (certs missing or plugin unreachable)"},
    },
)
async def ingest_audio(
    request: Request,
    file: UploadFile = File(
        ...,
        description=(
            "Media asset to ingest. MIME / extension must match a supported "
            "type (audio/wav, audio/mpeg, audio/flac, audio/ogg today)."
        ),
    ),
    algs: list[str] = Form(
        ...,
        description=(
            "Ordered list of soft-binding algorithm IDs to apply. Each must "
            "exist in algorithms.yaml and declare the upload's MIME in its "
            "`mediaTypes`. Order matters: watermark passes mutate bytes for "
            "subsequent passes (put watermarks first). "
            "Pass repeated form fields: `algs=a&algs=b`."
        ),
    ),
    title: str | None = Form(None, description="Optional manifest title"),
    service: IngestionService = Depends(get_ingestion_service),
    artifacts: ArtifactStore = Depends(get_artifact_store),
) -> IngestResponse:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload")

    payload = IngestionInput(
        filename=file.filename or "upload.bin",
        content_type=file.content_type,
        data=data,
        algs=algs,
        title=title,
    )

    try:
        result = await service.ingest(payload)
    except (UnsupportedMediaError, InvalidAlgRequestError) as exc:
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

    return _build_response(result.record, request, artifacts)


@router.get(
    "/ingest/{ingestionId}",
    response_model=IngestionRecord,
    summary="Get ingestion record",
    responses={404: {"description": "Ingestion not found"}},
)
async def get_ingestion(
    ingestionId: str,
    records: MongoIngestionRecordRepository = Depends(get_record_repository),
) -> IngestionRecord:
    record = await records.get(ingestionId)
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
    artifacts: ArtifactStore = Depends(get_artifact_store),
    records: MongoIngestionRecordRepository = Depends(get_record_repository),
):
    path = artifacts.load_signed_asset(ingestionId)
    if path is None:
        raise HTTPException(status_code=404, detail="Signed asset not found")
    record = await records.get(ingestionId)
    media_type = record.mimeType if record else "application/octet-stream"
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
    artifacts: ArtifactStore = Depends(get_artifact_store),
):
    path = artifacts.load_manifest_bytes_path(ingestionId)
    if path is None:
        raise HTTPException(status_code=404, detail="Manifest bytes not stored")
    return FileResponse(path, media_type="application/c2pa", filename=path.name)


@router.get(
    "/ingest",
    summary="Lightweight ingest endpoint info",
    include_in_schema=False,
)
async def ingest_root() -> JSONResponse:
    return JSONResponse(
        {
            "detail": (
                "POST /ingest with multipart form fields `file=<asset>` and "
                "`algs=<alg-id>` (repeat for multiple algs)."
            ),
        },
    )
