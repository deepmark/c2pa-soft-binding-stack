"""
Store route group

Ingests C2PA Manifest Stores and creates, updates, or deletes their
associations with soft bindings.
"""
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from database import (
    get_manifest_blobs_bucket,
    get_manifests_collection,
    get_soft_bindings_collection,
)
from models import (
    BindingsRequest,
    ManifestCreateResult,
    ManifestReceipt,
)

router = APIRouter(tags=["store"])


@router.post(
    "/bindings",
    status_code=204,
    summary="Binds a C2PA Manifest Store to a soft binding value",
    responses={
        204: {"description": "Manifest bound successfully"},
        400: {"description": "Invalid request body"},
        404: {"description": "Soft binding id or C2PA Manifest id not found"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"},
    },
)
async def associate_manifest(binding: BindingsRequest):
    """
    Associates the active C2PA Manifest of a C2PA Manifest Store with
    the given soft binding value.
    """
    try:
        # Validate request body (400 error)
        if not binding.alg or not binding.alg.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: 'alg' cannot be empty",
            )

        if not binding.bindingValue or not binding.bindingValue.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: 'bindingValue' cannot be empty",
            )

        if not binding.manifestId or not binding.manifestId.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: 'manifestId' cannot be empty",
            )

        # Check if manifest exists (404 error)
        manifests_col = get_manifests_collection()
        manifest_doc = await manifests_col.find_one({"_id": binding.manifestId})

        if not manifest_doc:
            raise HTTPException(
                status_code=404,
                detail="C2PA Manifest id not found",
            )

        # Create or update the soft binding association
        soft_bindings_col = get_soft_bindings_collection()
        await soft_bindings_col.insert_one({
            "alg": binding.alg,
            "value": binding.bindingValue,
            "manifestId": binding.manifestId,
            "similarityScore": 100,
        })

        return Response(status_code=204)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")


@router.put(
    "/bindings",
    status_code=204,
    summary="Update the C2PA Manifest Store for a soft binding value",
    responses={
        204: {"description": "Manifest updated successfully"},
        400: {"description": "Invalid request body"},
        404: {"description": "Soft binding value not found"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"},
    },
)
async def update_associated_manifest(binding: BindingsRequest):
    """
    Replaces the C2PA Manifest Store associated with the given soft binding value
    with an updated C2PA Manifest Store referenced by the active C2PA Manifest id.
    """
    try:
        # Validate request body (400 error)
        if not binding.alg or not binding.alg.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: 'alg' cannot be empty",
            )

        if not binding.bindingValue or not binding.bindingValue.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: 'bindingValue' cannot be empty",
            )

        if not binding.manifestId or not binding.manifestId.strip():
            raise HTTPException(
                status_code=400,
                detail="Invalid request body: 'manifestId' cannot be empty",
            )

        # Check if manifest exists (404 error)
        manifests_col = get_manifests_collection()
        manifest_doc = await manifests_col.find_one({"_id": binding.manifestId})

        if not manifest_doc:
            raise HTTPException(
                status_code=404,
                detail="C2PA Manifest id not found",
            )

        # Update the soft binding association (404 if not found)
        soft_bindings_col = get_soft_bindings_collection()
        result = await soft_bindings_col.update_one(
            {"alg": binding.alg, "value": binding.bindingValue},
            {"$set": {"manifestId": binding.manifestId}},
        )

        if result.matched_count == 0:
            raise HTTPException(
                status_code=404,
                detail="Soft binding value not found",
            )

        return Response(status_code=204)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")


@router.post(
    "/manifests",
    response_model=ManifestCreateResult,
    summary="Add a C2PA Manifest Store to the repository",
    responses={
        200: {"description": "C2PA Manifest Store stored successfully"},
        400: {"description": "Invalid request body"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"},
    },
)
async def add_manifest(
    request: Request,
    returnReceipt: bool = Query(False, description="Return verification receipt"),
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
                detail="Invalid request body: manifest data cannot be empty",
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
                    "manifestId": manifest_id,
                },
                anchor={
                    "uri": f"{request.base_url}anchors/{manifest_id[:6]}",
                    "proof": {"alg": "ES256", "value": "BASE64URL_PROOF_VALUE"},
                },
            )

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")


@router.delete(
    "/manifests/{manifestId}",
    status_code=204,
    summary="Remove a C2PA Manifest Store from the repository",
    responses={
        204: {"description": "Manifest deleted successfully"},
        400: {"description": "Invalid request"},
        404: {"description": "C2PA Manifest Store not found"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"},
    },
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
                detail="Invalid request: 'manifestId' cannot be empty",
            )

        # Delete the manifest (404 if not found)
        manifests_col = get_manifests_collection()
        manifest_doc = await manifests_col.find_one({"_id": manifestId})

        if not manifest_doc:
            raise HTTPException(
                status_code=404,
                detail="C2PA Manifest Store not found",
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
