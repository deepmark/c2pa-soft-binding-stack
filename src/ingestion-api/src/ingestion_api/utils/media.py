"""
Media MIME-type / extension helpers.

The C2PA Python SDK accepts a MIME type when adding ingredients and
signing — c2pa-rs has known good support for a range of audio/video/image
containers. This module is the single source of truth for which MIMEs we
accept on ingest and which top-level ``MediaType`` they map to.

Add a new MIME by extending ``SUPPORTED_MIME_TYPES`` (one place); the
catalog (``algorithms.yaml``) declares per-alg MIME compatibility
separately, and the orchestrator validates the requested algs against
the upload's MIME at ingest time.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from ingestion_api.models.ingestion import MediaType


# mime -> (media_type, canonical extension without the dot)
SUPPORTED_MIME_TYPES: dict[str, tuple[MediaType, str]] = {
    # Audio
    "audio/wav":   (MediaType.AUDIO, "wav"),
    "audio/wave":  (MediaType.AUDIO, "wav"),
    "audio/x-wav": (MediaType.AUDIO, "wav"),
    "audio/mpeg":  (MediaType.AUDIO, "mp3"),
    "audio/mp3":   (MediaType.AUDIO, "mp3"),
    "audio/flac":  (MediaType.AUDIO, "flac"),
    "audio/x-flac": (MediaType.AUDIO, "flac"),
    "audio/ogg":   (MediaType.AUDIO, "ogg"),
    # Video and image entries are added as plugins for those media land.
    # Reserve the keys here once you have a working pipeline end-to-end:
    #
    # "video/mp4":  (MediaType.VIDEO, "mp4"),
    # "image/jpeg": (MediaType.IMAGE, "jpg"),
}

# Reverse: extension (with leading dot) -> canonical MIME. Built lazily
# so additions to ``SUPPORTED_MIME_TYPES`` propagate without restating.
SUPPORTED_EXTENSIONS: dict[str, str] = {}
for _mime, (_kind, _ext) in SUPPORTED_MIME_TYPES.items():
    SUPPORTED_EXTENSIONS.setdefault(f".{_ext}", _mime)


def guess_media_format(
    filename: str | None, content_type: str | None,
) -> tuple[MediaType, str] | None:
    """Best-effort ``(MediaType, MIME)`` guess for an uploaded asset.

    Order of preference:
    1. Explicit ``content_type`` (when it's in the supported registry).
    2. The file extension of ``filename``.
    3. Python's ``mimetypes`` as a last resort.

    Returns ``None`` if nothing recognisable is found — callers should
    surface this as a 4xx and refuse the ingest.
    """
    mime = _normalise_mime(content_type)
    if mime and mime in SUPPORTED_MIME_TYPES:
        kind, _ = SUPPORTED_MIME_TYPES[mime]
        return kind, mime

    if filename:
        ext = Path(filename).suffix.lower()
        if ext in SUPPORTED_EXTENSIONS:
            mime = SUPPORTED_EXTENSIONS[ext]
            kind, _ = SUPPORTED_MIME_TYPES[mime]
            return kind, mime
        guessed, _ = mimetypes.guess_type(filename)
        guessed_norm = _normalise_mime(guessed)
        if guessed_norm and guessed_norm in SUPPORTED_MIME_TYPES:
            kind, _ = SUPPORTED_MIME_TYPES[guessed_norm]
            return kind, guessed_norm

    return None


def canonical_extension(mime: str) -> str:
    """Canonical file extension (with leading dot) for a supported MIME.

    Raises ``KeyError`` if the MIME isn't registered — caller is
    expected to have already validated via ``guess_media_format``.
    """
    _, ext = SUPPORTED_MIME_TYPES[mime]
    return f".{ext}"


def is_supported(filename: str | None, content_type: str | None) -> bool:
    return guess_media_format(filename, content_type) is not None


def _normalise_mime(mime: str | None) -> str | None:
    if not mime:
        return None
    return mime.lower().split(";", 1)[0].strip()
