"""Local signing material for manifest tests; no external credentials or TSA."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import cbor2
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ingestion_api.core.credentials import SignerCredentials


def make_test_credentials(directory: Path) -> SignerCredentials:
    """Generate a fresh ES256 test chain, valid now, under a pytest temp directory.

    The leaf has the C2PA signing profile. The generated root is deliberately
    untrusted unless a test explicitly trusts it; trust and signature validity
    are separate checks. No private CA key or long-lived credentials are kept.
    """
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    root_key = ec.generate_private_key(ec.SECP256R1())
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    root_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Ephemeral C2PA Test Root"),
    ])
    root = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=7))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False,
            key_encipherment=False, data_encipherment=False, key_agreement=False,
            key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "Ephemeral C2PA Test Signer"),
        ]))
        .issuer_name(root_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=7))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=True,
            key_encipherment=False, data_encipherment=False, key_agreement=False,
            key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]), critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )
    cert_path = directory / "es256_certs.pem"
    key_path = directory / "es256_private.key"
    cert_path.write_bytes(
        leaf.public_bytes(serialization.Encoding.PEM)
        + root.public_bytes(serialization.Encoding.PEM),
    )
    key_path.write_bytes(leaf_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    key_path.chmod(0o600)
    return SignerCredentials(cert_path, key_path, ta_url=None)


def _boxes(data: bytes):
    """Read box payloads without reserializing the bytes covered by the hash."""
    offset = 0
    while offset < len(data):
        assert len(data) - offset >= 8, "truncated box header"
        size = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        header_size = 8
        if size == 1:
            assert len(data) - offset >= 16, "truncated extended box header"
            size = int.from_bytes(data[offset + 8:offset + 16], "big")
            header_size = 16
        elif size == 0:
            size = len(data) - offset
        assert header_size <= size <= len(data) - offset, "invalid box length"
        yield kind, data[offset + header_size:offset + size]
        offset += size


def _superboxes(data: bytes):
    for kind, payload in _boxes(data):
        if kind != b"jumb":
            continue
        description = next(body for tag, body in _boxes(payload) if tag == b"jumd")
        assert len(description) >= 18 and description[16] & 2, "missing JUMBF label"
        label = description[17:].split(b"\0", 1)[0].decode("utf-8")
        yield label, payload


def _manifest_boxes(manifest_bytes: bytes, active_manifest_label: str) -> dict[str, bytes]:
    stores = dict(_superboxes(manifest_bytes))
    manifests = dict(_superboxes(stores["c2pa"]))
    return dict(_superboxes(manifests[active_manifest_label]))


def raw_claim(manifest_bytes: bytes, active_manifest_label: str) -> dict:
    boxes = _manifest_boxes(manifest_bytes, active_manifest_label)
    payload = next(body for label, body in boxes.items() if label.startswith("c2pa.claim"))
    content = next(body for tag, body in _boxes(payload) if tag == b"cbor")
    return cbor2.loads(content)


def raw_assertions(
    manifest_bytes: bytes, active_manifest_label: str,
) -> dict[str, tuple[bytes, object]]:
    """Return final assertion labels mapped to (original box payload, CBOR data).

    Hash the first value directly: it includes the JUMBF description and salt,
    plus the content box, but excludes the outer superbox header. Selecting the
    active manifest explicitly keeps parent ingredient assertions separate.
    """
    manifest_boxes = _manifest_boxes(manifest_bytes, active_manifest_label)
    assertions = {}
    for label, payload in _superboxes(manifest_boxes["c2pa.assertions"]):
        content = next(body for tag, body in _boxes(payload) if tag == b"cbor")
        assert label not in assertions, "duplicate final assertion label"
        assertions[label] = (payload, cbor2.loads(content))
    return assertions
