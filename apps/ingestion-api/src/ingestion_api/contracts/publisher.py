"""Resolution-api publisher input contracts."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ResolutionPushBinding:
    """One (alg, bindingValue) projection of a soft binding, in the
    shape resolution-api's ``/bindings`` endpoint expects.

    Distinct from ``models.soft_binding.SoftBindingRecord`` (the
    persisted record): we only push (alg, bindingValue) pairs to
    resolution-api. The rest of the record (kind, etc.) stays in
    ingestion-api's collection.
    """
    alg: str
    binding_value: str


@dataclass(slots=True)
class ResolutionPushRequest:
    """Request to push the manifest + bindings to resolution-api."""
    manifest_bytes: bytes
    manifest_id: str
    bindings: Sequence[ResolutionPushBinding]
