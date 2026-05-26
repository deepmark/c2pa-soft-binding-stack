"""C2PA Soft Binding Resolution API spec models (verbatim from the original
``models.py``)."""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class AlgorithmRecord(BaseModel):
    """Full algorithm record as stored in the supported_algorithms collection."""
    alg: str = Field(..., description="Unique algorithm identifier (primary key)")
    type: str = Field(..., description="Algorithm type (e.g. 'watermark', 'fingerprint')")
    bindingBits: int = Field(..., description="Number of bits in the binding value")
    mediaTypes: list[str] = Field(..., description="Supported IANA media types")
    url: str = Field(..., description="Internal plugin service URL")


class SoftBindingAlgorithm(BaseModel):
    """Public algorithm info returned to API clients"""
    alg: str = Field(..., description="Unique identifier of the algorithm")

class SoftBindingAlgList(BaseModel):
    """List of supported soft binding algorithms"""
    watermarks: list[SoftBindingAlgorithm] | None = Field(default_factory=list)
    fingerprints: list[SoftBindingAlgorithm] | None = Field(default_factory=list)


class ManifestMatch(BaseModel):
    """A matched C2PA Manifest"""
    manifestId: str = Field(..., description="Unique identifier of a matched C2PA Manifest")
    endpoint: str | None = Field(None, description="Endpoint where the manifest can be obtained")
    similarityScore: int | None = Field(
        None,
        ge=0,
        le=100,
        description="Match strength score (0-100)",
    )


class SoftBindingQueryResult(BaseModel):
    """Result of a soft binding query"""
    matches: list[ManifestMatch] = Field(default_factory=list)


class SoftBindingQuery(BaseModel):
    """Query parameters for soft binding search"""
    alg: str = Field(..., description="Soft binding algorithm identifier")
    value: str = Field(..., description="Base64-encoded soft binding value")


class AssetReferenceQuery(BaseModel):
    """Query parameters for asset reference search"""
    referenceUrl: HttpUrl = Field(
        ...,
        description="HTTPS URL referencing the resource to be used for finding C2PA Manifest identifiers",
    )
    assetLength: int = Field(
        ...,
        description="Size of the asset to be downloaded from the reference URL in bytes",
    )
    assetType: str | None = Field(
        None,
        description="IANA Media Type of the asset (e.g., 'video/mp4', 'audio/mpeg')",
    )
    region: list[Any] | None = Field(
        None,
        description="Optional array specifying the region of interest within the asset",
    )


class BindingsRequest(BaseModel):
    """
    Request for binding a manifest to a soft binding value.

    Note: The spec omits ``alg`` from the request body, which makes
    (POST|PUT) ``/bindings`` ambiguous when multiple algorithms share a value.
    Including it here matches the storage shape (alg, value, manifestId).
    """
    alg: str = Field(
        ...,
        max_length=256,
        description="Soft binding algorithm identifier (extension over spec)",
    )
    bindingValue: str = Field(
        ...,
        description="A soft binding value to be associated with a C2PA Manifest Store",
    )
    manifestId: str = Field(
        ...,
        max_length=512,
        description="Identifier of the active C2PA Manifest of a C2PA Manifest Store",
    )


class ManifestReceipt(BaseModel):
    """C2PA Manifest receipt"""
    context: Any = Field(..., alias="@context", description="JSON-LD context")
    type: str = Field(..., alias="@type", description="JSON-LD type identifier")
    repository: dict = Field(..., description="Repository information")
    anchor: dict = Field(..., description="Anchor proof information")

    model_config = ConfigDict(populate_by_name=True)


class VerifiedManifestReceipt(ManifestReceipt):
    """Verified C2PA Manifest receipt"""
    verified: bool = Field(..., description="Result of verification")
    error: str | None = Field(None, description="Error explanation if verification failed")


class ManifestCreateResult(BaseModel):
    """Result of creating/storing a manifest"""
    manifestId: str = Field(
        ...,
        description="Identifier of the active C2PA Manifest of the stored C2PA Manifest Store",
    )
    receipt: ManifestReceipt | None = Field(
        None,
        description="Verification receipt returned when returnReceipt=true",
    )
