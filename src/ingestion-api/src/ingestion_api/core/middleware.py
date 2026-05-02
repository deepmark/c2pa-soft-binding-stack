"""
Custom ASGI middleware for ingestion-api.

Implemented as raw ASGI (not BaseHTTPMiddleware) so it sits cleanly
above FastAPI without the body-buffering / exception-swallowing quirks
of Starlette's base middleware.

- ``RequestIDMiddleware`` — generates an ``X-Request-ID`` (or echoes the caller's),
  binds it to a ContextVar (so logs are stamped via ``_RequestIDFilter``),
  exposes it on ``request.state.request_id`` for routes/deps, and adds it
  to the response headers. Downstream HTTP callouts (plugin containers,
  resolution-api) read the contextvar via
  ``core.logging.get_request_id()`` and propagate it as their own
  ``X-Request-ID`` header so a single ingest can be traced end-to-end.
* Without it, correlating a failure in the plugin or resolution-api back
  to the originating ingest is next to impossible.
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ingestion_api.core.logging import (
    REQUEST_ID_HEADER,
    reset_request_id,
    set_request_id,
)

ASGIScope = dict[str, Any]
ASGIMessage = dict[str, Any]
ASGIReceive = Callable[[], Awaitable[ASGIMessage]]
ASGISend = Callable[[ASGIMessage], Awaitable[None]]
ASGIApp = Callable[[ASGIScope, ASGIReceive, ASGISend], Awaitable[None]]

_REQUEST_ID_HEADER_LOWER = REQUEST_ID_HEADER.lower().encode("latin-1")


class RequestIDMiddleware:
    """Generate / echo X-Request-ID, bind to a contextvar, expose on response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend,
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        rid = self._extract_or_generate(scope)
        # Starlette/FastAPI lazy-init scope["state"] when ``request.state`` is first accessed; 
        # pre-populate it so dependencies can read ``request.state.request_id`` without their own contextvar plumbing.
        state = scope.setdefault("state", {})
        state["request_id"] = rid

        token = set_request_id(rid)

        async def send_with_request_id(message: ASGIMessage) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                # Don't double-emit if the app already set the header.
                if not any(h[0].lower() == _REQUEST_ID_HEADER_LOWER for h in headers):
                    headers.append(
                        (_REQUEST_ID_HEADER_LOWER, rid.encode("latin-1")),
                    )
                    message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            reset_request_id(token)

    @staticmethod
    def _extract_or_generate(scope: ASGIScope) -> str:
        for name, value in scope.get("headers") or []:
            if name.lower() == _REQUEST_ID_HEADER_LOWER:
                try:
                    decoded = value.decode("latin-1").strip()
                except UnicodeDecodeError:
                    decoded = ""
                if decoded:
                    # Cap to 128 chars to defang clients sending unbounded ids that would end up in our logs verbatim.
                    return decoded[:128]
        return uuid.uuid4().hex
