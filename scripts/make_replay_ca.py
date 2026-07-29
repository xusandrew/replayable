#!/usr/bin/env python
"""Generate a mitmproxy-compatible CA whose validity starts in the past.

Why this exists
---------------
Replay pins the container's clock to the cassette's recorded ``t0``. If the CA
mitmproxy signs with was created *after* that moment, the container sees a
certificate that "is not yet valid" and every TLS handshake fails. ``runner``
detects this and refuses to run rather than emitting a confusing TLS error.

mitmproxy backdates a generated CA by exactly two days. That is fine on a
laptop whose CA predates its cassettes, but it makes CI a dated time bomb: a
runner that generates a fresh CA can only replay cassettes recorded in the last
48 hours.

So CI generates its CA here instead, with ``--not-before-days`` far enough back
to cover any cassette in the corpus. This handles the signing CA; the replay
addon separately moves mitmproxy's dynamically generated leaf-certificate
validity window to the cassette's ``t0``. Both are required. No private key is
committed to the repository: the CA is created fresh on each run and lives only
for that job.

Usage
-----
    python scripts/make_replay_ca.py --confdir ~/.mitmproxy --not-before-days 3650

Writes the files mitmproxy expects in a confdir:

* ``mitmproxy-ca.pem``       — private key + certificate (what mitmproxy signs with)
* ``mitmproxy-ca-cert.pem``  — certificate only (what containers trust)
"""

from __future__ import annotations

import argparse
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

# mitmproxy names its CA this way; keeping the subject identical avoids
# surprising anyone who inspects the certificate a container was asked to trust.
COMMON_NAME = "mitmproxy"
ORGANIZATION = "mitmproxy"


def build_ca(not_before: datetime, lifetime_days: int) -> tuple[bytes, bytes]:
    """Return ``(key_pem, cert_pem)`` for a self-signed CA."""

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, COMMON_NAME),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORGANIZATION),
        ]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_before + timedelta(days=lifetime_days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
    return key_pem, cert_pem


def _write_atomic(path: Path, content: bytes, mode: int) -> None:
    """Replace one CA file atomically without exposing partial key material."""

    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            temporary_path.chmod(mode)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def write_confdir(confdir: Path, key_pem: bytes, cert_pem: bytes) -> None:
    confdir.mkdir(parents=True, exist_ok=True)
    # mitmproxy signs with the combined key+cert file.
    _write_atomic(confdir / "mitmproxy-ca.pem", key_pem + cert_pem, 0o600)
    # Containers trust the certificate alone.
    _write_atomic(confdir / "mitmproxy-ca-cert.pem", cert_pem, 0o644)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confdir",
        type=Path,
        default=Path.home() / ".mitmproxy",
        help="mitmproxy configuration directory to write into.",
    )
    parser.add_argument(
        "--not-before-days",
        type=int,
        default=3650,
        help="Backdate the certificate's validity start by this many days.",
    )
    parser.add_argument(
        "--lifetime-days",
        type=int,
        default=3650 * 2,
        help="Total certificate lifetime, measured from --not-before-days.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing CA in the confdir.",
    )
    arguments = parser.parse_args()

    existing = arguments.confdir / "mitmproxy-ca.pem"
    if existing.exists() and not arguments.force:
        print(f"CA already present at {existing}; pass --force to replace it")
        return

    not_before = datetime.now(UTC) - timedelta(days=arguments.not_before_days)
    key_pem, cert_pem = build_ca(not_before, arguments.lifetime_days)
    write_confdir(arguments.confdir, key_pem, cert_pem)

    print(f"wrote CA to {arguments.confdir}")
    print(f"  not valid before: {not_before.isoformat()}")
    print(
        "  not valid after : "
        f"{(not_before + timedelta(days=arguments.lifetime_days)).isoformat()}"
    )


if __name__ == "__main__":
    main()
