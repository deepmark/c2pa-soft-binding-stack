"""Settings: AUDIO_ALGS env-var parsing.

The field is typed ``list[str]`` so pydantic-settings JSON-decodes the
env value. A bare string is rejected — the operator must wrap a single
alg in a JSON list (``AUDIO_ALGS=["alg.id"]``) so single-vs-list is
unambiguous.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from pydantic_settings.sources import SettingsError

from ingestion_api.core.config import Settings


@pytest.fixture
def required_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ALGORITHMS_CATALOG_PATH", str(tmp_path / "algorithms.yaml"))
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path / "creds"))


def test_default(required_env, monkeypatch):
    monkeypatch.delenv("AUDIO_ALGS", raising=False)
    assert Settings().audio_algs == ["me.deepmark.audio.vigil.128"]


def test_json_list_multi(required_env, monkeypatch):
    monkeypatch.setenv("AUDIO_ALGS", '["alg.one", "alg.two"]')
    assert Settings().audio_algs == ["alg.one", "alg.two"]


def test_json_list_single(required_env, monkeypatch):
    monkeypatch.setenv("AUDIO_ALGS", '["only.one"]')
    assert Settings().audio_algs == ["only.one"]


def test_bare_string_rejected(required_env, monkeypatch):
    """Forcing JSON-list syntax keeps single vs. list unambiguous."""
    monkeypatch.setenv("AUDIO_ALGS", "alg.one")
    with pytest.raises((SettingsError, ValidationError)):
        Settings()


def test_empty_list_rejected(required_env, monkeypatch):
    monkeypatch.setenv("AUDIO_ALGS", "[]")
    with pytest.raises(ValidationError):
        Settings()
