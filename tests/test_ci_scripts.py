"""Tests for the scripts CI depends on.

These two carry real weight: `make_replay_ca` is the reason the golden replay
does not start failing on a fixed date, and `check_replay` decides whether a CI
run is red or green.
"""

from __future__ import annotations

import json
import os
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509

from replayable.exit_codes import ExitCode

SCRIPTS = Path(__file__).parents[1] / "scripts"

_make_ca = runpy.run_path(str(SCRIPTS / "make_replay_ca.py"))
_check = runpy.run_path(str(SCRIPTS / "check_replay.py"))

build_ca = _make_ca["build_ca"]
write_confdir = _make_ca["write_confdir"]
write_ca_file_atomic = _make_ca["_write_atomic"]
build_report = _check["build_report"]


# --------------------------------------------------------------------------
# make_replay_ca
# --------------------------------------------------------------------------


def test_generated_ca_predates_the_golden_cassette():
    """The reason this script exists.

    mitmproxy backdates a generated CA by two days. Replay pins the container
    clock to the cassette's recorded t0, so a stock CA can only replay
    cassettes recorded within the last 48 hours — meaning CI would begin
    failing on a fixed date with a confusing TLS error rather than a clear one.
    """

    golden_t0 = 1785312714.927677  # tests/fixtures/cassettes/research-agent

    not_before = datetime.now(UTC) - timedelta(days=3650)
    _key_pem, cert_pem = build_ca(not_before, lifetime_days=7300)
    certificate = x509.load_pem_x509_certificate(cert_pem)

    assert certificate.not_valid_before_utc.timestamp() < golden_t0
    assert certificate.not_valid_after_utc > datetime.now(UTC)


def test_generated_ca_is_a_usable_certificate_authority():
    """Without basicConstraints CA:TRUE, mitmproxy cannot sign leaf certs."""

    not_before = datetime.now(UTC) - timedelta(days=30)
    _key_pem, cert_pem = build_ca(not_before, lifetime_days=365)
    certificate = x509.load_pem_x509_certificate(cert_pem)

    constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
    usage = certificate.extensions.get_extension_for_class(x509.KeyUsage)

    assert constraints.value.ca is True
    assert usage.value.key_cert_sign is True


def test_confdir_layout_matches_what_mitmproxy_expects(tmp_path):
    """mitmproxy signs with the combined file; containers trust the cert alone."""

    key_pem, cert_pem = build_ca(datetime.now(UTC) - timedelta(days=1), 365)
    write_confdir(tmp_path, key_pem, cert_pem)

    combined = (tmp_path / "mitmproxy-ca.pem").read_bytes()
    cert_only = (tmp_path / "mitmproxy-ca-cert.pem").read_bytes()

    assert combined == key_pem + cert_pem
    assert cert_only == cert_pem
    assert b"PRIVATE KEY" not in cert_only, "the mounted cert must not carry the key"
    # The signing key is readable only by its owner.
    assert (tmp_path / "mitmproxy-ca.pem").stat().st_mode & 0o077 == 0


def test_failed_ca_write_does_not_expose_a_partial_key(tmp_path, monkeypatch):
    path = tmp_path / "mitmproxy-ca.pem"
    path.write_bytes(b"old-complete-ca")

    def fail_to_flush(_descriptor):
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", fail_to_flush)

    with pytest.raises(OSError, match="disk full"):
        write_ca_file_atomic(path, b"new-partial-ca", 0o600)

    assert path.read_bytes() == b"old-complete-ca"
    assert not list(tmp_path.glob(".mitmproxy-ca.pem.*.tmp"))


# --------------------------------------------------------------------------
# check_replay
# --------------------------------------------------------------------------


def write_cassette(root: Path, *, manifest: dict, replay: dict | None, **extra) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if replay is not None:
        replay.setdefault("exit_code", int(ExitCode.SUCCESS))
        (root / "last-replay.json").write_text(json.dumps(replay), encoding="utf-8")
        (root / "replay-state.json").write_text(
            json.dumps({"unconsumed_sequences": []}),
            encoding="utf-8",
        )
    for name, payload in extra.items():
        (root / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


def test_matching_hashes_report_deterministic(tmp_path):
    cassette = write_cassette(
        tmp_path / "c",
        manifest={
            "workspace_sha256": "a" * 64,
            "stdout_sha256": "b" * 64,
            "record_wall_time_seconds": 32.0,
        },
        replay={
            "workspace_sha256": "a" * 64,
            "stdout_sha256": "b" * 64,
            "wall_time_seconds": 0.6,
        },
    )

    exit_code, lines = build_report(cassette)
    rendered = "\n".join(lines)

    assert exit_code == ExitCode.SUCCESS
    assert "DETERMINISTIC" in rendered
    assert "53× faster" in rendered  # noqa: RUF001 - typographic multiplication sign, display only


def test_diverging_workspace_hash_is_a_mismatch(tmp_path):
    cassette = write_cassette(
        tmp_path / "c",
        manifest={"workspace_sha256": "a" * 64, "stdout_sha256": "b" * 64},
        replay={
            "workspace_sha256": "c" * 64,
            "stdout_sha256": "b" * 64,
            "workspace_diff": {"added": [], "removed": ["report.md"], "changed": []},
        },
    )

    exit_code, lines = build_report(cassette)
    rendered = "\n".join(lines)

    assert exit_code == ExitCode.REPLAY_MISMATCH
    assert "Behaviour changed" in rendered
    assert "removed: report.md" in rendered


def test_unmatched_request_is_reported_with_nearest_candidates(tmp_path):
    """The mismatch report is the most useful thing in a red CI run."""

    cassette = write_cassette(
        tmp_path / "c",
        manifest={"workspace_sha256": "a" * 64, "stdout_sha256": "b" * 64},
        replay={"workspace_sha256": "a" * 64, "stdout_sha256": "b" * 64},
    )
    (cassette / "replay-report.json").write_text(
        json.dumps(
            {
                "live_request": {
                    "method": "POST",
                    "host": "api.anthropic.com",
                    "path": "/v1/messages",
                },
                "nearest_candidates": [
                    {"seq": 3, "method": "POST", "path": "/v1/messages"}
                ],
            }
        ),
        encoding="utf-8",
    )

    exit_code, lines = build_report(cassette)
    rendered = "\n".join(lines)

    assert exit_code == ExitCode.REPLAY_MISMATCH
    assert "Unmatched request" in rendered
    assert "api.anthropic.com/v1/messages" in rendered
    assert "nearest recorded flow 3" in rendered


def test_unserved_flows_are_reported(tmp_path):
    cassette = write_cassette(
        tmp_path / "c",
        manifest={"workspace_sha256": "a" * 64, "stdout_sha256": "b" * 64},
        replay={"workspace_sha256": "a" * 64, "stdout_sha256": "b" * 64},
    )
    (cassette / "replay-state.json").write_text(
        json.dumps({"unconsumed_sequences": [4, 5]}), encoding="utf-8"
    )

    _exit_code, lines = build_report(cassette)
    rendered = "\n".join(lines)

    assert "**Unserved flows:** [4, 5]" in rendered
    # Matching hashes plus unserved flows is not an exact reproduction, and the
    # report must not claim one — the reassuring half is the half people read.
    assert "DETERMINISTIC" not in rendered
    assert "not fully consumed" in rendered


def test_legacy_cassette_without_hash_baselines_is_a_passing_smoke_replay(tmp_path):
    """The workflow's curl-demo option predates workspace/stdout snapshots."""

    cassette = write_cassette(
        tmp_path / "c",
        manifest={},
        replay={
            "workspace_sha256": "a" * 64,
            "stdout_sha256": "b" * 64,
        },
    )

    exit_code, lines = build_report(cassette)
    rendered = "\n".join(lines)

    assert exit_code == ExitCode.SUCCESS
    assert "Replay passed" in rendered
    assert "no complete byte-level" in rendered
    assert "DETERMINISTIC" not in rendered


@pytest.mark.parametrize(
    ("replay_exit_code", "expected_text"),
    [
        (ExitCode.AGENT_FAILED, "Agent failed"),
        (ExitCode.REPLAY_MISMATCH, "Replay mismatch"),
        (ExitCode.HARNESS_ERROR, "Harness error"),
    ],
)
def test_nonzero_replay_result_cannot_be_reported_as_deterministic(
    tmp_path,
    replay_exit_code,
    expected_text,
):
    cassette = write_cassette(
        tmp_path / "c",
        manifest={"workspace_sha256": "a" * 64, "stdout_sha256": "b" * 64},
        replay={
            "exit_code": int(replay_exit_code),
            "workspace_sha256": "a" * 64,
            "stdout_sha256": "b" * 64,
        },
    )

    exit_code, lines = build_report(cassette)
    rendered = "\n".join(lines)

    assert exit_code == replay_exit_code
    assert expected_text in rendered
    assert "DETERMINISTIC" not in rendered


@pytest.mark.parametrize(
    "replay",
    [
        {},
        {"exit_code": 0},
        {"exit_code": 0, "workspace_sha256": "a" * 64},
        {
            "exit_code": "0",
            "workspace_sha256": "a" * 64,
            "stdout_sha256": "b" * 64,
        },
    ],
)
def test_incomplete_replay_metadata_is_a_harness_error(tmp_path, replay):
    cassette = write_cassette(
        tmp_path / "c",
        manifest={"workspace_sha256": "a" * 64, "stdout_sha256": "b" * 64},
        replay=replay,
    )

    exit_code, lines = build_report(cassette)

    assert exit_code == ExitCode.HARNESS_ERROR
    assert "Invalid" in "\n".join(lines)


def test_missing_replay_state_is_a_harness_error(tmp_path):
    cassette = write_cassette(
        tmp_path / "c",
        manifest={"workspace_sha256": "a" * 64, "stdout_sha256": "b" * 64},
        replay={"workspace_sha256": "a" * 64, "stdout_sha256": "b" * 64},
    )
    (cassette / "replay-state.json").unlink()

    exit_code, lines = build_report(cassette)

    assert exit_code == ExitCode.HARNESS_ERROR
    assert "replay is incomplete" in "\n".join(lines)


@pytest.mark.parametrize("report", [{}, [], {"live_request": []}])
def test_malformed_mismatch_report_is_a_harness_error(tmp_path, report):
    cassette = write_cassette(
        tmp_path / "c",
        manifest={"workspace_sha256": "a" * 64, "stdout_sha256": "b" * 64},
        replay={"workspace_sha256": "a" * 64, "stdout_sha256": "b" * 64},
    )
    (cassette / "replay-report.json").write_text(json.dumps(report), encoding="utf-8")

    exit_code, lines = build_report(cassette)

    assert exit_code == ExitCode.HARNESS_ERROR
    assert "Invalid replay report" in "\n".join(lines)


@pytest.mark.parametrize("missing", ["manifest", "replay"])
def test_incomplete_run_is_a_harness_error_not_a_mismatch(tmp_path, missing):
    """A replay that never finished is an environment problem, not a verdict.

    Reporting it as REPLAY_MISMATCH would tell a developer their change broke
    the agent, when in fact the harness never ran.
    """

    cassette = tmp_path / "c"
    if missing == "manifest":
        cassette.mkdir()
    else:
        write_cassette(cassette, manifest={"workspace_sha256": "a"}, replay=None)

    exit_code, _lines = build_report(cassette)

    assert exit_code == ExitCode.HARNESS_ERROR
