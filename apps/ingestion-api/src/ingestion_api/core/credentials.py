"""
C2PA signing credentials (value object).

Holds the paths + signing-alg metadata for the cert chain and private
key, and exposes cheap forensic helpers (``cert_sha1``,
``leaf_not_after``) that ``/health/deep`` and the persisted
``IngestionRecord`` consume.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from ingestion_api.core.config import settings
from ingestion_api.core.errors import MissingSigningMaterialError


@dataclass(slots=True)
class SignerCredentials:
    cert_chain_path: Path
    private_key_path: Path
    signing_alg: str = "ES256"
    ta_url: str | None = None

    @classmethod
    def from_settings(cls) -> SignerCredentials:
        return cls(
            cert_chain_path=settings.cert_chain_path,
            private_key_path=settings.private_key_path,
            signing_alg=settings.signing_alg,
            ta_url=settings.ta_url,
        )

    def cert_sha1(self) -> str | None:
        """SHA-1 fingerprint of the leaf cert (DER) — standard X.509 fingerprint.

        The leaf is the FIRST certificate in the chain PEM.
        Returns None if the chain file is missing or unparseable.
        validate() raises if the cert chain is missing or unparseable,
        so a None here is forensic-only.
        """
        try:
            pem_bytes = self.cert_chain_path.read_bytes()
            certs = x509.load_pem_x509_certificates(pem_bytes)
            if not certs:
                return None
            der = certs[0].public_bytes(serialization.Encoding.DER)
            return hashlib.sha1(der, usedforsecurity=False).hexdigest()
        except (OSError, ValueError):
            return None

    def validate(self) -> None:
        alg = self.signing_alg.lower()
        if not self.cert_chain_path.is_file():
            raise MissingSigningMaterialError(
                f"Certificate chain not found at {self.cert_chain_path}. "
                f"Place the {self.signing_alg} cert chain at "
                f"<CREDENTIALS_DIR>/{alg}_certs.pem."
            )
        if not self.private_key_path.is_file():
            raise MissingSigningMaterialError(
                f"Private key not found at {self.private_key_path}. "
                f"Place the {self.signing_alg} private key at "
                f"<CREDENTIALS_DIR>/{alg}_private.key."
            )

    def leaf_not_after(self) -> datetime | None:
        """Parse the leaf cert and return its ``notAfter`` (UTC), or None
        if the chain is missing / unparseable.

        Used by ``/health/deep`` to surface impending cert expiry to
        ops dashboards.
        This is forensic data, not a readiness gate. Never raises.
        """
        try:
            pem_bytes = self.cert_chain_path.read_bytes()
            certs = x509.load_pem_x509_certificates(pem_bytes)
            if not certs:
                return None
            return certs[0].not_valid_after_utc
        except (OSError, ValueError, AttributeError):
            return None
