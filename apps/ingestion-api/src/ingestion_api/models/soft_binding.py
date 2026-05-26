"""
Soft-binding records — discriminated union on ``kind``.

Today both kinds carry identical fields; the split exists so future
divergence (e.g. fingerprint confidence, watermark embedding strength)
is a one-class change rather than a breaking schema migration.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from ingestion_api.models.enums import SoftBindingKind


class _SoftBindingBase(BaseModel):
    alg: str = Field(..., description="Soft-binding algorithm identifier")
    bindingValue: str = Field(
        ...,
        description=(
            "Encoded binding value (string; encoding is plugin-specific — "
            "e.g. base64 for aware-20)"
        ),
    )


class WatermarkSoftBindingRecord(_SoftBindingBase):
    """Watermark binding: bytes were modified during /embed."""
    kind: Literal["watermark"] = "watermark"


class FingerprintSoftBindingRecord(_SoftBindingBase):
    """Fingerprint binding: pure read on the asset, no mutation."""
    kind: Literal["fingerprint"] = "fingerprint"


SoftBindingRecord = Annotated[
    WatermarkSoftBindingRecord | FingerprintSoftBindingRecord,
    Field(discriminator="kind"),
]


def make_soft_binding(
    *, alg: str, kind: SoftBindingKind, bindingValue: str,
) -> _SoftBindingBase:
    """Construct the right soft-binding subclass for a runtime ``kind``.

    Construction sites can't use the ``SoftBindingRecord`` alias directly
    (it's a typing union, not a class), so this factory keeps callers
    from having to repeat the if/else.
    """
    if kind == "watermark":
        return WatermarkSoftBindingRecord(alg=alg, bindingValue=bindingValue)
    if kind == "fingerprint":
        return FingerprintSoftBindingRecord(alg=alg, bindingValue=bindingValue)
    raise ValueError(f"Unknown soft-binding kind: {kind!r}")
