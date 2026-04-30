"""
Local filesystem storage for ingest artifacts.

We never persist the raw upload or any plugin-side watermarked
intermediate — plugin bytes flow over HTTP and stay in memory until
the signing step. This module only manages the *durable* artifact set:

    <storage_root>/ingestions/<ingestionId>/
      signed.<ext>            # signed asset emitted by Builder.sign
      manifest.c2pa           # raw manifest bytes returned by the SDK
      metadata.json           # IngestionRecord serialised
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ingestion_api.core.config import settings
from ingestion_api.models.ingestion import IngestionRecord


@dataclass(slots=True)
class IngestionArtifacts:
    ingestion_id: str
    base_dir: Path
    signed_path: Path
    manifest_bytes_path: Path
    metadata_path: Path


class LocalAssetStore:
    """File-backed artifact store. Safe to reuse across requests."""

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
            base_dir=base,
            signed_path=base / f"signed{ext}",
            manifest_bytes_path=base / "manifest.c2pa",
            metadata_path=base / "metadata.json",
        )

    def write_manifest_bytes(self, artifacts: IngestionArtifacts, data: bytes) -> None:
        artifacts.manifest_bytes_path.write_bytes(data)

    def write_metadata(self, artifacts: IngestionArtifacts, record: IngestionRecord) -> None:
        artifacts.metadata_path.write_text(
            record.model_dump_json(indent=2),
            encoding="utf-8",
        )

    def load_metadata(self, ingestion_id: str) -> IngestionRecord | None:
        metadata_path = self._ingest_root / ingestion_id / "metadata.json"
        if not metadata_path.is_file():
            return None
        return IngestionRecord.model_validate_json(metadata_path.read_text("utf-8"))

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
            for child in artifacts.base_dir.glob("*"):
                child.unlink(missing_ok=True)
            artifacts.base_dir.rmdir()
        except OSError:
            pass
