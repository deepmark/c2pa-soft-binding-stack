"""Tests for the resolution-api auto-push client."""
from __future__ import annotations

import json

import httpx

from ingestion_api.models.ingestion import ResolutionPushStatus
from ingestion_api.services.publisher import (
    BindingPair,
    ResolutionPushClient,
    ResolutionPushRequest,
)


def _client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


def _req(*pairs: tuple[str, str], manifest: bytes = b"manifest") -> ResolutionPushRequest:
    return ResolutionPushRequest(
        manifest_bytes=manifest,
        bindings=[BindingPair(alg=a, binding_value=v) for a, v in pairs],
    )


def test_skipped_when_url_empty():
    rc = ResolutionPushClient(base_url="")
    result, mid = rc.push(_req(("alg", "v")))
    assert result.status is ResolutionPushStatus.SKIPPED
    assert mid is None


def test_happy_path():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), request.headers.get("content-type"), request.content))
        if request.url.path == "/manifests":
            return httpx.Response(200, json={"manifestId": "urn:c2pa:abc"})
        if request.url.path == "/bindings":
            return httpx.Response(204)
        return httpx.Response(404)

    with _client(handler) as http:
        rc = ResolutionPushClient(base_url="http://soft-binding:8000", client=http)
        result, mid = rc.push(_req(("me.deepmark.audio.vigil.128", "ZmFrZQ==")))

    assert result.status is ResolutionPushStatus.OK
    assert mid == "urn:c2pa:abc"
    assert calls[0][0] == "http://soft-binding:8000/manifests"
    assert calls[0][1] == "application/c2pa"
    assert calls[1][0] == "http://soft-binding:8000/bindings"


def test_pushes_one_binding_per_pair():
    """Each (alg, value) -> one POST /bindings; manifest posted once."""
    binding_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/manifests":
            return httpx.Response(200, json={"manifestId": "urn:c2pa:abc"})
        if request.url.path == "/bindings":
            binding_calls.append(json.loads(request.content))
            return httpx.Response(204)
        return httpx.Response(404)

    with _client(handler) as http:
        rc = ResolutionPushClient(base_url="http://soft-binding:8000", client=http)
        result, mid = rc.push(
            _req(
                ("me.deepmark.audio.vigil.128", "WM=="),
                ("me.deepmark.audio.fp.chroma", "FP=="),
            ),
        )

    assert result.status is ResolutionPushStatus.OK
    assert mid == "urn:c2pa:abc"
    assert [b["alg"] for b in binding_calls] == [
        "me.deepmark.audio.vigil.128",
        "me.deepmark.audio.fp.chroma",
    ]
    assert all(b["manifestId"] == "urn:c2pa:abc" for b in binding_calls)


def test_failed_when_manifests_5xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    with _client(handler) as http:
        rc = ResolutionPushClient(base_url="http://soft-binding:8000", client=http)
        result, _ = rc.push(_req(("alg", "v")))

    assert result.status is ResolutionPushStatus.FAILED
    assert result.error


def test_failed_when_empty_bindings():
    rc = ResolutionPushClient(base_url="http://soft-binding:8000")
    result, _ = rc.push(ResolutionPushRequest(b"m", []))
    assert result.status is ResolutionPushStatus.FAILED
    assert "non-empty" in (result.error or "")
