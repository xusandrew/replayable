#!/usr/bin/env python
"""Compare a replayed run against the recording and report the verdict.

`replayable replay` already exits non-zero on divergence, but a bare exit code
makes for a poor CI experience: the interesting question is *what* diverged and
by how much. This renders the comparison, writes a GitHub step summary when one
is available, and exits with the harness's own exit-code contract.

    python scripts/check_replay.py path/to/cassette
"""

from __future__ import annotations

import argparse
import json
import os
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from replayable.exit_codes import ExitCode


def _load(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _short(digest: str | None) -> str:
    return f"{digest[:16]}…" if digest else "(none)"


def _recorded_digest(value: object) -> str | None:
    """Accept an omitted legacy baseline, but reject malformed values."""

    if value is None:
        return None
    if isinstance(value, str) and value:
        return value
    raise ValueError("recorded digest must be a non-empty string")


def _replayed_digest(value: object) -> str:
    if isinstance(value, str) and value:
        return value
    raise ValueError("replayed digest must be a non-empty string")


def _build_fork_report(cassette: Path, result: dict) -> tuple[int, list[str]]:
    raw_exit = result.get("exit_code")
    downstream = result.get("downstream")
    if (
        isinstance(raw_exit, bool)
        or not isinstance(raw_exit, int)
        or raw_exit not in set(ExitCode)
        or not isinstance(downstream, dict)
        or not isinstance(downstream.get("matches"), bool)
    ):
        return ExitCode.HARNESS_ERROR, [
            f"Invalid fork result in {cassette / 'fork-result.json'}."
        ]
    lines = [
        "| Check | Result |",
        "| --- | --- |",
        f"| downstream behavior | {'✅ match' if downstream['matches'] else '❌ changed'} |",
    ]
    similarity = downstream.get("similarity")
    if isinstance(similarity, dict):
        score = similarity.get("score")
        threshold = similarity.get("threshold")
        if all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in (score, threshold)
        ):
            lines.append(f"| similarity | {float(score):.1%} / {float(threshold):.1%} threshold |")
    segments = result.get("segments")
    live = segments.get("live") if isinstance(segments, dict) else None
    if isinstance(live, dict):
        cost = live.get("estimated_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            lines.append(f"| live API cost | ${float(cost):.4f} |")
    exit_code = ExitCode(raw_exit)
    if exit_code == ExitCode.SUCCESS and not downstream["matches"]:
        return ExitCode.HARNESS_ERROR, [
            f"Inconsistent fork result in {cassette / 'fork-result.json'}: "
            "exit code is 0 but downstream behavior does not match."
        ]
    lines += [
        "",
        (
            "**No drift detected.** Live execution still matches the baseline."
            if exit_code == ExitCode.SUCCESS and downstream["matches"]
            else "**Drift detected.** Inspect the hybrid result artifact before accepting."
        ),
    ]
    return exit_code, lines


def build_report(
    cassette: Path,
    *,
    stale_after_days: int | None = None,
    now: datetime | None = None,
) -> tuple[int, list[str]]:
    """Return ``(exit_code, lines)`` describing the replay outcome."""

    manifest = _load(cassette / "manifest.json")
    replay = _load(cassette / "last-replay.json")

    if manifest is None:
        return ExitCode.HARNESS_ERROR, [f"No manifest found in {cassette}."]
    if replay is None:
        fork_result = _load(cassette / "fork-result.json")
        if fork_result is not None:
            return _build_fork_report(cassette, fork_result)
        return ExitCode.HARNESS_ERROR, [
            f"No last-replay.json in {cassette}; the replay did not complete.",
        ]

    replay_exit_code = replay.get("exit_code")
    if (
        isinstance(replay_exit_code, bool)
        or not isinstance(replay_exit_code, int)
        or replay_exit_code not in set(ExitCode)
    ):
        return ExitCode.HARNESS_ERROR, [
            f"Invalid or missing exit_code in {cassette / 'last-replay.json'}.",
        ]

    try:
        recorded_workspace = _recorded_digest(manifest.get("workspace_sha256"))
        recorded_stdout = _recorded_digest(manifest.get("stdout_sha256"))
        replayed_workspace = _replayed_digest(replay.get("workspace_sha256"))
        replayed_stdout = _replayed_digest(replay.get("stdout_sha256"))
    except ValueError as exc:
        return ExitCode.HARNESS_ERROR, [
            f"Invalid replay hash metadata in {cassette}: {exc}.",
        ]

    workspace_ok = (
        recorded_workspace is None or recorded_workspace == replayed_workspace
    )
    stdout_ok = recorded_stdout is None or recorded_stdout == replayed_stdout
    workspace_result = "—" if recorded_workspace is None else ("✅" if workspace_ok else "❌")
    stdout_result = "—" if recorded_stdout is None else ("✅" if stdout_ok else "❌")

    lines = [
        "| Check | Recorded | Replayed | Result |",
        "| --- | --- | --- | --- |",
        f"| workspace sha256 | `{_short(recorded_workspace)}` | "
        f"`{_short(replayed_workspace)}` | {workspace_result} |",
        f"| stdout sha256 | `{_short(recorded_stdout)}` | "
        f"`{_short(replayed_stdout)}` | {stdout_result} |",
    ]

    recorded_seconds = manifest.get("record_wall_time_seconds")
    replayed_seconds = replay.get("wall_time_seconds")
    if (
        isinstance(recorded_seconds, (int, float))
        and not isinstance(recorded_seconds, bool)
        and isinstance(replayed_seconds, (int, float))
        and not isinstance(replayed_seconds, bool)
        and recorded_seconds > 0
        and replayed_seconds > 0
    ):
        speedup = recorded_seconds / replayed_seconds
        lines.append(
            f"| wall time | {recorded_seconds:.2f}s | {replayed_seconds:.2f}s | "
            f"{speedup:.0f}× faster |"  # noqa: RUF001 - typographic multiplication sign, display only
        )
    observation = _load(cassette / "observation.json")
    recorded_cost: float | None = None
    if observation is not None:
        model = observation.get("model")
        value = model.get("estimated_cost_usd") if isinstance(model, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            recorded_cost = float(value)
    recorded_cost_label = (
        f"${recorded_cost:.4f}" if recorded_cost is not None else "real API spend"
    )
    lines.append(f"| cost | {recorded_cost_label} | $0.00 | offline |")
    if recorded_cost is not None:
        lines.append(f"\n**Estimated API cost avoided:** ${recorded_cost:.4f}.")

    if stale_after_days is not None:
        created_at = manifest.get("created_at")
        if isinstance(created_at, str):
            try:
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except ValueError:
                created = None
            if created is not None:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                reference = now or datetime.now(UTC)
                age_days = (reference - created).total_seconds() / 86400
                if age_days > stale_after_days:
                    lines += [
                        "",
                        f"> ⚠️ Baseline is {age_days:.0f} days old "
                        f"(warning threshold: {stale_after_days} days).",
                    ]

    state_path = cassette / "replay-state.json"
    state = _load(state_path)
    if state is None:
        return ExitCode.HARNESS_ERROR, [
            f"No valid replay-state.json in {cassette}; the replay is incomplete.",
        ]
    unconsumed = state.get("unconsumed_sequences")
    if not isinstance(unconsumed, list) or not all(
        isinstance(sequence, int) and not isinstance(sequence, bool)
        for sequence in unconsumed
    ):
        return ExitCode.HARNESS_ERROR, [
            f"Invalid unconsumed_sequences in {state_path}.",
        ]

    exit_code = ExitCode(replay_exit_code)
    if not (workspace_ok and stdout_ok):
        exit_code = ExitCode.REPLAY_MISMATCH
        lines += ["", "**Behaviour changed.** The replay diverged from the recording."]
        diff = replay.get("workspace_diff")
        if diff is not None and not isinstance(diff, dict):
            return ExitCode.HARNESS_ERROR, [
                f"Invalid workspace_diff in {cassette / 'last-replay.json'}.",
            ]
        if isinstance(diff, dict):
            for label in ("added", "removed", "changed"):
                entries = diff.get(label) or []
                if not isinstance(entries, list) or not all(
                    isinstance(entry, str) for entry in entries
                ):
                    return ExitCode.HARNESS_ERROR, [
                        f"Invalid workspace_diff in {cassette / 'last-replay.json'}.",
                    ]
                if entries:
                    lines.append(f"- {label}: {', '.join(entries)}")

    report_path = cassette / "replay-report.json"
    report = _load(report_path)
    if report_path.exists() and report is None:
        return ExitCode.HARNESS_ERROR, [
            f"Invalid replay report at {report_path}.",
        ]
    if report is not None:
        live = report.get("live_request")
        candidates = report.get("nearest_candidates", [])
        if not isinstance(live, dict) or not isinstance(candidates, list) or not all(
            isinstance(candidate, dict) for candidate in candidates
        ):
            return ExitCode.HARNESS_ERROR, [
                f"Invalid replay report at {report_path}.",
            ]
        exit_code = ExitCode.REPLAY_MISMATCH
        lines += [
            "",
            "**Unmatched request.** The agent asked for something the cassette "
            "does not contain:",
            f"- `{live.get('method', '?')} {live.get('host', '')}"
            f"{live.get('path', '')}`",
        ]
        for candidate in candidates[:3]:
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
    baselines_complete = recorded_workspace is not None and recorded_stdout is not None
    if exit_code == ExitCode.AGENT_FAILED:
        lines += ["", "**Agent failed.** The replayed workload exited nonzero."]
    elif exit_code == ExitCode.HARNESS_ERROR:
        lines += ["", "**Harness error.** Replay did not complete reliably."]
    elif (
        exit_code == ExitCode.REPLAY_MISMATCH
        and workspace_ok
        and stdout_ok
        and report is None
    ):
        lines += ["", "**Replay mismatch.** See the replay output and state artifacts."]
    elif exit_code == ExitCode.SUCCESS and not unconsumed and baselines_complete:
        lines += ["", "**DETERMINISTIC** — the replay reproduced the recording exactly."]
    elif exit_code == ExitCode.SUCCESS and unconsumed and baselines_complete:
        lines += [
            "",
            "**Hashes match**, but the cassette was not fully consumed (see above).",
        ]
    elif exit_code == ExitCode.SUCCESS and unconsumed:
        lines += [
            "",
            "**Replay completed**, but this legacy cassette has no complete "
            "byte-level baseline and was not fully consumed.",
        ]
    elif exit_code == ExitCode.SUCCESS:
        lines += [
            "",
            "**Replay passed**, but this legacy cassette has no complete byte-level "
            "workspace/stdout baseline.",
        ]

    return exit_code, lines


def write_junit(path: Path, exit_code: int, lines: list[str]) -> None:
    """Write one portable JUnit testcase for CI artifact consumers."""

    suite = ET.Element(
        "testsuite",
        name="replayable",
        tests="1",
        failures="1" if exit_code in {ExitCode.AGENT_FAILED, ExitCode.REPLAY_MISMATCH} else "0",
        errors="1" if exit_code == ExitCode.HARNESS_ERROR else "0",
    )
    case = ET.SubElement(suite, "testcase", classname="replayable", name="deterministic replay")
    rendered = "\n".join(lines)
    if exit_code in {ExitCode.AGENT_FAILED, ExitCode.REPLAY_MISMATCH}:
        ET.SubElement(case, "failure", message=f"replay exited {int(exit_code)}").text = rendered
    elif exit_code == ExitCode.HARNESS_ERROR:
        ET.SubElement(case, "error", message="replay harness error").text = rendered
    else:
        ET.SubElement(case, "system-out").text = rendered
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("cassette", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--junit-out", type=Path)
    parser.add_argument("--stale-after-days", type=int, default=30)
    arguments = parser.parse_args()
    if arguments.stale_after_days < 0:
        parser.error("--stale-after-days must be non-negative")

    exit_code, lines = build_report(
        arguments.cassette,
        stale_after_days=arguments.stale_after_days,
    )

    heading = "## Replay verdict"
    markdown = f"{heading}\n\n" + "\n".join(lines) + "\n"
    print(heading)
    print("\n".join(lines))
    if arguments.markdown_out is not None:
        arguments.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        arguments.markdown_out.write_text(markdown, encoding="utf-8")
    if arguments.junit_out is not None:
        write_junit(arguments.junit_out, exit_code, lines)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(markdown)

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
