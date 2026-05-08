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
    git_sha: str = Field(
        "",
        description="Git commit SHA for the running build (set by CI/deploy).",
    )
    image_tag: str = Field(
        "",
        description="Container image tag for the running build (set by CI/deploy).",
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
    plugin_request_timeout_s: float = Field(default=60.0, gt=0)

    # ``max_algs_per_ingest`` must be at least 1 (zero would 400 every ingest); 
    max_algs_per_ingest: int = Field(default=8, ge=1)
    max_title_length: int = Field(default=128, ge=0)

    # Public base URL used when building absolute URLs in IngestResponse
    # (outputAssetUrl, manifestUrl). When unset, the app falls back to
    # request.base_url (rewritten by the proxy-headers middleware so
    # X-Forwarded-Proto/Host are honored). Set this in production behind
    # an LB to pin the canonical host regardless of forwarded headers.
    public_base_url: str = ""

    # Trusted proxy CIDRs for ProxyHeadersMiddleware. ``*`` accepts
    # X-Forwarded-* from any peer — fine when the pod is only reachable
    # from a known proxy (k8s ClusterIP, Docker overlay). Lock down to
    # the proxy's CIDR for hardened deploys.
    forwarded_allow_ips: str = "*"

    # Resolution-api auto-push. 
    # ``resolution_push_enabled=True`` (the default) requires ``resolution_api_url`` to be set.
    # Set ``RESOLUTION_PUSH_ENABLED=false`` for standalone use.
    resolution_push_enabled: bool = True
    resolution_api_url: str = ""
    resolution_request_timeout_s: float = Field(default=10.0, gt=0)
    # Per-call retry policy.
    # Each HTTP request (POST /manifests and each POST /bindings)
    # gets up to N additional attempts on transient failures (timeouts, connection errors, 5xx).
    # 4xx is treated as permanent and surfaces immediately.
    # Backoff doubles each retry, starting at ``resolution_retry_backoff_s``.
    resolution_max_retries: int = Field(default=1, ge=0)
    resolution_retry_backoff_s: float = Field(default=0.5, ge=0)

    # Claim generator metadata embedded in the manifest.
    claim_generator_name: str = "Deepmark Inc."
    claim_generator_version: str = "0.1.0"

    # Signing credentials. Only ``credentials_dir`` + ``signing_alg``
    # are configurable; cert + key file names are derived from the alg
    # (``<alg>_certs.pem`` / ``<alg>_private.key``, lowercase) so deploys
    # can't accidentally point at one file but not the other, and so
    # alg-specific fixtures (es256_*, es384_*, ed25519_*, ...) can sit
    # alongside each other in one credentials directory.
    credentials_dir: Path = Field(
        ...,
        description=(
            "Directory containing <alg>_certs.pem + <alg>_private.key "
            "(file names are derived from signing_alg)."
        ),
    )
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

    @property
    def cert_chain_path(self) -> Path:
        return self.credentials_dir / f"{self.signing_alg.lower()}_certs.pem"

    @property
    def private_key_path(self) -> Path:
        return self.credentials_dir / f"{self.signing_alg.lower()}_private.key"

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
