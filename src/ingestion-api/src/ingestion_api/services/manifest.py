"""
C2PA manifest builder.

Wraps ``c2pa.Builder`` for the soft-binding ingest case:

1. Open the original audio file as a stream, set ``C2paBuilderIntent.EDIT``.
   The SDK then auto-creates the ``parentOf`` ingredient from the source
   stream and wires up a ``c2pa.opened`` action that references it via
   ``ingredientIds`` — no manual JUMBF URL gymnastics required (see
   https://opensource.contentauthenticity.org/docs/c2pa-python/docs/intents).

2. We add a ``c2pa.watermarked.bound`` action and a ``c2pa.soft-binding``
   assertion ourselves. The assertion shape follows the C2PA 2.4 soft
   binding spec: ``{alg, blocks: [{scope, value}]}``. Scope is empty for
   now (whole-asset binding); when temporal scoping lands, replace the
   ``scope`` dict with ``{start, end}`` in samples.

3. ``Builder.sign(...)`` writes the signed asset to the output path and
   returns the raw manifest bytes, which we surface so the storage layer
   can persist them alongside the asset.

The watermark itself is *not* an ingredient — it's an action + an
assertion on the new asset. That's the whole point of
``c2pa.watermarked.bound``: the bound watermark is part of the new
content's provenance, not a referenced input.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from c2pa import Builder, C2paBuilderIntent, Signer

from ingestion_api.core.config import settings
from ingestion_api.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class BuiltManifest:
    """Result of a manifest build+sign operation."""
    output_path: Path
    manifest_bytes: bytes


class ManifestBuilderService:
    """
    Build and sign a C2PA manifest for an audio file.

    Stateless — instantiate once at startup (or per-request, both fine) and
    reuse. The ``Signer`` and the algorithm identifier are injected so this
    class doesn't reach into config directly; the orchestrator owns wiring.
    """

    def __init__(
        self,
        *,
        signer: Signer,
        soft_binding_alg: str,
        claim_generator_name: str | None = None,
        claim_generator_version: str | None = None,
    ) -> None:
        self._signer = signer
        self._alg = soft_binding_alg
        self._claim_generator_name = claim_generator_name or settings.claim_generator_name
        self._claim_generator_version = (
            claim_generator_version or settings.claim_generator_version
        )

    def build_and_sign(
        self,
        *,
        source_bytes: bytes,
        dest_path: Path,
        mime_type: str,
        binding_value_b64: str,
        title: str | None = None,
    ) -> BuiltManifest:
        """
        Build the manifest definition, set EDIT intent, sign.

        The source audio is fed through an in-memory stream — we never
        persist the raw upload or any (dummy) watermarked intermediate.
        Only the signed output is written, to ``dest_path``.

        Args:
            source_bytes: Watermarked audio bytes that will become the
                signed output's payload. The EDIT intent uses the same
                stream to auto-create the ``parentOf`` ingredient.
            dest_path: Where to write the signed audio.
            mime_type: e.g. ``audio/wav``.
            binding_value_b64: Base64-encoded soft-binding value.
            title: Optional human-readable manifest title.

        Returns:
            ``BuiltManifest`` with the output path and the raw manifest bytes.
        """
        manifest_def = self._manifest_definition(binding_value_b64, title=title)
        logger.debug(
            "Building manifest -> %s (mime=%s alg=%s, %d input bytes)",
            dest_path, mime_type, self._alg, len(source_bytes),
        )

        with Builder.from_json(manifest_def) as builder:
            # EDIT intent: SDK adds c2pa.opened + parentOf ingredient + the
            # right ingredientIds linkage from the source stream. See:
            # https://opensource.contentauthenticity.org/docs/c2pa-python/docs/intents
            builder.set_intent(C2paBuilderIntent.EDIT)
            with io.BytesIO(source_bytes) as src, open(dest_path, "w+b") as dst:
                manifest_bytes = builder.sign(self._signer, mime_type, src, dst)

        logger.info(
            "Signed manifest %s (manifest_bytes=%d)",
            dest_path, len(manifest_bytes),
        )
        return BuiltManifest(output_path=dest_path, manifest_bytes=manifest_bytes)

    def _manifest_definition(
        self, binding_value_b64: str, *, title: str | None = None,
    ) -> dict[str, Any]:
        """
        Manifest JSON passed to ``Builder.from_json``.

        We deliberately do **not** include ``c2pa.opened`` or the parent
        ingredient here — the EDIT intent injects both. We only contribute
        the watermark action and the soft-binding assertion.
        """
        actions_assertion = {
            "label": "c2pa.actions.v2",
            "data": {
                "actions": [
                    {
                        "action": "c2pa.watermarked.bound",
                        "softwareAgent": {
                            "name": self._claim_generator_name,
                            "version": self._claim_generator_version,
                        },
                        "parameters": {
                            "description": (
                                "Bound soft-binding watermark embedded "
                                f"({self._alg})"
                            ),
                        },
                    }
                ]
            },
        }

        soft_binding_assertion = {
            "label": "c2pa.soft-binding",
            "data": {
                "alg": self._alg,
                "blocks": [
                    {
                        # Whole-asset scope. When temporal scoping is added,
                        # replace this with {"start": <samples>, "end": <samples>}.
                        "scope": {},
                        "value": binding_value_b64,
                    }
                ],
            },
        }

        manifest: dict[str, Any] = {
            "claim_generator_info": [
                {
                    "name": self._claim_generator_name,
                    "version": self._claim_generator_version,
                }
            ],
            "assertions": [actions_assertion, soft_binding_assertion],
        }
        if title:
            manifest["title"] = title
        return manifest
