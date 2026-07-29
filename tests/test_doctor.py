"""Diagnostics tests.

Every check takes its dependencies as arguments, so all of these run without
Docker, without a certificate and without touching the network. The assertions
deliberately check the *fix* text too: a diagnostic that says "FAIL" without
saying what to do is only marginally better than the confusing failure it
replaced.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from replayable.doctor import (
    CheckResult,
    Status,
    check_ca,
    check_clock_skew,
    check_docker,
    check_host_gateway,
    check_mitmdump,
    check_proxy_port,
    render,
    render_json,
    worst_status,
)


def completed(stdout="", stderr="", returncode=0):
    def run(_command):
        return subprocess.CompletedProcess(
            args=_command, returncode=returncode, stdout=stdout, stderr=stderr
        )

    return run


def docker_on_path(_name):
    return "/usr/bin/docker"


def write_ca(path, *, not_before, lifetime=timedelta(days=365)):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_before + lifetime)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return path


# --------------------------------------------------------------------------
# mitmdump
# --------------------------------------------------------------------------


def test_missing_mitmdump_fails_with_the_install_command():
    result = check_mitmdump(which=lambda _name: None)

    assert result.status is Status.FAIL
    assert "uv sync" in result.fix


def test_present_mitmdump_passes_and_reports_its_path():
    result = check_mitmdump(which=lambda _name: "/opt/venv/bin/mitmdump")

    assert result.status is Status.PASS
    assert result.detail == "/opt/venv/bin/mitmdump"


# --------------------------------------------------------------------------
# docker
# --------------------------------------------------------------------------


def test_missing_docker_cli_fails_without_calling_the_runner():
    def must_not_run(_command):
        raise AssertionError("runner must not be called without a Docker CLI")

    result = check_docker(run=must_not_run, which=lambda _name: None)

    assert result.status is Status.FAIL
    assert "not found on PATH" in result.detail


def test_unreachable_docker_daemon_fails():
    result = check_docker(
        run=completed(stderr="Cannot connect to the Docker daemon", returncode=1),
        which=docker_on_path,
    )

    assert result.status is Status.FAIL
    assert "Cannot connect" in result.detail
    assert "start Docker" in result.fix


def test_docker_older_than_host_gateway_support_fails():
    """20.10 introduced host-gateway, which the whole proxy contract needs."""

    result = check_docker(run=completed(stdout="19.03.12\n"), which=docker_on_path)

    assert result.status is Status.FAIL
    assert "predates host-gateway" in result.detail
    assert "20.10" in result.fix


@pytest.mark.parametrize("version", ["20.10.0", "24.0.7", "28.4.0"])
def test_supported_docker_versions_pass(version):
    result = check_docker(run=completed(stdout=f"{version}\n"), which=docker_on_path)

    assert result.status is Status.PASS


def test_unparseable_docker_version_warns_rather_than_blocking():
    """An unrecognized version string is not proof of a broken environment."""

    result = check_docker(run=completed(stdout="wibble\n"), which=docker_on_path)

    assert result.status is Status.WARN


def test_docker_cli_execution_error_is_reported_without_a_traceback():
    def fail_to_start(_command):
        raise OSError("executable vanished")

    result = check_docker(run=fail_to_start, which=docker_on_path)

    assert result.status is Status.FAIL
    assert "executable vanished" in result.detail


# --------------------------------------------------------------------------
# host gateway
# --------------------------------------------------------------------------


def test_resolvable_host_gateway_passes():
    result = check_host_gateway(
        run=completed(stdout="192.168.65.254  host.docker.internal\n")
    )

    assert result.status is Status.PASS
    assert "192.168.65.254" in result.detail


def test_unresolvable_host_gateway_warns():
    result = check_host_gateway(run=completed(returncode=1, stderr="no such host"))

    assert result.status is Status.WARN
    assert "no such host" in result.detail


# --------------------------------------------------------------------------
# CA
# --------------------------------------------------------------------------


def test_absent_ca_fails_with_the_generation_command(tmp_path):
    result = check_ca(tmp_path / "nope.pem")

    assert result.status is Status.FAIL
    assert "mitmdump" in result.fix


def test_unreadable_ca_fails(tmp_path):
    path = tmp_path / "ca.pem"
    path.write_text("this is not a certificate", encoding="utf-8")

    result = check_ca(path)

    assert result.status is Status.FAIL
    assert "unreadable" in result.detail


def test_valid_ca_passes(tmp_path):
    path = write_ca(tmp_path / "ca.pem", not_before=datetime.now(UTC) - timedelta(days=1))

    result = check_ca(path)

    assert result.status is Status.PASS


def test_expired_ca_fails_and_explains_how_to_keep_old_cassettes_replayable(tmp_path):

    path = write_ca(
        tmp_path / "ca.pem",
        not_before=datetime.now(UTC) - timedelta(days=800),
        lifetime=timedelta(days=365),
    )

    result = check_ca(path)

    assert result.status is Status.FAIL
    assert "expired" in result.detail
    assert "older cassette" in result.fix
    assert "make_replay_ca.py" in result.fix
    assert "must be re-recorded" not in result.fix


def test_ca_from_the_future_points_at_the_host_clock(tmp_path):
    path = write_ca(tmp_path / "ca.pem", not_before=datetime.now(UTC) + timedelta(days=5))

    result = check_ca(path)

    assert result.status is Status.FAIL
    assert "host clock" in result.fix


# --------------------------------------------------------------------------
# clock skew
# --------------------------------------------------------------------------


def _daemon_time(offset_seconds: float, now: datetime):
    stamp = (now + timedelta(seconds=offset_seconds)).isoformat()
    return completed(stdout=stamp + "\n")


def test_synchronized_clocks_pass():
    now = datetime.now(UTC)

    result = check_clock_skew(run=_daemon_time(0.2, now), now=now)

    assert result.status is Status.PASS


def test_small_clock_drift_warns():
    now = datetime.now(UTC)

    result = check_clock_skew(run=_daemon_time(5, now), now=now)

    assert result.status is Status.WARN
    assert "restart Docker" in result.fix


def test_large_clock_drift_fails():
    """Docker Desktop's VM clock drifts after the host sleeps.

    Replay pins the container clock to the recorded t0, so drift surfaces as
    certificate-validity errors rather than as anything clock-shaped.
    """

    now = datetime.now(UTC)

    result = check_clock_skew(run=_daemon_time(-3600, now), now=now)

    assert result.status is Status.FAIL
    assert "resynchronize" in result.fix


def test_unreadable_daemon_time_warns():
    result = check_clock_skew(run=completed(returncode=1))

    assert result.status is Status.WARN


def test_unparseable_daemon_time_warns():
    result = check_clock_skew(run=completed(stdout="not-a-timestamp\n"))

    assert result.status is Status.WARN


# --------------------------------------------------------------------------
# proxy port
# --------------------------------------------------------------------------


def test_free_proxy_port_passes():
    result = check_proxy_port(8080, is_free=lambda _port: True)

    assert result.status is Status.PASS


def test_busy_proxy_port_fails_and_suggests_an_ephemeral_port():
    result = check_proxy_port(8080, is_free=lambda _port: False)

    assert result.status is Status.FAIL
    assert "--port 0" in result.fix


def test_ephemeral_port_request_is_always_fine():
    result = check_proxy_port(0, is_free=lambda _port: False)

    assert result.status is Status.PASS


# --------------------------------------------------------------------------
# aggregation and rendering
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([Status.PASS, Status.PASS], Status.PASS),
        ([Status.PASS, Status.WARN], Status.WARN),
        ([Status.WARN, Status.FAIL], Status.FAIL),
        ([Status.FAIL, Status.PASS], Status.FAIL),
    ],
)
def test_worst_status_is_the_most_severe(statuses, expected):
    results = [CheckResult(f"c{i}", status, "") for i, status in enumerate(statuses)]

    assert worst_status(results) is expected


def test_render_shows_fixes_only_for_problems():
    results = [
        CheckResult("fine", Status.PASS, "all good", fix="never shown"),
        CheckResult("broken", Status.FAIL, "went wrong", fix="do the thing"),
    ]

    rendered = render(results)

    assert "never shown" not in rendered
    assert "→ do the thing" in rendered
    assert "Not ready" in rendered


def test_render_json_is_machine_readable():
    import json

    results = [CheckResult("docker", Status.WARN, "odd", fix="look into it")]

    payload = json.loads(render_json(results))

    assert payload["status"] == "warn"
    assert payload["checks"][0] == {
        "name": "docker",
        "status": "warn",
        "detail": "odd",
        "fix": "look into it",
    }
