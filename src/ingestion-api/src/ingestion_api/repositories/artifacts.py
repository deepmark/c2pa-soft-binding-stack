"""
Local filesystem store for ingest binary artifacts 
(the signed asset and the raw signed manifest bytes).

We never persist the raw upload or any plugin-side watermarked intermediate.
Plugin bytes flow over HTTP and stay in memory until the signing step. 
This module only manages the *durable* binary set:

    <storage_root>/ingestions/<ingestionId>/
      signed.<ext>            # signed asset emitted by Builder.sign
      manifest.c2pa           # raw manifest bytes returned by the SDK

Structured ingestion records live in MongoDB — see ``repositories.ingestions``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ingestion_api.config import settings


@dataclass(slots=True)
class IngestionArtifacts:
    """Per-ingestion path bundle returned by ``ArtifactStore.allocate``.

    ``ingestion_dir`` is the leaf directory ``<storage_root>/ingestions/<id>/``;
    ``signed_path`` and ``manifest_bytes_path`` live inside it.
    """
    ingestion_id: str
    ingestion_dir: Path
    signed_path: Path
    manifest_bytes_path: Path


class ArtifactStore:
    """File-backed binary artifact store. Safe to reuse across requests."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = (root or settings.storage_root).resolve()
        self._ingest_root = self._root / "ingestions"
        self._ingest_root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def ingest_root(self) -> Path:
        return self._ingest_root

    def allocate(self, ingestion_id: str, ext: str) -> IngestionArtifacts:
        ext = ext.lower()
        if not ext.startswith("."):
            ext = "." + ext
        base = self._ingest_root / ingestion_id
        base.mkdir(parents=True, exist_ok=False)
        return IngestionArtifacts(
            ingestion_id=ingestion_id,
            ingestion_dir=base,
            signed_path=base / f"signed{ext}",
            manifest_bytes_path=base / "manifest.c2pa",
        )

    def write_manifest_bytes(self, artifacts: IngestionArtifacts, data: bytes) -> None:
        artifacts.manifest_bytes_path.write_bytes(data)

    def load_signed_asset(self, ingestion_id: str) -> Path | None:
        base = self._ingest_root / ingestion_id
        if not base.is_dir():
            return None
        for child in base.iterdir():
            if child.is_file() and child.stem == "signed":
                return child
        return None

    def load_manifest_bytes_path(self, ingestion_id: str) -> Path | None:
        path = self._ingest_root / ingestion_id / "manifest.c2pa"
        return path if path.is_file() else None

    def cleanup(self, artifacts: IngestionArtifacts) -> None:
        try:
            for child in artifacts.ingestion_dir.glob("*"):
                child.unlink(missing_ok=True)
            artifacts.ingestion_dir.rmdir()
        except OSError:
            pass

    def delete(self, ingestion_id: str) -> bool:
        """Remove the per-ingestion directory if present. Idempotent.

        Returns True if anything was on disk to delete, False if the
        directory was already absent. Safe to call from a takedown
        path even when only the record (or only the artifacts)
        previously existed.
        """
        base = self._ingest_root / ingestion_id
        if not base.exists():
            return False
        existed = False
        try:
            for child in base.glob("*"):
                child.unlink(missing_ok=True)
                existed = True
            base.rmdir()
            existed = True
        except OSError:
            return existed
        return existed
