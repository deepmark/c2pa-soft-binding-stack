"""Tests for the resolution-api auto-push client."""
from __future__ import annotations

import httpx

from ingestion_api.models.ingestion import ResolutionPushStatus
from ingestion_api.services.resolution import (
    ResolutionPushClient,
    ResolutionPushRequest,
)


def _client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


def test_skipped_when_url_empty():
    rc = ResolutionPushClient(base_url="")
    result, mid = rc.push(ResolutionPushRequest(b"manifest", "alg", "v"))
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
        result, mid = rc.push(
            ResolutionPushRequest(b"manifest", "me.deepmark.audio.vigil.128", "ZmFrZQ=="),
        )

    assert result.status is ResolutionPushStatus.OK
    assert mid == "urn:c2pa:abc"
    assert calls[0][0] == "http://soft-binding:8000/manifests"
    assert calls[0][1] == "application/c2pa"
    assert calls[1][0] == "http://soft-binding:8000/bindings"


def test_failed_when_manifests_5xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    with _client(handler) as http:
        rc = ResolutionPushClient(base_url="http://soft-binding:8000", client=http)
        result, _ = rc.push(ResolutionPushRequest(b"m", "alg", "v"))

    assert result.status is ResolutionPushStatus.FAILED
    assert result.error
