"""
Audio MIME-type / extension helpers.

The C2PA Python SDK accepts a MIME type when adding ingredients and signing.
We restrict ingest to formats that c2pa-rs has known good support for. WAV
is the primary format (it's the easiest case for c2pa-rs to embed JUMBF
into); MP3/FLAC are accepted but pass through the same code path.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

SUPPORTED_AUDIO_MIME_TYPES: dict[str, str] = {
    "audio/wav": "wav",
    "audio/wave": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/ogg": "ogg",
}

SUPPORTED_AUDIO_EXTENSIONS: dict[str, str] = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
}


def guess_audio_format(filename: str | None, content_type: str | None) -> str | None:
    """
    Best-effort MIME guess for an uploaded audio asset.

    Order of preference: explicit content_type (if it's a known audio type),
    then the extension of ``filename``, then ``mimetypes`` as a last resort.
    Returns None if nothing recognisable is found.
    """
    if content_type:
        ct = content_type.lower().split(";", 1)[0].strip()
        if ct in SUPPORTED_AUDIO_MIME_TYPES:
            return ct
    if filename:
        ext = Path(filename).suffix.lower()
        if ext in SUPPORTED_AUDIO_EXTENSIONS:
            return SUPPORTED_AUDIO_EXTENSIONS[ext]
        guessed, _ = mimetypes.guess_type(filename)
        if guessed and guessed.lower() in SUPPORTED_AUDIO_MIME_TYPES:
            return guessed.lower()
    return None


def is_supported_audio(filename: str | None, content_type: str | None) -> bool:
    return guess_audio_format(filename, content_type) is not None
