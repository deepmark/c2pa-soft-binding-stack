"""
Persisted ingestion models.

Two persisted shapes, each backed by its own Mongo collection:

- ``IngestionRecord`` — successful ingestions (``ingestions``
  collection, ``_id == ingestionId``).
- ``FailedIngestion`` — pipeline failures captured for ops/forensics
  (``failed_ingestions`` collection). Carries whatever metadata was
  knowable at the failure point plus the failure stage and error.

Wire response shapes live in ``models.responses``; soft-binding records
live in ``models.soft_binding``; enums + constants live in
``models.enums``.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from ingestion_api.models.enums import (
    SCHEMA_VERSION,
    FailureStage,
    MediaType,
    ResolutionPushStatus,
)
from ingestion_api.models.soft_binding import SoftBindingRecord


class _IngestionCommon(BaseModel):
    """Fields shared by IngestionRecord and IngestionResponse."""
    ingestionId: str = Field(..., description="Internal ingestion identifier")
    manifestId: str = Field(
        ...,
        description="C2PA active-manifest URN; foreign key into resolution-api",
    )
    softBindings: list[SoftBindingRecord] = Field(
        ...,
        description="Soft-bindings emitted in this ingest (one per c2pa.soft-binding assertion)",
        min_length=1,
    )
    mimeType: str = Field(
        ...,
        description="MIME type of the signed asset (same container as the upload)",
    )
    mediaType: MediaType = Field(
        ...,
        description="Top-level media category (audio/video/image) derived from mimeType",
    )
    signingAlg: str = Field(..., description="Signing algorithm (e.g. ES256)")
    taUrl: str | None = Field(None, description="RFC3161 timestamp authority used")
    signedAt: datetime | None = Field(None, description="Approximate signing timestamp")
    createdAt: datetime = Field(..., description="When the record was created")


# Status / error / attempts must agree. Each tuple is
# (status, requires_error_empty, requires_attempts_zero).
_PUSH_INVARIANTS: tuple[tuple[ResolutionPushStatus, bool, bool], ...] = (
    (ResolutionPushStatus.OK,      True,  False),
    (ResolutionPushStatus.SKIPPED, True,  True),
)


class IngestionRecord(_IngestionCommon):
    """Persisted ingestion metadata for a successful ingest (``_id == ingestionId``).

    Binary artifacts (signed asset + manifest bytes) live on disk under
    ``ArtifactStore``; their on-disk path is reconstructible from
    ``ingestionId`` so we don't store paths in this record.
    """

    schemaVersion: int = Field(
        default=SCHEMA_VERSION,
        description="Schema version of this record; migrations branch on this",
    )

    # Content identity for the signed asset + the raw upload.
    # The signed hash is the canonical fingerprint of what we serve back;
    # the upload hash lets you detect duplicate uploads even when each
    # gets a fresh signature/timestamp (signatures are non-deterministic
    # for ECDSA).
    assetSha256: str = Field(
        ..., description="SHA-256 hex digest of the signed asset bytes (64 chars)",
    )
    assetSizeBytes: int = Field(..., ge=0, description="Size in bytes of the signed asset")
    uploadSha256: str = Field(
        ..., description="SHA-256 hex digest of the raw uploaded bytes (64 chars)",
    )

    # Signing material identity — survives cert rotation. SHA-1 of the
    # DER-encoded leaf cert is the standard X.509 fingerprint format.
    signingCertSha1: str | None = Field(
        None,
        description="SHA-1 fingerprint of the leaf signing cert (DER), 40-char hex",
    )

    # Snapshot of plugin /info per alg at ingest time. 
    # Forensic value: months later you can tell which plugin version produced a binding.
    # Captured via a process-local cache to avoid an /info call per ingest.
    pluginVersions: dict[str, dict] | None = Field(
        None,
        description="Snapshot of /info per alg at ingest time (cached per process)",
    )

    # Resolution-api auto-push outcome
    resolutionPushStatus: ResolutionPushStatus = Field(
        default=ResolutionPushStatus.SKIPPED,
        description="Outcome of pushing the signed manifest to the resolution API",
    )
    resolutionPushError: str | None = Field(
        None,
        description="Error from the resolution API push (when status=failed)",
    )
    resolutionPushAttempts: int = Field(
        default=0,
        ge=0,
        description="Total push attempts (initial + reconciler retries)",
    )
    lastPushAttemptAt: datetime | None = Field(
        None,
        description="When the push was last attempted (None if status=SKIPPED throughout)",
    )

    @model_validator(mode="after")
    def _check_consistency(self) -> IngestionRecord:
        for status, error_must_be_empty, attempts_must_be_zero in _PUSH_INVARIANTS:
            if self.resolutionPushStatus is not status:
                continue
            if error_must_be_empty and self.resolutionPushError:
                raise ValueError(
                    f"resolutionPushError must be empty when status={status.value}"
                )
            if attempts_must_be_zero and self.resolutionPushAttempts > 0:
                raise ValueError(
                    f"resolutionPushAttempts must be 0 when status={status.value}"
                )
        return self


class FailedIngestion(BaseModel):
    """Persisted record of a pipeline failure (``_id == ingestionId``).

    Most fields are nullable because failures can happen before they're
    knowable (e.g. signing fails before manifestId extraction).
    """
    schemaVersion: int = Field(default=SCHEMA_VERSION)
    ingestionId: str = Field(..., description="Internal ingestion identifier")
    failureStage: FailureStage = Field(
        default=FailureStage.UNKNOWN,
        description="Where in the pipeline the failure landed",
    )
    error: str = Field(..., description="Error message captured at the failure point")
    mimeType: str | None = Field(
        None, description="MIME type if guessed before failure (always set in practice)",
    )
    mediaType: MediaType | None = Field(
        None, description="Media category if MIME was identified before failure",
    )
    uploadSha256: str = Field(
        ..., description="SHA-256 hex of the raw upload (always known when persisted)",
    )
    attemptedAlgs: list[str] = Field(
        default_factory=list,
        description="Algs the orchestrator was configured to run for this ingest",
    )
    signingCertSha1: str | None = Field(None)
    createdAt: datetime = Field(...)
