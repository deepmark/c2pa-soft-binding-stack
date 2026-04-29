"""
Fetch route group 

Retrieves C2PA Manifest Stores using provided identifiers, or verifies a
supplied receipt.
"""
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from soft_binding_api.core.database import get_manifest_blobs_bucket, get_manifests_collection
from soft_binding_api.models import ManifestReceipt, VerifiedManifestReceipt

router = APIRouter(tags=["fetch"])


@router.get(
    "/manifests/{manifestId}",
    summary="Returns a full C2PA Manifest Store or an active C2PA Manifest",
    responses={
        200: {"description": "Successful operation", "content": {"application/c2pa": {}}},
        400: {"description": "Invalid query value"},
        404: {"description": "C2PA Manifest not found"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"},
    },
)
async def get_manifest_by_id(
    manifestId: str,
    returnActiveManifest: bool = Query(
        False,
        description="Return only the active manifest (default: entire store)",
    ),
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
                detail="Invalid query value: 'manifestId' cannot be empty",
            )

        manifests_col = get_manifests_collection()

        # Find the manifest
        manifest_doc = await manifests_col.find_one({"_id": manifestId})

        if not manifest_doc:
            raise HTTPException(
                status_code=404,
                detail="C2PA Manifest Id not found",
            )

        # Return the appropriate manifest data (active vs full store, per query flag)
        file_id = manifest_doc.get(
            "activeManifestFileId" if returnActiveManifest else "manifestStoreFileId"
        )
        if file_id is None:
            raise HTTPException(
                status_code=404,
                detail="Manifest data not found",
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


@router.get(
    "/manifests/{manifestId}/receipts",
    response_model=VerifiedManifestReceipt,
    summary="Returns a receipt and verification value for a C2PA Manifest Store",
    responses={
        200: {"description": "Successful operation"},
        400: {"description": "Invalid query value"},
        404: {"description": "C2PA Manifest Store or receipt not found"},
        500: {"description": "Service failure"},
    },
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
                detail="Invalid query value: 'manifestId' cannot be empty",
            )

        # Check if manifest exists (404 if not found)
        manifests_col = get_manifests_collection()
        manifest_doc = await manifests_col.find_one({"_id": manifestId})

        if not manifest_doc:
            raise HTTPException(
                status_code=404,
                detail="C2PA Manifest Store not found",
            )

        # Generate receipt (in real implementation, retrieve stored receipt)
        receipt = VerifiedManifestReceipt(
            context={"c2pa": "https://c2pa.org/ns/", "receipt": "https://c2pa.org/ns/manifest-receipt#"},
            type="org.c2pa.manifest-receipt",
            repository={
                "uri": str(request.base_url),
                "manifestId": manifestId,
            },
            anchor={
                "uri": f"{request.base_url}anchors/{manifestId[:6]}",
                "proof": {"alg": "ES256", "value": "BASE64URL_PROOF_VALUE"},
            },
            verified=True,
        )

        return receipt

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")


@router.post(
    "/manifests/{manifestId}/receipts",
    response_model=VerifiedManifestReceipt,
    summary="Submit a receipt and receive a verification result",
    responses={
        200: {"description": "Successful operation"},
        400: {"description": "Invalid request body or verification input"},
        404: {"description": "C2PA Manifest not found"},
        500: {"description": "Service failure"},
    },
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
                detail="Invalid request body or verification input: 'manifestId' cannot be empty",
            )

        # Check if manifest exists (404 if not found)
        manifests_col = get_manifests_collection()
        manifest_doc = await manifests_col.find_one({"_id": manifestId})

        if not manifest_doc:
            raise HTTPException(
                status_code=404,
                detail="C2PA Manifest not found",
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
            error=error_msg,
        )

        return verified_receipt

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")
