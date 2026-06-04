"""
Ingest routes.

``POST /ingest`` accepts a single media asset upload + a caller-supplied
``algs`` list (repeated multipart form field), runs the full
soft-binding + manifest + sign pipeline, persists the record to MongoDB,
auto-pushes to the resolution API, and returns JSON describing the
resulting artifacts with download URLs.

Helpers:
- ``GET    /ingestions``                  page through ingestions (cursor)
- ``GET    /ingest/{ingestionId}``        ingestion record (from MongoDB)
- ``GET    /ingest/{ingestionId}/asset``    download signed asset
- ``GET    /ingest/{ingestionId}/manifest`` download raw signed manifest bytes
- ``DELETE /ingest/{ingestionId}``        local takedown (artifacts + record)

Edge concerns owned here:
- 415 for unsupported MIME, 400 for caller-shape errors.
- 503 mapped from ``MissingSigningMaterialError`` (cert/key vanished).
- Hybrid absolute-URL builder: ``settings.public_base_url`` wins when set,
  otherwise ``request.base_url`` rewritten by ProxyHeadersMiddleware.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from ingestion_api.contracts.ingestion import IngestionRequest
from ingestion_api.core.config import settings
from ingestion_api.core.credentials import MissingSigningMaterialError
from ingestion_api.core.errors import IngestionError, InvalidAlgRequestError, UnsupportedMediaError
from ingestion_api.core.logging import get_logger
from ingestion_api.models.enums import IngestionStatus, ResolutionPushStatus
from ingestion_api.models.ingestion import IngestionRecord
from ingestion_api.models.responses import (
    IngestionListResponse,
    IngestionResponse,
    ResolutionPushOutput,
)
from ingestion_api.repositories.artifacts import ArtifactStore
from ingestion_api.repositories.ingestions import IngestionListPage, IngestionRecordRepository
from ingestion_api.routers.dependencies import (
    get_artifact_store,
    get_ingestion_service,
    get_record_repository,
)
from ingestion_api.services.ingestion import IngestionService
from ingestion_api.utils.media import SUPPORTED_EXTENSIONS

logger = get_logger(__name__)
router = APIRouter(tags=["ingest"])

# Manifest download Content-Type
# matches what the resolution-api push uses on the wire and what c2pa-rs expects on inspection.
_MANIFEST_MEDIA_TYPE = "application/c2pa"
_OCTET_STREAM = "application/octet-stream"
_UPLOAD_READ_CHUNK_SIZE = 1 << 20


# ---------------------------------------------------------------------------
# Response builders.
# ---------------------------------------------------------------------------


def _response_base_url(request: Request) -> str:
    """Hybrid absolute-URL base.

    ``settings.public_base_url`` wins when set
    (production: pin the canonical host regardless of forwarded headers).
    Otherwise fallback to ``request.base_url``, which has been rewritten by the
    ProxyHeadersMiddleware to honor X-Forwarded-Proto/Host.
    """
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def _build_response(
    record: IngestionRecord,
    request: Request,
    artifacts: ArtifactStore,
) -> IngestionResponse:
    base = _response_base_url(request)
    output_url = f"{base}/ingest/{record.ingestionId}/asset"
    # Derive manifestUrl from the artifact store rather than persisting
    # path metadata on the record.
    manifest_url = (
        f"{base}/ingest/{record.ingestionId}/manifest"
        if artifacts.load_manifest_bytes_path(record.ingestionId)
        else None
    )
    push_result = ResolutionPushOutput(
        status=record.resolutionPushStatus,
        error=record.resolutionPushError,
    )
    return IngestionResponse(
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


def _signed_asset_media_type(path) -> str:
    """Infer served MIME from the deterministic signed artifact extension."""
    return SUPPORTED_EXTENSIONS.get(path.suffix.lower(), _OCTET_STREAM)


async def _read_upload_file_with_limit(file: UploadFile) -> bytes:
    max_size = settings.max_upload_size_bytes
    total = 0
    data = bytearray()

    while chunk := await file.read(_UPLOAD_READ_CHUNK_SIZE):
        total += len(chunk)
        if total > max_size:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds maximum allowed size of {max_size} bytes",
            )
        data.extend(chunk)

    return bytes(data)


# ---------------------------------------------------------------------------
# Routes.
# ---------------------------------------------------------------------------


@router.post(
    "/ingest",
    response_model=IngestionResponse,
    summary="Ingest a media asset: apply soft-bindings, build C2PA manifest, sign, store, push",
    responses={
        200: {"description": "Asset ingested successfully"},
        400: {"description": "Empty upload, empty / unknown / MIME-incompatible algs"},
        415: {"description": "Unsupported media format"},
        500: {"description": "Pipeline failure"},
        503: {"description": "Service not ready (signing material missing or plugin unreachable)"},
    },
)
async def ingest_media(
    request: Request,
    file: UploadFile = File(
        ...,
        description=(
            "Media asset to ingest. MIME / extension must match a supported "
            "type (audio/wav, audio/flac — lossy formats temporarily disabled)."
        ),
    ),
    algs: list[str] = Form(
        ...,
        description=(
            "Ordered list of soft-binding algorithm IDs to apply. Each must "
            "exist in the supported_algorithms catalog and declare the upload's "
            "MIME in its `mediaTypes`. Order matters: watermark passes mutate "
            "bytes for subsequent passes. We advise putting watermarks first. "
            "Pass repeated form fields: `algs=a&algs=b`. "
            f"Capped at {settings.max_algs_per_ingest} entries per request."
        ),
    ),
    title: str | None = Form(
        None,
        description=(
            "Optional manifest title. "
            f"Capped at {settings.max_title_length} characters."
        ),
    ),
    service: IngestionService = Depends(get_ingestion_service),
    artifacts: ArtifactStore = Depends(get_artifact_store),
) -> IngestionResponse:
    if len(algs) > settings.max_algs_per_ingest:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Too many algs: {len(algs)} (max {settings.max_algs_per_ingest})."
            ),
        )
    if title is not None and len(title) > settings.max_title_length:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Title too long: {len(title)} chars "
                f"(max {settings.max_title_length})."
            ),
        )

    data = await _read_upload_file_with_limit(file)
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload")
    if len(data) > settings.max_upload_size_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds maximum allowed size of {settings.max_upload_size_bytes} bytes",
        )

    payload = IngestionRequest(
        filename=file.filename or "upload.bin",
        content_type=file.content_type,
        data=data,
        algs=algs,
        title=title,
    )

    try:
        result = await service.ingest(payload)
    except UnsupportedMediaError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except InvalidAlgRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MissingSigningMaterialError as exc:
        # Cert/key vanished or became unreadable mid-flight.
        logger.exception("Missing signing material")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except IngestionError as exc:
        logger.exception("Ingestion error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected ingest failure")
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {exc}") from exc

    return _build_response(result.record, request, artifacts)


@router.get(
    "/ingestions",
    response_model=IngestionListResponse,
    summary="List ingestions newest-first (cursor pagination)",
    responses={
        200: {"description": "Page of ingestions"},
        400: {"description": "Invalid cursor"},
    },
)
async def list_ingestions(
    limit: int = Query(50, ge=1, le=200, description="Page size, 1..200"),
    cursor: str | None = Query(
        None, description="Opaque cursor from a previous page's nextCursor",
    ),
    status: IngestionStatus | None = Query(
        None,
        description="Filter to ingestions with this lifecycle status",
    ),
    resolutionPushStatus: ResolutionPushStatus | None = Query(  # noqa: N803
        None,
        description=(
            "Filter to ingestions with this push status (ok / failed / skipped)"
        ),
    ),
    records: IngestionRecordRepository = Depends(get_record_repository),
) -> IngestionListResponse:
    try:
        page: IngestionListPage = await records.list(
            limit=limit,
            cursor=cursor,
            ingestion_status=status,
            resolution_push_status=resolutionPushStatus,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return IngestionListResponse(
        items=page.items,
        nextCursor=page.next_cursor,
    )


@router.get(
    "/ingest/{ingestionId}",
    response_model=IngestionRecord,
    summary="Get ingestion record",
    responses={404: {"description": "Ingestion not found"}},
)
async def get_ingestion(
    ingestionId: str,
    records: IngestionRecordRepository = Depends(get_record_repository),
) -> IngestionRecord:
    record = await records.get(ingestionId)
    if record is None:
        raise HTTPException(status_code=404, detail="Ingestion not found")
    return record


@router.head(
    "/ingest/{ingestionId}/asset",
    summary="Check the signed media asset",
    responses={
        200: {"description": "Signed media headers"},
        404: {"description": "Ingestion or signed asset not found"},
    },
)
@router.get(
    "/ingest/{ingestionId}/asset",
    summary="Download the signed media asset",
    responses={
        200: {"description": "Signed media bytes"},
        404: {"description": "Ingestion or signed asset not found"},
    },
)
async def get_signed_asset(
    ingestionId: str,
    artifacts: ArtifactStore = Depends(get_artifact_store),
):
    path = artifacts.load_signed_asset(ingestionId)
    if path is None:
        raise HTTPException(status_code=404, detail="Signed asset not found")
    return FileResponse(path, media_type=_signed_asset_media_type(path), filename=path.name)


@router.head(
    "/ingest/{ingestionId}/manifest",
    summary="Check the raw signed C2PA manifest bytes",
    responses={
        200: {"description": "Raw manifest headers"},
        404: {"description": "Ingestion or manifest bytes not found"},
    },
)
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
    return FileResponse(path, media_type=_MANIFEST_MEDIA_TYPE, filename=path.name)


@router.delete(
    "/ingest/{ingestionId}",
    status_code=204,
    summary="Delete an ingestion (artifacts + record). Local only.",
    responses={
        204: {"description": "Deleted (or nothing was there to delete)"},
        404: {"description": "Neither artifacts nor a record exist for this id"},
    },
)
async def delete_ingestion(
    ingestionId: str,
    artifacts: ArtifactStore = Depends(get_artifact_store),
    records: IngestionRecordRepository = Depends(get_record_repository),
):
    """Local takedown: removes artifacts on disk and the Mongo record.

    Resolution-api is intentionally NOT touched here.

    Idempotent: succeeds with 204 even when only one of the two states exists.
    Returns 404 only when there is genuinely nothing on either side to remove.
    """
    artifacts_existed = artifacts.delete(ingestionId)
    record_existed = await records.delete(ingestionId)
    if not (artifacts_existed or record_existed):
        raise HTTPException(status_code=404, detail="Ingestion not found")
    logger.info(
        "Deleted ingestion %s (artifacts=%s record=%s)",
        ingestionId, artifacts_existed, record_existed,
    )
    return Response(status_code=204)


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
