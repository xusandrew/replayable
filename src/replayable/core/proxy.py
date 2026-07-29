"""Mitmproxy process lifecycle and host/port resolution."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from replayable.core.docker import _require_executable
from replayable.errors import HarnessError

DEFAULT_PROXY_PORT = 8080


def _port_is_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.1):
            return True
    except OSError:
        return False


def _resolve_port(port: int, host: str = "127.0.0.1") -> int:
    """Resolve port 0 on the same interface the proxy will bind.

    The probe uses ``SO_REUSEADDR`` so mitmdump can rebind immediately after
    the probe socket closes. This is still a short race under concurrency, but
    probing the real listen host (not always loopback) avoids the Linux case
    where a port is free on ``127.0.0.1`` yet taken on the bridge gateway.
    """

    if port != 0:
        return port
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, 0))
        return probe.getsockname()[1]


def _proxy_listen_host() -> str:
    """Bind the proxy to the narrowest interface containers can still reach.

    Docker Desktop (macOS/Windows) forwards host.docker.internal to the host
    loopback, so 127.0.0.1 suffices. Native Linux containers reach the host at
    the bridge gateway address, so bind there rather than every interface.
    """

    if sys.platform != "linux":
        return "127.0.0.1"
    completed = subprocess.run(
        [
            "docker",
            "network",
            "inspect",
            "bridge",
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    gateway = completed.stdout.strip()
    if completed.returncode == 0 and gateway:
        return gateway
    print(
        "replayable: warning: cannot resolve the Docker bridge gateway; the "
        "proxy will listen on all interfaces for this run",
        file=sys.stderr,
    )
    return "0.0.0.0"


def _wait_for_proxy(
    process: subprocess.Popen[bytes],
    port: int,
    timeout_seconds: float,
    host: str = "127.0.0.1",
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise HarnessError(
                f"mitmdump exited before becoming ready (exit {return_code}); "
                "inspect proxy.log for details"
            )
        if _port_is_open(port, host):
            return
        time.sleep(0.05)
    raise HarnessError(
        f"mitmdump did not listen on port {port} within {timeout_seconds:g}s; "
        "check whether the port is blocked"
    )


def _stop_proxy(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@contextmanager
def proxy_process(
    *,
    addon: Path,
    port: int,
    addon_environment: dict[str, str],
    log_path: Path,
    readiness_timeout_seconds: float = 5,
    listen_host: str | None = None,
    confdir: Path | None = None,
) -> Iterator[None]:
    """Start mitmdump, wait for its port, and always stop it with SIGTERM."""

    mitmdump = _require_executable(
        "mitmdump", "install project dependencies with `uv sync`"
    )
    if not addon.is_file():
        raise HarnessError(
            f"mitmproxy addon not found at {addon}; reinstall replayable"
        )
    listen_host = listen_host if listen_host is not None else _proxy_listen_host()
    check_host = "127.0.0.1" if listen_host == "0.0.0.0" else listen_host
    if _port_is_open(port, check_host):
        raise HarnessError(
            f"proxy port {port} is already in use; stop the process using that "
            "port or pass --port 0 to pick a free one"
        )

    environment = os.environ.copy()
    environment.update(addon_environment)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        mitmdump,
        "--listen-host",
        listen_host,
        "--listen-port",
        str(port),
        "-s",
        str(addon),
        "--set",
        "flow_detail=0",
        "--set",
        "connection_strategy=lazy",
    ]
    if confdir is not None:
        command.extend(["--set", f"confdir={confdir}"])
    with log_path.open("wb") as proxy_log:
        try:
            process = subprocess.Popen(
                command,
                env=environment,
                stdout=proxy_log,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            raise HarnessError(
                f"failed to start mitmdump: {exc}; verify the uv environment"
            ) from exc

        try:
            _wait_for_proxy(process, port, readiness_timeout_seconds, check_host)
            yield
            return_code = process.poll()
            if return_code is not None:
                raise HarnessError(
                    f"mitmdump stopped during the container run (exit {return_code}); "
                    "inspect proxy.log for details"
                )
        finally:
            _stop_proxy(process)
