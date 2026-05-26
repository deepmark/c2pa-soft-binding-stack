"""
Service route group.

Exposes repository capabilities, primarily the list of supported soft
binding algorithms. Source of truth is the ``supported_algorithms``
MongoDB collection.
"""
from fastapi import APIRouter, HTTPException

from resolution_api.core.database import get_supported_algorithms_collection
from resolution_api.core.logging import get_logger
from resolution_api.models import SoftBindingAlgList, SoftBindingAlgorithm

logger = get_logger(__name__)

router = APIRouter(tags=["service"])


@router.get(
    "/services/supportedAlgorithms",
    response_model=SoftBindingAlgList,
    summary="Returns a list of supported soft binding algorithms",
    responses={
        200: {"description": "Successful operation"},
        500: {"description": "Service failure"},
    },
)
async def get_supported_algorithms():
    """
    Enumerate the names of soft binding algorithms supported by this
    deployment. See https://github.com/c2pa-org/softbinding-algorithm-list
    for the cross-org canonical registry.
    """
    try:
        col = get_supported_algorithms_collection()
        docs = await col.find({}, {"_id": 0, "url": 0}).to_list(length=100)
        watermarks = [
            SoftBindingAlgorithm(**doc) for doc in docs if doc.get("type") == "watermark"
        ]
        fingerprints = [
            SoftBindingAlgorithm(**doc) for doc in docs if doc.get("type") == "fingerprint"
        ]
        return SoftBindingAlgList(watermarks=watermarks, fingerprints=fingerprints)
    except Exception:
        logger.exception("Unexpected error")
        raise HTTPException(status_code=500, detail="Service failure")
