#!/usr/bin/env python
"""Compare a replayed run against the recording and report the verdict.

`replayable replay` already exits non-zero on divergence, but a bare exit code
makes for a poor CI experience: the interesting question is *what* diverged and
by how much. This renders the comparison, writes a GitHub step summary when one
is available, and exits with the harness's own exit-code contract.

    python scripts/check_replay.py path/to/cassette
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from replayable.exit_codes import ExitCode


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _short(digest: str | None) -> str:
    return f"{digest[:16]}…" if digest else "(none)"


def build_report(cassette: Path) -> tuple[int, list[str]]:
    """Return ``(exit_code, lines)`` describing the replay outcome."""

    manifest = _load(cassette / "manifest.json")
    replay = _load(cassette / "last-replay.json")

    if manifest is None:
        return ExitCode.HARNESS_ERROR, [f"No manifest found in {cassette}."]
    if replay is None:
        return ExitCode.HARNESS_ERROR, [
            f"No last-replay.json in {cassette}; the replay did not complete.",
        ]

    recorded_workspace = manifest.get("workspace_sha256")
    recorded_stdout = manifest.get("stdout_sha256")
    replayed_workspace = replay.get("workspace_sha256")
    replayed_stdout = replay.get("stdout_sha256")

    workspace_ok = recorded_workspace == replayed_workspace
    stdout_ok = recorded_stdout == replayed_stdout

    lines = [
        "| Check | Recorded | Replayed | Result |",
        "| --- | --- | --- | --- |",
        f"| workspace sha256 | `{_short(recorded_workspace)}` | "
        f"`{_short(replayed_workspace)}` | {'✅' if workspace_ok else '❌'} |",
        f"| stdout sha256 | `{_short(recorded_stdout)}` | "
        f"`{_short(replayed_stdout)}` | {'✅' if stdout_ok else '❌'} |",
    ]

    recorded_seconds = manifest.get("record_wall_time_seconds")
    replayed_seconds = replay.get("wall_time_seconds")
    if recorded_seconds and replayed_seconds:
        speedup = recorded_seconds / replayed_seconds if replayed_seconds else 0
        lines.append(
            f"| wall time | {recorded_seconds:.2f}s | {replayed_seconds:.2f}s | "
            f"{speedup:.0f}× faster |"  # noqa: RUF001 - typographic multiplication sign, display only
        )
    lines.append("| cost | real API spend | $0.00 | offline |")

    state = _load(cassette / "replay-state.json") or {}
    unconsumed = state.get("unconsumed_sequences") or []

    exit_code = ExitCode.SUCCESS
    if not (workspace_ok and stdout_ok):
        exit_code = ExitCode.REPLAY_MISMATCH
        lines += ["", "**Behaviour changed.** The replay diverged from the recording."]
        diff = replay.get("workspace_diff")
        if diff:
            for label in ("added", "removed", "changed"):
                entries = diff.get(label) or []
                if entries:
                    lines.append(f"- {label}: {', '.join(entries)}")

    report = _load(cassette / "replay-report.json")
    if report:
        exit_code = ExitCode.REPLAY_MISMATCH
        live = report.get("live_request", {})
        lines += [
            "",
            "**Unmatched request.** The agent asked for something the cassette "
            "does not contain:",
            f"- `{live.get('method', '?')} {live.get('host', '')}"
            f"{live.get('path', '')}`",
        ]
        for candidate in (report.get("nearest_candidates") or [])[:3]:
            lines.append(
                f"  - nearest recorded flow {candidate.get('seq')}: "
                f"`{candidate.get('method')} {candidate.get('path')}`"
            )

    if unconsumed:
        lines += [
            "",
            f"**Unserved flows:** {unconsumed} — the agent took a shorter path "
            "through the cassette than it did when recorded. Whether that fails "
            "the run is `--strict`'s decision, already made by `replayable "
            "replay`; this report does not override it.",
        ]

    # Only claim an exact reproduction when nothing at all was left over.
    # Saying "reproduced exactly" in the same breath as "some flows were never
    # served" would be self-contradictory, and the reassuring half is the half
    # people read.
    if exit_code == ExitCode.SUCCESS and not unconsumed:
        lines += ["", "**DETERMINISTIC** — the replay reproduced the recording exactly."]
    elif exit_code == ExitCode.SUCCESS:
        lines += [
            "",
            "**Hashes match**, but the cassette was not fully consumed (see above).",
        ]

    return exit_code, lines


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: check_replay.py <cassette-directory>", file=sys.stderr)
        raise SystemExit(ExitCode.HARNESS_ERROR)

    cassette = Path(sys.argv[1])
    exit_code, lines = build_report(cassette)

    heading = "## Replay verdict"
    print(heading)
    print("\n".join(lines))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(f"{heading}\n\n" + "\n".join(lines) + "\n")

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
