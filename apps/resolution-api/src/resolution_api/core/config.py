"""
Application configuration.

This service is the lookup half of the soft-binding stack: matches by
binding, manifest store CRUD, supported-algorithm catalog. It does not
sign manifests or run the ingest pipeline (that lives in
``ingestion-api``).
"""
from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Mongo
    mongodb_url: str = "mongodb://localhost:27017"
    mongodb_database: str = Field(
        default="c2pa",
        validation_alias=AliasChoices("MONGODB_DATABASE", "DATABASE_NAME"),
    )
    manifests_collection: str = "manifests"
    soft_bindings_collection: str = "soft_bindings"
    supported_algorithms_collection: str = "supported_algorithms"
    manifest_blobs_bucket: str = "manifest_blobs"

    # API metadata
    api_title: str = "C2PA Soft Binding Resolution API"
    api_version: str = "1.1.0"
    api_description: str = (
        "Web service API endpoint for matching soft bindings to C2PA Manifests."
    )


    # Limits
    max_download_size_bytes: int = 10 * 1024 * 1024  # 10 MB — /matches/byReference
    max_upload_size_bytes: int = 10 * 1024 * 1024    # 10 MB — /matches/byContent

    # Logging
    log_level: str = "INFO"
    log_json: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )


settings = Settings()
