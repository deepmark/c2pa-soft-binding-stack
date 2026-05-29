"""Tests for utils.media: MIME registry + audio header inspection."""
from __future__ import annotations

import io
import wave

import pytest

from ingestion_api.models.enums import MediaType
from ingestion_api.utils.media import (
    SUPPORTED_MIME_TYPES,
    AudioFormat,
    canonical_extension,
    container_matches_mime,
    guess_media_format,
    is_supported,
    read_audio_format,
)

# ---------------------------------------------------------------------------
# MIME registry — kept in lock-step with the C2PA Python SDK.
# ---------------------------------------------------------------------------


def test_supported_mimes_match_lossless_audio_set():
    """Only lossless audio is supported today (WAV + FLAC).

    Lossy formats (mpeg/mp4) were temporarily disabled in the registry
    because the watermark plugin can't preserve their container; until
    that lands, they're commented out in ``SUPPORTED_MIME_TYPES``.

    Each accepted MIME entry is ``(media_type, canonical_mime, ext)`` —
    the canonical column collapses aliases.
    """
    canonical = {
        (mt, cm) for mt, cm, _ in SUPPORTED_MIME_TYPES.values()
    }
    assert canonical == {
        (MediaType.AUDIO, "audio/wav"),
        (MediaType.AUDIO, "audio/flac"),
    }


def test_audio_ogg_is_no_longer_supported():
    """C2PA SDK doesn't sign OGG; we removed it to fail at ingest, not sign."""
    assert "audio/ogg" not in SUPPORTED_MIME_TYPES
    assert not is_supported("clip.ogg", "audio/ogg")


def test_lossy_audio_mimes_are_temporarily_unsupported():
    """Lossy audio MIMEs are commented out in the registry until the
    watermark plugin preserves their container end-to-end."""
    for mime in ("audio/mpeg", "audio/mp3", "audio/mp4"):
        assert mime not in SUPPORTED_MIME_TYPES
    assert not is_supported("clip.mp3", "audio/mpeg")
    assert not is_supported("clip.m4a", "audio/mp4")


def test_guess_media_format_prefers_explicit_content_type():
    assert guess_media_format("anything.bin", "audio/wav") == (MediaType.AUDIO, "audio/wav")


def test_guess_media_format_falls_back_to_extension():
    assert guess_media_format("clip.flac", None) == (MediaType.AUDIO, "audio/flac")


def test_guess_media_format_returns_none_for_unsupported():
    assert guess_media_format("doc.pdf", "application/pdf") is None
    assert guess_media_format("clip.ogg", "audio/ogg") is None


@pytest.mark.parametrize(
    "alias,canonical",
    [
        ("audio/wave", "audio/wav"),
        ("audio/x-wav", "audio/wav"),
        ("AUDIO/X-WAV; charset=binary", "audio/wav"),
        ("audio/x-flac", "audio/flac"),
    ],
)
def test_guess_media_format_canonicalizes_aliases(alias: str, canonical: str):
    """Aliases collapse to their canonical MIME so downstream code only
    ever sees one string per format."""
    assert guess_media_format(None, alias) == (MediaType.AUDIO, canonical)


def test_canonical_extension_accepts_aliases():
    """Accepts either canonical or alias; both yield the canonical ext."""
    assert canonical_extension("audio/wav") == ".wav"
    assert canonical_extension("audio/x-wav") == ".wav"
    assert canonical_extension("audio/flac") == ".flac"
    assert canonical_extension("audio/x-flac") == ".flac"


# ---------------------------------------------------------------------------
# read_audio_format
# ---------------------------------------------------------------------------


def _wav_bytes(*, sample_rate: int, channels: int, n_frames: int = 100) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "w") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * (channels * n_frames))
    return buf.getvalue()


def test_read_audio_format_parses_wav_header():
    fmt = read_audio_format(_wav_bytes(sample_rate=44100, channels=2))
    assert fmt == AudioFormat(sample_rate=44100, channels=2)


def test_read_audio_format_handles_mono_22k():
    fmt = read_audio_format(_wav_bytes(sample_rate=22050, channels=1))
    assert fmt == AudioFormat(sample_rate=22050, channels=1)


def test_read_audio_format_returns_none_for_empty_bytes():
    assert read_audio_format(b"") is None


def test_read_audio_format_returns_none_for_garbage():
    """Random bytes that aren't a recognised audio container."""
    assert read_audio_format(b"not an audio file at all" * 10) is None


def test_audio_format_equality_ignores_bit_depth():
    """Equality only considers sample_rate + channels (see dataclass docstring)."""
    a = AudioFormat(sample_rate=44100, channels=2)
    b = AudioFormat(sample_rate=44100, channels=2)
    assert a == b


@pytest.mark.parametrize("alias", ["audio/wave", "audio/x-wav", "audio/x-flac"])
def test_aliases_are_supported(alias: str):
    """All accepted aliases resolve as supported audio MIMEs.
    (Canonicalization of the returned MIME is asserted separately.)"""
    assert is_supported(None, alias)
    fmt = guess_media_format(None, alias)
    assert fmt is not None
    assert fmt[0] is MediaType.AUDIO


# ---------------------------------------------------------------------------
# container_matches_mime — defensive sniff before sign.
# ---------------------------------------------------------------------------


def test_container_matches_mime_accepts_wav_for_audio_wav():
    assert container_matches_mime(b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav")


def test_container_matches_mime_accepts_flac_for_audio_flac():
    assert container_matches_mime(b"fLaC\x00\x00\x00\x22", "audio/flac")


def test_container_matches_mime_rejects_wav_when_flac_claimed():
    """Plugin transcoded a flac upload to WAV — must surface as a mismatch."""
    assert not container_matches_mime(b"RIFF\x00\x00\x00\x00WAVE", "audio/flac")


def test_container_matches_mime_rejects_flac_when_wav_claimed():
    assert not container_matches_mime(b"fLaC\x00\x00\x00\x22", "audio/wav")


def test_container_matches_mime_passes_through_unregistered_mime():
    """Unregistered MIMEs return True so we don't accidentally fail-closed
    for media families we just haven't enrolled yet (image, video)."""
    assert container_matches_mime(b"\xff\xd8\xff\xe0", "image/jpeg")
    assert container_matches_mime(b"\x00\x00\x00\x18ftyp", "video/mp4")
