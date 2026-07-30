"""Provider-neutral, deterministic observation artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from replayable.cassette import CassetteError, CassetteReader
from replayable.channels.providers.anthropic import (
    AnthropicParseError,
    ModelCall,
    ToolCall,
    ordered_tool_calls,
    parse_calls,
)
from replayable.snapshot import SnapshotError, load_recorded_snapshot
from replayable.verdict.usage import TokenUsage

OBSERVATION_FILE_NAME = "observation.json"
OBSERVATION_VERSION = 1
RUN_LOG_FILE_NAME = "run.log"


class ObservationError(RuntimeError):
    """A trustworthy observation could not be built or persisted."""


@dataclass(frozen=True)
class Transcript:
    stdout: str
    stderr: str
    stdout_sha256: str
    stderr_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
        }


@dataclass(frozen=True)
class ModelSummary:
    calls: int
    models: tuple[str, ...]
    usage_complete: bool
    tokens: TokenUsage | None
    cost_complete: bool
    estimated_cost_usd: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "models": list(self.models),
            "usage_complete": self.usage_complete,
            "tokens": self.tokens.as_dict() if self.tokens is not None else None,
            "cost_complete": self.cost_complete,
            "estimated_cost_usd": self.estimated_cost_usd,
        }


@dataclass(frozen=True)
class Observation:
    transcript: Transcript
    tool_calls: tuple[ToolCall, ...]
    workspace_sha256: str
    workspace_files: tuple[dict[str, Any], ...]
    exit_code: int | None
    model: ModelSummary
    wall_time_seconds: float | None
    version: int = OBSERVATION_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "transcript": self.transcript.as_dict(),
            "tool_calls": [call.as_dict() for call in self.tool_calls],
            "workspace": {
                "sha256": self.workspace_sha256,
                "files": list(self.workspace_files),
            },
            "process": {"exit_code": self.exit_code},
            "model": self.model.as_dict(),
            "timing": {"wall_time_seconds": self.wall_time_seconds},
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_transcript(cassette: Path, manifest: dict[str, Any]) -> Transcript:
    values: dict[str, str] = {}
    for stream in ("stdout", "stderr"):
        path = cassette / f"agent.{stream}"
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ObservationError(
                f"cannot read recorded {stream} at {path}: {exc}"
            ) from exc
        digest = _sha256(raw)
        declared = manifest.get(f"{stream}_sha256")
        if declared is not None and declared != digest:
            raise ObservationError(
                f"recorded {stream} hash does not match the cassette manifest"
            )
        values[stream] = raw.decode("utf-8", errors="replace")
        values[f"{stream}_sha256"] = digest
    return Transcript(
        stdout=values["stdout"],
        stderr=values["stderr"],
        stdout_sha256=values["stdout_sha256"],
        stderr_sha256=values["stderr_sha256"],
    )


def _record_lifecycle(cassette: Path) -> tuple[int | None, float | None]:
    path = cassette / RUN_LOG_FILE_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeError) as exc:
        raise ObservationError(f"record log is unreadable at {path}: {exc}") from exc

    exit_code: int | None = None
    wall_time: float | None = None
    for line_number, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ObservationError(
                f"record log contains invalid JSON at {path}:{line_number}"
            ) from exc
        if not isinstance(event, dict):
            raise ObservationError(
                f"record log event at {path}:{line_number} must be an object"
            )
        if event.get("event") == "container_exit" and event.get("mode") == "record":
            raw_exit = event.get("return_code")
            raw_wall_time = event.get("wall_time_seconds")
            if isinstance(raw_exit, bool) or not isinstance(raw_exit, int):
                raise ObservationError("record log container exit code is invalid")
            if (
                isinstance(raw_wall_time, bool)
                or not isinstance(raw_wall_time, (int, float))
                or raw_wall_time < 0
            ):
                raise ObservationError("record log wall time is invalid")
            exit_code = raw_exit
            wall_time = float(raw_wall_time)
    return exit_code, wall_time


def _manifest_int(
    manifest: dict[str, Any],
    name: str,
    fallback: int | None,
) -> int | None:
    value = manifest.get(name, fallback)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ObservationError(f"manifest {name} must be an integer")
    return value


def _manifest_seconds(
    manifest: dict[str, Any],
    name: str,
    fallback: float | None,
) -> float | None:
    value = manifest.get(name, fallback)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ObservationError(f"manifest {name} must be a non-negative number")
    return float(value)


def _model_summary(calls: tuple[ModelCall, ...]) -> ModelSummary:
    if not calls:
        return ModelSummary(
            calls=0,
            models=(),
            usage_complete=True,
            tokens=TokenUsage(),
            cost_complete=True,
            estimated_cost_usd=0.0,
        )
    usages = [call.usage for call in calls]
    usage_complete = all(usage is not None for usage in usages)
    tokens = None
    if usage_complete:
        available = [usage for usage in usages if usage is not None]
        tokens = TokenUsage(
            input=sum(usage.input for usage in available),
            output=sum(usage.output for usage in available),
            cache_write=sum(usage.cache_write for usage in available),
            cache_read=sum(usage.cache_read for usage in available),
        )
    costs = [call.estimated_cost_usd for call in calls]
    cost_complete = all(cost is not None for cost in costs)
    return ModelSummary(
        calls=len(calls),
        models=tuple(dict.fromkeys(call.model for call in calls)),
        usage_complete=usage_complete,
        tokens=tokens,
        cost_complete=cost_complete,
        estimated_cost_usd=(
            round(sum(cost for cost in costs if cost is not None), 12)
            if cost_complete
            else None
        ),
    )


def build_observation(cassette: Path) -> Observation:
    """Build one observation from recorded artifacts without mutating the cassette."""

    cassette = cassette.resolve()
    reader = CassetteReader(cassette)
    try:
        manifest = reader.load_manifest()
        flows = reader.load_flows().flows
        declared_flow_count = manifest.get("flow_count")
        if (
            isinstance(declared_flow_count, bool)
            or not isinstance(declared_flow_count, int)
            or declared_flow_count != len(flows)
        ):
            raise ObservationError(
                "manifest flow_count does not match the recorded flows"
            )
        workspace_sha256, workspace_files = load_recorded_snapshot(cassette)
        calls = parse_calls(reader, flows)
        tool_calls = ordered_tool_calls(calls)
    except (CassetteError, SnapshotError, AnthropicParseError) as exc:
        raise ObservationError(f"cannot build observation: {exc}") from exc

    declared_workspace = manifest.get("workspace_sha256")
    if declared_workspace is not None and declared_workspace != workspace_sha256:
        raise ObservationError(
            "recorded workspace hash does not match the cassette manifest"
        )
    log_exit_code, log_wall_time = _record_lifecycle(cassette)
    exit_code = _manifest_int(manifest, "record_exit_code", log_exit_code)
    wall_time = _manifest_seconds(manifest, "record_wall_time_seconds", log_wall_time)
    if (
        log_exit_code is not None
        and exit_code is not None
        and log_exit_code != exit_code
    ):
        raise ObservationError(
            "record exit code does not match the cassette lifecycle log"
        )
    if (
        log_wall_time is not None
        and wall_time is not None
        and log_wall_time != wall_time
    ):
        raise ObservationError(
            "record wall time does not match the cassette lifecycle log"
        )
    return Observation(
        transcript=_read_transcript(cassette, manifest),
        tool_calls=tool_calls,
        workspace_sha256=workspace_sha256,
        workspace_files=tuple(workspace_files),
        exit_code=exit_code,
        model=_model_summary(calls),
        wall_time_seconds=wall_time,
    )


def serialize_observation(observation: Observation) -> str:
    """Render stable JSON suitable for golden files and content hashing."""

    return (
        json.dumps(
            observation.as_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def write_observation(
    cassette: Path,
    observation: Observation | None = None,
) -> Path:
    """Atomically publish observation.json, preserving the previous file on failure."""

    cassette = cassette.resolve()
    target = cassette / OBSERVATION_FILE_NAME
    try:
        payload = serialize_observation(observation or build_observation(cassette))
    except (TypeError, ValueError) as exc:
        raise ObservationError(f"observation is not serializable: {exc}") from exc
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=cassette,
            prefix=f".{OBSERVATION_FILE_NAME}.",
            suffix=".tmp",
        )
    except OSError as exc:
        raise ObservationError(f"cannot write observation at {target}: {exc}") from exc
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ObservationError(f"cannot write observation at {target}: {exc}") from exc
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target
