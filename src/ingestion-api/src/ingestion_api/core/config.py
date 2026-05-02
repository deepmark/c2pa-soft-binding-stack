"""
Application configuration for ingestion-api.

This service:
- accepts audio uploads,
- delegates watermark embed to a plugin container over HTTP,
- builds + signs a C2PA manifest,
- persists signed asset + manifest bytes to disk,
- writes an ``IngestionRecord`` to its own MongoDB database,
- auto-pushes the manifest store + bindings to the resolution API at
  ``RESOLUTION_API_URL`` (set ``RESOLUTION_PUSH_ENABLED=false`` to opt
  out for standalone deployments).

Required env vars: ``MONGODB_URL``, ``DATABASE_NAME``, 
``STORAGE_ROOT``, ``ALGORITHMS_CATALOG_PATH``, ``CREDENTIALS_DIR``.
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
        "Watermark and build a signed C2PA manifests for audio assets."
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

    # Soft-binding algs applied to each audio upload, in order. 
    # Each id must match an entry in ``algorithms.yaml``. 
    audio_algs: list[str] = Field(
        default_factory=lambda: ["me.deepmark.audio.vigil.128"],
        min_length=1,
    )

    # Algorithm catalog — same file must be mounted into resolution-api.
    algorithms_catalog_path: Path = Field(
        ...,
        description="Absolute path to the shared algorithms.yaml catalog.",
    )

    # Persistent on-disk artifact store (signed asset + manifest bytes
    # only — record metadata lives in MongoDB). Must be a writable directory.
    storage_root: Path = Field(
        ...,
        description="Absolute path to the artifact store directory.",
    )

    # Plugin HTTP timeouts.
    plugin_request_timeout_s: float = 60.0

    # Resolution-api auto-push. ``resolution_push_enabled=True`` (the
    # default) requires ``resolution_api_url`` to be set — startup fails
    # otherwise, so a forgotten env var can't silently produce orphan
    # records that no resolver can find. Set
    # ``RESOLUTION_PUSH_ENABLED=false`` for genuine standalone use.
    resolution_push_enabled: bool = True
    resolution_api_url: str = ""
    resolution_request_timeout_s: float = 10.0

    # Claim generator metadata embedded in the manifest.
    claim_generator_name: str = "Deepmark Inc."
    claim_generator_version: str = "0.1.0"

    # Signing credentials. ``credentials_dir`` is required; the cert/key
    # paths default to the conventional filenames inside it but can be
    # overridden individually.
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
