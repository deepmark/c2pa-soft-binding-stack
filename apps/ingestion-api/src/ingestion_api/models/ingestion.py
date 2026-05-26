"""
Persisted ingestion model.

``IngestionRecord`` is backed by the ``ingestions`` collection
(``_id == ingestionId``). It represents the full lifecycle:
pending, succeeded, or failed.

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
    IngestionStatus,
    MediaType,
    ResolutionPushStatus,
)
from ingestion_api.models.soft_binding import SoftBindingRecord


class _IngestionCommon(BaseModel):
    """Fields shared by IngestionRecord and IngestionResponse."""
    ingestionId: str = Field(..., description="Internal ingestion identifier")
    manifestId: str | None = Field(
        None,
        description="C2PA active-manifest URN; foreign key into resolution-api",
    )
    softBindings: list[SoftBindingRecord] = Field(
        default_factory=list,
        description="Soft-bindings emitted in this ingest (one per c2pa.soft-binding assertion)",
    )
    mimeType: str | None = Field(
        None,
        description="MIME type of the signed asset (same container as the upload)",
    )
    mediaType: MediaType | None = Field(
        None,
        description="Top-level media category (audio/video/image) derived from mimeType",
    )
    signingAlg: str | None = Field(None, description="Signing algorithm (e.g. ES256)")
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
    """Persisted ingestion metadata (``_id == ingestionId``).

    Successful records include manifest, binding, signed asset, signing,
    and resolution-push metadata. Failed records preserve whatever was
    knowable when the pipeline failed.
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
    status: IngestionStatus = Field(
        default=IngestionStatus.PENDING,
        description="Lifecycle status of the ingestion",
    )

    assetSha256: str | None = Field(
        None, description="SHA-256 hex digest of the signed asset bytes (64 chars)",
    )
    assetSizeBytes: int | None = Field(
        None, ge=0, description="Size in bytes of the signed asset",
    )
    uploadSha256: str = Field(
        ..., description="SHA-256 hex digest of the raw uploaded bytes (64 chars)",
    )

    attemptedAlgs: list[str] = Field(
        default_factory=list,
        description="Algs the orchestrator was configured to run for this ingest",
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

    failureStage: FailureStage | None = Field(
        None,
        description="Where in the pipeline the failure landed when status=failed",
    )
    error: str | None = Field(
        None,
        description="Error message captured at the failure point when status=failed",
    )

    @model_validator(mode="after")
    def _check_consistency(self) -> IngestionRecord:
        if self.status is IngestionStatus.SUCCEEDED:
            missing = [
                name
                for name in (
                    "manifestId",
                    "mimeType",
                    "mediaType",
                    "signingAlg",
                    "signedAt",
                    "assetSha256",
                    "assetSizeBytes",
                )
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(
                    "succeeded ingestion missing required fields: "
                    + ", ".join(missing)
                )
            if not self.softBindings:
                raise ValueError("succeeded ingestion requires at least one softBinding")
            if self.failureStage is not None or self.error:
                raise ValueError("succeeded ingestion cannot have failure details")

        if self.status is IngestionStatus.FAILED:
            if self.failureStage is None:
                raise ValueError("failed ingestion requires failureStage")
            if not self.error:
                raise ValueError("failed ingestion requires error")

        if self.status is not IngestionStatus.FAILED:
            if self.failureStage is not None or self.error:
                raise ValueError("failure details require status=failed")

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
