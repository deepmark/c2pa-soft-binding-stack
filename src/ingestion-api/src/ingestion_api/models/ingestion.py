"""
Models for the audio ingest pipeline.

``IngestionRecord`` is the persisted shape (filesystem JSON sidecar).
``IngestResponse`` is what ``POST /ingest`` returns to the caller.

Both carry ``softBindings`` as a list of ``SoftBindingRecord`` —
one entry per ``c2pa.soft-binding`` assertion in the signed manifest.
The list is non-empty (every ingest produces at least one binding);
when fingerprinting joins watermarking, it'll just be a longer list.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SoftBindingKind = Literal["watermark", "fingerprint"]


class IngestionStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"


class ResolutionPushStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"  # RESOLUTION_PUSH_ENABLED=false (standalone deploy)


class ResolutionPushResult(BaseModel):
    """Outcome of the auto-push to resolution-api."""
    status: ResolutionPushStatus
    error: str | None = None


class SoftBindingRecord(BaseModel):
    """One soft-binding produced during ingest."""
    alg: str = Field(..., description="Soft-binding algorithm identifier")
    kind: SoftBindingKind = Field(..., description="watermark | fingerprint")
    bindingValue: str = Field(..., description="Base64-encoded binding value")


class IngestionRecord(BaseModel):
    """Persisted ingestion metadata (JSON sidecar at <storage_root>/ingestions/<id>/metadata.json)."""

    model_config = ConfigDict(populate_by_name=True)

    ingestionId: str = Field(..., description="Internal ingestion identifier")
    originalFilename: str = Field(..., description="As-uploaded filename")
    originalMimeType: str = Field(..., description="Detected/declared MIME type")
    outputAssetPath: str = Field(..., description="Local path to the signed output asset")
    manifestBytesPath: str | None = Field(
        None,
        description="Local path to the raw signed manifest bytes returned by the SDK",
    )
    softBindings: list[SoftBindingRecord] = Field(
        ...,
        description="All soft-bindings emitted in this ingest (one per c2pa.soft-binding assertion)",
        min_length=1,
    )
    manifestId: str = Field(
        ...,
        description="C2PA active-manifest URN extracted from the signed asset",
    )
    signingAlg: str = Field(..., description="Signing algorithm (e.g. ES256)")
    taUrl: str | None = Field(None, description="RFC3161 timestamp authority used")
    signedAt: datetime | None = Field(None, description="Approximate signing timestamp")
    createdAt: datetime = Field(..., description="When the record was created")
    status: IngestionStatus = Field(default=IngestionStatus.OK)
    error: str | None = Field(None, description="Error detail if status=failed")
    resolutionPushStatus: ResolutionPushStatus = Field(
        default=ResolutionPushStatus.SKIPPED,
        description="Outcome of pushing the signed manifest to the resolution API",
    )
    resolutionPushError: str | None = Field(
        None,
        description="Error from the resolution API push (if status=failed)",
    )


class IngestResponse(BaseModel):
    """Response body for ``POST /ingest``."""
    ingestionId: str
    manifestId: str
    softBindings: list[SoftBindingRecord] = Field(
        ...,
        description="All soft-bindings emitted in this ingest",
        min_length=1,
    )
    originalFilename: str
    originalMimeType: str
    outputAssetUrl: str = Field(
        ...,
        description="URL to download the watermarked, signed audio asset",
    )
    manifestUrl: str | None = Field(
        None,
        description="URL to download the raw signed manifest bytes",
    )
    signingAlg: str
    taUrl: str | None = None
    signedAt: datetime | None = None
    createdAt: datetime
    resolutionPush: ResolutionPushResult = Field(
        ...,
        description="Outcome of the auto-push to the soft-binding resolution API",
    )
