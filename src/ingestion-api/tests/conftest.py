"""Shared pytest fixtures."""
from __future__ import annotations

import math
import os
import struct
import tempfile
import wave
from pathlib import Path

import pytest

# Required settings must be in the env BEFORE the ingestion_api
# package imports (Settings() runs at module import). We point paths at
# the dev checkout's repo root for catalog + credentials, and at a
# tmp dir for storage so tests don't write under the repo.
#
# MONGODB_URL + DATABASE_NAME must be set for Settings() to validate,
# but the unit tests use InMemoryIngestionRecordRepository — no live
# Mongo connection is ever opened from the test process.
_REPO_ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("PLUGINS_CATALOG_PATH", str(_REPO_ROOT / "plugins.yaml"))
os.environ.setdefault("CREDENTIALS_DIR", str(_REPO_ROOT / "credentials"))
os.environ.setdefault(
    "STORAGE_ROOT", str(Path(tempfile.gettempdir()) / "ingestion-api-tests-storage")
)
os.environ.setdefault("MONGODB_URL", "mongodb://localhost:27017")
os.environ.setdefault("DATABASE_NAME", "ingestion_api_tests")
# Tests stub the resolution client directly (see test_orchestrator.py),
# so no real push target exists. Disable the auto-push gate so
# Settings() validation passes without a URL.
os.environ.setdefault("RESOLUTION_PUSH_ENABLED", "false")
# Default retries to 0 in tests so 5xx-failure cases don't sleep
# through backoff. Tests that exercise the retry branch construct
# ResolutionPushClient with explicit max_retries.
os.environ.setdefault("RESOLUTION_MAX_RETRIES", "0")

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
