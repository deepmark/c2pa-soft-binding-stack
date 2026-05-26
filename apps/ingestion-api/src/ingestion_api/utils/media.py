"""
Media MIME-type / extension helpers + audio header inspection.

The C2PA Python SDK accepts a MIME type when adding ingredients and signing.
This module is the single source of truth for which MIMEs we accept on
ingest and which top-level ``MediaType`` they map to.

Supported MIMEs are constrained to what the C2PA Python SDK can sign
(see https://opensource.contentauthenticity.org/docs/c2pa-python/docs/supported-formats).
Adding a MIME the SDK doesn't support could cause a manifest-sign failure later in the pipeline.
The catalog (``supported_algorithms`` collection) declares per-alg MIME compatibility separately,
and the orchestrator validates the requested algs against the upload's MIME at ingest time.

Format-preservation inspection is currently audio-only: ``read_audio_format``
extracts sample rate + channels so the orchestrator can reject watermark
plugins that re-sample or mono-mix. When image / video plugins land they
will have their own dimensions (resolution, frame rate, color space, …)
and should grow sibling helpers rather than overload ``AudioFormat``.
"""
from __future__ import annotations

import io
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from ingestion_api.core.logging import get_logger
from ingestion_api.models.enums import MediaType

logger = get_logger(__name__)


# Accepted MIME -> (media_type, canonical_mime, canonical_ext_without_dot).
#
# Canonical MIMEs are the C2PA Python SDK supported list:
# audio/wav, audio/mpeg, audio/flac, audio/mp4.
# We also accept common aliases (audio/wave, audio/x-wav, audio/mp3,
# audio/x-flac) but ``guess_media_format`` always returns the canonical
# form so downstream code (catalog matching, plugin headers, manifest
# signing, persisted records, response bodies) only ever sees one MIME
# string per format.

SUPPORTED_MIME_TYPES: dict[str, tuple[MediaType, str, str]] = {
    # Audio
    "audio/wav":    (MediaType.AUDIO, "audio/wav",  "wav"),
    "audio/wave":   (MediaType.AUDIO, "audio/wav",  "wav"),
    "audio/x-wav":  (MediaType.AUDIO, "audio/wav",  "wav"),
    "audio/mpeg":   (MediaType.AUDIO, "audio/mpeg", "mp3"),
    "audio/mp3":    (MediaType.AUDIO, "audio/mpeg", "mp3"),
    "audio/flac":   (MediaType.AUDIO, "audio/flac", "flac"),
    "audio/x-flac": (MediaType.AUDIO, "audio/flac", "flac"),
    "audio/mp4":    (MediaType.AUDIO, "audio/mp4",  "m4a"),
    # Image and video MIMEs the C2PA SDK supports, kept commented out
    # until a plugin and an end-to-end test land for that media family.
    #
    # Image:  image/avif, image/x-adobe-dng, image/gif, image/heic,
    #         image/heif, image/jpeg, image/jxl, image/png,
    #         image/svg+xml, image/tiff, image/webp
    # Video:  video/avi (+ aliases), video/mp4 (+ application/mp4),
    #         video/quicktime
}

# Reverse: extension (with leading dot) -> canonical MIME.
# Aliases for the same format share a canonical MIME + extension, so
# repeated writes in this comprehension are idempotent.
SUPPORTED_EXTENSIONS: dict[str, str] = {
    f".{ext}": canonical_mime
    for _, canonical_mime, ext in SUPPORTED_MIME_TYPES.values()
}


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """Header-derived audio container metadata.

    Bit depth is excluded from equality checks because lossy formats
    (MP3, AAC) don't have a meaningful sample width.
    """
    sample_rate: int
    channels: int


def guess_media_format(
    filename: str | None, content_type: str | None,
) -> tuple[MediaType, str] | None:
    """Best-effort ``(MediaType, canonical MIME)`` guess for an uploaded asset.

    Order of preference:
    1. Explicit ``content_type`` (when it's in the supported registry).
    2. The file extension of ``filename``.
    3. Python's ``mimetypes`` as a last resort.

    Aliases are collapsed: ``audio/wave``, ``audio/x-wav`` -> ``audio/wav``;
    ``audio/mp3`` -> ``audio/mpeg``; ``audio/x-flac`` -> ``audio/flac``.
    The original upload Content-Type is not preserved here.

    Returns ``None`` if nothing recognisable is found.
    Callers should surface this as a 4xx and refuse the ingestion.
    """
    mime = _normalise_mime(content_type)
    if mime and mime in SUPPORTED_MIME_TYPES:
        kind, canonical_mime, _ = SUPPORTED_MIME_TYPES[mime]
        return kind, canonical_mime

    if filename:
        ext = Path(filename).suffix.lower()
        if ext in SUPPORTED_EXTENSIONS:
            canonical_mime = SUPPORTED_EXTENSIONS[ext]
            kind, _, _ = SUPPORTED_MIME_TYPES[canonical_mime]
            return kind, canonical_mime
        guessed, _ = mimetypes.guess_type(filename)
        guessed_norm = _normalise_mime(guessed)
        if guessed_norm and guessed_norm in SUPPORTED_MIME_TYPES:
            kind, canonical_mime, _ = SUPPORTED_MIME_TYPES[guessed_norm]
            return kind, canonical_mime

    return None


def canonical_extension(mime: str) -> str:
    """Canonical file extension (with leading dot) for a supported MIME.

    Accepts either a canonical MIME or one of its aliases.
    Raises ``KeyError`` if the MIME isn't registered — caller is
    expected to have already validated via ``guess_media_format``.
    """
    _, _, ext = SUPPORTED_MIME_TYPES[mime]
    return f".{ext}"


def is_supported(filename: str | None, content_type: str | None) -> bool:
    """Check if a content type or filename extension is supported."""
    return guess_media_format(filename, content_type) is not None


def read_audio_format(audio_bytes: bytes) -> AudioFormat | None:
    """Parse sample rate + channels from an audio container header.

    Returns ``None`` if mutagen can't recognise the container or the
    bytes are too short.

    Callers that rely on this for safety checks should treat ``None`` as
    "skip the check, log a warning" rather than fail-closed.

    Mutagen only reads the container header (a few KB),
    so this is cheap to call on every plugin pass.
    """
    if not audio_bytes:
        return None
    try:
        from mutagen import File as MutagenFile

        info = MutagenFile(io.BytesIO(audio_bytes))
    except Exception:
        logger.debug("mutagen failed to parse audio header", exc_info=True)
        return None
    if info is None or getattr(info, "info", None) is None:
        return None

    sample_rate = int(getattr(info.info, "sample_rate", 0) or 0)
    channels = int(getattr(info.info, "channels", 0) or 0)
    if sample_rate <= 0 or channels <= 0:
        return None
    return AudioFormat(sample_rate=sample_rate, channels=channels)


def _normalise_mime(mime: str | None) -> str | None:
    """Normalise a MIME type to its canonical form."""
    if not mime:
        return None
    return mime.lower().split(";", 1)[0].strip()
