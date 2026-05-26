"""
Fetch route group

Retrieves C2PA Manifest Stores using provided identifiers, or verifies a
supplied receipt.
"""
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from resolution_api.core.database import get_manifest_blobs_bucket, get_manifests_collection
from resolution_api.core.logging import get_logger
from resolution_api.models import ManifestReceipt

logger = get_logger(__name__)

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

        if returnActiveManifest:
            raise HTTPException(
                status_code=501,
                detail="Returning only the active manifest is not yet implemented",
            )

        file_id = manifest_doc.get("manifestStoreFileId")
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
    except Exception:
        logger.exception("Unexpected error")
        raise HTTPException(status_code=500, detail="Service failure")


@router.get(
    "/manifests/{manifestId}/receipts",
    summary="Returns a receipt and verification value for a C2PA Manifest Store",
    responses={
        501: {"description": "Not implemented"},
    },
)
async def get_verified_receipt(manifestId: str, request: Request):
    """
    Retrieve the receipt of a C2PA Manifest Store selected by manifest identifier
    along with its verification status. A receipt is a proof that a C2PA Manifest
    Store was ingested by the repository.

    Not yet implemented: cryptographic receipt generation is out of scope for
    the current release.
    """
    raise HTTPException(
        status_code=501,
        detail="Receipt generation is not yet implemented",
    )


@router.post(
    "/manifests/{manifestId}/receipts",
    summary="Submit a receipt and receive a verification result",
    responses={
        501: {"description": "Not implemented"},
    },
)
async def verify_receipt(manifestId: str, receipt: ManifestReceipt):
    """
    Verify the supplied receipt against the specified C2PA Manifest identifier
    and return the verified receipt.

    Not yet implemented: cryptographic receipt verification is out of scope
    for the current release.
    """
    raise HTTPException(
        status_code=501,
        detail="Receipt verification is not yet implemented",
    )
