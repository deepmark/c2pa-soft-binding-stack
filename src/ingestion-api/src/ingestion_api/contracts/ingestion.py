"""Ingest service input/output contracts."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ingestion_api.models.ingestion import IngestionRecord


@dataclass(slots=True)
class IngestionInput:
    """Inputs to a single ingest call.

    ``algs`` is the caller-supplied, ordered list of algorithm IDs to
    apply. Each must exist in ``algorithms.yaml`` and declare the
    upload's MIME in its ``mediaTypes``. Order matters — watermark
    passes mutate bytes for subsequent passes (put watermarks first).
    """
    filename: str
    content_type: str | None
    data: bytes
    algs: list[str]
    title: str | None = None


@dataclass(slots=True)
class IngestionResult:
    record: IngestionRecord
    signed_asset_path: Path
    manifest_bytes_path: Path | None
