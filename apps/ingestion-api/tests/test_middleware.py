"""Tests for the RequestIDMiddleware ASGI wrapper."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request

from ingestion_api.core.logging import REQUEST_ID_HEADER, get_request_id
from ingestion_api.middleware.request_id import RequestIDMiddleware


def _build_app() -> FastAPI:
    """Tiny FastAPI app with the middleware mounted + an introspection route.

    The route returns the request id the contextvar / state see — that's
    what we actually want to assert on (echoing the incoming header would
    pass even with a no-op middleware).
    """
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/echo")
    async def echo(request: Request) -> dict:
        return {
            "from_state": getattr(request.state, "request_id", None),
            "from_contextvar": get_request_id(),
        }

    return app


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_build_app()),
        base_url="http://test",
    )


@pytest.mark.asyncio
async def test_generates_request_id_when_header_missing(client: httpx.AsyncClient):
    async with client:
        r = await client.get("/echo")
    assert r.status_code == 200
    rid = r.headers[REQUEST_ID_HEADER]
    assert rid  # non-empty
    assert len(rid) == 32  # uuid4().hex
    body = r.json()
    assert body["from_state"] == rid
    assert body["from_contextvar"] == rid


@pytest.mark.asyncio
async def test_echoes_caller_supplied_request_id(client: httpx.AsyncClient):
    async with client:
        r = await client.get(
            "/echo", headers={REQUEST_ID_HEADER: "client-correlation-123"},
        )
    assert r.headers[REQUEST_ID_HEADER] == "client-correlation-123"
    body = r.json()
    assert body["from_state"] == "client-correlation-123"
    assert body["from_contextvar"] == "client-correlation-123"


@pytest.mark.asyncio
async def test_caps_oversized_caller_id_at_128_chars(client: httpx.AsyncClient):
    """A pathological client sending a 4kB request id shouldn't pollute logs."""
    long_rid = "x" * 4096
    async with client:
        r = await client.get("/echo", headers={REQUEST_ID_HEADER: long_rid})
    rid = r.headers[REQUEST_ID_HEADER]
    assert len(rid) == 128
    assert rid == "x" * 128
    assert r.json()["from_state"] == rid


@pytest.mark.asyncio
async def test_falls_back_to_uuid_when_caller_id_blank(client: httpx.AsyncClient):
    """Empty / whitespace-only header should trigger a fresh UUID, not pass through empty."""
    async with client:
        r = await client.get("/echo", headers={REQUEST_ID_HEADER: "   "})
    rid = r.headers[REQUEST_ID_HEADER]
    assert rid.strip()
    assert len(rid) == 32  # uuid4().hex


@pytest.mark.asyncio
async def test_request_ids_are_unique_across_requests(client: httpx.AsyncClient):
    async with client:
        r1 = await client.get("/echo")
        r2 = await client.get("/echo")
    assert r1.headers[REQUEST_ID_HEADER] != r2.headers[REQUEST_ID_HEADER]


@pytest.mark.asyncio
async def test_does_not_double_emit_when_handler_already_set_header():
    """If a handler explicitly sets X-Request-ID we keep its value, not append ours."""
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/sets-its-own")
    async def handler():
        from fastapi.responses import JSONResponse
        return JSONResponse({}, headers={REQUEST_ID_HEADER: "handler-pinned"})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/sets-its-own")

    # FastAPI/Starlette response headers are case-insensitive; httpx
    # surfaces them via .headers.get_list to catch double-emission.
    rid_values = r.headers.get_list(REQUEST_ID_HEADER)
    assert rid_values == ["handler-pinned"], rid_values


@pytest.mark.asyncio
async def test_contextvar_resets_after_request(client: httpx.AsyncClient):
    """ContextVar must be reset on the way out so a stray log statement
    after the response doesn't carry the wrong id into the next request."""
    async with client:
        await client.get("/echo")
    # Outside any request scope: contextvar default applies.
    assert get_request_id() is None
