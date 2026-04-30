"""
Query route group

Searches for matching manifests using a soft binding. The soft binding value
is either provided by the caller, or is computed from an asset.
"""
import base64

import httpx
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile

from resolution_api.core.database import get_soft_bindings_collection
from resolution_api.core.logging import get_logger
from resolution_api.models import (
    AssetReferenceQuery,
    ManifestMatch,
    SoftBindingQuery,
    SoftBindingQueryResult,
)
from resolution_api.services.algorithms_catalog import (
    AlgorithmEntry,
    AlgorithmNotFoundError,
    AsyncPluginClient,
    PluginUnavailableError,
    load_catalog,
    resolve,
)

logger = get_logger(__name__)

router = APIRouter(tags=["query"])


def _resolve_or_400(alg: str) -> AlgorithmEntry:
    try:
        return resolve(alg)
    except AlgorithmNotFoundError:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported algorithm: '{alg}' is not registered",
        )


async def _detect_from_bytes(
    content: bytes,
    *,
    alg: str | None,
    maxResults: int,
) -> SoftBindingQueryResult:
    """Shared detection + DB lookup used by byContent and byReference."""
    soft_bindings_col = get_soft_bindings_collection()
    all_matches: list[ManifestMatch] = []

    if alg:
        entries_to_try = [_resolve_or_400(alg)]
    else:
        catalog = load_catalog()
        entries_to_try = [
            e for e in catalog
            if e.type == "watermark" and e.url
        ]
        if not entries_to_try:
            raise HTTPException(
                status_code=400,
                detail="No watermark detectors registered in the system",
            )
        logger.info("No alg specified, trying %d registered detectors", len(entries_to_try))

    for entry in entries_to_try:
        try:
            client = AsyncPluginClient(entry)
            detected_value = await client.detect(audio_bytes=content)
        except PluginUnavailableError as exc:
            logger.warning("Detection failed for alg=%s: %s", entry.alg, exc)
            if alg:
                raise HTTPException(status_code=500, detail=f"Watermark detection failed: {exc}")
            continue

        if detected_value is None:
            logger.info("No watermark detected with alg=%s", entry.alg)
            continue

        logger.info("Watermark detected with alg=%s", entry.alg)
        docs = await soft_bindings_col.find(
            {"alg": entry.alg, "value": detected_value}
        ).to_list(length=maxResults)

        for doc in docs:
            all_matches.append(ManifestMatch(
                manifestId=doc.get("manifestId"),
                endpoint=doc.get("endpoint"),
            ))

    seen: set[str] = set()
    unique: list[ManifestMatch] = []
    for m in all_matches:
        if m.manifestId not in seen:
            seen.add(m.manifestId)
            unique.append(m)

    logger.info("Found %d unique matches", len(unique))
    return SoftBindingQueryResult(matches=unique[:maxResults])


@router.get(
    "/matches/byBinding",
    response_model=SoftBindingQueryResult,
    summary="Returns C2PA Manifest identifiers corresponding to a soft binding",
    responses={
        200: {"description": "Successful operation"},
        400: {"description": "Invalid query value"},
        414: {"description": "Query too long, consider using POST instead"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"},
    },
)
async def query_by_binding(
    request: Request,
    value: str = Query(..., description="Base64-encoded soft binding value"),
    alg: str = Query(..., description="Soft binding algorithm identifier"),
    maxResults: int = Query(10, ge=1, description="Maximum number of results"),
):
    """
    Given one soft binding, find zero or more manifest identifiers
    within the manifest store matching the soft binding.
    """
    try:
        # Check if URI is too long (414 error)
        # Most servers limit URLs to ~2048 chars, we'll use 2000 as threshold
        request_url = str(request.url)
        if len(request_url) > 2000:
            raise HTTPException(
                status_code=414,
                detail="Query too long, consider using POST instead",
            )

        # Validate query parameters (400 error)
        if not value or not value.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid query value: 'value' parameter cannot be empty",
            )

        if not alg or not alg.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid query value: 'alg' parameter cannot be empty",
            )

        # Validate base64 encoding
        try:
            base64.b64decode(value, validate=True)
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="Invalid query value: 'value' must be a valid base64-encoded string",
            )

        # Query the soft_bindings collection
        soft_bindings_col = get_soft_bindings_collection()

        # Find matching soft bindings
        docs = await soft_bindings_col.find(
            {"alg": alg, "value": value}
        ).to_list(length=maxResults)

        matches = [
            ManifestMatch(
                manifestId=doc.get("manifestId"),
                endpoint=doc.get("endpoint"),
            )
            for doc in docs
        ]
        return SoftBindingQueryResult(matches=matches)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")


@router.post(
    "/matches/byBinding",
    response_model=SoftBindingQueryResult,
    summary="Returns C2PA Manifest identifiers corresponding to a large soft binding value",
    responses={
        200: {"description": "Successful operation"},
        400: {"description": "Invalid request body"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"},
    },
)
async def query_by_large_binding(
    query: SoftBindingQuery,
    maxResults: int = Query(10, ge=1, description="Maximum number of results"),
):
    """
    Given a large soft binding value, find zero or more matching manifest identifiers.
    Use this method if the size of the soft binding value is too large to fit in a URL.
    """
    try:
        # Validate request body (400 error)
        if not query.value or not query.value.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: 'value' field cannot be empty",
            )

        if not query.alg or not query.alg.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: 'alg' field cannot be empty",
            )

        # Validate base64 encoding
        try:
            base64.b64decode(query.value, validate=True)
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: 'value' must be a valid base64-encoded string",
            )

        # Query the soft_bindings collection
        soft_bindings_col = get_soft_bindings_collection()

        # Find matching soft bindings
        docs = await soft_bindings_col.find(
            {"alg": query.alg, "value": query.value}
        ).to_list(length=maxResults)

        matches = [
            ManifestMatch(
                manifestId=doc.get("manifestId"),
                endpoint=doc.get("endpoint"),
            )
            for doc in docs
        ]
        return SoftBindingQueryResult(matches=matches)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")


@router.post(
    "/matches/byContent",
    response_model=SoftBindingQueryResult,
    summary="Finds C2PA Manifest identifiers using a digital asset",
    responses={
        200: {"description": "Successful operation"},
        400: {"description": "Invalid request body"},
        415: {"description": "Invalid asset type"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"},
    },
)
async def query_by_content(
    file: UploadFile = File(...),
    alg: str | None = Query(None, description="Soft binding algorithm identifier"),
    maxResults: int = Query(10, ge=1, description="Maximum number of results"),
    hintAlg: str | None = Query(None, description="Additional algorithm hint"),
    hintValue: str | None = Query(None, description="Additional value hint"),
):
    """
    Find zero or more C2PA Manifest identifiers within the manifest store
    using an uploaded file containing a digital asset.
    """
    try:
        if not file.content_type:
            raise HTTPException(
                status_code=415,
                detail="Invalid asset type: content type not specified",
            )

        supported_prefixes = ["audio/"]
        if not any(file.content_type.startswith(p) for p in supported_prefixes):
            raise HTTPException(
                status_code=415,
                detail=f"Invalid asset type: {file.content_type} is not supported",
            )

        content = await file.read()
        if not content:
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: uploaded file is empty",
            )

        return await _detect_from_bytes(content, alg=alg, maxResults=maxResults)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")


@router.post(
    "/matches/byReference",
    response_model=SoftBindingQueryResult,
    summary="Finds C2PA Manifest identifiers using a reference URL",
    responses={
        200: {"description": "Successful operation"},
        400: {"description": "Invalid request body"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"},
    },
)
async def query_by_reference(
    query: AssetReferenceQuery,
    alg: str | None = Query(None, description="Soft binding algorithm identifier"),
    maxResults: int = Query(10, ge=1, description="Maximum number of results"),
    hintAlg: str | None = Query(None, description="Additional algorithm hint"),
    hintValue: str | None = Query(None, description="Additional value hint"),
):
    """
    Optional endpoint to find zero or more C2PA Manifest identifiers within the manifest store
    by downloading an asset or parts of an asset via a reference HTTPS URL provided by the client.

    Security considerations:
    - Only HTTPS URLs are allowed
    - Asset size is limited based on assetLength parameter
    - Asset type validation is performed
    - SSRF attack prevention measures are applied
    """
    try:
        # Validate HTTPS URL (400 error)
        if not str(query.referenceUrl).startswith("https://"):
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: referenceUrl must use HTTPS",
            )

        # Validate asset length (400 error)
        if query.assetLength <= 0:
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: assetLength must be greater than 0",
            )

        # Set a maximum download size limit (e.g., 100MB) to prevent abuse
        MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024  # 100 MB
        if query.assetLength > MAX_DOWNLOAD_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid request body: assetLength exceeds maximum allowed size of {MAX_DOWNLOAD_SIZE} bytes",
            )

        # SSRF Prevention: block obvious local hosts.
        # In production, implement more comprehensive SSRF protection
        # (e.g., an allowlist of domains, blocking cloud metadata endpoints).
        blocked_hosts = ["localhost", "127.0.0.1", "0.0.0.0", "::1"]
        url_str = str(query.referenceUrl)
        if any(host in url_str.lower() for host in blocked_hosts):
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: referenceUrl points to a blocked host",
            )

        # Download the asset with size limit
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                async with client.stream("GET", str(query.referenceUrl)) as response:
                    response.raise_for_status()

                    # Validate content type if specified
                    if query.assetType:
                        content_type = response.headers.get("content-type", "")
                        if not content_type.startswith(query.assetType.split("/")[0]):
                            raise HTTPException(
                                status_code=400,
                                detail=f"Invalid request body: downloaded asset type '{content_type}' does not match expected type '{query.assetType}'",
                            )

                    # Download content with size limit
                    downloaded = 0
                    content_chunks = []
                    async for chunk in response.aiter_bytes():
                        downloaded += len(chunk)
                        if downloaded > query.assetLength:
                            raise HTTPException(
                                status_code=400,
                                detail="Invalid request body: actual asset size exceeds specified assetLength",
                            )
                        content_chunks.append(chunk)

                    content = b"".join(content_chunks)

        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid request body: failed to download asset from referenceUrl: {str(e)}",
            )

        return await _detect_from_bytes(content, alg=alg, maxResults=maxResults)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")
