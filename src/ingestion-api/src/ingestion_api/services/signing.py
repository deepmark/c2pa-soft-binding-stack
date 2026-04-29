"""
C2PA signer factory.

Implementation note: the C2PA Python SDK's ``Signer.from_info`` is broken
for ES256 in 0.32.3 (returns ``Signature: empty string``). The official
example (``c2pa-python/examples/sign.py``) instead drives signing via
``Signer.from_callback`` with the ``cryptography`` library doing the
actual ECDSA / RSA-PSS work — that's what this module does.

The cert chain (PEM) is passed to the SDK as a string. The private key
(PEM) stays in Python; ``cryptography.serialization.load_pem_private_key``
parses it once at startup, and a per-algorithm signer callback signs each
chunk of bytes the SDK hands us.

Supported signing algorithms (mirrors ``c2pa.C2paSigningAlg``):
- ES256 / ES384 / ES512 (ECDSA)
- PS256 / PS384 / PS512 (RSA-PSS)
- ED25519

If you want to use a remote KMS / HSM later, swap ``_make_callback`` for a
function that calls out to your service instead of using ``cryptography``.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from c2pa import C2paSigningAlg, Signer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from ingestion_api.core.config import settings
from ingestion_api.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class SignerCredentials:
    cert_chain_path: Path
    private_key_path: Path
    signing_alg: str = "ES256"
    ta_url: str | None = None

    @classmethod
    def from_settings(cls) -> SignerCredentials:
        return cls(
            cert_chain_path=settings.resolved_cert_chain_path(),
            private_key_path=settings.resolved_private_key_path(),
            signing_alg=settings.signing_alg,
            ta_url=settings.ta_url,
        )

    def validate(self) -> None:
        if not self.cert_chain_path.is_file():
            raise FileNotFoundError(
                f"Certificate chain not found at {self.cert_chain_path}. "
                "Place ES256 test certs under ./credentials/es256_certs.pem "
                "or override CERT_CHAIN_PATH."
            )
        if not self.private_key_path.is_file():
            raise FileNotFoundError(
                f"Private key not found at {self.private_key_path}. "
                "Place ES256 test private key under "
                "./credentials/es256_private.key or override PRIVATE_KEY_PATH."
            )


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


def build_signer(creds: SignerCredentials) -> Signer:
    """Load PEMs from disk and instantiate a ``c2pa.Signer`` (callback-backed)."""
    creds.validate()
    cert_pem = creds.cert_chain_path.read_text(encoding="utf-8")
    key_pem = creds.private_key_path.read_bytes()
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
    Holds a single ``Signer`` instance for the app's lifetime.

    Construction is lazy: ``ensure_loaded()`` is called on first use so
    missing certs surface as a clean 503 rather than tearing down startup.
    """

    def __init__(self, creds: SignerCredentials | None = None) -> None:
        self._creds = creds or SignerCredentials.from_settings()
        self._signer: Signer | None = None

    @property
    def credentials(self) -> SignerCredentials:
        return self._creds

    def ensure_loaded(self) -> Signer:
        if self._signer is None:
            self._signer = build_signer(self._creds)
        return self._signer

    @property
    def signer(self) -> Signer:
        return self.ensure_loaded()

    def close(self) -> None:
        if self._signer is not None:
            try:
                self._signer.close()
            except Exception:
                logger.exception("Failed to close C2PA signer")
            self._signer = None
