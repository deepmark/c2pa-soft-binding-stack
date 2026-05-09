"""
Service route group.

Exposes repository capabilities, primarily the list of supported soft
binding algorithms.

Source of truth is the shared ``plugins.yaml`` catalog mounted into
the container — same file that ingestion-api uses for plugin routing,
so what's listed here is exactly what's wired up end-to-end.
"""
from fastapi import APIRouter, HTTPException

from resolution_api.models import SoftBindingAlgList, SoftBindingAlgorithm
from resolution_api.services.plugins_catalog import load_plugin_catalog

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
        entries = load_plugin_catalog()
        watermarks = [
            SoftBindingAlgorithm(alg=e.alg) for e in entries if e.type == "watermark"
        ]
        fingerprints = [
            SoftBindingAlgorithm(alg=e.alg) for e in entries if e.type == "fingerprint"
        ]
        return SoftBindingAlgList(watermarks=watermarks, fingerprints=fingerprints)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service failure: {str(e)}")
