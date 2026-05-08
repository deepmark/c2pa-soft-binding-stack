"""Resolution-api publisher input contracts."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class BindingPair:
    """One (alg, bindingValue) tuple to register against the manifest."""
    alg: str
    binding_value: str


@dataclass(slots=True)
class ResolutionPushRequest:
    manifest_bytes: bytes
    manifest_id: str
    bindings: Sequence[BindingPair]
