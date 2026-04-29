"""Smoke tests that don't require MongoDB — verify the app boots and exposes
the routes the C2PA Decoupled spec defines, plus health endpoints."""
from resolution_api.__main__ import app


def _registered_paths() -> set[str]:
    return {r.path for r in app.routes if hasattr(r, "methods")}


def test_query_routes_registered():
    paths = _registered_paths()
    assert "/matches/byBinding" in paths
    assert "/matches/byContent" in paths
    assert "/matches/byReference" in paths


def test_store_routes_registered():
    paths = _registered_paths()
    assert "/manifests" in paths
    assert "/manifests/{manifestId}" in paths
    assert "/bindings" in paths


def test_fetch_routes_registered():
    paths = _registered_paths()
    assert "/manifests/{manifestId}/receipts" in paths


def test_service_routes_registered():
    paths = _registered_paths()
    assert "/services/supportedAlgorithms" in paths


def test_health_routes_registered():
    paths = _registered_paths()
    assert "/health" in paths
    assert "/ready" in paths


def test_ingest_routes_not_registered():
    """Ingest moved to ingestion-api; this service must not expose it."""
    paths = _registered_paths()
    assert "/ingest" not in paths
