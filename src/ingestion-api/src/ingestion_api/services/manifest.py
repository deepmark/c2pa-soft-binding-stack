"""
C2PA manifest builder.

Wraps ``c2pa.Builder`` for the soft-binding ingest case. Configuration
flows through the SDK's ``Context`` API (the supported replacement for
the deprecated ``load_settings`` global) so that builder + reader
defaults are declared in one place per service rather than scattered
across imperative calls and inline manifest JSON.

Pipeline:

1. ``Context`` carries our builder defaults: EDIT intent, claim
   generator info, thumbnail disabled (we sign audio — no thumbnail to
   generate). With EDIT intent the SDK auto-creates the ``parentOf``
   ingredient from the source stream and wires up a ``c2pa.opened``
   action that references it via ``ingredientIds`` — no manual JUMBF
   URL gymnastics required (see
   https://opensource.contentauthenticity.org/docs/c2pa-python/docs/intents
   and
   https://opensource.contentauthenticity.org/docs/c2pa-python/docs/context-settings/).

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
   can persist them alongside the asset. The signer is passed
   explicitly to ``sign()`` (not via Context) so the long-lived
   ``SigningService`` keeps ownership — explicit signers take
   precedence over context signers per the SDK's precedence rules.

4. ``Reader`` (used by ``read_active_manifest_label``) is pinned to
   ``_READER_CTX``: no remote manifest fetch, no OCSP fetch, no
   verification on read. We just signed the file ourselves and only
   want the active_manifest URN — any network roundtrip here is wasted
   at best, a latency hazard at worst.

The watermark itself is *not* an ingredient — it's an action + an
assertion on the new asset. That's the whole point of
``c2pa.watermarked.bound``: the bound watermark is part of the new
content's provenance, not a referenced input.
"""
from __future__ import annotations

import io
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from c2pa import Builder, Context, Reader, Signer

from ingestion_api.core.config import settings
from ingestion_api.contracts.manifest import SoftBindingSpec
from ingestion_api.core.logging import get_logger

logger = get_logger(__name__)

SOFT_BINDING_LABEL = "c2pa.soft-binding"
# JUMBF URI prefix for self-references to assertions in this manifest.
# C2PA action v2 ``parameters.relatedAssertions`` requires hashed JUMBF
# URIs (``self#jumbf=c2pa.assertions/<label>``); the builder fills in
# the hash at sign time.
_ASSERTION_JUMBF_PREFIX = "self#jumbf=c2pa.assertions/"

# Reader context for ``read_active_manifest_label``. We just signed the
# file ourselves — there is nothing remote to validate against and
# nothing to OCSP-check. Disable network-touching defaults so a slow
# OCSP responder can't extend ingest latency by seconds. Also skip
# verify-after-reading: we only need the active_manifest URN string,
# not a re-validation of bytes we just produced.
_READER_CTX = Context.from_dict({
    "verify": {
        "verify_after_reading": False,
        "remote_manifest_fetch": False,
        "ocsp_fetch": False,
    },
})


@dataclass(slots=True)
class BuiltManifest:
    """Result of a manifest build+sign operation."""
    output_path: Path
    manifest_bytes: bytes


def read_active_manifest_label(signed_path: Path) -> str | None:
    """Extract the ``active_manifest`` URN from a freshly-signed asset.

    Drives ``IngestionRecord.manifestId`` and the foreign-key handed to
    resolution-api. Returns None on any reader failure (logged at debug)
    so the orchestrator can decide whether the missing label is fatal —
    ingestion-api treats it as a hard pipeline failure today, but a
    background reconciler that re-reads existing files would want to
    distinguish "missing" from "raise."
    """
    try:
        with Reader(str(signed_path), context=_READER_CTX) as reader:
            data = json.loads(reader.json())
            return data.get("active_manifest")
    except Exception:
        logger.debug(
            "Could not read active manifest label from %s", signed_path, exc_info=True,
        )
        return None


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

    Stateless aside from the Builder ``Context`` (which is just config).
    Instantiate once at startup (or per-request, both fine) and reuse.
    The ``Signer`` is injected so this class doesn't reach into config
    directly; the orchestrator owns wiring.

    Builder defaults (intent, claim generator info, thumbnail) live in
    the per-instance ``Context`` so they're declared in one place
    instead of split between an imperative ``set_intent`` call and an
    inline ``claim_generator_info`` field on the manifest JSON.
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
        # NOTE: ``claim_generator_info`` deliberately stays in the
        # per-manifest JSON (see ``_manifest_definition``), NOT on the
        # Context. In c2pa-python 0.32.3 the Context-set value isn't
        # propagated into the signed manifest — only the SDK's own
        # ``c2pa-rs`` library marker shows up. Re-evaluate when bumping
        # the SDK; if Context starts honoring it, move the field here
        # so all builder defaults live in one place.
        self._builder_ctx = Context.from_dict({
            "builder": {
                # EDIT intent: SDK adds c2pa.opened + parentOf ingredient
                # + the right ingredientIds linkage from the source stream.
                # See: https://opensource.contentauthenticity.org/docs/c2pa-python/docs/intents
                "intent": {"Edit": None},
                # Audio ingest — no thumbnail to generate. Disabling
                # also avoids the SDK trying and silently swallowing the
                # error (default ``ignore_errors=true``).
                "thumbnail": {"enabled": False},
            },
        })

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
        Build the manifest definition and sign.

        The source bytes are fed through an in-memory stream — we never
        persist the raw upload or any (dummy) watermarked intermediate.
        Only the signed output is written, to ``dest_path``.

        Args:
            source_bytes: Watermarked media bytes that will become the
                signed output's payload. The EDIT intent uses the same
                stream to auto-create the ``parentOf`` ingredient.
            dest_path: Where to write the signed asset.
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

        with Builder.from_json(manifest_def, context=self._builder_ctx) as builder:
            with io.BytesIO(source_bytes) as src, open(dest_path, "w+b") as dst:
                # Explicit signer takes precedence over any context
                # signer (per SDK precedence rules); we keep ownership
                # of the long-lived Signer in SigningService.
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

        Carries:
        - per-ingest content (the watermark action + soft-binding
          assertions);
        - ``claim_generator_info`` — kept here rather than on the
          Context because the Context-set value isn't propagated into
          the signed manifest in c2pa-python 0.32.3 (see __init__ note).

        We deliberately do **not** include ``c2pa.opened`` or the
        parent ingredient: the EDIT intent (declared on the Context)
        injects both.
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
