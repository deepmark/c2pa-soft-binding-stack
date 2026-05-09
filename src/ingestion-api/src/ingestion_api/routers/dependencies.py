"""
FastAPI dependency providers for ingestion-api routes.
"""
from __future__ import annotations

from typing import TypeVar

from fastapi import HTTPException, Request

from ingestion_api.repositories.artifacts import ArtifactStore
from ingestion_api.repositories.ingestions import IngestionRecordRepository
from ingestion_api.services.ingestion import IngestionService

_T = TypeVar("_T")


def _state(request: Request, name: str, label: str, expected: type[_T]) -> _T:
    """Pull a long-lived collaborator off ``app.state`` or 503.

    ``expected`` is only used for the return type hint and a runtime
    safety check — startup misconfiguration that wires the wrong type
    onto ``app.state`` will surface here as a 500 instead of failing in
    a method call deep in the handler.
    """
    obj = getattr(request.app.state, name, None)
    if obj is None:
        raise HTTPException(
            status_code=503,
            detail=f"{label} not initialised (check /ready)",
        )
    if not isinstance(obj, expected):
        raise HTTPException(
            status_code=500,
            detail=(
                f"{label} on app.state is the wrong type "
                f"(got {type(obj).__name__}, expected {expected.__name__})"
            ),
        )
    return obj


def get_ingestion_service(request: Request) -> IngestionService:
    return _state(request, "ingestion_service", "Ingestion service", IngestionService)


def get_artifact_store(request: Request) -> ArtifactStore:
    return _state(request, "artifacts", "Artifact store", ArtifactStore)


def get_record_repository(request: Request) -> IngestionRecordRepository:
    """Returns the active ``IngestionRecordRepository`` (Mongo in prod,
    in-memory in tests). Typed against the Protocol so handlers can
    accept either backend without importing the concrete class.

    We can't ``isinstance``-check a Protocol at runtime without
    ``@runtime_checkable``, so the provider asserts presence only and
    trusts startup wiring for the type contract.
    """
    obj = getattr(request.app.state, "records", None)
    if obj is None:
        raise HTTPException(
            status_code=503,
            detail="Record repository not initialised (check /ready)",
        )
    return obj
