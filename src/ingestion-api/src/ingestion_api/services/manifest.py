"""
C2PA manifest builder.

Wraps ``c2pa.Builder`` for the soft-binding ingest case. Configuration
flows through the SDK's ``Context`` API (the supported replacement for
the deprecated ``load_settings`` global) so that builder + reader
defaults are declared in one place per service rather than scattered
across imperative calls and inline manifest JSON.

Pipeline:

1. ``Context`` carries our builder defaults: EDIT intent, thumbnail
   disabled (we sign audio — no thumbnail to generate), and a
   ``verify`` block that disables remote manifest fetch + OCSP fetch
   from inside the signing path. The latter closes a hidden SSRF
   surface: when ``add_ingredient`` runs over a parent stream that
   carries an ``xmpMM:Manifest`` / ``dcterms:provenance`` URL, the
   default ``fetch_remote_manifests`` would otherwise issue an
   outbound HTTP request from inside our worker. See
   https://opensource.contentauthenticity.org/docs/c2pa-python/docs/intents
   and
   https://opensource.contentauthenticity.org/docs/c2pa-python/docs/context-settings/

2. The parent ingredient is added EXPLICITLY via ``add_ingredient``
   from the *original* upload bytes. EDIT intent then wires up the
   ``c2pa.opened`` action referencing it via ``ingredientIds``. We do
   NOT rely on the auto-parent path (where EDIT derives the parent
   from the ``sign()`` source stream) because the source we sign over
   is the post-watermark byte stream — the auto-parent would otherwise
   point the ingredient at the watermarked output, producing a
   semantically circular "we opened the file we just produced" claim
   and silently dropping any prior provenance the upload carried.

   If the parent bytes already carry an embedded JUMBF manifest box,
   the SDK extracts it during ingredient construction and the prior
   provenance chain is preserved automatically — no extra work on
   our side. **Sidecar / remote-only manifests are not supported
   today** (c2pa-python 0.32.3 / c2pa-rs 0.80.0 raises
   ``Encoding: unable to encode assertion data`` whenever an
   ingredient declares a ``manifest_data`` ResourceRef per c2pa-rs
   PR #1091); callers whose uploads' provenance lives in a
   ``.c2pa`` sidecar must embed it back into the asset bytes
   client-side before uploading.

3. We add a ``c2pa.watermarked.bound`` action and one
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

4. ``Builder.sign(...)`` writes the signed asset to the output path
   (the post-watermark bytes + the new manifest box) and returns the
   raw manifest bytes, which we surface so the storage layer can
   persist them alongside the asset. The signer is passed explicitly
   to ``sign()`` (not via Context) so the long-lived ``SigningService``
   keeps ownership — explicit signers take precedence over context
   signers per the SDK's precedence rules.

5. ``Reader`` (used by ``read_active_manifest_label``) is pinned to
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

# Verify settings shared by Reader (post-sign) and Builder (during
# ingredient validation). Common rationale:
# - ``remote_manifest_fetch=False``: never issue outbound HTTP from
#   inside a signing/reading worker. SSRF surface + unbounded latency.
#   Callers that need remote provenance must pre-fetch and supply the
#   sidecar bytes via ``parent_manifest_data``.
# - ``ocsp_fetch=False``: same argument, plus a slow OCSP responder
#   can extend ingest latency by seconds.
_VERIFY_NO_NETWORK = {
    "remote_manifest_fetch": False,
    "ocsp_fetch": False,
}

# Reader context for ``read_active_manifest_label``. We just signed the
# file ourselves — there is nothing remote to validate against and
# nothing to OCSP-check. Also skip verify-after-reading: we only need
# the active_manifest URN string, not a re-validation of bytes we just
# produced.
_READER_CTX = Context.from_dict({
    "verify": {
        "verify_after_reading": False,
        **_VERIFY_NO_NETWORK,
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
    Build and sign a C2PA manifest for a media asset.

    No mutable instance state across calls; the Builder ``Context``
    stored on ``self`` is declarative config only. Safe to share a
    single instance across concurrent requests, or instantiate per
    request — either works. The ``Signer`` is injected so this class
    doesn't reach into config directly; the orchestrator owns wiring.

    Builder defaults (intent, thumbnail, verify) live in the
    per-instance ``Context`` so they're declared in one place instead
    of being split between imperative method calls and inline manifest
    JSON.
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
                # EDIT intent: SDK adds the c2pa.opened action wired to
                # the parent ingredient we add explicitly in
                # ``build_and_sign``. We don't rely on the auto-parent
                # path (deriving the parent from the sign() source
                # stream) because the source we sign over is the
                # post-watermark output — the auto-parent would point
                # the ingredient at our own output, not the upload.
                # See: https://opensource.contentauthenticity.org/docs/c2pa-python/docs/intents
                "intent": {"Edit": None},
                # Audio ingest — no thumbnail to generate. Disabling
                # also avoids the SDK trying and silently swallowing
                # the error (default ``ignore_errors=true``).
                "thumbnail": {"enabled": False},
            },
            # Disable network-touching defaults during ingredient
            # validation. add_ingredient runs the Reader internally on
            # the parent stream; with these on, a parent that carries
            # an XMP remote-manifest URL or whose certs need OCSP would
            # trigger outbound HTTP from inside the signing path.
            "verify": _VERIFY_NO_NETWORK,
        })

    def build_and_sign(
        self,
        *,
        source_bytes: bytes,
        parent_bytes: bytes,
        dest_path: Path,
        mime_type: str,
        soft_bindings: Sequence[SoftBindingSpec],
        title: str | None = None,
    ) -> BuiltManifest:
        """
        Build the manifest definition and sign.

        The source / parent bytes are fed through in-memory streams —
        we never persist the raw upload or any watermarked
        intermediate. Only the signed output is written, to
        ``dest_path``.

        Args:
            source_bytes: Post-watermark media bytes that will become
                the signed output's payload. These are what get hashed
                into the new manifest's data hash assertion.
            parent_bytes: ORIGINAL upload bytes. Added explicitly as a
                ``parentOf`` ingredient so the new manifest's
                ``c2pa.opened`` action / provenance chain points at
                the actual source the user gave us, not at our own
                post-watermark output. If the parent bytes carry an
                embedded C2PA manifest, the SDK pulls it into the
                ingredient automatically and the prior chain is
                preserved. Sidecar / remote-only manifests are NOT
                supported today (see module docstring) — callers must
                pre-embed before uploading.
            dest_path: Where to write the signed asset.
            mime_type: e.g. ``audio/wav``. Applies to both the source
                and parent streams (we don't currently support cases
                where the watermark plugin transcodes to a different
                MIME — the orchestrator rejects that earlier).
            soft_bindings: One or more ``SoftBindingSpec`` to embed.
                One ``c2pa.soft-binding`` assertion is emitted per
                entry.
            title: Optional human-readable manifest title. Also used
                as the parent ingredient title when present.

        Returns:
            ``BuiltManifest`` with the output path and the raw
            manifest bytes.
        """
        if not soft_bindings:
            raise ValueError("at least one SoftBindingSpec is required")
        if not parent_bytes:
            raise ValueError("parent_bytes is required")

        manifest_def = self._manifest_definition(soft_bindings, title=title)
        algs = ",".join(s.alg for s in soft_bindings)
        logger.debug(
            "Building manifest -> %s (mime=%s algs=%s, %d source / %d parent bytes)",
            dest_path, mime_type, algs, len(source_bytes), len(parent_bytes),
        )

        with Builder.from_json(manifest_def, context=self._builder_ctx) as builder:
            self._add_parent_ingredient(
                builder, parent_bytes=parent_bytes, mime_type=mime_type, title=title,
            )
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

    def _add_parent_ingredient(
        self,
        builder: Builder,
        *,
        parent_bytes: bytes,
        mime_type: str,
        title: str | None,
    ) -> None:
        """Attach the ``parentOf`` ingredient to the builder.

        Plain ``add_ingredient`` over the parent stream. If the parent
        bytes already contain a JUMBF box, the SDK extracts it and
        chains the prior provenance into the new manifest
        automatically. Once the ingredient is on the builder, EDIT
        intent inserts the matching ``c2pa.opened`` action with
        ``parameters.ingredientIds`` wired up at sign time.
        """
        ingredient_json: dict[str, Any] = {
            "title": title or "source",
            "relationship": "parentOf",
            "format": mime_type,
        }
        with io.BytesIO(parent_bytes) as parent_src:
            builder.add_ingredient(ingredient_json, mime_type, parent_src)

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
