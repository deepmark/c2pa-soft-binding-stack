"""
Algorithm catalog loader.

The catalog of supported soft-binding algorithms lives in a single YAML
file at the repo root (``algorithms.yaml``). Both ``resolution-api``
and ``ingestion-api`` mount this file from the same source of truth, so
the response of ``GET /services/supportedAlgorithms`` always matches what
ingestion-api can route to.

Schema::

    algorithms:
      - alg: me.deepmark.audio.vigil.128
        type: watermark            # watermark | fingerprint
        valueBits: 128
        mediaTypes: ["audio/wav", "audio/mpeg"]
        url: http://watermark-vigil-128:8000   # ingestion-api uses this; resolution
                                                # api ignores it but keeps it for
                                                # /ready dumps if exposed.

Hot-reloads on every read. Cheap (small file, parsed lazily) and avoids
having to bounce the service when a new plugin is added to the catalog.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from resolution_api.core.config import settings
from resolution_api.core.logging import get_logger

logger = get_logger(__name__)

AlgorithmType = Literal["watermark", "fingerprint"]


@dataclass(slots=True, frozen=True)
class AlgorithmEntry:
    alg: str
    type: AlgorithmType
    value_bits: int | None = None
    media_types: tuple[str, ...] = ()
    url: str | None = None


def load_catalog(path: Path | None = None) -> list[AlgorithmEntry]:
    """Read + validate the YAML catalog. Returns an empty list if the file
    is missing — that's a degraded state the operator can fix at runtime."""
    catalog_path = path or settings.algorithms_catalog_path
    if not catalog_path.is_file():
        logger.warning("Algorithm catalog not found at %s", catalog_path)
        return []

    raw = yaml.safe_load(catalog_path.read_text("utf-8")) or {}
    algorithms = raw.get("algorithms") or []
    out: list[AlgorithmEntry] = []
    for i, entry in enumerate(algorithms):
        try:
            out.append(
                AlgorithmEntry(
                    alg=str(entry["alg"]),
                    type=entry["type"],
                    value_bits=int(entry["valueBits"]) if entry.get("valueBits") else None,
                    media_types=tuple(entry.get("mediaTypes") or ()),
                    url=entry.get("url"),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "Skipping malformed algorithm entry #%d in %s: %s", i, catalog_path, exc,
            )
    return out
