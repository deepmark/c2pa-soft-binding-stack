"""Shared pytest fixtures."""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def sample_wav_path(fixtures_dir: Path) -> Path:
    """0.5 s 22050 Hz mono 440 Hz tone. ~22 KB."""
    out = fixtures_dir / "sample.wav"
    if out.is_file():
        return out
    sr = 22050
    n = sr // 2
    with wave.open(str(out), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = b"".join(
            struct.pack(
                "<h",
                int(0.3 * 32767 * math.sin(2 * math.pi * 440 * i / sr)),
            )
            for i in range(n)
        )
        w.writeframes(frames)
    return out


@pytest.fixture(scope="session")
def sample_wav_bytes(sample_wav_path: Path) -> bytes:
    return sample_wav_path.read_bytes()


@pytest.fixture(scope="session")
def credentials_present() -> bool:
    """True if both ES256 test cert + key are on disk under <repo>/credentials.

    File layout: ``<repo>/src/ingestion-api/tests/conftest.py`` -> repo root
    is three folders up."""
    repo_root = Path(__file__).resolve().parents[3]
    return (
        (repo_root / "credentials" / "es256_certs.pem").is_file()
        and (repo_root / "credentials" / "es256_private.key").is_file()
    )
