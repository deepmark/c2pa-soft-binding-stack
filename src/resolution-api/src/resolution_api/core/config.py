"""
Application configuration.

This service is the lookup half of the soft-binding stack: matches by
binding, manifest store CRUD, supported-algorithm catalog. It does not
sign manifests or run the ingest pipeline (that lives in
``ingestion-api``).
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _service_root() -> Path:
    # src/resolution-api/src/resolution_api/core/config.py -> resolution-api/
    return Path(__file__).resolve().parents[3]


def _repo_root() -> Path:
    # service folder -> .../src/ -> repo root
    return _service_root().parent.parent


class Settings(BaseSettings):
    # Mongo
    mongodb_url: str = "mongodb://localhost:27017"
    database_name: str = "c2pa_soft_bindings"

    # API metadata
    api_title: str = "C2PA Soft Binding Resolution API"
    api_version: str = "1.1.0"
    api_description: str = (
        "Web service API endpoint for matching soft bindings to C2PA Manifests."
    )

    # Algorithm catalog (shared YAML mounted into the container).
    # Defaults to ``<repo>/algorithms.yaml`` for local dev.
    algorithms_catalog_path: Path = Field(
        default_factory=lambda: _repo_root() / "algorithms.yaml"
    )

    # Logging
    log_level: str = "INFO"
    log_json: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
