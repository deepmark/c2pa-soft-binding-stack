"""
Pydantic request/response models.

- ``spec`` re-exports the models that mirror the C2PA Decoupled spec
  (kept here so existing routers continue to import from
  ``soft_binding_api.models``).
- ``ingestion`` adds the request/response models for the audio ingest
  pipeline.
"""
from soft_binding_api.models.ingestion import (
    IngestionRecord,
    IngestionStatus,
    IngestResponse,
)
from soft_binding_api.models.spec import (
    AssetReferenceQuery,
    BindingsRequest,
    ManifestCreateResult,
    ManifestMatch,
    ManifestReceipt,
    SoftBindingAlgList,
    SoftBindingAlgorithm,
    SoftBindingQuery,
    SoftBindingQueryResult,
    VerifiedManifestReceipt,
)

__all__ = [
    # spec
    "AssetReferenceQuery",
    "BindingsRequest",
    "ManifestCreateResult",
    "ManifestMatch",
    "ManifestReceipt",
    "SoftBindingAlgList",
    "SoftBindingAlgorithm",
    "SoftBindingQuery",
    "SoftBindingQueryResult",
    "VerifiedManifestReceipt",
    # ingestion
    "IngestResponse",
    "IngestionRecord",
    "IngestionStatus",
]
