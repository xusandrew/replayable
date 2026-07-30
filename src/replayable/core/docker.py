"""Docker command construction and immutable image selection."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from replayable.errors import HarnessError

PROXY_HOSTNAME = "host.docker.internal"
CONTAINER_CA_PATH = "/etc/replayable/ca.pem"
FAKETIME_LIBRARY = "/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1"


def _require_executable(name: str, likely_fix: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise HarnessError(f"{name} was not found; {likely_fix}")
    return executable


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
