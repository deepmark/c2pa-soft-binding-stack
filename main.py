from fastapi import FastAPI, HTTPException, Query, Request, File, UploadFile
from fastapi.responses import Response
from contextlib import asynccontextmanager
from typing import Optional
import base64
import io
import uuid
import httpx

from config import settings
from database import (
    MongoDB,
    get_manifest_blobs_bucket,
    get_manifests_collection,
    get_soft_bindings_collection,
    get_supported_algorithms_collection,
)
from models import (
    SoftBindingQueryResult,
    SoftBindingAlgList,
    ManifestMatch,
    SoftBindingQuery,
    AssetReferenceQuery,
    BindingsRequest,
    ManifestReceipt,
    VerifiedManifestReceipt,
    ManifestCreateResult
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    await MongoDB.connect()
    yield
    await MongoDB.close()

app = FastAPI(
    title=settings.api_title,
    version=settings.api_version,
    description=settings.api_description,
    lifespan=lifespan
)

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "C2PA Soft Binding Resolution API",
        "version": settings.api_version,
        "endpoints": {
            "query_by_binding": "/matches/byBinding",
            "get_manifest": "/manifests/{manifestId}",
            "supported_algorithms": "/services/supportedAlgorithms"
        }
    }

@app.get(
    "/matches/byBinding",
    response_model=SoftBindingQueryResult,
    tags=["query"],
    summary="Returns C2PA Manifest identifiers corresponding to a soft binding",
    responses={
        200: {"description": "Successful operation"},
        400: {"description": "Invalid query value"},
        414: {"description": "Query too long, consider using POST instead"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"}
    }
)
async def query_by_binding(
    request: Request,
    value: str = Query(..., description="Base64-encoded soft binding value"),
    alg: str = Query(..., description="Soft binding algorithm identifier"),
    maxResults: int = Query(10, ge=1, description="Maximum number of results")
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
                detail="Query too long, consider using POST instead"
            )

        # Validate query parameters (400 error)
        if not value or not value.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid query value: 'value' parameter cannot be empty"
            )

        if not alg or not alg.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid query value: 'alg' parameter cannot be empty"
            )

        # Validate base64 encoding
        try:
            base64.b64decode(value, validate=True)
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="Invalid query value: 'value' must be a valid base64-encoded string"
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
                similarityScore=doc.get("similarityScore"),
            )
            for doc in docs
        ]
        return SoftBindingQueryResult(matches=matches)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")

@app.post(
    "/matches/byBinding",
    response_model=SoftBindingQueryResult,
    tags=["query"],
    summary="Returns C2PA Manifest identifiers corresponding to a large soft binding value",
    responses={
        200: {"description": "Successful operation"},
        400: {"description": "Invalid request body"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"}
    }
)
async def query_by_large_binding(
    query: SoftBindingQuery,
    maxResults: int = Query(10, ge=1, description="Maximum number of results")
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
                detail="Invalid request body: 'value' field cannot be empty"
            )

        if not query.alg or not query.alg.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: 'alg' field cannot be empty"
            )

        # Validate base64 encoding
        try:
            base64.b64decode(query.value, validate=True)
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: 'value' must be a valid base64-encoded string"
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
                similarityScore=doc.get("similarityScore"),
            )
            for doc in docs
        ]
        return SoftBindingQueryResult(matches=matches)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")

@app.post(
    "/matches/byContent",
    response_model=SoftBindingQueryResult,
    tags=["query"],
    summary="Finds C2PA Manifest identifiers using a digital asset",
    responses={
        200: {"description": "Successful operation"},
        400: {"description": "Invalid request body"},
        415: {"description": "Invalid asset type"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"}
    }
)
async def query_by_content(
    file: UploadFile = File(...),
    alg: Optional[str] = Query(None, description="Soft binding algorithm identifier"),
    maxResults: int = Query(10, ge=1, description="Maximum number of results"),
    hintAlg: Optional[str] = Query(None, description="Additional algorithm hint"),
    hintValue: Optional[str] = Query(None, description="Additional value hint")
):
    """
    Find zero or more C2PA Manifest identifiers within the manifest store
    using an uploaded file containing a digital asset.
    """
    try:
        # Validate content type (415 error)
        if not file.content_type:
            raise HTTPException(
                status_code=415,
                detail="Invalid asset type: content type not specified"
            )

        # Check if content type is supported
        supported_types = ["image/", "audio/", "video/", "application/", "model/", "text/"]
        if not any(file.content_type.startswith(prefix) for prefix in supported_types):
            raise HTTPException(
                status_code=415,
                detail=f"Invalid asset type: {file.content_type} is not supported"
            )

        # Read file content
        content = await file.read()

        # Validate file is not empty (400 error)
        if not content:
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: uploaded file is empty"
            )

        # TODO
        # 1. Extract soft binding from the asset using the specified algorithm
        # 2. Query the database with the extracted binding (same way as in byBinding endpoints)
        # 3. Return matching manifests
        #
        # For now, we return empty results since actual soft binding extraction
        # requires algorithm-specific implementations!

        return SoftBindingQueryResult(matches=[])

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")

@app.post(
    "/matches/byReference",
    response_model=SoftBindingQueryResult,
    tags=["query"],
    summary="Finds C2PA Manifest identifiers using a reference URL",
    responses={
        200: {"description": "Successful operation"},
        400: {"description": "Invalid request body"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"}
    }
)
async def query_by_reference(
    query: AssetReferenceQuery,
    alg: Optional[str] = Query(None, description="Soft binding algorithm identifier"),
    maxResults: int = Query(10, ge=1, description="Maximum number of results"),
    hintAlg: Optional[str] = Query(None, description="Additional algorithm hint"),
    hintValue: Optional[str] = Query(None, description="Additional value hint")
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
                detail="Invalid request body: referenceUrl must use HTTPS"
            )

        # Validate asset length (400 error)
        if query.assetLength <= 0:
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: assetLength must be greater than 0"
            )

        # Set a maximum download size limit (e.g., 100MB) to prevent abuse
        MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024  # 100 MB
        if query.assetLength > MAX_DOWNLOAD_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid request body: assetLength exceeds maximum allowed size of {MAX_DOWNLOAD_SIZE} bytes"
            )

        # sta je ovo????
        # SSRF Prevention: Block private IP ranges
        # In production, implement more comprehensive SSRF protection
        # (e.g., using a allowlist of domains, blocking cloud metadata endpoints)
        blocked_hosts = ["localhost", "127.0.0.1", "0.0.0.0", "::1"]
        url_str = str(query.referenceUrl)
        if any(host in url_str.lower() for host in blocked_hosts):
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: referenceUrl points to a blocked host"
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
                                detail=f"Invalid request body: downloaded asset type '{content_type}' does not match expected type '{query.assetType}'"
                            )

                    # Download content with size limit
                    downloaded = 0
                    content_chunks = []
                    async for chunk in response.aiter_bytes():
                        downloaded += len(chunk)
                        if downloaded > query.assetLength:
                            raise HTTPException(
                                status_code=400,
                                detail="Invalid request body: actual asset size exceeds specified assetLength"
                            )
                        content_chunks.append(chunk)

                    content = b"".join(content_chunks)

        except httpx.HTTPError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid request body: failed to download asset from referenceUrl: {str(e)}"
            )

        # TODO
        # 1. Extract soft binding from the downloaded asset using the specified algorithm
        # 2. Apply region of interest if specified
        # 3. Query the database with the extracted binding
        # 4. Return matching manifests
        #
        # For now, we return empty results since actual soft binding extraction
        # requires algorithm-specific implementations

        return SoftBindingQueryResult(matches=[])

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")

@app.post(
    "/bindings",
    status_code=204,
    tags=["store"],
    summary="Binds a C2PA Manifest Store to a soft binding value",
    responses={
        204: {"description": "Manifest bound successfully"},
        400: {"description": "Invalid request body"},
        404: {"description": "Soft binding id or C2PA Manifest id not found"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"}
    }
)
async def associate_manifest(binding: BindingsRequest):
    """
    Associates the active C2PA Manifest of a C2PA Manifest Store with
    the given soft binding value.
    """
    try:
        # Validate request body (400 error)
        if not binding.bindingValue or not binding.bindingValue.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: 'bindingValue' cannot be empty"
            )

        if not binding.manifestId or not binding.manifestId.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: 'manifestId' cannot be empty"
            )

        # Check if manifest exists (404 error)
        manifests_col = get_manifests_collection()
        manifest_doc = await manifests_col.find_one({"_id": binding.manifestId})

        if not manifest_doc:
            raise HTTPException(
                status_code=404,
                detail="C2PA Manifest id not found"
            )

        # Create or update the soft binding association
        soft_bindings_col = get_soft_bindings_collection()

        # Insert new binding (you may want to extract alg from bindingValue format)
        # For now, we'll store the bindingValue directly
        await soft_bindings_col.insert_one({
            "value": binding.bindingValue,
            "manifestId": binding.manifestId,
            "alg": "default",  # You should parse this from bindingValue or accept it as a parameter
            "similarityScore": 100,
        })

        return Response(status_code=204)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")

@app.put(
    "/bindings",
    status_code=204,
    tags=["store"],
    summary="Update the C2PA Manifest Store for a soft binding value",
    responses={
        204: {"description": "Manifest updated successfully"},
        400: {"description": "Invalid request body"},
        404: {"description": "Soft binding value not found"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"}
    }
)
async def update_associated_manifest(binding: BindingsRequest):
    """
    Replaces the C2PA Manifest Store associated with the given soft binding value
    with an updated C2PA Manifest Store referenced by the active C2PA Manifest id.
    """
    try:
        # Validate request body (400 error)
        if not binding.bindingValue or not binding.bindingValue.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: 'bindingValue' cannot be empty"
            )

        if not binding.manifestId or not binding.manifestId.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: 'manifestId' cannot be empty"
            )

        # Check if manifest exists (404 error)
        manifests_col = get_manifests_collection()
        manifest_doc = await manifests_col.find_one({"_id": binding.manifestId})

        if not manifest_doc:
            raise HTTPException(
                status_code=404,
                detail="C2PA Manifest id not found"
            )

        # Update the soft binding association (404 if not found)
        soft_bindings_col = get_soft_bindings_collection()
        result = await soft_bindings_col.update_one(
            {"value": binding.bindingValue},
            {"$set": {"manifestId": binding.manifestId}},
        )

        if result.matched_count == 0:
            raise HTTPException(
                status_code=404,
                detail="Soft binding value not found"
            )

        return Response(status_code=204)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")

@app.post(
    "/manifests",
    response_model=ManifestCreateResult,
    tags=["store"],
    summary="Add a C2PA Manifest Store to the repository",
    responses={
        200: {"description": "C2PA Manifest Store stored successfully"},
        400: {"description": "Invalid request body"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"}
    }
)
async def add_manifest(
    request: Request,
    returnReceipt: bool = Query(False, description="Return verification receipt")
):
    """
    Submit a C2PA Manifest Store to store it in the repository.
    """
    try:
        # Read the raw body as C2PA manifest data
        manifest_data = await request.body()

        # Validate manifest is not empty (400 error)
        if not manifest_data:
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: manifest data cannot be empty"
            )

        # TODO:
        # Generate a unique manifest ID (in real implementation, extract from manifest)
        # here should be a code where we obtain this id from a manifest somehow using tool or sdk
        # Per C2PA Technical Spec, the canonical format is `urn:c2pa:<UUID>`.
        manifest_id = f"urn:c2pa:{uuid.uuid4()}"

        # TODO:
        # Somehow, an active manifest should also be extracted and saved

        # Store the manifest. Blobs go in GridFS (manifest stores can exceed Mongo's 16MB doc limit).
        fs = get_manifest_blobs_bucket()
        store_id = await fs.upload_from_stream(
            f"{manifest_id}.store", manifest_data
        )
        active_id = await fs.upload_from_stream(
            f"{manifest_id}.active", manifest_data  # In real implementation, extract active manifest
        )

        manifests_col = get_manifests_collection()
        await manifests_col.insert_one({
            "_id": manifest_id,
            "manifestStoreFileId": store_id,
            "activeManifestFileId": active_id,
        })

        result = ManifestCreateResult(manifestId=manifest_id)

        # TODO:
        # see what the receipt is, change the code below!!
        # Optionally add receipt
        if returnReceipt:
            result.receipt = ManifestReceipt(
                context={"c2pa": "https://c2pa.org/ns/", "receipt": "https://c2pa.org/ns/manifest-receipt#"},
                type="org.c2pa.manifest-receipt",
                repository={
                    "uri": str(request.base_url),
                    "manifestId": manifest_id
                },
                anchor={
                    "uri": f"{request.base_url}anchors/{manifest_id[:6]}",
                    "proof": {"alg": "ES256", "value": "BASE64URL_PROOF_VALUE"}
                }
            )

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")

@app.get(
    "/manifests/{manifestId}",
    tags=["fetch"],
    summary="Returns a full C2PA Manifest Store or an active C2PA Manifest",
    responses={
        200: {"description": "Successful operation", "content": {"application/c2pa": {}}},
        400: {"description": "Invalid query value"},
        404: {"description": "C2PA Manifest not found"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"}
    }
)
async def get_manifest_by_id(
    manifestId: str,
    returnActiveManifest: bool = Query(
        False,
        description="Return only the active manifest (default: entire store)"
    )
):
    """
    Retrieve a C2PA Manifest by manifest identifier.
    Returns either the active manifest or the entire C2PA Manifest Store.
    """
    try:
        # Validate manifestId (400 error)
        if not manifestId or not manifestId.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid query value: 'manifestId' cannot be empty"
            )

        manifests_col = get_manifests_collection()

        # Find the manifest
        manifest_doc = await manifests_col.find_one({"_id": manifestId})

        if not manifest_doc:
            raise HTTPException(
                status_code=404,
                detail="C2PA Manifest Id not found"
            )

        # Return the appropriate manifest data (active vs full store, per query flag)
        file_id = manifest_doc.get(
            "activeManifestFileId" if returnActiveManifest else "manifestStoreFileId"
        )
        if file_id is None:
            raise HTTPException(
                status_code=404,
                detail="Manifest data not found"
            )

        # Stream the blob out of GridFS
        fs = get_manifest_blobs_bucket()
        buffer = io.BytesIO()
        await fs.download_to_stream(file_id, buffer)
        manifest_data = buffer.getvalue()

        # Return as application/c2pa content type
        return Response(
            content=manifest_data,
            media_type="application/c2pa",
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")

@app.delete(
    "/manifests/{manifestId}",
    status_code=204,
    tags=["store"],
    summary="Remove a C2PA Manifest Store from the repository",
    responses={
        204: {"description": "Manifest deleted successfully"},
        400: {"description": "Invalid request"},
        404: {"description": "C2PA Manifest Store not found"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"}
    }
)
async def delete_manifest(manifestId: str):
    """
    Delete a stored C2PA Manifest Store. This should also remove the corresponding
    soft binding associated with the manifest to ensure it is no longer returned
    in query results.
    """
    try:
        # Validate manifestId (400 error)
        if not manifestId or not manifestId.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid request: 'manifestId' cannot be empty"
            )

        # Delete the manifest (404 if not found)
        manifests_col = get_manifests_collection()
        manifest_doc = await manifests_col.find_one({"_id": manifestId})

        if not manifest_doc:
            raise HTTPException(
                status_code=404,
                detail="C2PA Manifest Store not found"
            )

        # Delete GridFS blobs
        fs = get_manifest_blobs_bucket()
        for key in ("manifestStoreFileId", "activeManifestFileId"):
            file_id = manifest_doc.get(key)
            if file_id is not None:
                try:
                    await fs.delete(file_id)
                except Exception:
                    pass

        await manifests_col.delete_one({"_id": manifestId})

        # Also delete associated soft bindings
        soft_bindings_col = get_soft_bindings_collection()
        await soft_bindings_col.delete_many({"manifestId": manifestId})

        return Response(status_code=204)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")

@app.get(
    "/manifests/{manifestId}/receipts",
    response_model=VerifiedManifestReceipt,
    tags=["fetch"],
    summary="Returns a receipt and verification value for a C2PA Manifest Store",
    responses={
        200: {"description": "Successful operation"},
        400: {"description": "Invalid query value"},
        404: {"description": "C2PA Manifest Store or receipt not found"},
        500: {"description": "Service failure"}
    }
)
async def get_verified_receipt(manifestId: str, request: Request):
    """
    Retrieve the receipt of a C2PA Manifest Store selected by manifest identifier
    along with its verification status. A receipt is a proof that a C2PA Manifest
    Store was ingested by the repository.
    """
    try:
        # Validate manifestId (400 error)
        if not manifestId or not manifestId.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid query value: 'manifestId' cannot be empty"
            )

        # Check if manifest exists (404 if not found)
        manifests_col = get_manifests_collection()
        manifest_doc = await manifests_col.find_one({"_id": manifestId})

        if not manifest_doc:
            raise HTTPException(
                status_code=404,
                detail="C2PA Manifest Store not found"
            )

        # Generate receipt (in real implementation, retrieve stored receipt)
        receipt = VerifiedManifestReceipt(
            context={"c2pa": "https://c2pa.org/ns/", "receipt": "https://c2pa.org/ns/manifest-receipt#"},
            type="org.c2pa.manifest-receipt",
            repository={
                "uri": str(request.base_url),
                "manifestId": manifestId
            },
            anchor={
                "uri": f"{request.base_url}anchors/{manifestId[:6]}",
                "proof": {"alg": "ES256", "value": "BASE64URL_PROOF_VALUE"}
            },
            verified=True
        )

        return receipt

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")

@app.post(
    "/manifests/{manifestId}/receipts",
    response_model=VerifiedManifestReceipt,
    tags=["fetch"],
    summary="Submit a receipt and receive a verification result",
    responses={
        200: {"description": "Successful operation"},
        400: {"description": "Invalid request body or verification input"},
        404: {"description": "C2PA Manifest not found"},
        500: {"description": "Service failure"}
    }
)
async def verify_receipt(manifestId: str, receipt: ManifestReceipt):
    """
    Verify the supplied receipt against the specified C2PA Manifest identifier
    and return the verified receipt.
    """
    try:
        # Validate manifestId (400 error)
        if not manifestId or not manifestId.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid request body or verification input: 'manifestId' cannot be empty"
            )

        # Check if manifest exists (404 if not found)
        manifests_col = get_manifests_collection()
        manifest_doc = await manifests_col.find_one({"_id": manifestId})

        if not manifest_doc:
            raise HTTPException(
                status_code=404,
                detail="C2PA Manifest not found"
            )

        # Verify the receipt (simplified verification)
        # In real implementation, verify cryptographic proof
        verified = True
        error_msg = None

        # Check if manifestId in receipt matches
        if receipt.repository.get("manifestId") != manifestId:
            verified = False
            error_msg = "The supplied receipt manifestId does not match the requested manifestId"

        # Create verified receipt
        verified_receipt = VerifiedManifestReceipt(
            context=receipt.context,
            type=receipt.type,
            repository=receipt.repository,
            anchor=receipt.anchor,
            verified=verified,
            error=error_msg
        )

        return verified_receipt

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")

@app.get(
    "/services/supportedAlgorithms",
    response_model=SoftBindingAlgList,
    tags=["service"],
    summary="Returns a list of supported soft binding algorithms",
    responses={
        200: {"description": "Successful operation"},
        400: {"description": "Invalid request"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"}
    }
)
async def get_supported_algorithms():
    """
    Enumerate the names of soft binding algorithms supported by this service.
    See https://github.com/c2pa-org/softbinding-algorithm-list
    """
    try:
        algorithms_col = get_supported_algorithms_collection()

        # Fetch the supported algorithms
        watermarks = await algorithms_col.find(
            {"type": "watermark"}, projection={"_id": 0, "alg": 1}
        ).to_list(length=None)
        fingerprints = await algorithms_col.find(
            {"type": "fingerprint"}, projection={"_id": 0, "alg": 1}
        ).to_list(length=None)

        return SoftBindingAlgList(
            watermarks=watermarks,
            fingerprints=fingerprints,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")
