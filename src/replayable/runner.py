"""Proxy, Docker, cassette, and normalized replay orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from cryptography import x509

from replayable.cassette import (
    CassetteError,
    CassetteReader,
    CassetteWriter,
    base_manifest,
    env_fingerprint,
)
from replayable.exit_codes import ExitCode
from replayable.normalize_rules import (
    RulesError,
    load_rules,
)
from replayable.redact import (
    EnvFileError,
    SecretConfigError,
    load_secret_name_overrides,
    parse_env_file,
    redact_body,
    redacted_placeholder,
    secret_names,
    secret_values,
)
from replayable.snapshot import (
    SnapshotError,
    create_snapshot,
    diff_file_manifests,
    load_recorded_snapshot,
)

DEFAULT_PROXY_PORT = 8080
PROXY_HOSTNAME = "host.docker.internal"
CONTAINER_CA_PATH = "/etc/replayable/ca.pem"
REPLAY_REPORT_FILE_NAME = "replay-report.json"
REPLAY_STATE_FILE_NAME = "replay-state.json"
RUN_LOG_FILE_NAME = "run.log"
REPLAY_LOG_FILE_NAME = "replay.log"
REPLAY_PROXY_LOG_FILE_NAME = "replay-proxy.log"
AGENT_STDOUT_FILE_NAME = "agent.stdout"
AGENT_STDERR_FILE_NAME = "agent.stderr"
REPLAY_STDOUT_FILE_NAME = "replay-agent.stdout"
REPLAY_STDERR_FILE_NAME = "replay-agent.stderr"
LAST_REPLAY_FILE_NAME = "last-replay.json"
FAKETIME_LIBRARY = "/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1"


class HarnessError(RuntimeError):
    """An infrastructure failure with an actionable user-facing message."""


def default_ca_path() -> Path:
    """Return mitmproxy's generated certificate path."""

    return Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"


def _require_executable(name: str, likely_fix: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise HarnessError(f"{name} was not found; {likely_fix}")
    return executable


def _require_ca(ca_path: Path) -> None:
    if not ca_path.is_file():
        raise HarnessError(
            f"mitmproxy CA not found at {ca_path}; run `uv run mitmdump` once "
            "and stop it after startup to generate the certificate"
        )


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


def _require_ca_valid_at(ca_path: Path, t0_epoch: float) -> None:
    """Fail fast when the pinned clock predates the CA certificate."""

    try:
        certificate = x509.load_pem_x509_certificate(ca_path.read_bytes())
    except (OSError, ValueError):
        return  # unreadable CAs already produce mitmproxy's own startup error
    not_before = certificate.not_valid_before_utc.timestamp()
    if t0_epoch < not_before:
        raise HarnessError(
            f"the mitmproxy CA at {ca_path} was generated after this cassette's "
            "recording time, so the pinned container clock would see a "
            "'certificate is not yet valid' TLS failure; restore the CA that "
            "existed at record time or record a new cassette"
        )


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
    with log_path.open("wb") as proxy_log:
        try:
            process = subprocess.Popen(
                [
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
                ],
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


def docker_command(
    *,
    image: str,
    command: Sequence[str],
    port: int,
    ca_path: Path,
    run_id: str,
    workspace: Path | None = None,
    env_file: Path | None = None,
    extra_environment: dict[str, str] | None = None,
) -> list[str]:
    """Build the Docker CLI invocation for the proxy and CA contract."""

    docker_args = [
        "docker",
        "run",
        "--rm",
        "--name",
        f"replayable-{run_id}",
        "--add-host=host.docker.internal:host-gateway",
        "-v",
        f"{ca_path}:{CONTAINER_CA_PATH}:ro",
    ]
    if workspace is not None:
        docker_args.extend(["-v", f"{workspace.resolve()}:/workspace"])
    if env_file is not None:
        docker_args.extend(["--env-file", str(env_file.resolve())])
    environment = dict(extra_environment or {})
    environment.update(injected_container_environment(port))
    for name, value in environment.items():
        docker_args.extend(["-e", f"{name}={value}"])
    docker_args.extend([image, *command])
    return docker_args


def injected_container_environment(port: int) -> dict[str, str]:
    """Environment values Replayable always injects after the user's env file."""

    proxy_url = f"http://{PROXY_HOSTNAME}:{port}"
    return {
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "NO_PROXY": "localhost,127.0.0.1",
        "SSL_CERT_FILE": CONTAINER_CA_PATH,
        "REQUESTS_CA_BUNDLE": CONTAINER_CA_PATH,
        "CURL_CA_BUNDLE": CONTAINER_CA_PATH,
        "NODE_EXTRA_CA_CERTS": CONTAINER_CA_PATH,
        "PYTHONHASHSEED": "0",
    }


def replay_time_environment(t0_epoch: float) -> dict[str, str]:
    """Return libfaketime settings that pin replay to the recording epoch."""

    formatted = datetime.fromtimestamp(t0_epoch, UTC).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "LD_PRELOAD": FAKETIME_LIBRARY,
        "FAKETIME": formatted,
        "FAKETIME_DONT_FAKE_MONOTONIC": "1",
    }


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


def _resolve_image_identity(image: str) -> tuple[str, str]:
    """Return the image's pullable digest and immutable local image ID.

    The local ID is the ground truth for "these exact bytes ran"; the repo
    digest (when present) additionally lets another machine pull the image.
    """

    _require_executable("docker", "install Docker and make sure it is on PATH")
    completed = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}\t{{json .RepoDigests}}",
            image,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 or "\t" not in completed.stdout:
        detail = completed.stderr.strip() or "image is not available locally"
        raise HarnessError(
            f"Docker cannot inspect image {image!r}: {detail}; build or pull it first"
        )
    image_id, _tab, digests_json = completed.stdout.strip().partition("\t")
    try:
        repo_digests = json.loads(digests_json)
    except json.JSONDecodeError:
        repo_digests = []
    if not image_id:
        raise HarnessError(f"Docker cannot resolve an immutable ID for {image!r}")
    digest = (
        str(repo_digests[0])
        if isinstance(repo_digests, list) and repo_digests
        else image_id
    )
    return digest, image_id


def _local_image_id(reference: str) -> str | None:
    completed = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _select_replay_image(
    *,
    image_ref: str,
    image_digest: str,
    image_id: str,
    allow_image_mismatch: bool,
) -> str:
    """Require the exact recorded image bytes unless the escape hatch is set."""

    _require_executable("docker", "install Docker and make sure it is on PATH")
    if _local_image_id(image_id) is not None:
        return image_id
    if image_digest != image_id and _local_image_id(image_digest) == image_id:
        return image_digest
    if allow_image_mismatch:
        print(
            "replayable: warning: the recorded image bytes are unavailable; "
            f"using mutable reference {image_ref!r}",
            file=sys.stderr,
        )
        return image_ref
    raise HarnessError(
        f"recorded image (id {image_id!r}, digest {image_digest!r}) is not "
        "present locally; pull/build that exact image or pass "
        "--allow-image-mismatch for development"
    )


def _log_event(path: Path, event: str, **fields: object) -> None:
    record = {
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "event": event,
        **fields,
    }
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")


@contextmanager
def _runtime_workspace(
    selected: Path | None,
) -> Iterator[Path]:
    if selected is None:
        with tempfile.TemporaryDirectory(prefix="replayable-workspace-") as directory:
            yield Path(directory)
        return

    path = selected.expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    yield path


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _addon_path(name: str) -> Path:
    return Path(__file__).parent / "addons" / name


def _prepare_output_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HarnessError(f"cannot create output directory {path}: {exc}") from exc


@contextmanager
def _secret_values_file(secrets: dict[str, str]) -> Iterator[Path | None]:
    """Expose secret values to the record addon through a private 0600 file."""

    if not secrets:
        yield None
        return
    descriptor, name = tempfile.mkstemp(prefix="replayable-secrets-", suffix=".json")
    path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(secrets, output, separators=(",", ":"))
        os.chmod(path, 0o600)
        yield path
    finally:
        path.unlink(missing_ok=True)


def _remove_stale_replay_artifacts(out: Path) -> None:
    for stale in (
        REPLAY_REPORT_FILE_NAME,
        REPLAY_STATE_FILE_NAME,
        LAST_REPLAY_FILE_NAME,
        REPLAY_STDOUT_FILE_NAME,
        REPLAY_STDERR_FILE_NAME,
        REPLAY_LOG_FILE_NAME,
        REPLAY_PROXY_LOG_FILE_NAME,
    ):
        (out / stale).unlink(missing_ok=True)


def record_run(
    *,
    image: str,
    command: Sequence[str],
    workspace: Path | None = None,
    env_file: Path | None = None,
    out: Path = Path("cassettes"),
    port: int = DEFAULT_PROXY_PORT,
    ca_path: Path | None = None,
    timeout_seconds: float | None = None,
) -> ExitCode:
    """Record traffic, transcript, immutable image identity, and workspace state."""

    if not command:
        raise HarnessError("container command is empty; pass it after `--`")
    ca_path = (ca_path or default_ca_path()).expanduser().resolve()
    _require_ca(ca_path)
    if env_file is not None and not env_file.is_file():
        raise HarnessError(
            f"environment file not found at {env_file}; check --env-file"
        )

    listen_host = _proxy_listen_host()
    port = _resolve_port(port, listen_host)
    out = out.resolve()
    _prepare_output_directory(out)
    try:
        user_environment = parse_env_file(env_file) if env_file is not None else {}
    except EnvFileError as exc:
        raise HarnessError(f"environment file is invalid: {exc}") from exc
    project_rules_path = Path.cwd() / "replayable.toml"
    if not project_rules_path.is_file():
        project_rules_path = None
    try:
        rules = load_rules(project_rules_path)
    except RulesError as exc:
        raise HarnessError(f"normalization rules are invalid: {exc}") from exc
    try:
        secret_name_overrides = load_secret_name_overrides(project_rules_path)
    except SecretConfigError as exc:
        raise HarnessError(f"secret overrides are invalid: {exc}") from exc
    classified_secret_names = secret_names(
        user_environment,
        extra_names=secret_name_overrides,
    )
    image_digest, image_id = _resolve_image_identity(image)
    writer = CassetteWriter(out)
    manifest = base_manifest(
        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        t0_epoch=0.0,
        image_ref=image,
        image_digest=image_digest,
        image_id=image_id,
        command=list(command),
        environment_fingerprint=env_fingerprint(
            user_environment,
            secret_names=classified_secret_names,
        ),
        ruleset_version=rules.version,
    )
    manifest["env_names"] = sorted(user_environment)
    manifest["secret_env_names"] = sorted(classified_secret_names)
    manifest["nonsecret_env"] = {
        name: value
        for name, value in sorted(user_environment.items())
        if name not in classified_secret_names
    }
    writer.initialize(manifest)
    cassette_rules_path = out / "replayable.toml"
    if project_rules_path is None:
        cassette_rules_path.unlink(missing_ok=True)
    elif project_rules_path.resolve() != cassette_rules_path.resolve():
        shutil.copyfile(project_rules_path, cassette_rules_path)
    _remove_stale_replay_artifacts(out)
    run_log = out / RUN_LOG_FILE_NAME
    run_log.write_text("", encoding="utf-8")
    run_id = uuid.uuid4().hex[:12]
    redaction_secret_values = secret_values(
        user_environment,
        extra_names=secret_name_overrides,
    )
    with (
        _runtime_workspace(workspace) as runtime_workspace,
        _secret_values_file(redaction_secret_values) as secrets_path,
    ):
        _log_event(run_log, "proxy_start", mode="record", port=port)
        with proxy_process(
            addon=_addon_path("record_addon.py"),
            port=port,
            addon_environment={
                "REPLAYABLE_CASSETTE_DIR": str(out),
                "REPLAYABLE_SECRET_VALUES_FILE": (
                    str(secrets_path) if secrets_path is not None else ""
                ),
            },
            log_path=out / "proxy.log",
            listen_host=listen_host,
        ):
            t0_epoch = time.time()
            writer.update_manifest(t0_epoch=t0_epoch)
            container_command = docker_command(
                image=image,
                command=command,
                port=port,
                ca_path=ca_path,
                run_id=run_id,
                workspace=runtime_workspace,
                env_file=env_file,
                extra_environment=replay_time_environment(t0_epoch),
            )
            _log_event(
                run_log,
                "container_start",
                mode="record",
                run_id=run_id,
                image_digest=image_digest,
                image_id=image_id,
            )
            started = time.monotonic()
            return_code = _run_container(
                container_command,
                stdout_path=out / AGENT_STDOUT_FILE_NAME,
                stderr_path=out / AGENT_STDERR_FILE_NAME,
                redaction_secrets=redaction_secret_values,
                container_name=f"replayable-{run_id}",
                timeout_seconds=timeout_seconds,
            )
            (out / AGENT_STDOUT_FILE_NAME).touch(exist_ok=True)
            (out / AGENT_STDERR_FILE_NAME).touch(exist_ok=True)
            wall_time = time.monotonic() - started
            _log_event(
                run_log,
                "container_exit",
                mode="record",
                return_code=return_code,
                wall_time_seconds=wall_time,
            )

        try:
            workspace_snapshot = create_snapshot(runtime_workspace, out)
        except SnapshotError as exc:
            raise HarnessError(f"workspace snapshot failed: {exc}") from exc
        _log_event(
            run_log,
            "workspace_snapshot",
            sha256=workspace_snapshot.sha256,
            file_count=len(workspace_snapshot.files),
        )

    try:
        loaded = CassetteReader(out).load_flows()
        writer.update_manifest(
            flow_count=len(loaded.flows),
            record_wall_time_seconds=wall_time,
            workspace_sha256=workspace_snapshot.sha256,
            stdout_sha256=_sha256_path(out / AGENT_STDOUT_FILE_NAME),
            stderr_sha256=_sha256_path(out / AGENT_STDERR_FILE_NAME),
        )
    except CassetteError as exc:
        raise HarnessError(f"recorded cassette is invalid: {exc}") from exc
    _log_event(run_log, "record_complete", flow_count=len(loaded.flows))
    return ExitCode.SUCCESS if return_code == 0 else ExitCode.AGENT_FAILED


def replay_run(
    *,
    cassette: Path,
    strict: bool = False,
    out_workspace: Path | None = None,
    port: int = DEFAULT_PROXY_PORT,
    ca_path: Path | None = None,
    allow_image_mismatch: bool = False,
    timeout_seconds: float | None = None,
) -> ExitCode:
    """Replay offline with pinned time/image and deterministic output checks."""

    cassette = cassette.resolve()
    if not cassette.is_dir():
        raise HarnessError(
            f"cassette directory not found at {cassette}; check --cassette"
        )
    listen_host = _proxy_listen_host()
    port = _resolve_port(port, listen_host)
    try:
        reader = CassetteReader(cassette)
        manifest = reader.load_manifest()
        reader.load_flows()
        image_ref = manifest["image"]["ref"]
        image_digest = manifest["image"]["digest"]
        image_id = manifest["image"].get("id", image_digest)
        command = manifest["command"]
        t0_epoch = float(manifest["t0_epoch"])
        if (
            not isinstance(image_ref, str)
            or not isinstance(image_digest, str)
            or not isinstance(image_id, str)
            or not isinstance(command, list)
            or not all(isinstance(item, str) for item in command)
        ):
            raise CassetteError("manifest image, digest, or command is invalid")
    except (CassetteError, KeyError, TypeError, ValueError) as exc:
        raise HarnessError(f"cassette cannot be replayed: {exc}") from exc
    # The cassette is self-describing: only a rules file pinned inside it may
    # apply. A replayable.toml in the current directory must not change how a
    # recorded cassette is matched.
    rules_path: Path | None = cassette / "replayable.toml"
    if not rules_path.is_file():
        rules_path = None
    try:
        rules = load_rules(rules_path)
    except RulesError as exc:
        raise HarnessError(f"normalization rules are invalid: {exc}") from exc
    recorded_ruleset = manifest.get("ruleset_version")
    if recorded_ruleset is not None and recorded_ruleset != rules.version:
        raise HarnessError(
            "normalization rules do not match the cassette manifest; restore the "
            "replayable.toml inside the cassette to the recorded version or "
            "record a new cassette"
        )
    ca_path = (ca_path or default_ca_path()).expanduser().resolve()
    _require_ca(ca_path)
    _require_ca_valid_at(ca_path, t0_epoch)
    image = _select_replay_image(
        image_ref=image_ref,
        image_digest=image_digest,
        image_id=image_id,
        allow_image_mismatch=allow_image_mismatch,
    )
    report_path = cassette / REPLAY_REPORT_FILE_NAME
    state_path = cassette / REPLAY_STATE_FILE_NAME
    report_path.unlink(missing_ok=True)
    state_path.unlink(missing_ok=True)
    run_id = uuid.uuid4().hex[:12]
    environment_names = manifest.get("env_names", [])
    if not isinstance(environment_names, list) or not all(
        isinstance(name, str) for name in environment_names
    ):
        raise HarnessError("cassette manifest env_names must be an array of strings")
    secret_environment_names = manifest.get("secret_env_names", environment_names)
    nonsecret_environment = manifest.get("nonsecret_env", {})
    if not isinstance(secret_environment_names, list) or not all(
        isinstance(name, str) for name in secret_environment_names
    ):
        raise HarnessError(
            "cassette manifest secret_env_names must be an array of strings"
        )
    if not isinstance(nonsecret_environment, dict) or not all(
        isinstance(name, str) and isinstance(value, str)
        for name, value in nonsecret_environment.items()
    ):
        raise HarnessError("cassette manifest nonsecret_env must be a string mapping")
    replay_user_environment = dict(nonsecret_environment)
    # Use the same token record wrote into bodies/transcripts so body-auth APIs
    # and echoed secrets stay matchable without real credentials.
    replay_user_environment.update(
        {name: redacted_placeholder(name) for name in secret_environment_names}
    )
    replay_fingerprint = env_fingerprint(
        replay_user_environment,
        secret_names=secret_environment_names,
    )
    if manifest.get("env_fingerprint") != replay_fingerprint:
        print(
            "replayable: warning: replay environment differs from the recorded "
            "environment fingerprint",
            file=sys.stderr,
        )
    replay_environment = {
        **replay_user_environment,
        **replay_time_environment(t0_epoch),
    }
    run_log = cassette / REPLAY_LOG_FILE_NAME
    run_log.write_text("", encoding="utf-8")

    with _runtime_workspace(out_workspace) as runtime_workspace:
        container_command = docker_command(
            image=image,
            command=command,
            port=port,
            ca_path=ca_path,
            run_id=run_id,
            workspace=runtime_workspace,
            extra_environment=replay_environment,
        )
        _log_event(run_log, "proxy_start", mode="replay", port=port)
        with proxy_process(
            addon=_addon_path("replay_addon.py"),
            port=port,
            addon_environment={
                "REPLAYABLE_CASSETTE_DIR": str(cassette),
                "REPLAYABLE_REPORT_FILE": str(report_path),
                "REPLAYABLE_STATE_FILE": str(state_path),
                "REPLAYABLE_RULES_FILE": str(rules_path) if rules_path else "",
            },
            log_path=cassette / REPLAY_PROXY_LOG_FILE_NAME,
            listen_host=listen_host,
        ):
            _log_event(
                run_log,
                "container_start",
                mode="replay",
                run_id=run_id,
                image=image,
            )
            started = time.monotonic()
            return_code = _run_container(
                container_command,
                stdout_path=cassette / REPLAY_STDOUT_FILE_NAME,
                stderr_path=cassette / REPLAY_STDERR_FILE_NAME,
                container_name=f"replayable-{run_id}",
                timeout_seconds=timeout_seconds,
            )
            (cassette / REPLAY_STDOUT_FILE_NAME).touch(exist_ok=True)
            (cassette / REPLAY_STDERR_FILE_NAME).touch(exist_ok=True)
            replay_wall_time = time.monotonic() - started
            _log_event(
                run_log,
                "container_exit",
                mode="replay",
                return_code=return_code,
                wall_time_seconds=replay_wall_time,
            )

        with tempfile.TemporaryDirectory(prefix="replayable-snapshot-") as snapshot_dir:
            try:
                replay_snapshot = create_snapshot(
                    runtime_workspace,
                    Path(snapshot_dir),
                )
            except SnapshotError as exc:
                raise HarnessError(f"workspace snapshot failed: {exc}") from exc
            replay_workspace_hash = replay_snapshot.sha256
            replay_files = replay_snapshot.files

    report = _load_optional_json(report_path)
    final_code: ExitCode
    if report is not None:
        _print_mismatch_summary(report)
        final_code = ExitCode.REPLAY_MISMATCH
    else:
        state = _load_optional_json(state_path)
        if state is None:
            raise HarnessError(
                f"replay state was not written at {state_path}; inspect replay-proxy.log"
            )
        unconsumed = state.get("unconsumed_sequences", [])
        if not isinstance(unconsumed, list):
            raise HarnessError(f"replay state is invalid at {state_path}")
        if unconsumed:
            message = f"replay left {len(unconsumed)} unconsumed flow(s): {unconsumed}"
            if strict:
                print(f"replayable: {message}", file=sys.stderr)
                final_code = ExitCode.REPLAY_MISMATCH
            else:
                print(f"replayable: warning: {message}", file=sys.stderr)
                final_code = (
                    ExitCode.SUCCESS if return_code == 0 else ExitCode.AGENT_FAILED
                )
        else:
            final_code = ExitCode.SUCCESS if return_code == 0 else ExitCode.AGENT_FAILED

    workspace_matches, workspace_diff = _verify_workspace(
        cassette,
        run_log,
        replay_workspace_hash=replay_workspace_hash,
        replay_files=replay_files,
    )
    if not workspace_matches:
        final_code = ExitCode.REPLAY_MISMATCH

    stdout_hash = _sha256_path(cassette / REPLAY_STDOUT_FILE_NAME)
    if not _verify_transcripts(cassette, manifest, replay_stdout_hash=stdout_hash):
        final_code = ExitCode.REPLAY_MISMATCH

    last_replay = {
        "exit_code": int(final_code),
        "wall_time_seconds": replay_wall_time,
        "workspace_sha256": replay_workspace_hash,
        "stdout_sha256": stdout_hash,
        "workspace_diff": workspace_diff,
    }
    (cassette / LAST_REPLAY_FILE_NAME).write_text(
        json.dumps(last_replay, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _log_event(run_log, "replay_complete", **last_replay)
    return final_code


def _verify_workspace(
    cassette: Path,
    run_log: Path,
    *,
    replay_workspace_hash: str,
    replay_files: list[dict[str, Any]],
) -> tuple[bool, dict[str, list[str]] | None]:
    """Compare the replay workspace with the recorded baseline, if one exists."""

    workspace_hash_path = cassette / "workspace.sha256"
    workspace_files_path = cassette / "workspace.files.json"
    if not workspace_hash_path.exists() and not workspace_files_path.exists():
        return True, None
    try:
        recorded_workspace_hash, recorded_files = load_recorded_snapshot(cassette)
    except SnapshotError as exc:
        raise HarnessError(f"workspace baseline is invalid: {exc}") from exc

    if replay_workspace_hash == recorded_workspace_hash:
        print("DETERMINISTIC ✓ (workspace sha256 matches)")
        _log_event(run_log, "workspace_match", sha256=replay_workspace_hash)
        return True, None

    workspace_diff = diff_file_manifests(recorded_files, replay_files)
    print("replayable: workspace differs from recording", file=sys.stderr)
    for category in ("added", "removed", "changed"):
        if workspace_diff[category]:
            print(
                f"  {category}: {', '.join(workspace_diff[category])}",
                file=sys.stderr,
            )
    if (
        workspace_diff["removed"]
        and not workspace_diff["added"]
        and not workspace_diff["changed"]
    ):
        print(
            "  hint: only recorded files are missing; if the workload reads "
            "input files from /workspace, pre-seed an identical workspace and "
            "pass it with --out-workspace",
            file=sys.stderr,
        )
    _log_event(
        run_log,
        "workspace_mismatch",
        sha256=replay_workspace_hash,
        diff=workspace_diff,
    )
    return False, workspace_diff


def _verify_transcripts(
    cassette: Path,
    manifest: dict[str, Any],
    *,
    replay_stdout_hash: str,
) -> bool:
    """Compare replay stdout against the recording; warn (only) on stderr drift."""

    recorded_stdout = cassette / AGENT_STDOUT_FILE_NAME
    recorded_stdout_hash = manifest.get("stdout_sha256")
    if not isinstance(recorded_stdout_hash, str) and recorded_stdout.is_file():
        recorded_stdout_hash = _sha256_path(recorded_stdout)

    recorded_stderr_hash = manifest.get("stderr_sha256")
    recorded_stderr = cassette / AGENT_STDERR_FILE_NAME
    if not isinstance(recorded_stderr_hash, str) and recorded_stderr.is_file():
        recorded_stderr_hash = _sha256_path(recorded_stderr)
    if (
        isinstance(recorded_stderr_hash, str)
        and _sha256_path(cassette / REPLAY_STDERR_FILE_NAME) != recorded_stderr_hash
    ):
        print(
            "replayable: warning: agent stderr differs from the recorded "
            "transcript (informational only)",
            file=sys.stderr,
        )

    if (
        isinstance(recorded_stdout_hash, str)
        and replay_stdout_hash != recorded_stdout_hash
    ):
        print(
            "replayable: agent stdout differs from the recorded transcript",
            file=sys.stderr,
        )
        return False
    return True


def _load_optional_json(path: Path) -> dict | None:
    if not path.is_file() or not path.stat().st_size:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"replay output is invalid at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"replay output at {path} must be a JSON object")
    return value


def _print_mismatch_summary(report: dict) -> None:
    live = report.get("live_request", {})
    print(
        f"replayable: mismatch: {live.get('method', '?')} {live.get('path', '?')}",
        file=sys.stderr,
    )
    diff = report.get("diff", "")
    if not isinstance(diff, str) or not diff:
        return
    hunks: list[list[str]] = []
    current: list[str] = []
    for line in diff.splitlines():
        if line.startswith("@@"):
            if current:
                hunks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        hunks.append(current)
    selected_lines = [line for hunk in hunks[:5] for line in hunk] or diff.splitlines()[
        :20
    ]
    print("\n".join(selected_lines), file=sys.stderr)
