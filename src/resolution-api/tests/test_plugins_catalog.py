"""Tests for the MongoDB-backed plugin catalog."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from resolution_api.services.plugins_catalog import (
    PluginEntry,
    PluginNotFoundError,
    load_all_plugins,
    resolve,
)


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        if length is not None:
            return self._docs[:length]
        return list(self._docs)


def _make_col(docs, find_one_result=None):
    col = AsyncMock()
    col.find_one = AsyncMock(return_value=find_one_result)
    col.find = lambda *a, **kw: _FakeCursor(docs)
    return col


class TestResolve:
    async def test_returns_entry_for_known_alg(self):
        doc = {
            "alg": "me.deepmark.audio.aware.20",
            "type": "watermark",
            "bindingBits": 20,
            "mediaTypes": ["audio/wav"],
            "url": "http://watermark-aware-20:9004",
        }
        col = _make_col([], find_one_result=doc)
        with patch(
            "resolution_api.services.plugins_catalog.get_supported_algorithms_collection",
            return_value=col,
        ):
            entry = await resolve("me.deepmark.audio.aware.20")
        assert entry.alg == "me.deepmark.audio.aware.20"
        assert entry.type == "watermark"
        assert entry.binding_bits == 20
        assert entry.url == "http://watermark-aware-20:9004"
        assert "audio/wav" in entry.media_types

    async def test_raises_for_unknown_alg(self):
        col = _make_col([], find_one_result=None)
        with patch(
            "resolution_api.services.plugins_catalog.get_supported_algorithms_collection",
            return_value=col,
        ):
            with pytest.raises(PluginNotFoundError):
                await resolve("no.such.alg")

    async def test_handles_missing_optional_fields(self):
        doc = {
            "alg": "me.deepmark.audio.aware.20",
            "type": "watermark",
        }
        col = _make_col([], find_one_result=doc)
        with patch(
            "resolution_api.services.plugins_catalog.get_supported_algorithms_collection",
            return_value=col,
        ):
            entry = await resolve("me.deepmark.audio.aware.20")
        assert entry.binding_bits == 0
        assert entry.media_types == ()
        assert entry.url is None


class TestLoadAllPlugins:
    async def test_returns_all_entries(self):
        docs = [
            {
                "alg": "me.deepmark.audio.aware.20",
                "type": "watermark",
                "bindingBits": 20,
                "mediaTypes": ["audio/wav"],
                "url": "http://watermark-aware-20:9004",
            },
            {
                "alg": "org.example.audiofp.v1",
                "type": "fingerprint",
                "bindingBits": 64,
                "mediaTypes": ["audio/mpeg"],
            },
        ]
        col = _make_col(docs)
        with patch(
            "resolution_api.services.plugins_catalog.get_supported_algorithms_collection",
            return_value=col,
        ):
            entries = await load_all_plugins()
        assert len(entries) == 2
        wm = next(e for e in entries if e.type == "watermark")
        assert wm.alg == "me.deepmark.audio.aware.20"
        assert wm.binding_bits == 20
        assert wm.url == "http://watermark-aware-20:9004"
        fp = next(e for e in entries if e.type == "fingerprint")
        assert fp.alg == "org.example.audiofp.v1"
        assert fp.url is None

    async def test_returns_empty_list_when_no_docs(self):
        col = _make_col([])
        with patch(
            "resolution_api.services.plugins_catalog.get_supported_algorithms_collection",
            return_value=col,
        ):
            entries = await load_all_plugins()
        assert entries == []
