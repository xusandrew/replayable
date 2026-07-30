"""Container subprocess execution and transcript capture."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import BinaryIO

from replayable.core.docker import _require_executable
from replayable.errors import HarnessError
from replayable.redact import redact_body


def _copy_stream(
    source: BinaryIO,
    destination: BinaryIO,
    mirror: BinaryIO,
    secrets: dict[str, str] | None = None,
) -> None:
    """Mirror a stream live while writing a secret-redacted copy to disk.

    Both the on-disk transcript and the live TTY/CI mirror are redacted so
    console capture cannot undo cassette redaction. Hold back the last
    ``max_secret_length - 1`` bytes so a secret split across read boundaries is
    still replaced once it completes.
    """

    secrets = {name: value for name, value in (secrets or {}).items() if value}
    if not secrets:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            destination.write(chunk)
            destination.flush()
            mirror.write(chunk)
            mirror.flush()
        return

    hold = max(len(value.encode("utf-8")) for value in secrets.values()) - 1
    carry = b""
    for chunk in iter(lambda: source.read(64 * 1024), b""):
        redacted = redact_body(carry + chunk, secrets)
        split = max(len(redacted) - hold, 0)
        emitted = redacted[:split]
        destination.write(emitted)
        destination.flush()
        mirror.write(emitted)
        mirror.flush()
        carry = redacted[split:]
    destination.write(carry)
    destination.flush()
    mirror.write(carry)
    mirror.flush()


def _kill_container(name: str) -> None:
    subprocess.run(
        ["docker", "kill", name],
        check=False,
        capture_output=True,
    )


def _run_container(
    command: list[str],
    *,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    redaction_secrets: dict[str, str] | None = None,
    container_name: str | None = None,
    timeout_seconds: float | None = None,
) -> int:
    _require_executable("docker", "install Docker and make sure it is on PATH")

    def wait_with_watchdog(process: subprocess.Popen[bytes]) -> int:
        try:
            return process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            if container_name is not None:
                _kill_container(container_name)
            process.wait()
            raise HarnessError(
                f"container did not finish within {timeout_seconds:g}s and was "
                "killed; frozen wall-clock deadlines inside the workload are a "
                "known cause (see docs/limitations.md)"
            ) from None

    try:
        if stdout_path is None or stderr_path is None:
            process = subprocess.Popen(command)
            return_code = wait_with_watchdog(process)
        else:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            with (
                stdout_path.open("wb") as stdout_file,
                stderr_path.open("wb") as stderr_file,
            ):
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                assert process.stdout is not None
                assert process.stderr is not None
                stdout_thread = threading.Thread(
                    target=_copy_stream,
                    args=(
                        process.stdout,
                        stdout_file,
                        sys.stdout.buffer,
                        redaction_secrets,
                    ),
                    daemon=True,
                )
                stderr_thread = threading.Thread(
                    target=_copy_stream,
                    args=(
                        process.stderr,
                        stderr_file,
                        sys.stderr.buffer,
                        redaction_secrets,
                    ),
                    daemon=True,
                )
                stdout_thread.start()
                stderr_thread.start()
                try:
                    return_code = wait_with_watchdog(process)
                finally:
                    stdout_thread.join()
                    stderr_thread.join()
    except OSError as exc:
        raise HarnessError(
            f"failed to start Docker: {exc}; make sure Docker Desktop or Docker Engine is running"
        ) from exc
    if return_code == 125:
        raise HarnessError(
            "Docker could not launch the container (exit 125); check the image name, "
            "Docker daemon, and the preceding Docker error"
        )
    return return_code
