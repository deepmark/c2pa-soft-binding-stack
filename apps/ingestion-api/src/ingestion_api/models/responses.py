"""
Wire-shape models for HTTP responses.

``ResolutionPushOutput`` lives here despite not being a top-level HTTP
response: it's a Pydantic model so it can nest inside
``IngestionResponse.resolutionPush`` without dataclass-to-pydantic
conversion at the router. The publisher adapter also returns it
directly to the service layer.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ingestion_api.models.enums import ResolutionPushStatus
from ingestion_api.models.ingestion import IngestionRecord, _IngestionCommon


class ResolutionPushOutput(BaseModel):
    """Outcome of the auto-push to resolution-api.

    Used both as the return type of ``ResolutionPushClient.push`` and as
    the embedded ``resolutionPush`` field on ``IngestionResponse``.
    """
    status: ResolutionPushStatus
    error: str | None = None


class IngestionResponse(_IngestionCommon):
    """Response body for ``POST /ingest``."""
    outputAssetUrl: str = Field(
        ...,
        description="URL to download the watermarked, signed media asset",
    )
    manifestUrl: str | None = Field(
        None,
        description="URL to download the raw signed manifest bytes",
    )
    assetSha256: str = Field(
        ...,
        description="SHA-256 hex digest of the signed asset (64 chars)",
    )
    resolutionPush: ResolutionPushOutput = Field(
        ...,
        description="Outcome of the auto-push to the soft-binding resolution API",
    )


class IngestionListResponse(BaseModel):
    """Response body for ``GET /ingestions``."""
    items: list[IngestionRecord] = Field(
        default_factory=list,
        description="Page of ingestion records, ordered newest-first",
    )
    nextCursor: str | None = Field(
        None,
        description="Opaque cursor to fetch the next page, or null when this is the last page",
    )
