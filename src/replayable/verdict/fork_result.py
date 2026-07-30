"""Stable hybrid-replay result artifacts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from replayable.snapshot import diff_file_manifests
from replayable.verdict.differ_structural import diff_tool_calls
from replayable.verdict.observation import Observation

FORK_RESULT_FILE_NAME = "fork-result.json"
FORK_RESULT_VERSION = 1


class ForkResultError(ValueError):
    """A hybrid replay result is inconsistent or cannot be persisted."""


def _count(state: dict[str, Any], name: str) -> int:
    value = state.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ForkResultError(f"fork state {name} must be a non-negative integer")
    return value


def _timestamp(state: dict[str, Any], name: str) -> float | None:
    value = state.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ForkResultError(f"fork state {name} must be a non-negative number or null")
    return float(value)


def build_fork_result(
    *,
    baseline: Observation,
    candidate: Observation,
    live: Observation,
    state: dict[str, Any],
    captured_flow_count: int,
    wall_time_seconds: float,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare a hybrid candidate with its immutable baseline."""

    if (
        isinstance(captured_flow_count, bool)
        or not isinstance(captured_flow_count, int)
        or captured_flow_count < 0
    ):
        raise ForkResultError("captured flow count must be a non-negative integer")
    if (
        isinstance(wall_time_seconds, bool)
        or not isinstance(wall_time_seconds, (int, float))
        or wall_time_seconds < 0
    ):
        raise ForkResultError("fork wall time must be a non-negative number")

    pinned_target = _count(state, "pinned_target")
    pinned_served = _count(state, "pinned_served")
    live_requests = _count(state, "live_requests")
    live_responses = _count(state, "live_responses")
    live_errors = _count(state, "live_errors")
    if pinned_served > pinned_target:
        raise ForkResultError("fork state served more pinned flows than requested")
    if live_responses + live_errors > live_requests:
        raise ForkResultError("fork state completed more live requests than it started")
    if captured_flow_count != live_responses:
        raise ForkResultError("captured flow count does not match completed live responses")

    live_started = _timestamp(state, "live_started_epoch")
    live_completed = _timestamp(state, "live_completed_epoch")
    if (live_started is None) != (live_completed is None):
        raise ForkResultError("fork live timestamps must both be present or both be null")
    if live_started is not None and live_completed is not None:
        if live_completed < live_started:
            raise ForkResultError("fork live completion precedes its start")
        live_wall_time = live_completed - live_started
    else:
        if live_requests:
            raise ForkResultError("fork state omitted timestamps for live requests")
        live_wall_time = 0.0

    workspace_diff = diff_file_manifests(
        list(baseline.workspace_files),
        list(candidate.workspace_files),
    )
    workspace_matches = baseline.workspace_sha256 == candidate.workspace_sha256
    stdout_matches = baseline.transcript.stdout_sha256 == candidate.transcript.stdout_sha256
    exit_matches = baseline.exit_code == candidate.exit_code
    tool_diff = diff_tool_calls(baseline.tool_calls, candidate.tool_calls)
    downstream_matches = workspace_matches and stdout_matches and exit_matches and tool_diff.matches

    return {
        "version": FORK_RESULT_VERSION,
        "mode": "hybrid",
        "fork_at": pinned_target,
        "segments": {
            "pinned": {
                "target_flow_count": pinned_target,
                "served_flow_count": pinned_served,
                "estimated_cost_usd": 0.0,
            },
            "live": {
                "request_count": live_requests,
                "response_count": live_responses,
                "error_count": live_errors,
                "flow_count": captured_flow_count,
                "model_calls": live.model.calls,
                "models": list(live.model.models),
                "usage_complete": live.model.usage_complete,
                "tokens": (live.model.tokens.as_dict() if live.model.tokens is not None else None),
                "cost_complete": live.model.cost_complete,
                "estimated_cost_usd": live.model.estimated_cost_usd,
                "wall_time_seconds": live_wall_time,
            },
        },
        "timing": {"wall_time_seconds": float(wall_time_seconds)},
        "downstream": {
            "matches": downstream_matches,
            "exit_code": {
                "matches": exit_matches,
                "baseline": baseline.exit_code,
                "candidate": candidate.exit_code,
            },
            "stdout": {
                "matches": stdout_matches,
                "baseline_sha256": baseline.transcript.stdout_sha256,
                "candidate_sha256": candidate.transcript.stdout_sha256,
            },
            "workspace": {
                "matches": workspace_matches,
                "baseline_sha256": baseline.workspace_sha256,
                "candidate_sha256": candidate.workspace_sha256,
                "diff": workspace_diff,
            },
            "tool_calls": tool_diff.as_dict(),
        },
        "live_observation": live.as_dict(),
        "events": events,
    }


def write_fork_result(cassette: Path, result: dict[str, Any]) -> Path:
    """Atomically publish fork-result.json."""

    target = cassette.resolve() / FORK_RESULT_FILE_NAME
    try:
        payload = (
            json.dumps(
                result,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
    except (OSError, TypeError, ValueError) as exc:
        raise ForkResultError(f"cannot prepare fork result at {target}: {exc}") from exc
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        raise ForkResultError(f"cannot write fork result at {target}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return target
