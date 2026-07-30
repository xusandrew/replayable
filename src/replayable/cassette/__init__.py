"""Versioned Milestone 2 cassette bundle storage."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from replayable import __version__

if TYPE_CHECKING:
    from replayable.cassette.events import Event

CASSETTE_VERSION = "2.0"
SUPPORTED_CASSETTE_MAJOR_VERSIONS = frozenset({1, 2})
BLOB_THRESHOLD_BYTES = 256 * 1024
MANIFEST_FILE_NAME = "manifest.json"
FLOW_FILE_NAME = "flows.jsonl"
BLOB_DIRECTORY_NAME = "blobs"
DEFAULT_REDACTED_HEADERS = [
    "authorization",
    "x-api-key",
    "api-key",
    "cookie",
    "set-cookie",
]


class CassetteError(RuntimeError):
    """A malformed, unsupported, or inaccessible cassette bundle."""


class CassetteVersionError(CassetteError):
    """The cassette's major version is unsupported."""


@dataclass(frozen=True)
class FlowLoadResult:
    """Loaded flows and whether a truncated trailing record was discarded."""

    flows: list[dict[str, Any]]
    dropped_truncated_final_line: bool = False


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sse_chunk_bytes(chunk: dict[str, str]) -> bytes:
    """Decode one recorded SSE chunk, in UTF-8 or base64 representation."""

    if "data_utf8" in chunk:
        return chunk["data_utf8"].encode("utf-8")
    if "data_base64" in chunk:
        try:
            return base64.b64decode(chunk["data_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise CassetteError(f"invalid base64 SSE chunk: {exc}") from exc
    raise CassetteError(f"invalid SSE chunk representation: {sorted(chunk)!r}")


def _major_version(value: str) -> int:
    try:
        return int(value.split(".", maxsplit=1)[0])
    except (AttributeError, ValueError) as exc:
        raise CassetteError(f"invalid cassette_version {value!r}") from exc


def validate_cassette_version(value: str) -> None:
    """Reject incompatible major versions while permitting minor additions."""

    if _major_version(value) not in SUPPORTED_CASSETTE_MAJOR_VERSIONS:
        supported = ", ".join(
            str(version) for version in sorted(SUPPORTED_CASSETTE_MAJOR_VERSIONS)
        )
        raise CassetteVersionError(
            f"cassette major version {value!r} is unsupported; "
            f"this harness supports major versions {supported}"
        )


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def env_fingerprint(
    environment: dict[str, str],
    *,
    secret_names: Iterable[str],
) -> str:
    """Hash rendered environment shape without hashing secret values."""

    secrets = set(secret_names)
    rendered = [
        key if key in secrets else f"{key}={value}"
        for key, value in sorted(environment.items())
    ]
    return f"sha256:{sha256_bytes(chr(10).join(rendered).encode())}"


class CassetteWriter:
    """Incrementally write a bundle with content-addressed body blobs."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.manifest_path = root / MANIFEST_FILE_NAME
        self.flow_path = root / FLOW_FILE_NAME
        self.event_path = root / "events.jsonl"
        self.blob_directory = root / BLOB_DIRECTORY_NAME

    def initialize(self, manifest: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.blob_directory.exists():
            shutil.rmtree(self.blob_directory)
        self.blob_directory.mkdir(parents=True, exist_ok=True)
        self.flow_path.write_text("", encoding="utf-8")
        self.event_path.write_text("", encoding="utf-8")
        self.write_manifest(manifest)

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        _write_json_atomic(self.manifest_path, manifest)

    def update_manifest(self, **updates: Any) -> dict[str, Any]:
        manifest = CassetteReader(self.root).load_manifest(validate_version=False)
        manifest.update(updates)
        self.write_manifest(manifest)
        return manifest

    def represent_body(self, body: bytes) -> dict[str, str]:
        """Store a body inline when small UTF-8, otherwise as a SHA blob."""

        if len(body) <= BLOB_THRESHOLD_BYTES:
            try:
                return {"inline_utf8": body.decode("utf-8")}
            except UnicodeDecodeError:
                pass

        digest = sha256_bytes(body)
        blob_path = self.blob_directory / digest
        if not blob_path.exists():
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.blob_directory,
                prefix=f".{digest}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(body)
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    temporary_path.replace(blob_path)
                except FileExistsError:
                    temporary_path.unlink(missing_ok=True)
            except BaseException:
                temporary_path.unlink(missing_ok=True)
                raise
        return {"blob": f"{BLOB_DIRECTORY_NAME}/{digest}"}

    def append_event(self, event: Event) -> None:
        """Append and durably flush one validated event record."""

        with self.event_path.open("a", encoding="utf-8") as output:
            output.write(
                json.dumps(event.as_dict(), separators=(",", ":"), sort_keys=True)
                + "\n"
            )
            output.flush()
            os.fsync(output.fileno())

    def append_flow(self, flow: dict[str, Any], *, event: Event | None = None) -> None:
        """Append one flow and its corresponding network event.

        Validate the event before either file changes. A process crash can still
        interrupt the two physical appends, so completed recordings verify both
        counts before publishing their final manifest.
        """

        from replayable.cassette.events import event_from_flow

        paired_event = event or event_from_flow(flow, lamport=flow.get("seq", 0))
        if paired_event.seq != flow.get("seq"):
            raise CassetteError("flow and event sequence numbers must match")
        if paired_event.payload.get("flow") != flow:
            raise CassetteError("network event payload must contain its exact flow")

        with self.flow_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(flow, separators=(",", ":"), sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        self.append_event(paired_event)


class CassetteReader:
    """Load and validate a cassette bundle."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.manifest_path = root / MANIFEST_FILE_NAME
        self.flow_path = root / FLOW_FILE_NAME

    def load_manifest(self, *, validate_version: bool = True) -> dict[str, Any]:
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CassetteError(f"manifest not found at {self.manifest_path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise CassetteError(
                f"manifest is unreadable at {self.manifest_path}: {exc}"
            ) from exc
        if not isinstance(manifest, dict):
            raise CassetteError(f"manifest at {self.manifest_path} must be an object")
        version = manifest.get("cassette_version")
        if not isinstance(version, str):
            raise CassetteError("manifest is missing a string cassette_version")
        if validate_version:
            validate_cassette_version(version)
        return manifest

    def load_flows(self) -> FlowLoadResult:
        try:
            raw = self.flow_path.read_bytes()
        except FileNotFoundError as exc:
            raise CassetteError(f"flow file not found at {self.flow_path}") from exc
        except OSError as exc:
            raise CassetteError(f"flow file is unreadable at {self.flow_path}: {exc}") from exc

        lines = raw.splitlines(keepends=True)
        flows: list[dict[str, Any]] = []
        dropped = False
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                is_unterminated_final_line = index == len(lines) - 1 and not raw.endswith(
                    (b"\n", b"\r")
                )
                if is_unterminated_final_line:
                    dropped = True
                    break
                raise CassetteError(
                    f"invalid JSONL record at {self.flow_path}:{index + 1}: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise CassetteError(
                    f"flow at {self.flow_path}:{index + 1} must be an object"
                )
            flows.append(value)
        return FlowLoadResult(
            flows=flows,
            dropped_truncated_final_line=dropped,
        )

    def read_body(self, representation: dict[str, str] | None) -> bytes:
        if representation is None:
            return b""
        if set(representation) == {"inline_utf8"}:
            return representation["inline_utf8"].encode("utf-8")
        if set(representation) == {"blob"}:
            relative_path = Path(representation["blob"])
            expected_prefix = Path(BLOB_DIRECTORY_NAME)
            if relative_path.is_absolute() or relative_path.parent != expected_prefix:
                raise CassetteError(f"invalid blob path {relative_path}")
            path = self.root / relative_path
            try:
                body = path.read_bytes()
            except OSError as exc:
                raise CassetteError(f"blob is unreadable at {path}: {exc}") from exc
            expected_digest = relative_path.name
            if sha256_bytes(body) != expected_digest:
                raise CassetteError(f"blob digest mismatch at {path}")
            return body
        raise CassetteError(f"invalid body representation: {representation!r}")


def base_manifest(
    *,
    created_at: str,
    t0_epoch: float,
    image_ref: str,
    image_digest: str,
    command: list[str],
    environment_fingerprint: str,
    image_id: str | None = None,
    ruleset_version: str | None = None,
) -> dict[str, Any]:
    """Build the required versioned M2 manifest."""

    image: dict[str, str] = {"ref": image_ref, "digest": image_digest}
    if image_id is not None:
        image["id"] = image_id
    manifest = {
        "cassette_version": CASSETTE_VERSION,
        "harness_version": __version__,
        "created_at": created_at,
        "t0_epoch": t0_epoch,
        "image": image,
        "command": command,
        "env_fingerprint": environment_fingerprint,
        "redaction": {"headers": DEFAULT_REDACTED_HEADERS},
        "flow_count": 0,
        "event_count": 0,
    }
    if ruleset_version is not None:
        manifest["ruleset_version"] = ruleset_version
    return manifest
