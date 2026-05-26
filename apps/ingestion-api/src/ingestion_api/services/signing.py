"""
C2PA signer service.

Builds the ``c2pa.Signer`` once at app startup (eager-validated so a
missing-cert deploy fails fast instead of throwing 503s on first
ingest), then transfers ownership to ``ManifestBuilderService``'s
Context via ``release_signer()`` — the SDK's preferred pattern. 
After release this service is a thin wrapper around ``SignerCredentials``: 
it still surfaces the cert metadata that the ``IngestionRecord`` and 
``/health/deep`` need (``cert_sha1``, ``signing_alg``, ``ta_url``, ``leaf_not_after``),
but the FFI handle for the Signer lives in the manifest builder for
the rest of the process lifetime.

Implementation note: the C2PA Python SDK's ``Signer.from_info`` is broken
for ES256 in 0.32.3 (returns ``Signature: empty string``). The official
example (``c2pa-python/examples/sign.py``) instead drives signing via
``Signer.from_callback`` with the ``cryptography`` library doing the
actual ECDSA / RSA-PSS work.

The cert chain (PEM) is passed to the SDK as a string. The private key
(PEM) stays in Python; ``cryptography.serialization.load_pem_private_key``
parses it once at startup, and a per-algorithm signer callback signs each
chunk of bytes the SDK hands us.

Supported signing algorithms (mirrors ``c2pa.C2paSigningAlg``):
- ES256 / ES384 / ES512 (ECDSA)
- PS256 / PS384 / PS512 (RSA-PSS)
- ED25519

Credentials (cert/key paths, fingerprint helpers, expiry) live in
``ingestion_api.core.credentials`` so consumers that only need
forensic data (``/health/deep``, the ``IngestionRecord`` builder)
don't have to import this module and drag the c2pa SDK with them.

If you want to use a remote KMS / HSM later, swap ``_make_callback`` for a
function that calls out to your service instead of using ``cryptography``.
"""
from __future__ import annotations

from collections.abc import Callable

from c2pa import C2paSigningAlg, Signer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from ingestion_api.core.logging import get_logger
from ingestion_api.core.credentials import (
    MissingSigningMaterialError,
    SignerCredentials,
)

logger = get_logger(__name__)


def _signing_alg(name: str) -> C2paSigningAlg:
    name_norm = name.strip().upper()
    try:
        return getattr(C2paSigningAlg, name_norm)
    except AttributeError as exc:
        valid = [a.name for a in C2paSigningAlg]
        raise ValueError(
            f"Unsupported signing alg {name!r}. Valid options: {valid}"
        ) from exc


def _make_callback(alg: C2paSigningAlg, key_pem: bytes) -> Callable[[bytes], bytes]:
    """Build the ``bytes -> bytes`` callback the SDK expects."""
    private_key = serialization.load_pem_private_key(key_pem, password=None)

    if alg in (C2paSigningAlg.ES256, C2paSigningAlg.ES384, C2paSigningAlg.ES512):
        if not isinstance(private_key, ec.EllipticCurvePrivateKey):
            raise ValueError(f"{alg.name} requires an EC private key")
        digest = {
            C2paSigningAlg.ES256: hashes.SHA256(),
            C2paSigningAlg.ES384: hashes.SHA384(),
            C2paSigningAlg.ES512: hashes.SHA512(),
        }[alg]

        def _ecdsa(data: bytes) -> bytes:
            return private_key.sign(data, ec.ECDSA(digest))

        return _ecdsa

    if alg in (C2paSigningAlg.PS256, C2paSigningAlg.PS384, C2paSigningAlg.PS512):
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError(f"{alg.name} requires an RSA private key")
        digest = {
            C2paSigningAlg.PS256: hashes.SHA256(),
            C2paSigningAlg.PS384: hashes.SHA384(),
            C2paSigningAlg.PS512: hashes.SHA512(),
        }[alg]

        def _rsapss(data: bytes) -> bytes:
            return private_key.sign(
                data,
                padding.PSS(
                    mgf=padding.MGF1(digest),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                digest,
            )

        return _rsapss

    if alg == C2paSigningAlg.ED25519:
        if not isinstance(private_key, ed25519.Ed25519PrivateKey):
            raise ValueError("ED25519 requires an Ed25519 private key")

        def _ed25519(data: bytes) -> bytes:
            return private_key.sign(data)

        return _ed25519

    raise ValueError(f"Unsupported signing algorithm: {alg!r}")


def _build_signer(creds: SignerCredentials) -> Signer:
    """Load PEMs from disk and instantiate a ``c2pa.Signer`` (callback-backed)."""
    creds.validate()
    try:
        cert_pem = creds.cert_chain_path.read_text(encoding="utf-8")
        key_pem = creds.private_key_path.read_bytes()
    except FileNotFoundError as exc:
        # Race between validate() and read — surface as the same typed
        # error so the router can map to 503 without a bare except.
        raise MissingSigningMaterialError(str(exc)) from exc
    alg = _signing_alg(creds.signing_alg)
    callback = _make_callback(alg, key_pem)

    # ``Signer.from_callback`` validates ``tsa_url.startswith(http*)`` only when
    # truthy; ``None`` (not ``""``) is the right "no TSA" sentinel.
    ta_url = creds.ta_url or None

    signer = Signer.from_callback(
        callback,
        alg,
        cert_pem,
        ta_url,
    )
    logger.info(
        "Loaded C2PA signer (alg=%s, cert=%s, ta_url=%s)",
        creds.signing_alg,
        creds.cert_chain_path,
        ta_url or "<none>",
    )
    return signer


class SigningService:
    """
    Builds a ``Signer`` once at startup, then hands it off.

    Lifecycle:
    1. ``__init__`` stores credentials only (no Signer yet).
    2. ``validate()`` (called from app startup) eagerly parses cert +
       key and constructs the ``Signer``. Raises if either is missing
       or malformed — fail-fast deploy gating.
    3. ``release_signer()`` transfers ownership of the Signer to
       ``ManifestBuilderService``'s Context (which consumes it via
       ``_mark_consumed``). After this call this service no longer
       owns the FFI handle; ``close()`` is a no-op for the Signer.
    4. ``credentials`` keeps working forever. It's just the cert
       metadata (paths, alg, ta_url, ``cert_sha1``, ``leaf_not_after``)
       that the ``IngestionRecord`` and ``/health/deep`` need.

    ``ensure_loaded()`` / ``signer`` raise after ``release_signer()``

    ``is_loaded`` is sticky-true once a Signer has ever been built
    (released or not), so the readiness probe stays green after
    ownership transfer.
    """

    def __init__(self, creds: SignerCredentials | None = None) -> None:
        self._creds = creds or SignerCredentials.from_settings()
        self._signer: Signer | None = None
        self._released = False

    @property
    def credentials(self) -> SignerCredentials:
        return self._creds

    @property
    def is_loaded(self) -> bool:
        """True once startup has built a Signer.

        Stays True after ``release_signer()``. The readiness probe
        wants to know that startup completed successfully (i.e. the
        cert + key parsed and a Signer was constructed), not that
        this service still owns the FFI handle.
        """
        return self._signer is not None or self._released

    def validate(self) -> None:
        """Verify cert+key are present and parseable, build the Signer.

        Called from app startup. Raises ``MissingSigningMaterialError``
        if either file is missing, ``ValueError`` if the PEMs are
        malformed for the configured signing alg.
        """
        self._creds.validate()
        # Forces a parse of the private key + a Signer.from_callback —
        # any malformed PEM surfaces here, not on first ingest.
        self.ensure_loaded()

    def ensure_loaded(self) -> Signer:
        if self._released:
            raise RuntimeError(
                "Signer has been released to another owner; "
                "construct a new SigningService if you need a fresh Signer.",
            )
        if self._signer is None:
            self._signer = _build_signer(self._creds)
        return self._signer

    @property
    def signer(self) -> Signer:
        return self.ensure_loaded()

    def release_signer(self) -> Signer:
        """Transfer ownership of the Signer to the caller.

        Use when handing the Signer to a ``c2pa.Context``, which
        consumes it. After this call, ``close()`` becomes a no-op
        (the new owner / its Context manages the FFI handle's
        lifecycle) and ``signer`` / ``ensure_loaded()`` raise.
        """
        signer = self.ensure_loaded()
        self._signer = None
        self._released = True
        return signer

    def close(self) -> None:
        if self._signer is not None:
            try:
                self._signer.close()
            except Exception:
                logger.exception("Failed to close C2PA signer")
            self._signer = None
