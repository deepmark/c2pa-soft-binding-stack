"""
Tests for /health, /ready, /health/deep.

Strategy:
- Build a minimal FastAPI app that mounts only the health router.
- Stub ``app.state.signing_service`` with a tiny double exposing the
  attributes the router actually reads (``is_loaded`` + ``credentials``).
- Patch ``MongoDB.client`` to simulate a working / unreachable mongo.
- Patch ``httpx.AsyncClient`` (in the health router's namespace) to
  return a client backed by ``MockTransport`` so plugin + resolution
  probes don't go anywhere real.
- Fake ``algorithms.yaml`` via ``settings.algorithms_catalog_path`` so
  ``load_catalog()`` returns a deterministic plugin list.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from textwrap import dedent
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from ingestion_api.config import settings
from ingestion_api.repositories.database import MongoDB
from ingestion_api.routers import health as health_module
from ingestion_api.routers.health import router as health_router


class _StubCredentials:
    """Just enough surface for ``_cert_info`` to read."""
    def __init__(self, not_after: datetime | None = None) -> None:
        self._not_after = not_after

    def leaf_not_after(self) -> datetime | None:
        return self._not_after


class _StubSigningService:
    """Just enough surface for ``_signing_loaded`` + ``_cert_info`` to read."""
    def __init__(
        self,
        *,
        is_loaded: bool = True,
        not_after: datetime | None = None,
    ) -> None:
        self.is_loaded = is_loaded
        self.credentials = _StubCredentials(not_after=not_after)


def _build_app(signing_service: _StubSigningService | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(health_router)
    if signing_service is not None:
        app.state.signing_service = signing_service
    return app


def _patch_mongo_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend MongoDB is connected and the ping succeeds."""
    fake_client = MagicMock()
    fake_client.admin.command = AsyncMock(return_value={"ok": 1})
    monkeypatch.setattr(MongoDB, "client", fake_client)


def _patch_mongo_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend MongoDB connect was never called."""
    monkeypatch.setattr(MongoDB, "client", None)


def _patch_mongo_ping_raises(
    monkeypatch: pytest.MonkeyPatch, exc: Exception,
) -> None:
    fake_client = MagicMock()
    fake_client.admin.command = AsyncMock(side_effect=exc)
    monkeypatch.setattr(MongoDB, "client", fake_client)


def _patch_credentials_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path, *, present: bool = True,
) -> None:
    """Re-point ``credentials_dir`` at a tmp dir; optionally write fixture files."""
    creds_dir = tmp_path / "credentials"
    creds_dir.mkdir(parents=True, exist_ok=True)
    if present:
        # File names follow the SIGNING_ALG-derived convention.
        (creds_dir / f"{settings.signing_alg.lower()}_certs.pem").write_text("fake")
        (creds_dir / f"{settings.signing_alg.lower()}_private.key").write_text("fake")
    monkeypatch.setattr(settings, "credentials_dir", creds_dir)


def _patch_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path, urls: list[str]) -> None:
    """Write a deterministic algorithms.yaml and re-point the catalog path."""
    p = tmp_path / "algorithms.yaml"
    entries = "\n".join(
        dedent(f"""\
            - alg: alg.{i}
              type: watermark
              bindingBits: 128
              mediaTypes: ["audio/wav"]
              url: {url}
        """)
        for i, url in enumerate(urls)
    )
    p.write_text(f"algorithms:\n{entries}")
    monkeypatch.setattr(settings, "algorithms_catalog_path", p)


def _patch_async_client(
    monkeypatch: pytest.MonkeyPatch, handler,
) -> None:
    """Inject a MockTransport into ``httpx.AsyncClient`` *only* when no
    transport was supplied by the caller.

    The outer test client constructs
    ``httpx.AsyncClient(transport=ASGITransport(app=app))`` after this
    patch is in place — it must keep its ASGITransport to reach the
    FastAPI app. The inner client created inside ``health_deep`` passes
    no transport, so it gets our mock.
    """
    mock_transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def _factory(*args, **kwargs):
        if kwargs.get("transport") is not None:
            return real_cls(*args, **kwargs)
        return real_cls(*args, transport=mock_transport, **kwargs)

    monkeypatch.setattr(health_module.httpx, "AsyncClient", _factory)


# ---------------------------------------------------------------------------
# /health (liveness)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_always_ok():
    """Liveness never reaches outside the process — always 200."""
    app = _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"]
    assert body["version"]


# ---------------------------------------------------------------------------
# /ready (readiness)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ready_ok_when_everything_healthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    _patch_mongo_ok(monkeypatch)
    _patch_credentials_present(monkeypatch, tmp_path, present=True)
    app = _build_app(_StubSigningService(is_loaded=True))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/ready")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["mongodb"]["ok"] is True
    assert body["credentials"]["ok"] is True
    assert body["signing"]["loaded"] is True


@pytest.mark.asyncio
async def test_ready_503_when_mongo_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    _patch_mongo_down(monkeypatch)
    _patch_credentials_present(monkeypatch, tmp_path, present=True)
    app = _build_app(_StubSigningService(is_loaded=True))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/ready")

    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["mongodb"]["ok"] is False
    assert body["credentials"]["ok"] is True
    assert body["signing"]["loaded"] is True


@pytest.mark.asyncio
async def test_ready_503_when_signing_not_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    _patch_mongo_ok(monkeypatch)
    _patch_credentials_present(monkeypatch, tmp_path, present=True)
    app = _build_app(_StubSigningService(is_loaded=False))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/ready")

    assert r.status_code == 503
    body = r.json()
    assert body["signing"]["loaded"] is False


@pytest.mark.asyncio
async def test_ready_503_when_credentials_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    _patch_mongo_ok(monkeypatch)
    _patch_credentials_present(monkeypatch, tmp_path, present=False)
    app = _build_app(_StubSigningService(is_loaded=True))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/ready")

    assert r.status_code == 503
    assert r.json()["credentials"]["ok"] is False


@pytest.mark.asyncio
async def test_ready_503_when_mongo_ping_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    _patch_mongo_ping_raises(monkeypatch, RuntimeError("connection refused"))
    _patch_credentials_present(monkeypatch, tmp_path, present=True)
    app = _build_app(_StubSigningService(is_loaded=True))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/ready")

    assert r.status_code == 503
    assert "connection refused" in r.json()["mongodb"]["error"]


# ---------------------------------------------------------------------------
# /health/deep (full fan-out)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deep_ok_when_plugins_and_resolution_healthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    _patch_mongo_ok(monkeypatch)
    _patch_credentials_present(monkeypatch, tmp_path, present=True)
    _patch_catalog(monkeypatch, tmp_path, ["http://plugin-a:8000", "http://plugin-b:8000"])
    monkeypatch.setattr(settings, "resolution_push_enabled", True)
    monkeypatch.setattr(settings, "resolution_api_url", "http://resolution:8001")

    def handler(request: httpx.Request) -> httpx.Response:
        # Every probed /health returns 200 — fan-out is healthy.
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    _patch_async_client(monkeypatch, handler)

    not_after = datetime.now(timezone.utc) + timedelta(days=42)
    app = _build_app(_StubSigningService(is_loaded=True, not_after=not_after))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/health/deep")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    plugins = {p["alg"]: p for p in body["plugins"]}
    assert plugins["alg.0"]["ok"] is True
    assert plugins["alg.1"]["ok"] is True
    assert body["resolution_api"]["status"] == "ok"
    # Cert expiry surfaced as forensic data, not a gate.
    # `expires_in_days` is computed from `(not_after - now).total_seconds() // 86400`,
    # which truncates — anything between 41 and 42 satisfies "set 42 days out".
    assert body["credentials"]["expires_in_days"] in (41, 42)
    assert body["credentials"]["expired"] is False


@pytest.mark.asyncio
async def test_deep_503_when_a_plugin_is_unhealthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    _patch_mongo_ok(monkeypatch)
    _patch_credentials_present(monkeypatch, tmp_path, present=True)
    _patch_catalog(monkeypatch, tmp_path, ["http://plugin-a:8000", "http://plugin-b:8000"])
    monkeypatch.setattr(settings, "resolution_push_enabled", False)
    monkeypatch.setattr(settings, "resolution_api_url", "")

    def handler(request: httpx.Request) -> httpx.Response:
        # plugin-a healthy, plugin-b 500.
        if "plugin-a" in str(request.url):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(500, text="kaboom")

    _patch_async_client(monkeypatch, handler)

    app = _build_app(_StubSigningService(is_loaded=True))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/health/deep")

    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    by_alg = {p["alg"]: p for p in body["plugins"]}
    assert by_alg["alg.0"]["ok"] is True
    assert by_alg["alg.1"]["ok"] is False
    assert "kaboom" in by_alg["alg.1"]["error"] or by_alg["alg.1"]["error"]
    # Resolution-api skipped because push is disabled — not gating overall.
    assert body["resolution_api"]["status"] == "skipped"


@pytest.mark.asyncio
async def test_deep_resolution_skipped_does_not_gate_overall(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    _patch_mongo_ok(monkeypatch)
    _patch_credentials_present(monkeypatch, tmp_path, present=True)
    _patch_catalog(monkeypatch, tmp_path, [])  # no plugins
    monkeypatch.setattr(settings, "resolution_push_enabled", False)
    monkeypatch.setattr(settings, "resolution_api_url", "")

    _patch_async_client(monkeypatch, lambda r: httpx.Response(404))

    app = _build_app(_StubSigningService(is_loaded=True))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/health/deep")

    assert r.status_code == 200
    body = r.json()
    assert body["resolution_api"]["status"] == "skipped"
    assert body["resolution_api"]["push_enabled"] is False
    assert body["catalog"]["entries"] == 0


@pytest.mark.asyncio
async def test_deep_503_when_resolution_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    _patch_mongo_ok(monkeypatch)
    _patch_credentials_present(monkeypatch, tmp_path, present=True)
    _patch_catalog(monkeypatch, tmp_path, [])
    monkeypatch.setattr(settings, "resolution_push_enabled", True)
    monkeypatch.setattr(settings, "resolution_api_url", "http://resolution:8001")

    _patch_async_client(monkeypatch, lambda r: httpx.Response(500))

    app = _build_app(_StubSigningService(is_loaded=True))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/health/deep")

    assert r.status_code == 503
    body = r.json()
    assert body["resolution_api"]["status"] == "failed"


@pytest.mark.asyncio
async def test_deep_surfaces_expired_cert_without_failing(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    """Cert expiry is informational — never gates readiness even when expired."""
    _patch_mongo_ok(monkeypatch)
    _patch_credentials_present(monkeypatch, tmp_path, present=True)
    _patch_catalog(monkeypatch, tmp_path, [])
    monkeypatch.setattr(settings, "resolution_push_enabled", False)
    monkeypatch.setattr(settings, "resolution_api_url", "")
    _patch_async_client(monkeypatch, lambda r: httpx.Response(404))

    not_after = datetime.now(timezone.utc) - timedelta(days=5)  # expired
    app = _build_app(_StubSigningService(is_loaded=True, not_after=not_after))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/health/deep")

    assert r.status_code == 200  # NOT 503 — expiry is forensic, not a gate
    body = r.json()
    assert body["credentials"]["expired"] is True
    assert body["credentials"]["expires_in_days"] == 0


@pytest.mark.asyncio
async def test_deep_handles_missing_signing_service_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
):
    """Boot-time race: app.state.signing_service not yet set."""
    _patch_mongo_ok(monkeypatch)
    _patch_credentials_present(monkeypatch, tmp_path, present=True)
    _patch_catalog(monkeypatch, tmp_path, [])
    monkeypatch.setattr(settings, "resolution_push_enabled", False)
    monkeypatch.setattr(settings, "resolution_api_url", "")
    _patch_async_client(monkeypatch, lambda r: httpx.Response(404))

    app = _build_app(signing_service=None)  # nothing on app.state

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/health/deep")

    assert r.status_code == 503
    body: dict[str, Any] = r.json()
    assert body["signing"]["loaded"] is False
    # _cert_info returns None-tuple safely:
    assert body["credentials"]["not_after"] is None
