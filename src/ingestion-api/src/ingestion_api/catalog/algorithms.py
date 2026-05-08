"""
Algorithm catalog loader.

Catalog source of truth is the shared ``algorithms.yaml`` mounted at
``settings.algorithms_catalog_path``. Each entry maps an algorithm
identifier to:

- its type (``watermark`` or ``fingerprint``),
- its binding value width in bits,
- supported media types,
- a base URL for the plugin container (``url``).

This module is pure config — no HTTP, no plugin invocation. The
outbound HTTP wrapper that actually talks to plugins lives in
``adapters.dispatcher`` and re-uses ``AlgorithmEntry`` from here.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ingestion_api.config import settings
from ingestion_api.logging import get_logger
from ingestion_api.models.enums import SoftBindingKind

logger = get_logger(__name__)


class AlgorithmNotFoundError(LookupError):
    """The catalog has no entry for the requested ``alg`` identifier."""


@dataclass(slots=True, frozen=True)
class AlgorithmEntry:
    alg: str
    type: SoftBindingKind
    binding_bits: int
    media_types: tuple[str, ...]
    url: str | None


def load_catalog(path: Path | None = None) -> list[AlgorithmEntry]:
    """Read + validate the YAML catalog. Returns an empty list if the file
    is missing or malformed (each malformed row is skipped with a warning).

    ``bindingBits`` is required and must be positive. Non-byte-aligned
    widths are allowed — the value generator zeroes the unused high
    bits of the first byte. Entries with missing or non-positive
    ``bindingBits`` are skipped at load time so the misconfig surfaces
    at boot, not at first request.
    """
    catalog_path = path or settings.algorithms_catalog_path
    if not catalog_path.is_file():
        logger.warning("Algorithm catalog not found at %s", catalog_path)
        return []

    raw = yaml.safe_load(catalog_path.read_text("utf-8")) or {}
    out: list[AlgorithmEntry] = []
    for i, entry in enumerate(raw.get("algorithms") or []):
        try:
            binding_bits = int(entry["bindingBits"])
            if binding_bits <= 0:
                raise ValueError(
                    f"bindingBits must be positive, got {binding_bits}",
                )
            out.append(
                AlgorithmEntry(
                    alg=str(entry["alg"]),
                    type=entry["type"],
                    binding_bits=binding_bits,
                    media_types=tuple(entry.get("mediaTypes") or ()),
                    url=entry.get("url"),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "Skipping malformed algorithm entry #%d in %s: %s", i, catalog_path, exc,
            )
    return out


def resolve(alg: str, *, catalog: list[AlgorithmEntry] | None = None) -> AlgorithmEntry:
    """Find an algorithm by id; raises ``AlgorithmNotFoundError`` if missing."""
    entries = catalog if catalog is not None else load_catalog()
    for entry in entries:
        if entry.alg == alg:
            return entry
    raise AlgorithmNotFoundError(
        f"alg={alg!r} not found in catalog {settings.algorithms_catalog_path}. "
        f"Known algorithms: {[e.alg for e in entries]}"
    )
