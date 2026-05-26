"""
Store route group

Ingests C2PA Manifest Stores and creates, updates, or deletes their
associations with soft bindings.
"""
import io
import json

from c2pa import Reader
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from resolution_api.core.logging import get_logger
from resolution_api.core.database import (
    get_manifest_blobs_bucket,
    get_manifests_collection,
    get_soft_bindings_collection,
)
from resolution_api.models import (
    BindingsRequest,
    ManifestCreateResult,
)

logger = get_logger(__name__)

router = APIRouter(tags=["store"])


def _extract_manifest_id(manifest_data: bytes) -> str:
    """Parse a raw C2PA manifest store and return its active_manifest URN.

    Raises HTTPException(400) if the bytes don't contain a valid C2PA
    manifest store or no active_manifest label can be read. Both
    ingestion-api and resolution-api use the same SDK call, so for
    identical bytes both services derive the same ID — that's what
    makes ``POST /manifests`` idempotent.
    """

    # Create a reader from the manifest data
    # We need to provide a format, but since we're reading from manifest_data,
    # the format doesn't matter as much - it won't read the stream
    dummy_stream = io.BytesIO(b"")
    reader = Reader(
        format_or_path="audio/wav",  # Dummy format
        stream=dummy_stream,
        manifest_data=manifest_data  # This contains the actual manifest store
    )
    # Get the full manifest store as JSON
    manifest_store_json = reader.json()
    manifest_store = json.loads(manifest_store_json)
    # Get the active manifest ID
    active_manifest_id = manifest_store.get("active_manifest")
    if not active_manifest_id:
        raise HTTPException(
            status_code=400,
            detail="Manifest store has no active_manifest label",
        )
    return active_manifest_id


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

        soft_bindings_col = get_soft_bindings_collection()
        await soft_bindings_col.update_one(
            {"alg": binding.alg, "value": binding.bindingValue, "manifestId": binding.manifestId},
            {"$setOnInsert": {"alg": binding.alg, "value": binding.bindingValue, "manifestId": binding.manifestId}},
            upsert=True,
        )

        return Response(status_code=204)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unexpected error")
        raise HTTPException(status_code=500, detail="Service failure")


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
        logger.exception("Unexpected error")
        raise HTTPException(status_code=500, detail="Service failure")


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

        # Extract the canonical manifestId from the manifest bytes.
        # Same derivation ingestion-api uses → idempotent storage.
        manifest_id = _extract_manifest_id(manifest_data)

        manifests_col = get_manifests_collection()
        existing = await manifests_col.find_one({"_id": manifest_id})

        if existing is None:
            # First time we see this manifest — write blobs + record.
            # TODO: extract the *active* manifest from the store and
            # store it as a separate blob (currently we duplicate the
            # store bytes into both slots).
            fs = get_manifest_blobs_bucket()
            store_id = await fs.upload_from_stream(
                f"{manifest_id}.store", manifest_data,
            )
            active_id = await fs.upload_from_stream(
                f"{manifest_id}.active", manifest_data,
            )
            await manifests_col.insert_one({
                "_id": manifest_id,
                "manifestStoreFileId": store_id,
                "activeManifestFileId": active_id,
            })
        # else: idempotent re-push, blobs + record already there.

        result = ManifestCreateResult(manifestId=manifest_id)

        # TODO: potentially add manifest receipt
        if returnReceipt:
            result.receipt = None

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unexpected error")
        raise HTTPException(status_code=500, detail="Service failure")


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
        logger.exception("Unexpected error")
        raise HTTPException(status_code=500, detail="Service failure")
