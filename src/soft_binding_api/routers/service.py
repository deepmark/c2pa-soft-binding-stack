"""
Service route group 

Exposes repository capabilities, such as the list of supported soft binding
algorithms.
"""
from fastapi import APIRouter, HTTPException

from soft_binding_api.database import get_supported_algorithms_collection
from soft_binding_api.models import SoftBindingAlgList

router = APIRouter(tags=["service"])


@router.get(
    "/services/supportedAlgorithms",
    response_model=SoftBindingAlgList,
    summary="Returns a list of supported soft binding algorithms",
    responses={
        200: {"description": "Successful operation"},
        400: {"description": "Invalid request"},
        403: {"description": "Client not allowed to perform this operation"},
        500: {"description": "Service failure"},
    },
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
