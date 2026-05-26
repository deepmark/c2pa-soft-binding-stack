"""
Pydantic models for the ingest pipeline.

Submodules:
- ``enums``        — MediaType, FailureStage, IngestionStatus,
                     ResolutionPushStatus, SoftBindingKind, SCHEMA_VERSION
- ``soft_binding`` — Watermark/Fingerprint records + ``make_soft_binding``
- ``ingestion``    — IngestionRecord (Mongo-persisted shape)
- ``responses``    — IngestionResponse, IngestionListResponse,
                     ResolutionPushOutput (HTTP wire shapes)

Import from the specific submodule rather than this package — e.g.
``from ingestion_api.models.ingestion import IngestionRecord``.
"""
