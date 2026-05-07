"""
Media MIME-type / extension helpers + audio header inspection.

The C2PA Python SDK accepts a MIME type when adding ingredients and signing.
This module is the single source of truth for which MIMEs we accept on
ingest, which top-level ``MediaType`` they map to, and how we extract
sample rate / channels for format-preservation checks.

Supported MIMEs are constrained to what the C2PA Python SDK can sign
(see https://opensource.contentauthenticity.org/docs/c2pa-python/docs/supported-formats).
Adding a MIME the SDK doesn't support could cause a manifest-sign failure later in the pipeline.
The catalog (``algorithms.yaml``) declares per-alg MIME compatibility separately, 
and the orchestrator validates the requested algs against the upload's MIME at ingest time.
"""
from __future__ import annotations

import io
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from ingestion_api.core.logging import get_logger
from ingestion_api.models.ingestion import MediaType

logger = get_logger(__name__)


# MIME -> (media_type, canonical extension without the dot).
#
# Audio MIMEs are kept in lock-step with the C2PA Python SDK supported list:
# audio/wav, audio/mpeg, audio/flac, audio/mp4. 
# We also accept common aliases (audio/wave, audio/x-wav, audio/mp3, audio/x-flac)

SUPPORTED_MIME_TYPES: dict[str, tuple[MediaType, str]] = {
    # Audio
    "audio/wav":   (MediaType.AUDIO, "wav"),
    "audio/wave":  (MediaType.AUDIO, "wav"),
    "audio/x-wav": (MediaType.AUDIO, "wav"),
    "audio/mpeg":  (MediaType.AUDIO, "mp3"),
    "audio/mp3":   (MediaType.AUDIO, "mp3"),
    "audio/flac":  (MediaType.AUDIO, "flac"),
    "audio/x-flac": (MediaType.AUDIO, "flac"),
    "audio/mp4":   (MediaType.AUDIO, "m4a"),
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
# ``setdefault`` keeps the first MIME when several map to the same
# extension (e.g. audio/wav, audio/wave, audio/x-wav all share ``.wav``).
SUPPORTED_EXTENSIONS: dict[str, str] = {}
for _mime, (_kind, _ext) in SUPPORTED_MIME_TYPES.items():
    SUPPORTED_EXTENSIONS.setdefault(f".{_ext}", _mime)


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
    """Best-effort ``(MediaType, MIME)`` guess for an uploaded asset.

    Order of preference:
    1. Explicit ``content_type`` (when it's in the supported registry).
    2. The file extension of ``filename``.
    3. Python's ``mimetypes`` as a last resort.

    Returns ``None`` if nothing recognisable is found.
    Callers should surface this as a 4xx and refuse the ingestion.
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
