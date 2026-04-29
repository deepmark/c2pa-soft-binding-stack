"""
Application configuration.

All settings are overridable via environment variables (see ``.env`` example
in the README). 

Configuration related to the ingest pipeline (credentials, storage root, 
default soft-binding algorithm, optional RFC3161 timestamp authority URL) lives here too.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _project_root() -> Path:
    # src/soft_binding_api/core/config.py -> project root
    return Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Application configuration settings."""

    # MongoDB
    mongodb_url: str = "mongodb://localhost:27017"
    database_name: str = "c2pa_soft_bindings"

    # API metadata
    api_title: str = "C2PA Soft Binding Resolution API by DeepMark Inc."
    api_version: str = "1.0.0"
    api_description: str = (
        "Web service API endpoint for matching soft bindings to C2PA Manifests, "
        "plus an audio ingest pipeline that watermarks and signs assets."
    )

    # Ingest pipeline
    default_audio_alg: str = "me.deepmark.audio.vigil.128"
    

    claim_generator_name: str = "DeepMark Inc."
    claim_generator_version: str = "1.0.0"

    # Local artifact storage
    storage_root: Path = Field(default_factory=lambda: _project_root() / "storage")
    """Where signed assets, raw manifest bytes, and metadata sidecars live.
    Originals and watermarked intermediates are streamed through memory
    and never written to disk."""

    # Signing credentials (test certs by default; production deployments must
    # mount real certs into this location or override the paths).
    credentials_dir: Path = Field(default_factory=lambda: _project_root() / "credentials")
    cert_chain_path: Path | None = None
    private_key_path: Path | None = None
    signing_alg: str = "ES256"
    ta_url: str | None = None
    """Optional RFC 3161 timestamp authority URL passed to the C2PA signer."""

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


settings = Settings()
