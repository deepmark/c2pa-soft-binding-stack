"""
Models for the audio ingest pipeline.

Two persisted shapes, one wire shape:

- ``IngestionRecord`` — successful ingestions (``ingestions`` Mongo
  collection, ``_id == ingestionId``). All success-only fields populated.
- ``FailedIngestion`` — pipeline failures captured for ops/forensics
  (``failed_ingestions`` Mongo collection). Carries whatever metadata
  was knowable at the failure point plus the failure stage and error.
- ``IngestResponse`` — body of ``POST /ingest`` (success path only —
  failures surface as HTTP 5xx).

Successful records carry ``softBindings`` as a list of soft-binding
records discriminated by ``kind`` (``watermark`` vs ``fingerprint``),
one entry per ``c2pa.soft-binding`` assertion in the signed manifest.
The discriminated-union pattern lets the two kinds diverge in the
future (e.g. fingerprint adds a confidence score) without touching
construction sites.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator

SoftBindingKind = Literal["watermark", "fingerprint"]

SCHEMA_VERSION = 1


class FailureStage(str, Enum):
    """Where in the pipeline a failure landed. Set by ``_run_pipeline``."""
    PLUGIN_PASS = "plugin_pass"                  # /embed or /compute failed
    MANIFEST_SIGN = "manifest_sign"              # Builder.sign blew up
    MANIFEST_ID_EXTRACT = "manifest_id_extract"  # Reader returned no active_manifest
    UNKNOWN = "unknown"                          # caught everything else


class ResolutionPushStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"  # when RESOLUTION_PUSH_ENABLED=false (standalone deploy)


class ResolutionPushResult(BaseModel):
    """Outcome of the auto-push to resolution-api."""
    status: ResolutionPushStatus
    error: str | None = None


# ---------------------------------------------------------------------------
# Soft-binding records — discriminated union on ``kind``.
# Today both kinds carry identical fields; the split exists so future
# divergence (e.g. fingerprint confidence, watermark embedding strength)
# is a one-class change rather than a breaking schema migration.
# ---------------------------------------------------------------------------


class _SoftBindingBase(BaseModel):
    alg: str = Field(..., description="Soft-binding algorithm identifier")
    bindingValue: str = Field(
        ...,
        description=(
            "Encoded binding value (string; encoding is plugin-specific — "
            "e.g. base64 for vigil-128)"
        ),
    )


class WatermarkSoftBindingRecord(_SoftBindingBase):
    """Watermark binding: bytes were modified during /embed."""
    kind: Literal["watermark"] = "watermark"


class FingerprintSoftBindingRecord(_SoftBindingBase):
    """Fingerprint binding: pure read on the asset, no mutation."""
    kind: Literal["fingerprint"] = "fingerprint"


SoftBindingRecord = Annotated[
    Union[WatermarkSoftBindingRecord, FingerprintSoftBindingRecord],
    Field(discriminator="kind"),
]


def make_soft_binding(
    *, alg: str, kind: SoftBindingKind, bindingValue: str,
) -> _SoftBindingBase:
    """Construct the right soft-binding subclass for a runtime ``kind``.

    Construction sites can't use the ``SoftBindingRecord`` alias directly
    (it's a typing union, not a class), so this factory keeps callers
    from having to repeat the if/else.
    """
    if kind == "watermark":
        return WatermarkSoftBindingRecord(alg=alg, bindingValue=bindingValue)
    if kind == "fingerprint":
        return FingerprintSoftBindingRecord(alg=alg, bindingValue=bindingValue)
    raise ValueError(f"Unknown soft-binding kind: {kind!r}")


# ---------------------------------------------------------------------------
# Common shape between persisted record and HTTP response — a single
# source of truth for the fields they share, so they can't drift.
# ---------------------------------------------------------------------------


class _IngestionCommon(BaseModel):
    """Fields shared by IngestionRecord and IngestResponse."""
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
    signingAlg: str = Field(..., description="Signing algorithm (e.g. ES256)")
    taUrl: str | None = Field(None, description="RFC3161 timestamp authority used")
    signedAt: datetime | None = Field(None, description="Approximate signing timestamp")
    createdAt: datetime = Field(..., description="When the record was created")


# ---------------------------------------------------------------------------
# IngestionRecord — persisted in the ``ingestions`` collection.
# Success-only. Pipeline failures live in ``failed_ingestions``.
# ---------------------------------------------------------------------------


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

    # Content identity for the signed asset + the raw upload. The signed
    # hash is the canonical fingerprint of what we serve back; the upload
    # hash lets you detect duplicate uploads even when each gets a fresh
    # signature/timestamp (signatures are non-deterministic for ECDSA).
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

    # Snapshot of plugin /info per alg at ingest time. Forensic value:
    # months later you can tell which plugin version produced a binding.
    # Captured via a process-local cache to avoid an /info call per ingest.
    pluginVersions: dict[str, dict] | None = Field(
        None,
        description="Snapshot of /info per alg at ingest time (cached per process)",
    )

    # Resolution-api auto-push outcome — flat columns so the reconciler
    # can build cheap (status, age) queries without unwinding nested objects.
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

    # Updated on every write (initial create + every reconciler retry).
    # Lets ops queries find stale records ("not touched in 24h") cheaply.
    updatedAt: datetime = Field(..., description="When the record was last written")

    @model_validator(mode="after")
    def _check_consistency(self) -> "IngestionRecord":
        if self.resolutionPushStatus is ResolutionPushStatus.OK and self.resolutionPushError:
            raise ValueError("resolutionPushError must be empty when status=OK")
        if (
            self.resolutionPushStatus is ResolutionPushStatus.SKIPPED
            and self.resolutionPushError
        ):
            raise ValueError("resolutionPushError must be empty when status=SKIPPED")
        if (
            self.resolutionPushStatus is ResolutionPushStatus.SKIPPED
            and self.resolutionPushAttempts > 0
        ):
            raise ValueError("resolutionPushAttempts must be 0 when status=SKIPPED")
        return self


# ---------------------------------------------------------------------------
# FailedIngestion — persisted in the ``failed_ingestions`` collection.
# Separate model + collection so IngestionRecord stays strict for the
# success path, and ops can query failures independently.
# ---------------------------------------------------------------------------


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
    uploadSha256: str = Field(
        ..., description="SHA-256 hex of the raw upload (always known when persisted)",
    )
    attemptedAlgs: list[str] = Field(
        default_factory=list,
        description="Algs the orchestrator was configured to run for this ingest",
    )
    signingCertSha1: str | None = Field(None)
    createdAt: datetime = Field(...)
    updatedAt: datetime = Field(...)


# ---------------------------------------------------------------------------
# IngestResponse — wire shape for POST /ingest (success path only).
# ---------------------------------------------------------------------------


class IngestResponse(_IngestionCommon):
    """Response body for ``POST /ingest``."""
    outputAssetUrl: str = Field(
        ...,
        description="URL to download the watermarked, signed audio asset",
    )
    manifestUrl: str | None = Field(
        None,
        description="URL to download the raw signed manifest bytes",
    )
    assetSha256: str = Field(
        ...,
        description="SHA-256 hex digest of the signed asset (64 chars)",
    )
    resolutionPush: ResolutionPushResult = Field(
        ...,
        description="Outcome of the auto-push to the soft-binding resolution API",
    )
