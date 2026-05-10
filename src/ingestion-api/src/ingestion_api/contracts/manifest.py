"""Manifest builder input contracts."""
from __future__ import annotations

from dataclasses import dataclass

from ingestion_api.models.enums import SoftBindingKind


@dataclass(slots=True, frozen=True)
class SoftBindingSpec:
    """One soft-binding to embed in the manifest.

    One spec -> exactly one ``c2pa.soft-binding`` assertion. Multiple
    specs -> labels are suffixed (``__1``, ``__2``, ...).

    ``related_to_watermark_action`` is a layering hook: when True, this
    spec's assertion label is listed in the ``c2pa.watermarked.bound``
    action's ``relatedAssertions``. 
    For fingerprints leave this as False. They don't get a c2pa.watermarked.bound action.
    """
    alg: str
    kind: SoftBindingKind
    value: str
    related_to_watermark_action: bool = False
    # TODO: temporal scoping — replace whole-asset scope with
    # {"start": <samples>, "end": <samples>} when the plugin layer
    # starts emitting per-block bindings.
