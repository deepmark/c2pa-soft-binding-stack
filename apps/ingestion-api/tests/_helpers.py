"""Shared test-only helpers.

Lives next to ``conftest.py`` so pytest's rootdir machinery puts it
on ``sys.path`` for free — import as ``from _helpers import ...``.
"""
from __future__ import annotations

import base64
import hashlib


def stub_binding_value(b: bytes) -> str:
    """Deterministic non-secret stand-in for a binding value, for stubs.

    Tests only care that ``same bytes -> same string`` so they can
    assert round-trip equality through the pipeline. The real
    watermark plugin mints its value with ``secrets`` (random) and
    echoes whatever the caller hands it; this helper has nothing to
    do with that — it's just a stable function over bytes.
    """
    return base64.b64encode(hashlib.sha256(b).digest()[:16]).decode("ascii")
