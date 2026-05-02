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

MID = "urn:c2pa:test-id"


def _client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


def _req(*pairs: tuple[str, str], manifest: bytes = b"manifest") -> ResolutionPushRequest:
    return ResolutionPushRequest(
        manifest_bytes=manifest,
        manifest_id=MID,
        bindings=[BindingPair(alg=a, binding_value=v) for a, v in pairs],
    )


def test_skipped_when_url_empty():
    rc = ResolutionPushClient(base_url="")
    result = rc.push(_req(("alg", "v")))
    assert result.status is ResolutionPushStatus.SKIPPED


def test_happy_path():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), request.headers.get("content-type"), request.content))
        if request.url.path == "/manifests":
            return httpx.Response(200, json={"manifestId": MID})
        if request.url.path == "/bindings":
            return httpx.Response(204)
        return httpx.Response(404)

    with _client(handler) as http:
        rc = ResolutionPushClient(base_url="http://soft-binding:8000", client=http)
        result = rc.push(_req(("me.deepmark.audio.vigil.128", "ZmFrZQ==")))

    assert result.status is ResolutionPushStatus.OK
    assert calls[0][0] == "http://soft-binding:8000/manifests"
    assert calls[0][1] == "application/c2pa"
    assert calls[1][0] == "http://soft-binding:8000/bindings"
    # Caller-supplied manifestId is what binding posts use, regardless
    # of what /manifests returned.
    binding_body = json.loads(calls[1][2])
    assert binding_body["manifestId"] == MID


def test_pushes_one_binding_per_pair():
    """Each (alg, value) -> one POST /bindings; manifest posted once."""
    binding_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/manifests":
            return httpx.Response(200, json={"manifestId": MID})
        if request.url.path == "/bindings":
            binding_calls.append(json.loads(request.content))
            return httpx.Response(204)
        return httpx.Response(404)

    with _client(handler) as http:
        rc = ResolutionPushClient(base_url="http://soft-binding:8000", client=http)
        result = rc.push(
            _req(
                ("me.deepmark.audio.vigil.128", "WM=="),
                ("me.deepmark.audio.fp.chroma", "FP=="),
            ),
        )

    assert result.status is ResolutionPushStatus.OK
    assert [b["alg"] for b in binding_calls] == [
        "me.deepmark.audio.vigil.128",
        "me.deepmark.audio.fp.chroma",
    ]
    assert all(b["manifestId"] == MID for b in binding_calls)


def test_failed_when_manifests_5xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    with _client(handler) as http:
        rc = ResolutionPushClient(base_url="http://soft-binding:8000", client=http)
        result = rc.push(_req(("alg", "v")))

    assert result.status is ResolutionPushStatus.FAILED
    assert result.error


def test_failed_when_empty_bindings():
    rc = ResolutionPushClient(base_url="http://soft-binding:8000")
    result = rc.push(ResolutionPushRequest(manifest_bytes=b"m", manifest_id=MID, bindings=[]))
    assert result.status is ResolutionPushStatus.FAILED
    assert "non-empty" in (result.error or "")


def test_retries_transient_5xx_then_succeeds():
    """One transient 503 on /manifests, then 200 — exactly one retry consumed."""
    manifests_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal manifests_calls
        if request.url.path == "/manifests":
            manifests_calls += 1
            if manifests_calls == 1:
                return httpx.Response(503, text="warming up")
            return httpx.Response(200, json={"manifestId": MID})
        if request.url.path == "/bindings":
            return httpx.Response(204)
        return httpx.Response(404)

    with _client(handler) as http:
        rc = ResolutionPushClient(
            base_url="http://soft-binding:8000",
            client=http,
            max_retries=1,
            retry_backoff_s=0,  # no sleep in tests
        )
        result = rc.push(_req(("alg", "v")))

    assert result.status is ResolutionPushStatus.OK
    assert manifests_calls == 2  # one initial + one retry


def test_does_not_retry_4xx():
    """4xx is permanent — no retry, immediate FAILED."""
    manifests_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal manifests_calls
        manifests_calls += 1
        return httpx.Response(400, text="bad request")

    with _client(handler) as http:
        rc = ResolutionPushClient(
            base_url="http://soft-binding:8000",
            client=http,
            max_retries=3,
            retry_backoff_s=0,
        )
        result = rc.push(_req(("alg", "v")))

    assert result.status is ResolutionPushStatus.FAILED
    assert manifests_calls == 1  # no retries on 4xx
