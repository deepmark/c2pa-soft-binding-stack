"""Tests for utils.media: MIME registry + audio header inspection."""
from __future__ import annotations

import io
import wave

import pytest

from ingestion_api.models.ingestion import MediaType
from ingestion_api.utils.media import (
    SUPPORTED_MIME_TYPES,
    AudioFormat,
    canonical_extension,
    guess_media_format,
    is_supported,
    read_audio_format,
)


# ---------------------------------------------------------------------------
# MIME registry — kept in lock-step with the C2PA Python SDK.
# ---------------------------------------------------------------------------


def test_supported_mimes_match_c2pa_audio_set():
    """The C2PA SDK signs audio/wav, audio/mpeg, audio/flac, audio/mp4."""
    canonical = {
        SUPPORTED_MIME_TYPES["audio/wav"],
        SUPPORTED_MIME_TYPES["audio/mpeg"],
        SUPPORTED_MIME_TYPES["audio/flac"],
        SUPPORTED_MIME_TYPES["audio/mp4"],
    }
    assert canonical == {
        (MediaType.AUDIO, "wav"),
        (MediaType.AUDIO, "mp3"),
        (MediaType.AUDIO, "flac"),
        (MediaType.AUDIO, "m4a"),
    }


def test_audio_ogg_is_no_longer_supported():
    """C2PA SDK doesn't sign OGG; we removed it to fail at ingest, not sign."""
    assert "audio/ogg" not in SUPPORTED_MIME_TYPES
    assert not is_supported("clip.ogg", "audio/ogg")


def test_audio_mp4_resolves_to_m4a_extension():
    assert canonical_extension("audio/mp4") == ".m4a"
    assert is_supported("clip.m4a", "audio/mp4")


def test_guess_media_format_prefers_explicit_content_type():
    assert guess_media_format("anything.bin", "audio/wav") == (MediaType.AUDIO, "audio/wav")


def test_guess_media_format_falls_back_to_extension():
    assert guess_media_format("clip.flac", None) == (MediaType.AUDIO, "audio/flac")


def test_guess_media_format_returns_none_for_unsupported():
    assert guess_media_format("doc.pdf", "application/pdf") is None
    assert guess_media_format("clip.ogg", "audio/ogg") is None


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


@pytest.mark.parametrize("alias", ["audio/wave", "audio/x-wav", "audio/mp3", "audio/x-flac"])
def test_aliases_resolve_to_canonical_extension(alias: str):
    assert is_supported(None, alias)
    fmt = guess_media_format(None, alias)
    assert fmt is not None
    assert fmt[0] is MediaType.AUDIO
