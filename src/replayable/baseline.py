"""Safely record, review, and publish replacement baselines."""

from __future__ import annotations

import difflib
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from replayable.cassette import CassetteError, CassetteReader
from replayable.exit_codes import ExitCode
from replayable.snapshot import diff_file_manifests
from replayable.verdict.diff_render import render_structural_diff
from replayable.verdict.differ_structural import StructuralDiffError, diff_tool_calls
from replayable.verdict.observation import Observation, ObservationError, build_observation

RecordExecutor = Callable[..., ExitCode]
MAX_TRANSCRIPT_DIFF_LINES = 160
MAX_WORKSPACE_DIFF_ENTRIES = 200
MAX_TOOL_DIFF_LINES = 240


class BaselineError(RuntimeError):
    """A candidate baseline could not be prepared or published safely."""


def _manifest(cassette: Path) -> dict[str, Any]:
    try:
        manifest = CassetteReader(cassette).load_manifest()
        image = manifest["image"]["ref"]
        command = manifest["command"]
    except (CassetteError, KeyError, TypeError) as exc:
        raise BaselineError(f"baseline manifest is invalid: {exc}") from exc
    if not isinstance(image, str) or not image:
        raise BaselineError("baseline manifest image ref must be non-empty")
    if not isinstance(command, list) or not command or not all(
        isinstance(item, str) for item in command
    ):
        raise BaselineError("baseline manifest command must be a non-empty string array")
    return manifest


def _transcript_diff(baseline: Observation, candidate: Observation) -> list[str]:
    lines = list(
        difflib.unified_diff(
            baseline.transcript.stdout.splitlines(),
            candidate.transcript.stdout.splitlines(),
            fromfile="baseline/stdout",
            tofile="candidate/stdout",
            lineterm="",
            n=3,
        )
    )
    if len(lines) <= MAX_TRANSCRIPT_DIFF_LINES:
        return lines
    omitted = len(lines) - MAX_TRANSCRIPT_DIFF_LINES
    return [
        *lines[:MAX_TRANSCRIPT_DIFF_LINES],
        f"... {omitted} additional transcript diff line(s) omitted",
    ]


def _bounded(lines: list[str], limit: int, label: str) -> list[str]:
    if len(lines) <= limit:
        return lines
    omitted = len(lines) - limit
    return [*lines[:limit], f"... {omitted} additional {label} line(s) omitted"]


def render_baseline_diff(baseline: Observation, candidate: Observation) -> str:
    """Render a bounded, deterministic review of a proposed baseline."""

    workspace_diff = diff_file_manifests(
        list(baseline.workspace_files),
        list(candidate.workspace_files),
    )
    try:
        structural = diff_tool_calls(baseline.tool_calls, candidate.tool_calls)
    except StructuralDiffError as exc:
        raise BaselineError(f"candidate tool calls cannot be compared safely: {exc}") from exc
    lines = [
        "Baseline candidate",
        "==================",
        f"process exit: {baseline.exit_code!r} -> {candidate.exit_code!r}",
        f"stdout sha256: {baseline.transcript.stdout_sha256} -> "
        f"{candidate.transcript.stdout_sha256}",
        f"workspace sha256: {baseline.workspace_sha256} -> "
        f"{candidate.workspace_sha256}",
        f"model calls: {baseline.model.calls} -> {candidate.model.calls}",
        f"estimated cost: {baseline.model.estimated_cost_usd!r} -> "
        f"{candidate.model.estimated_cost_usd!r}",
        "",
        "Workspace changes",
        "-----------------",
    ]
    changed_workspace = False
    for label in ("added", "removed", "changed"):
        entries = workspace_diff[label]
        if entries:
            changed_workspace = True
            displayed = entries[:MAX_WORKSPACE_DIFF_ENTRIES]
            lines.append(f"{label}: {', '.join(displayed)}")
            if len(entries) > len(displayed):
                lines.append(
                    f"... {len(entries) - len(displayed)} additional "
                    f"{label} workspace path(s) omitted"
                )
    if not changed_workspace:
        lines.append("No workspace changes.")

    lines += ["", "Tool-call changes", "-----------------"]
    lines.extend(
        _bounded(
            render_structural_diff(structural, context=3).rstrip().splitlines(),
            MAX_TOOL_DIFF_LINES,
            "tool-call diff",
        )
    )
    lines += ["", "Stdout changes", "--------------"]
    transcript = _transcript_diff(baseline, candidate)
    lines.extend(transcript or ["No stdout changes."])
    return "\n".join(lines) + "\n"


@dataclass
class PreparedBaseline:
    """A fully recorded candidate that is not visible at its destination yet."""

    source: Path
    destination: Path
    staging: Path
    preview: str
    _published: bool = False

    def publish(self, *, replace: bool) -> None:
        if self._published:
            raise BaselineError("baseline candidate has already been published")
        if self.destination.exists() and not replace:
            raise BaselineError(f"destination already exists at {self.destination}")

        backup_root: Path | None = None
        backup: Path | None = None
        preserve_backup = False
        try:
            if self.destination.exists():
                backup_root = Path(
                    tempfile.mkdtemp(
                        dir=self.destination.parent,
                        prefix=f".{self.destination.name}.backup.",
                    )
                )
                backup = backup_root / "baseline"
                os.replace(self.destination, backup)
            try:
                os.replace(self.staging, self.destination)
            except OSError as exc:
                if backup is not None and backup.exists():
                    if self.destination.exists():
                        preserve_backup = True
                        raise BaselineError(
                            "cannot publish baseline because the destination changed "
                            f"concurrently; original preserved at {backup}"
                        ) from exc
                    try:
                        os.replace(backup, self.destination)
                    except OSError as rollback_exc:
                        preserve_backup = True
                        raise BaselineError(
                            "cannot publish or restore baseline atomically; "
                            f"original preserved at {backup}"
                        ) from rollback_exc
                raise
            self._published = True
        except OSError as exc:
            raise BaselineError(f"cannot publish baseline atomically: {exc}") from exc
        finally:
            if backup_root is not None and not preserve_backup:
                shutil.rmtree(backup_root, ignore_errors=True)


@contextmanager
def prepare_baseline(
    *,
    source: Path,
    destination: Path,
    env_file: Path | None,
    record_executor: RecordExecutor,
    port: int | None = None,
    ca_path: Path | None = None,
    timeout_seconds: float | None = None,
) -> Iterator[PreparedBaseline]:
    """Record a candidate beside its destination and remove it unless published."""

    source = source.expanduser().resolve()
    if not source.is_dir():
        raise BaselineError(f"baseline directory not found at {source}")
    parent = destination.expanduser().absolute().parent.resolve()
    destination = parent / destination.name
    if destination.name in {"", ".", ".."}:
        raise BaselineError("baseline destination name is invalid")
    if not parent.is_dir():
        raise BaselineError(f"baseline destination parent not found at {parent}")

    manifest = _manifest(source)
    try:
        baseline_observation = build_observation(source)
    except ObservationError as exc:
        raise BaselineError(f"existing baseline observation is invalid: {exc}") from exc

    staging = Path(
        tempfile.mkdtemp(
            dir=parent,
            prefix=f".{destination.name}.candidate.",
        )
    )
    kwargs: dict[str, Any] = {
        "image": manifest["image"]["ref"],
        "command": manifest["command"],
        "env_file": env_file,
        "out": staging,
    }
    if port is not None:
        kwargs["port"] = port
    if ca_path is not None:
        kwargs["ca_path"] = ca_path
    if timeout_seconds is not None:
        kwargs["timeout_seconds"] = timeout_seconds
    try:
        code = record_executor(**kwargs)
        if code != ExitCode.SUCCESS:
            raise BaselineError(f"candidate recording exited {int(code)}; baseline unchanged")
        try:
            candidate_observation = build_observation(staging)
        except ObservationError as exc:
            raise BaselineError(f"candidate baseline is invalid: {exc}") from exc
        prepared = PreparedBaseline(
            source=source,
            destination=destination,
            staging=staging,
            preview=render_baseline_diff(
                baseline_observation,
                candidate_observation,
            ),
        )
        yield prepared
    finally:
        if staging.exists():
            shutil.rmtree(staging)
