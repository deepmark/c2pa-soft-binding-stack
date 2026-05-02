"""
Application configuration for ingestion-api.

This service:
- accepts media uploads (audio today; video / image as plugins ship),
- delegates watermark embed and fingerprint compute to plugin
  containers over HTTP — selected per request from the catalog,
- builds + signs a C2PA manifest,
- persists signed asset + manifest bytes to disk,
- writes an ``IngestionRecord`` to its own MongoDB database,
- auto-pushes the manifest store + bindings to the resolution API at
  ``RESOLUTION_API_URL`` (set ``RESOLUTION_PUSH_ENABLED=false`` to opt
  out for standalone deployments).

Required env vars: ``MONGODB_URL``, ``DATABASE_NAME``,
``STORAGE_ROOT``, ``ALGORITHMS_CATALOG_PATH``, ``CREDENTIALS_DIR``.

Note: the per-ingest alg list is supplied by the caller in the
``POST /ingest`` form field ``algs``; there is no service-wide default.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # API metadata
    api_title: str = "Deepmark C2PA Ingestion API"
    api_version: str = "0.1.0"
    api_description: str = (
        "Watermark and build a signed C2PA manifests for digital assets."
    )

    # MongoDB (Ingestion-api's own DB cluster).
    mongodb_url: str = Field(
        ...,
        description="Mongo connection string for ingestion-api's own DB.",
    )
    database_name: str = Field(
        ...,
        description="Database name for the ingestions collection.",
    )

    # Persistent on-disk artifact store (signed asset + manifest bytes - metadata is stored in MongoDB).
    storage_root: Path = Field(
        ...,
        description="Absolute path to the artifact store directory.",
    )

    # Algorithm catalog.
    # The same file must be mounted into resolution-api.
    algorithms_catalog_path: Path = Field(
        ...,
        description="Absolute path to the shared algorithms.yaml catalog.",
    )


    # Plugin HTTP timeouts.
    plugin_request_timeout_s: float = 60.0

    # Resolution-api auto-push. 
    # ``resolution_push_enabled=True`` (the default) requires ``resolution_api_url`` to be set.
    # Set ``RESOLUTION_PUSH_ENABLED=false`` for standalone use.
    resolution_push_enabled: bool = True
    resolution_api_url: str = ""
    resolution_request_timeout_s: float = 10.0
    # Per-call retry policy. 
    # Each HTTP request (POST /manifests and each POST /bindings) 
    # gets up to N additional attempts on transient failures (timeouts, connection errors, 5xx). 
    # 4xx is treated as permanent and surfaces immediately. 
    # Backoff doubles each retry, starting at ``resolution_retry_backoff_s``.
    resolution_max_retries: int = 1
    resolution_retry_backoff_s: float = 0.5

    # Claim generator metadata embedded in the manifest.
    claim_generator_name: str = "Deepmark Inc."
    claim_generator_version: str = "0.1.0"

    # Signing credentials. ``credentials_dir`` is required; 
    credentials_dir: Path = Field(
        ...,
        description="Directory containing the ES256 cert chain + private key.",
    )
    cert_chain_path: Path | None = None
    private_key_path: Path | None = None
    signing_alg: str = "ES256"
    ta_url: str | None = None

    # Logging
    log_level: str = "INFO"
    log_json: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def resolved_cert_chain_path(self) -> Path:
        return self.cert_chain_path or (self.credentials_dir / "es256_certs.pem")

    def resolved_private_key_path(self) -> Path:
        return self.private_key_path or (self.credentials_dir / "es256_private.key")

    @model_validator(mode="after")
    def _require_url_when_push_enabled(self) -> Settings:
        if self.resolution_push_enabled and not self.resolution_api_url.strip():
            raise ValueError(
                "RESOLUTION_API_URL must be set when RESOLUTION_PUSH_ENABLED=true. "
                "Set RESOLUTION_PUSH_ENABLED=false for standalone deployments "
                "with no resolution-api downstream."
            )
        return self


settings = Settings()
