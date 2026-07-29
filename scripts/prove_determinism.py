#!/usr/bin/env python3
"""Replay a cassette repeatedly and prove workspace/transcript determinism."""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = round((len(ordered) - 1) * fraction)
    return ordered[index]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cassette", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("results/determinism.json"),
    )
    parser.add_argument("--allow-image-mismatch", action="store_true")
    arguments = parser.parse_args()
    if arguments.runs < 1:
        parser.error("--runs must be at least 1")

    cassette = arguments.cassette.resolve()
    command = [
        sys.executable,
        "-m",
        "replayable.cli",
        "replay",
        "--cassette",
        str(cassette),
        "--strict",
    ]
    if arguments.allow_image_mismatch:
        command.append("--allow-image-mismatch")

    runs: list[dict[str, object]] = []
    for number in range(1, arguments.runs + 1):
        started = time.monotonic()
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        elapsed = time.monotonic() - started
        if completed.returncode != 0:
            sys.stderr.write(completed.stdout)
            sys.stderr.write(completed.stderr)
            raise SystemExit(f"replay {number} failed with exit {completed.returncode}")
        try:
            replay = json.loads(
                (cassette / "last-replay.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"cannot read replay result after run {number}: {exc}") from exc
        runs.append(
            {
                "run": number,
                "wall_time_seconds": float(replay["wall_time_seconds"]),
                "cli_wall_time_seconds": elapsed,
                "workspace_sha256": replay["workspace_sha256"],
                "stdout_sha256": replay["stdout_sha256"],
            }
        )
        print(f"{number}/{arguments.runs}", file=sys.stderr)

    durations = [float(run["wall_time_seconds"]) for run in runs]
    cli_durations = [float(run["cli_wall_time_seconds"]) for run in runs]
    workspace_hashes = {str(run["workspace_sha256"]) for run in runs}
    stdout_hashes = {str(run["stdout_sha256"]) for run in runs}
    result = {
        "n": arguments.runs,
        "distinct_workspace_hashes": sorted(workspace_hashes),
        "distinct_stdout_hashes": sorted(stdout_hashes),
        "deterministic": len(workspace_hashes) == 1 and len(stdout_hashes) == 1,
        "wall_time_seconds": {
            "min": min(durations),
            "mean": statistics.fmean(durations),
            "median": statistics.median(durations),
            "p95": percentile(durations, 0.95),
            "max": max(durations),
        },
        "cli_wall_time_seconds": {
            "min": min(cli_durations),
            "mean": statistics.fmean(cli_durations),
            "median": statistics.median(cli_durations),
            "p95": percentile(cli_durations, 0.95),
            "max": max(cli_durations),
        },
        "runs": runs,
    }
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not result["deterministic"]:
        raise SystemExit("replay hashes diverged")
    print(
        f"{arguments.runs}/{arguments.runs} identical workspace and stdout hashes; "
        f"wrote {arguments.out}"
    )


if __name__ == "__main__":
    main()
