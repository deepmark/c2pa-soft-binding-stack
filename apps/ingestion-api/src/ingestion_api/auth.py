"""API key guard for routes that change or expose stored content."""
from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from ingestion_api.core.config import settings


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Reject the request unless it carries the configured ``X-API-Key``.

    An empty ``API_KEY`` leaves the routes open for local development; startup logs a warning.
    """
    if settings.api_key and not (x_api_key and secrets.compare_digest(x_api_key, settings.api_key)):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key")
