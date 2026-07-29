"""Preflight environment diagnostics for the `doctor` command.

Every failure mode here has cost someone an afternoon: a CA that was never
generated, a Docker daemon whose clock drifted away from the host, a proxy port
already bound by a previous crashed run. Each of them surfaces during a record
or replay as something misleading — a TLS error, an unexplained mismatch, a
hang — so `doctor` names them up front and says what to do.

Every check takes its dependencies as arguments so it can be tested without
Docker, without a certificate, and without touching the network.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from cryptography import x509

# Docker gained `--add-host=host.docker.internal:host-gateway` in 20.10. The
# whole container-to-host proxy contract depends on it.
MINIMUM_DOCKER_VERSION = (20, 10)

# Beyond this the container's wall clock differs enough from the host's that a
# freshly generated CA can look "not yet valid" inside the container.
CLOCK_SKEW_WARN_SECONDS = 2.0
CLOCK_SKEW_FAIL_SECONDS = 60.0


class Status(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str
    fix: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "status": str(self.status),
            "detail": self.detail,
            "fix": self.fix,
        }


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def check_mitmdump(which: Callable[[str], str | None] = shutil.which) -> CheckResult:
    """mitmdump is the proxy; without it nothing records or replays."""

    path = which("mitmdump")
    if path is None:
        return CheckResult(
            name="mitmdump",
            status=Status.FAIL,
            detail="not found on PATH",
            fix="install project dependencies with `uv sync`",
        )
    return CheckResult("mitmdump", Status.PASS, path)


def _parse_docker_version(text: str) -> tuple[int, ...] | None:
    parts = text.strip().split(".")
    try:
        return tuple(int(part) for part in parts[:2])
    except ValueError:
        return None


def check_docker(
    run: Runner = _run,
    which: Callable[[str], str | None] = shutil.which,
) -> CheckResult:
    """The daemon must be reachable and new enough for host-gateway."""

    if which("docker") is None:
        return CheckResult(
            name="docker",
            status=Status.FAIL,
            detail="not found on PATH",
            fix="install Docker and make sure the `docker` CLI is on PATH",
        )

    try:
        completed = run(["docker", "version", "--format", "{{.Server.Version}}"])
    except OSError as exc:
        return CheckResult(
            name="docker",
            status=Status.FAIL,
            detail=f"could not run the Docker CLI: {exc}",
            fix="install Docker and make sure the `docker` CLI is usable",
        )
    if completed.returncode != 0:
        return CheckResult(
            name="docker",
            status=Status.FAIL,
            detail=(completed.stderr or completed.stdout).strip()
            or "daemon did not respond",
            fix="start Docker (`open -a Docker` on macOS) and retry",
        )

    version_text = completed.stdout.strip()
    version = _parse_docker_version(version_text)
    if version is None:
        return CheckResult(
            name="docker",
            status=Status.WARN,
            detail=f"unrecognized server version {version_text!r}",
            fix="verify `docker version` reports a numeric server version",
        )
    if version < MINIMUM_DOCKER_VERSION:
        rendered = ".".join(str(part) for part in MINIMUM_DOCKER_VERSION)
        return CheckResult(
            name="docker",
            status=Status.FAIL,
            detail=f"server {version_text} predates host-gateway support",
            fix=f"upgrade Docker to {rendered} or newer",
        )
    return CheckResult("docker", Status.PASS, f"server {version_text}")


def check_host_gateway(run: Runner = _run) -> CheckResult:
    """The container must be able to resolve the host.

    Everything the agent sends is routed to `host.docker.internal`, so if that
    name does not resolve inside a container, every request fails in a way that
    looks like the agent's fault rather than the harness's.
    """

    completed = run(
        [
            "docker",
            "run",
            "--rm",
            "--add-host=host.docker.internal:host-gateway",
            "alpine:3",
            "getent",
            "hosts",
            "host.docker.internal",
        ]
    )
    if completed.returncode == 0 and completed.stdout.strip():
        address = completed.stdout.split()[0]
        return CheckResult(
            "host-gateway", Status.PASS, f"host.docker.internal -> {address}"
        )
    return CheckResult(
        name="host-gateway",
        status=Status.WARN,
        detail=(completed.stderr or completed.stdout).strip()
        or "could not resolve host.docker.internal in a container",
        fix=(
            "check Docker can pull alpine:3 and that this Docker version "
            "supports --add-host=host.docker.internal:host-gateway"
        ),
    )


def check_ca(ca_path: Path, now: datetime | None = None) -> CheckResult:
    """The CA must exist and already be valid.

    A CA generated *after* a cassette was recorded is the subtlest failure in
    the system: replay pins the container clock to the recording time, so the
    container sees a certificate that is not yet valid and every TLS handshake
    fails.
    """

    now = now or datetime.now(UTC)

    if not ca_path.is_file():
        return CheckResult(
            name="mitmproxy CA",
            status=Status.FAIL,
            detail=f"not found at {ca_path}",
            fix=(
                "generate it once with `uv run mitmdump` and stop it after "
                "startup"
            ),
        )

    try:
        certificate = x509.load_pem_x509_certificate(ca_path.read_bytes())
    except (OSError, ValueError) as exc:
        return CheckResult(
            name="mitmproxy CA",
            status=Status.FAIL,
            detail=f"unreadable at {ca_path}: {exc}",
            fix="delete the file and regenerate it with `uv run mitmdump`",
        )

    not_before = certificate.not_valid_before_utc
    not_after = certificate.not_valid_after_utc

    if now < not_before:
        return CheckResult(
            name="mitmproxy CA",
            status=Status.FAIL,
            detail=f"not valid until {not_before.isoformat()}",
            fix="check the host clock; the CA appears to be from the future",
        )
    if now > not_after:
        return CheckResult(
            name="mitmproxy CA",
            status=Status.FAIL,
            detail=f"expired on {not_after.isoformat()}",
            fix=(
                "regenerate it before recording; to replay an older cassette, "
                "use a replacement CA whose validity begins before that "
                "cassette's t0 (see scripts/make_replay_ca.py)"
            ),
        )
    return CheckResult(
        name="mitmproxy CA",
        status=Status.PASS,
        detail=f"valid until {not_after.date().isoformat()}",
    )


def check_clock_skew(run: Runner = _run, now: datetime | None = None) -> CheckResult:
    """Host and Docker VM clocks must agree.

    On macOS and Windows the daemon runs in a VM whose clock drifts after the
    host sleeps. Since replay pins the container clock to the recorded `t0`,
    drift shows up as certificate-validity errors rather than as a clock
    problem.
    """

    now = now or datetime.now(UTC)
    completed = run(["docker", "info", "--format", "{{.SystemTime}}"])
    if completed.returncode != 0 or not completed.stdout.strip():
        return CheckResult(
            name="clock skew",
            status=Status.WARN,
            detail="could not read the Docker daemon's system time",
            fix="ensure the Docker daemon is running",
        )

    raw = completed.stdout.strip()
    try:
        daemon_time = datetime.fromisoformat(raw)
    except ValueError:
        return CheckResult(
            name="clock skew",
            status=Status.WARN,
            detail=f"unparseable daemon time {raw!r}",
        )
    if daemon_time.tzinfo is None:
        daemon_time = daemon_time.replace(tzinfo=UTC)

    skew = abs((daemon_time - now).total_seconds())
    detail = f"{skew:.1f}s between host and Docker daemon"
    if skew >= CLOCK_SKEW_FAIL_SECONDS:
        return CheckResult(
            name="clock skew",
            status=Status.FAIL,
            detail=detail,
            fix="restart Docker to resynchronize its VM clock with the host",
        )
    if skew >= CLOCK_SKEW_WARN_SECONDS:
        return CheckResult(
            name="clock skew",
            status=Status.WARN,
            detail=detail,
            fix="restart Docker if replays fail with certificate errors",
        )
    return CheckResult("clock skew", Status.PASS, detail)


def _port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.1):
            return False
    except OSError:
        return True


def check_proxy_port(
    port: int,
    is_free: Callable[[int], bool] = _port_is_free,
) -> CheckResult:
    """The default proxy port must be free, or a crashed run still holds it."""

    if port == 0:
        return CheckResult("proxy port", Status.PASS, "ephemeral port requested")
    if is_free(port):
        return CheckResult("proxy port", Status.PASS, f"{port} is free")
    return CheckResult(
        name="proxy port",
        status=Status.FAIL,
        detail=f"{port} is already in use",
        fix=f"stop whatever holds port {port}, or pass `--port 0`",
    )


def run_checks(
    *,
    ca_path: Path,
    port: int,
    include_docker_run: bool = True,
) -> list[CheckResult]:
    """Run every diagnostic and return results in reporting order."""

    results = [
        check_mitmdump(),
        check_docker(),
        check_ca(ca_path),
        check_proxy_port(port),
    ]
    # These two need a working daemon, so only run them if Docker answered.
    docker_ok = results[1].status is not Status.FAIL
    if docker_ok:
        results.append(check_clock_skew())
        if include_docker_run:
            results.append(check_host_gateway())
    return results


def worst_status(results: list[CheckResult]) -> Status:
    if any(result.status is Status.FAIL for result in results):
        return Status.FAIL
    if any(result.status is Status.WARN for result in results):
        return Status.WARN
    return Status.PASS


_SYMBOLS = {Status.PASS: "ok  ", Status.WARN: "warn", Status.FAIL: "FAIL"}


def render(results: list[CheckResult]) -> str:
    """Render results as an aligned report with fixes for anything not passing."""

    width = max((len(result.name) for result in results), default=0)
    lines = []
    for result in results:
        lines.append(
            f"[{_SYMBOLS[result.status]}] {result.name.ljust(width)}  {result.detail}"
        )
        if result.fix and result.status is not Status.PASS:
            lines.append(f"{' ' * (width + 7)}→ {result.fix}")

    overall = worst_status(results)
    lines.append("")
    if overall is Status.PASS:
        lines.append("All checks passed. Ready to record and replay.")
    elif overall is Status.WARN:
        lines.append("Usable, but the warnings above may cause confusing failures.")
    else:
        lines.append("Not ready. Fix the FAIL items above before recording.")
    return "\n".join(lines)


def render_json(results: list[CheckResult]) -> str:
    return json.dumps(
        {
            "status": str(worst_status(results)),
            "checks": [result.as_dict() for result in results],
        },
        indent=2,
    )
