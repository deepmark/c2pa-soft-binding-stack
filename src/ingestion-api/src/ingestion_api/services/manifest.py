"""
C2PA manifest builder.

Wraps ``c2pa.Builder`` for the soft-binding ingest case:

1. Open the original audio file as a stream, set ``C2paBuilderIntent.EDIT``.
   The SDK then auto-creates the ``parentOf`` ingredient from the source
   stream and wires up a ``c2pa.opened`` action that references it via
   ``ingredientIds`` — no manual JUMBF URL gymnastics required (see
   https://opensource.contentauthenticity.org/docs/c2pa-python/docs/intents).

2. We add a ``c2pa.watermarked.bound`` action and one
   ``c2pa.soft-binding`` assertion per ``SoftBindingSpec``. Multiple
   instances of the same assertion type are labelled
   ``c2pa.soft-binding``, ``c2pa.soft-binding__1``, ``c2pa.soft-binding__2``,
   etc. (per C2PA spec). Watermark specs trigger a single
   ``c2pa.watermarked.bound`` action whose
   ``parameters.relatedAssertions`` is an array of hashed JUMBF URIs
   pointing at the watermark soft-binding assertions (per C2PA actions
   v2). Fingerprint specs only emit the assertion (no action).

   The assertion shape follows the C2PA 2.4 soft binding spec:
   ``{alg, blocks: [{scope, value}]}``. Scope is empty for now
   (whole-asset binding); when temporal scoping lands, replace the
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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from c2pa import Builder, C2paBuilderIntent, Signer

from ingestion_api.core.config import settings
from ingestion_api.core.logging import get_logger
from ingestion_api.models.ingestion import SoftBindingKind

logger = get_logger(__name__)

SOFT_BINDING_LABEL = "c2pa.soft-binding"
# JUMBF URI prefix for self-references to assertions in this manifest.
# C2PA action v2 ``parameters.relatedAssertions`` requires hashed JUMBF
# URIs (``self#jumbf=c2pa.assertions/<label>``); the builder fills in
# the hash at sign time.
_ASSERTION_JUMBF_PREFIX = "self#jumbf=c2pa.assertions/"


@dataclass(slots=True, frozen=True)
class SoftBindingSpec:
    """
    One soft-binding to embed in the manifest.

    One spec -> exactly one ``c2pa.soft-binding`` assertion. Multiple
    specs -> labels are suffixed (``__1``, ``__2``, ...).

    ``related_to_watermark_action`` is a layering hook: when True, this
    spec's assertion label is listed in the ``c2pa.watermarked.bound``
    action's ``relatedAssertions``. Fingerprints leave it False — they
    don't get a watermark action.
    """
    alg: str
    kind: SoftBindingKind
    value: str
    related_to_watermark_action: bool = False
    # TODO: temporal scoping — replace whole-asset scope with
    # {"start": <samples>, "end": <samples>} when the plugin layer
    # starts emitting per-block bindings.


@dataclass(slots=True)
class BuiltManifest:
    """Result of a manifest build+sign operation."""
    output_path: Path
    manifest_bytes: bytes


def soft_binding_label(index: int) -> str:
    """JUMBF assertion label of the i-th soft-binding in the *signed*
    manifest.

    First instance is ``c2pa.soft-binding``; subsequent instances are
    suffixed ``__1``, ``__2``, ... per C2PA assertion-labelling rules.

    NB: this predicts the label the c2pa-rs Builder will assign — we
    DON'T pass these suffixed labels in on input. The Builder
    auto-suffixes duplicate assertion type labels itself; pre-suffixing
    produces double-suffixes (``__1__1``) and a hashedURI mismatch.
    Use this helper when you need to *reference* a soft-binding
    assertion (e.g. ``relatedAssertions``).
    """
    return SOFT_BINDING_LABEL if index == 0 else f"{SOFT_BINDING_LABEL}__{index}"


class ManifestBuilderService:
    """
    Build and sign a C2PA manifest for an audio file.

    Stateless — instantiate once at startup (or per-request, both fine) and
    reuse. The ``Signer`` is injected so this class doesn't reach into
    config directly; the orchestrator owns wiring.
    """

    def __init__(
        self,
        *,
        signer: Signer,
        claim_generator_name: str | None = None,
        claim_generator_version: str | None = None,
    ) -> None:
        self._signer = signer
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
        soft_bindings: Sequence[SoftBindingSpec],
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
            soft_bindings: One or more ``SoftBindingSpec`` to embed. One
                ``c2pa.soft-binding`` assertion is emitted per entry.
            title: Optional human-readable manifest title.

        Returns:
            ``BuiltManifest`` with the output path and the raw manifest bytes.
        """
        if not soft_bindings:
            raise ValueError("at least one SoftBindingSpec is required")

        manifest_def = self._manifest_definition(soft_bindings, title=title)
        algs = ",".join(s.alg for s in soft_bindings)
        logger.debug(
            "Building manifest -> %s (mime=%s algs=%s, %d input bytes)",
            dest_path, mime_type, algs, len(source_bytes),
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
        self,
        soft_bindings: Sequence[SoftBindingSpec],
        *,
        title: str | None = None,
    ) -> dict[str, Any]:
        """
        Manifest JSON passed to ``Builder.from_json``.

        We deliberately do **not** include ``c2pa.opened`` or the parent
        ingredient here — the EDIT intent injects both. We contribute the
        watermark action (if any) and one ``c2pa.soft-binding`` assertion
        per spec.
        """
        watermark_labels = [
            soft_binding_label(i)
            for i, s in enumerate(soft_bindings)
            if s.related_to_watermark_action
        ]
        watermark_algs = [
            s.alg for s in soft_bindings if s.related_to_watermark_action
        ]

        actions: list[dict[str, Any]] = []
        if watermark_labels:
            actions.append(self._watermark_action(watermark_algs, watermark_labels))

        assertions: list[dict[str, Any]] = []
        if actions:
            assertions.append({
                "label": "c2pa.actions.v2",
                "data": {"actions": actions},
            })

        for i, spec in enumerate(soft_bindings):
            assertions.append(self._soft_binding_assertion(spec, index=i))

        manifest: dict[str, Any] = {
            "claim_generator_info": [
                {
                    "name": self._claim_generator_name,
                    "version": self._claim_generator_version,
                }
            ],
            "assertions": assertions,
        }
        if title:
            manifest["title"] = title
        return manifest

    def _watermark_action(
        self, algs: Sequence[str], related_labels: Sequence[str],
    ) -> dict[str, Any]:
        # Per C2PA actions v2: relatedAssertions belongs inside
        # ``parameters`` and is an array of hashed JUMBF URIs. We supply
        # the URL only ({"url": "self#jumbf=c2pa.assertions/<label>"});
        # the Builder resolves and fills in alg/hash at sign time.
        related_uris = [
            {"url": f"{_ASSERTION_JUMBF_PREFIX}{label}"} for label in related_labels
        ]
        return {
            "action": "c2pa.watermarked.bound",
            "softwareAgent": {
                "name": self._claim_generator_name,
                "version": self._claim_generator_version,
            },
            "parameters": {
                "description": (
                    f"Bound soft-binding watermark embedded ({', '.join(algs)})"
                ),
                "relatedAssertions": related_uris,
            },
        }

    def _soft_binding_assertion(
        self, spec: SoftBindingSpec, *, index: int,
    ) -> dict[str, Any]:
        # Always pass the base type label; the Builder auto-suffixes
        # duplicates to ``c2pa.soft-binding__1``, ``__2``, ... in the
        # signed manifest. ``index`` is unused on input but kept in the
        # signature for callers that want to know the eventual JUMBF
        # label (use ``soft_binding_label(index)``).
        del index
        return {
            "label": SOFT_BINDING_LABEL,
            "data": {
                "alg": spec.alg,
                "blocks": [
                    {
                        # Whole-asset scope. Replace with
                        # {"start": <samples>, "end": <samples>} once
                        # temporal scoping is supported end-to-end.
                        "scope": {},
                        "value": spec.value,
                    }
                ],
            },
        }
