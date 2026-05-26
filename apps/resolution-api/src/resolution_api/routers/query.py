"""
Query route group

Searches for matching manifests using a soft binding. The soft binding value
is either provided by the caller, or is computed from an asset.
"""
import asyncio
import base64
import ipaddress
import socket
import typing
from urllib.parse import urlparse

import httpcore
import httpx
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile

from resolution_api.core.config import settings
from resolution_api.core.database import get_soft_bindings_collection
from resolution_api.core.logging import get_logger
from resolution_api.models import (
    AssetReferenceQuery,
    ManifestMatch,
    SoftBindingQuery,
    SoftBindingQueryResult,
)
from resolution_api.services.plugins_catalog import (
    AsyncPluginClient,
    PluginEntry,
    PluginNotFoundError,
    PluginUnavailableError,
    load_all_plugins,
    resolve,
)

logger = get_logger(__name__)

router = APIRouter(tags=["query"])


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Network backend that forces TCP connections to a pre-resolved IP."""

    def __init__(self, resolved_ip: str) -> None:
        self._resolved_ip = resolved_ip
        self._backend = httpcore._backends.auto.AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[typing.Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._backend.connect_tcp(
            self._resolved_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


def _make_pinned_transport(resolved_ip: str) -> httpx.AsyncHTTPTransport:
    """Create an httpx transport that pins all connections to resolved_ip."""
    transport = httpx.AsyncHTTPTransport(verify=True)
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=transport._pool._ssl_context,
        network_backend=_PinnedBackend(resolved_ip),
    )
    return transport


async def _resolve_or_400(alg: str) -> PluginEntry:
    try:
        return await resolve(alg)
    except PluginNotFoundError:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported algorithm: '{alg}' is not registered",
        )


_BLOCKED_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "169.254.169.254",
    }
)


def _is_blocked_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if the resolved address must not be reached."""
    if addr.is_private:
        return True
    if addr.is_loopback:
        return True
    if addr.is_link_local:
        return True
    if addr.is_multicast:
        return True
    if addr.is_reserved:
        return True
    if addr.is_unspecified:
        return True
    # CGN (Carrier-Grade NAT) range not covered by is_private in older Python
    if isinstance(addr, ipaddress.IPv4Address):
        if addr in ipaddress.IPv4Network("100.64.0.0/10"):
            return True
    return False


async def _validate_and_resolve_url(url_str: str) -> tuple[str, int, str]:
    """Resolve hostname, validate all resulting IPs, return (ip, port, hostname).

    Raises HTTPException(400) if the URL targets a blocked address.
    """
    parsed = urlparse(url_str)
    hostname = parsed.hostname
    port = parsed.port or 443

    if not hostname:
        raise HTTPException(status_code=400, detail="Invalid referenceUrl: no hostname")

    if hostname.lower() in _BLOCKED_METADATA_HOSTS:
        raise HTTPException(
            status_code=400,
            detail="Invalid request body: referenceUrl points to a blocked host",
        )

    try:
        loop = asyncio.get_running_loop()
        addrinfos = await loop.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise HTTPException(
            status_code=400,
            detail="Invalid request body: referenceUrl hostname could not be resolved",
        )

    if not addrinfos:
        raise HTTPException(
            status_code=400,
            detail="Invalid request body: referenceUrl hostname could not be resolved",
        )

    for _family, _, _, _, sockaddr in addrinfos:
        ip = ipaddress.ip_address(sockaddr[0])
        if _is_blocked_ip(ip):
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: referenceUrl points to a blocked host",
            )

    # Return first resolved IP to pin the connection
    resolved_ip = addrinfos[0][4][0]
    return resolved_ip, port, hostname


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
        entries_to_try = [await _resolve_or_400(alg)]
    else:
        catalog = await load_all_plugins()
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
    except Exception:
        logger.exception("Unexpected error")
        raise HTTPException(status_code=500, detail="Service failure")


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
    except Exception:
        logger.exception("Unexpected error")
        raise HTTPException(status_code=500, detail="Service failure")


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

        max_size = settings.max_upload_size_bytes
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(1 << 20)  # 1 MiB
            if not chunk:
                break
            total += len(chunk)
            if total > max_size:
                raise HTTPException(
                    status_code=413,
                    detail=f"Invalid request body: file exceeds maximum allowed size of {max_size} bytes",
                )
            chunks.append(chunk)

        if total == 0:
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: uploaded file is empty",
            )

        content = b"".join(chunks)
        return await _detect_from_bytes(content, alg=alg, maxResults=maxResults)

    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error")
        raise HTTPException(status_code=500, detail="Service failure")


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

        if query.assetLength > settings.max_download_size_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid request body: assetLength exceeds maximum allowed size of {settings.max_download_size_bytes} bytes",
            )

        # SSRF prevention: resolve hostname, reject internal/metadata IPs,
        # pin connection to the resolved IP, disable redirects.
        url_str = str(query.referenceUrl)
        resolved_ip, port, hostname = await _validate_and_resolve_url(url_str)

        # Download the asset with size limit.
        # Connection is pinned to the pre-validated resolved IP to prevent
        # DNS rebinding between our check and the actual TCP connect.
        try:
            async with httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=False,
                transport=_make_pinned_transport(resolved_ip),
            ) as client:
                async with client.stream("GET", url_str) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        raise HTTPException(
                            status_code=400,
                            detail="Invalid request body: referenceUrl returned a redirect",
                        )
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
    except Exception:
        logger.exception("Unexpected error")
        raise HTTPException(status_code=500, detail="Service failure")
