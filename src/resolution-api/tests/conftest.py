"""Shared pytest fixtures + env bootstrap for resolution-api tests."""
from __future__ import annotations

import os
from pathlib import Path

# Required path settings must be in the env BEFORE the resolution_api
# package imports (Settings() runs at module import). Point at the dev
# checkout's repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("PLUGINS_CATALOG_PATH", str(_REPO_ROOT / "plugins.yaml"))
