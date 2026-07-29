"""Certificate authority validation and mitmproxy configuration."""

from __future__ import annotations

from pathlib import Path

from cryptography import x509

from replayable.errors import HarnessError


def default_ca_path() -> Path:
    """Return mitmproxy's generated certificate path."""

    return Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"


def _require_ca(ca_path: Path) -> None:
    if not ca_path.is_file():
        raise HarnessError(
            f"mitmproxy CA not found at {ca_path}; run `uv run mitmdump` once "
            "and stop it after startup to generate the certificate"
        )

def _mitmproxy_confdir_for_ca(ca_path: Path) -> Path | None:
    """Use a custom generated CA for signing as well as container trust."""

    combined_ca = ca_path.with_name("mitmproxy-ca.pem")
    if ca_path.name == "mitmproxy-ca-cert.pem" and combined_ca.is_file():
        return ca_path.parent
    return None


def _require_ca_valid_at(ca_path: Path, t0_epoch: float) -> None:
    """Fail fast unless the CA validity window contains the pinned clock."""

    try:
        certificate = x509.load_pem_x509_certificate(ca_path.read_bytes())
    except (OSError, ValueError):
        return  # unreadable CAs already produce mitmproxy's own startup error
    not_before = certificate.not_valid_before_utc.timestamp()
    not_after = certificate.not_valid_after_utc.timestamp()
    if t0_epoch < not_before:
        raise HarnessError(
            f"the mitmproxy CA at {ca_path} was generated after this cassette's "
            "recording time, so the pinned container clock would see a "
            "'certificate is not yet valid' TLS failure; use a CA whose "
            "validity begins before the cassette's t0 (for CI, see "
            "scripts/make_replay_ca.py)"
        )
    if t0_epoch > not_after:
        raise HarnessError(
            f"the mitmproxy CA at {ca_path} expired before this cassette's "
            "recording time, so the pinned container clock would reject it; "
            "use a CA whose validity includes the cassette's t0"
        )
